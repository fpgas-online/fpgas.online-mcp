"""In-use check: the stage 1 script over the shared ssh master (welland, ps1) or the
Tiny Tapeout `status.json` rule. Verdicts: idle, in use, unknown ("unknown" is never "idle").

The claim holder's own activity is discounted (spec, "In-use check"): `~/Uploads` changes newer
than the claim's start, and on Tiny Tapeout `waiting` / `left: ...` after a `loaded` state at
claim time. Every other signal still counts.
"""

import asyncio
import re
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources
from typing import Literal

from .boards import Board
from .claims import Claim
from .ssh import defuse

SCRIPT_TIMEOUT_S = 30.0
MAX_SIGNALS = 10
MAX_SIGNAL_LEN = 200
TT_MIN_GAP_S = 20.5  # the site caches status.json for 5 s: 15 s apart plus margin for the cache
TT_WAIT_S = 75.0
TT_MAX_READS = 10
TT_MAX_WAITING_READS = 3  # the first read plus two re-reads

MAX_OTHER_CONNECTIONS = 5
TRUNCATED = "in-use output was truncated"

_ACTIVE = re.compile(r"^active: (.*)$")
_UPLOADS_ACTIVE = re.compile(r"^~/Uploads change, (\d+) s ago$")
_VERDICT = re.compile(r"^verdict: (idle|in use|unknown.*)$")
# Every `unknown=1` path in the script, and the line it prints (the verdict then reads "in use" if
# anything else is active, which hides the unknown, so we must see these lines ourselves):
#   missing tool        -> "missing tool on host: <tool>"
#   tmux unexpected     -> "tmux: unexpected: <text>"
#   who line unparsed   -> "  pts/N <host> idle >= ? s"
#   own login not found -> "  could not find your own login in the process list"
#   `ss ... | awk ... || unknown=1` -> prints nothing (and only fires when awk itself fails, since
#     a pipeline's status is awk's). This one cannot be detected from the output; a missing `ss`
#     is covered by the missing-tool line. Truncated output is handled separately (never idle).
_UNKNOWN_MARKERS = (
    re.compile(r"^missing tool on host: "),
    re.compile(r"^tmux: unexpected: "),
    re.compile(r"^\s+pts/\S+ \S+ idle >= \? s$"),
    re.compile(r"^\s+could not find your own login"),
)
_CONN = re.compile(r"^  (\S+)$")
_TMUX_CLIENT = re.compile(r"^  (/dev/\S+ \S+ idle \d+ s)$")


@dataclass(frozen=True)
class Verdict:
    verdict: Literal["idle", "in use", "unknown"]
    signals: list[str] = field(default_factory=list)
    as_of: str = ""


def remote_script() -> str:
    """The heredoc body of data/in_use.sh (the file is the whole block from in-use.md)."""
    text = resources.files("fpgas_online_mcp").joinpath("data/in_use.sh").read_text()
    start = text.index("<<'EOF'\n") + len("<<'EOF'\n")
    return text[start : text.rindex("\nEOF") + 1]


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tidy(signals: list[str]) -> list[str]:
    out = []
    for s in signals:
        s = " ".join(str(s).split())
        out.append(s if len(s) <= MAX_SIGNAL_LEN else s[: MAX_SIGNAL_LEN - 1] + "…")
    if len(out) > MAX_SIGNALS:
        out = [*out[: MAX_SIGNALS - 1], f"… and {len(out) - MAX_SIGNALS + 1} more signals"]
    return out


def _verdict(v: str, signals: list[str]) -> Verdict:
    return Verdict(v, _tidy(signals), _now_iso())  # type: ignore[arg-type]


def _board_text(text: str) -> str:
    """Mark a signal as carrying board text, with any '</board-output' neutralised."""
    return "board says: " + defuse(text)


def _section(lines: list[str], header: str) -> list[str]:
    """Indented lines that follow `header`."""
    for i, ln in enumerate(lines):
        if ln.startswith(header):
            out = []
            for item in lines[i + 1 :]:
                if not item.startswith("  "):
                    break
                out.append(item)
            return out
    return []


def _other_addresses(lines: list[str]) -> list[str]:
    seen: list[str] = []
    for ln in _section(lines, "other ssh connections"):
        m = _CONN.match(ln)
        if m:
            addr = m[1].rsplit(":", 1)[0].removeprefix("::ffff:")
            if addr not in seen:
                seen.append(addr)
    return seen


def parse_script_output(
    text: str, uploads_ignore_s: float | None = None, truncated: bool = False
) -> tuple[str, list[str]]:
    """Read the script's output. `uploads_ignore_s`: ignore a ~/Uploads change younger than this many seconds.

    Truncated output is never idle: a script "in use" stays "in use" (undiscounted), anything else is unknown.
    """
    lines = text.splitlines()
    if not lines or not _VERDICT.match(lines[-1].strip()):
        return "unknown", ["the in-use script printed no verdict line"]
    script_verdict = _VERDICT.match(lines[-1].strip())[1]
    if truncated:
        uploads_ignore_s = None
    active, notes = [], []
    for ln in lines:
        m = _ACTIVE.match(ln.strip())
        if not m:
            continue
        up = _UPLOADS_ACTIVE.match(m[1])
        if up and uploads_ignore_s is not None and int(up[1]) < uploads_ignore_s:
            notes.append(f"ignored the claim holder's own ~/Uploads change, {up[1]} s ago")
        else:
            active.append(_board_text(m[1]))
    unknown = [_board_text(ln.strip()) for ln in lines if any(p.match(ln) for p in _UNKNOWN_MARKERS)]
    clients = [_board_text("tmux client " + m[1]) for ln in lines if (m := _TMUX_CLIENT.match(ln))]
    if truncated:
        if script_verdict == "in use":
            return "in use", [*active, *clients, TRUNCATED]
        return "unknown", [TRUNCATED, *unknown]
    if script_verdict.startswith("unknown"):
        return "unknown", [*unknown, *notes] or ["the in-use script could not check everything"]
    if active:
        return "in use", [*active, *clients, *notes]
    if unknown:
        return "unknown", [*unknown, *notes]
    if script_verdict == "idle" or notes:
        # idle is not a promise: show who else is connected so the caller can ask the user
        others = [_board_text(f"other ssh connection from {a}") for a in _other_addresses(lines)]
        if len(others) > MAX_OTHER_CONNECTIONS:
            extra = len(others) - MAX_OTHER_CONNECTIONS
            others = [*others[:MAX_OTHER_CONNECTIONS], f"… and {extra} more ssh connection address(es)"]
        return "idle", [*notes, *others]
    return "unknown", ["the in-use script gave an unreadable verdict"]


def _live_claim(board: Board, claim: Claim | None, now: Callable[[], datetime]) -> tuple[Claim | None, list[str]]:
    """The claim to discount for, or None plus a signal when a claim was given but is not live here."""
    if claim is None:
        return None, []
    if claim.board == str(board.id) and claim.expires > now():
        return claim, []
    return None, [f"claim {claim.claim_id} is not a live claim on this board; nothing discounted"]


async def _check_ssh(board: Board, ssh, claim: Claim | None, now: Callable[[], datetime]) -> Verdict:
    command = "sh -c " + shlex.quote(remote_script())
    result = await ssh.run(board.id, command, SCRIPT_TIMEOUT_S)
    if result.timed_out:
        return _verdict("unknown", [f"the in-use script timed out after {SCRIPT_TIMEOUT_S:g} s"])
    if result.exit_status != 0:
        return _verdict("unknown", [f"the in-use script exited with status {result.exit_status}"])
    live, claim_notes = _live_claim(board, claim, now)
    ignore = (now() - live.started).total_seconds() if live else None
    verdict, signals = parse_script_output(result.output, ignore, bool(result.truncated))
    return _verdict(verdict, [*claim_notes, *signals])


def _tt_state(status: dict) -> tuple[str | None, int | None]:
    display = status.get("idle_display")
    state = display.get("state") if isinstance(display, dict) else None
    clients = status.get("clients")
    if isinstance(clients, bool) or not isinstance(clients, int):
        clients = None
    return (state if isinstance(state, str) else None), clients


async def _check_tt(board: Board, sites, claim: Claim | None, clock, sleep, now) -> Verdict:
    live, claim_notes = _live_claim(board, claim, now)
    holder_loaded = live is not None and live.tt_state_at_claim == "loaded"
    first_ok: float | None = None
    waiting = 0
    for _ in range(TT_MAX_READS):
        status, entry = await sites.tt_status(board.name, live=True)
        if entry is not None and entry.stale:
            return _verdict("unknown", [*claim_notes, "status.json is stale (the site could not be refreshed)"])
        if status.get("reachable") is not True:
            return _verdict("unknown", ["the board is not reachable (no status from the site)"])
        state, clients = _tt_state(status)
        if clients is None or state is None:
            return _verdict("unknown", ["status.json has no usable clients/idle_display.state"])
        if clients > 0:
            return _verdict("in use", [f"{clients} serial client(s) connected"])
        note = list(claim_notes)
        if holder_loaded and (state == "waiting" or state.startswith("left:")):
            note = [f"ignored the claim holder's own state change to {defuse(state)!r}"]
            state = "loaded"
        if state == "in use":
            return _verdict("in use", [*claim_notes, "idle_display.state is 'in use'"])
        if state.startswith("left:"):
            return _verdict("in use", [_board_text(f"a design was left on the board ({state})")])
        if state == "waiting":
            waiting += 1
            first_ok = None
            if waiting >= TT_MAX_WAITING_READS:
                return _verdict("in use", ["still 'waiting' after two re-reads: the board keeps being used"])
            await sleep(TT_WAIT_S)
            continue
        if state != "loaded":
            return _verdict("unknown", [_board_text(f"idle_display.state is {state!r}, which is not idle")])
        now = clock()
        if first_ok is None:
            first_ok = now
            await sleep(TT_MIN_GAP_S + 0.5)
            continue
        if now - first_ok >= TT_MIN_GAP_S:
            return _verdict("idle", note)
        await sleep(TT_MIN_GAP_S - (now - first_ok) + 0.5)
    return _verdict("unknown", ["status.json never settled to a stable reading"])


async def check(
    board: Board,
    ssh,
    sites,
    claim: Claim | None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], object] = asyncio.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Verdict:
    """Live in-use check of one board. Never raises: any failure gives an `unknown` verdict."""
    try:
        if board.site == "tinytapeout":
            if board.type == "chip board":
                return _verdict("unknown", ["chip boards have no idle rule: ask the user"])
            return await _check_tt(board, sites, claim, clock, sleep, now)
        return await _check_ssh(board, ssh, claim, now)
    except Exception as exc:
        return _verdict("unknown", [f"the check failed: {type(exc).__name__}: {exc}"])
