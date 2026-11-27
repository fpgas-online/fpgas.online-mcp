"""In-use check: the stage 1 script over the shared ssh master (welland, ps1), and for a Tiny
Tapeout board both that script on the welland Pi it is plugged into and the site's `status.json`
rule. Verdicts: idle, in use, unknown ("unknown" is never "idle").

Two checks combine as: in use if either says so, else unknown if either says so, else idle.

The claim holder's own activity is discounted (spec, "In-use check"): `~/Uploads` changes newer
than the claim's start, and on Tiny Tapeout `waiting` / `left: ...` after a `loaded` state at
claim time. Every other signal still counts.
"""

import asyncio
import re
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from importlib import resources
from typing import Literal

from .boards import Board, BoardId, carries_tt, not_offered, tt_board_with_serial, tt_host, tt_serials_on
from .claims import Claim
from .sites import SiteError
from .ssh import defuse

SCRIPT_TIMEOUT_S = 30.0
MAX_SIGNALS = 10
MAX_SIGNAL_LEN = 200
TT_MIN_GAP_S = 20.5  # the site caches status.json for 5 s: 15 s apart plus margin for the cache
TT_WAIT_S = 75.0
TT_MAX_READS = 10
TT_MAX_WAITING_READS = 3  # the first read plus two re-reads

MAX_OTHER_CONNECTIONS = 5
TT_NOT_LISTED = "carries a Tiny Tapeout board the Tiny Tapeout site does not list"
TT_NO_SERIAL = "the Pi did not report its Tiny Tapeout board's serial, so web visitors cannot be checked"
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
    # unknown because the site does not offer this welland Pi (its page is a 404) and allow_unoffered
    # was off: overriding the verdict would not help, the claim could not reach the Pi anyway
    not_offered: bool = False


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


def _failed(exc: Exception) -> Verdict:
    return _verdict("unknown", [f"the check failed: {type(exc).__name__}: {exc}"])


async def _safe(check) -> Verdict:
    """Await one half of a combined check; a failure is that half's `unknown`, so the other half's
    signals are still shown."""
    try:
        return await check
    except Exception as exc:
        return _failed(exc)


def _combine(site: Verdict, ssh: Verdict) -> Verdict:
    """In use if either is, else unknown if either is, else idle; signals from both, the site's first."""
    verdicts = (site.verdict, ssh.verdict)
    verdict = "in use" if "in use" in verdicts else "unknown" if "unknown" in verdicts else "idle"
    v = _verdict(verdict, list(dict.fromkeys([*site.signals, *ssh.signals])))
    return replace(v, not_offered=site.not_offered or ssh.not_offered)


async def _check_ssh(
    board: Board,
    ssh,
    claim: Claim | None,
    now: Callable[[], datetime],
    host: BoardId | None = None,
    allow_unoffered: bool = False,
) -> Verdict:
    """The script on the board's host: the board itself, or `host` (the Pi a Tiny Tapeout board is
    plugged into; the claim is still compared against the Tiny Tapeout board). `allow_unoffered`
    reaches a welland Pi the site does not offer (never used for a Tiny Tapeout board's Pi)."""
    command = "sh -c " + shlex.quote(remote_script())
    if host is None:
        try:
            result = await ssh.run(board.id, command, SCRIPT_TIMEOUT_S, allow_unoffered=allow_unoffered)
        except SiteError as e:
            if e.status != 404 or board.site != "welland" or allow_unoffered:
                raise
            return replace(_verdict("unknown", [not_offered(board)]), not_offered=True)
    else:
        result = await ssh.run(host, command, SCRIPT_TIMEOUT_S, via=board.id)
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


def _usable_status(status: dict, entry) -> str | None:
    """Why a status.json read cannot be used, or None."""
    if entry is not None and entry.stale:
        return "status.json is stale (the site could not be refreshed)"
    if status.get("reachable") is not True:
        return "the board is not reachable (no status from the site)"
    return None


async def _check_tt(board: Board, sites, claim: Claim | None, clock, sleep, now) -> Verdict:
    live, claim_notes = _live_claim(board, claim, now)
    holder_loaded = live is not None and live.tt_state_at_claim == "loaded"
    first_ok: float | None = None
    waiting = 0
    for _ in range(TT_MAX_READS):
        status, entry = await sites.tt_status(board.name, live=True)
        if (why := _usable_status(status, entry)) is not None:
            return _verdict("unknown", [*claim_notes, why])
        state, clients = _tt_state(status)
        if clients is None or state is None:
            return _verdict("unknown", [*claim_notes, "status.json has no usable clients/idle_display.state"])
        if clients > 0:
            return _verdict("in use", [*claim_notes, f"{clients} serial client(s) connected"])
        note = list(claim_notes)
        if holder_loaded and (state == "waiting" or state.startswith("left:")):
            note = [_board_text(f"ignored the claim holder's own state change to {state!r}")]
            state = "loaded"
        if state == "in use":
            return _verdict("in use", [*claim_notes, "idle_display.state is 'in use'"])
        if state.startswith("left:"):
            return _verdict("in use", [*claim_notes, _board_text(f"a design was left on the board ({state})")])
        if state == "waiting":
            waiting += 1
            first_ok = None
            if waiting >= TT_MAX_WAITING_READS:
                return _verdict(
                    "in use", [*claim_notes, "still 'waiting' after two re-reads: the board keeps being used"]
                )
            await sleep(TT_WAIT_S)
            continue
        if state != "loaded":
            return _verdict(
                "unknown", [*claim_notes, _board_text(f"idle_display.state is {state!r}, which is not idle")]
            )
        t = clock()
        if first_ok is None:
            first_ok = t
            await sleep(TT_MIN_GAP_S + 0.5)
            continue
        if t - first_ok >= TT_MIN_GAP_S:
            return _verdict("idle", note)
        await sleep(TT_MIN_GAP_S - (t - first_ok) + 0.5)
    return _verdict("unknown", [*claim_notes, "status.json never settled to a stable reading"])


async def _check_tt_clients(board: Board, sites, clock, sleep) -> Verdict:
    """Chip boards: idle only when `clients` (serial clients) is 0 on two reads at least TT_MIN_GAP_S
    apart. `idle_display.state` is not used (a chip board always says 'not an fpga board')."""
    first_ok: float | None = None
    for _ in range(TT_MAX_READS):
        status, entry = await sites.tt_status(board.name, live=True)
        if (why := _usable_status(status, entry)) is not None:
            return _verdict("unknown", [why])
        _, clients = _tt_state(status)
        if clients is None:
            return _verdict("unknown", ["status.json has no usable clients count"])
        if clients > 0:
            return _verdict("in use", [f"{clients} serial client(s) connected"])
        t = clock()
        if first_ok is not None and t - first_ok >= TT_MIN_GAP_S:
            return _verdict("idle", [])
        if first_ok is None:
            first_ok = t
        await sleep(TT_MIN_GAP_S - (t - first_ok) + 0.5)
    return _verdict("unknown", ["status.json never settled to a stable reading"])


async def _tt_site_rule(board: Board, sites, claim: Claim | None, clock, sleep, now) -> Verdict:
    if board.type == "chip board":
        return await _check_tt_clients(board, sites, clock, sleep)
    return await _check_tt(board, sites, claim, clock, sleep, now)


async def _check_tt_board(board: Board, ssh, sites, claim: Claim | None, clock, sleep, now) -> Verdict:
    """A Tiny Tapeout board: the site rule and the ssh script on its Pi, at the same time."""
    host = await tt_host(sites, board.name)
    if host is None:
        return _verdict("unknown", ["the board page names no Pi: the board is not connected"])
    site_verdict, ssh_verdict = await asyncio.gather(
        _safe(_tt_site_rule(board, sites, claim, clock, sleep, now)),
        _safe(_check_ssh(board, ssh, claim, now, host)),
    )
    return _combine(site_verdict, ssh_verdict)


async def _tt_rule_of_pi(board: Board, sites, clock, sleep, now) -> Verdict:
    """The site rule of the Tiny Tapeout board plugged into welland Pi `board`.

    When the live registry shows no such board now, an `idle` that leaves the ssh verdict alone. A
    board whose serial the Pi did not report, or that the Tiny Tapeout site does not list (after one
    live re-read of the catalogue: the cached one may predate the board), is `unknown`: nobody can
    tell whether a web visitor is on it, and unknown is never idle. No claim discount: a claim on
    the Pi is not one on the Tiny Tapeout board.
    """
    serials = await tt_serials_on(sites, board.name)
    if not serials:
        return _verdict("idle", [])
    if None in serials:
        return _verdict("unknown", [TT_NO_SERIAL])
    tt_board = await tt_board_with_serial(sites, serials) or await tt_board_with_serial(sites, serials, live=True)
    if tt_board is None:
        return _verdict("unknown", [TT_NOT_LISTED])
    v = await _tt_site_rule(tt_board, sites, None, clock, sleep, now)
    return _verdict(v.verdict, [f"carries the Tiny Tapeout board {tt_board.id}", *v.signals])


async def _check_pi_with_tt(
    board: Board, ssh, sites, claim: Claim | None, clock, sleep, now, allow_unoffered: bool = False
) -> Verdict:
    """A welland Pi that carries a Tiny Tapeout board: its ssh script and that board's site rule, so a
    web visitor on the Tiny Tapeout page blocks a claim on the Pi too."""
    site_verdict, ssh_verdict = await asyncio.gather(
        _safe(_tt_rule_of_pi(board, sites, clock, sleep, now)),
        _safe(_check_ssh(board, ssh, claim, now, allow_unoffered=allow_unoffered)),
    )
    return _combine(site_verdict, ssh_verdict)


async def check(
    board: Board,
    ssh,
    sites,
    claim: Claim | None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], object] = asyncio.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    allow_unoffered: bool = False,
) -> Verdict:
    """Live in-use check of one board. Never raises: any failure gives an `unknown` verdict.

    `allow_unoffered` lets the script reach a welland Pi the site does not offer (the caller decides
    that the option applies); a Tiny Tapeout board ignores it (it reaches its Pi through `via`)."""
    try:
        if board.site == "tinytapeout":
            return await _check_tt_board(board, ssh, sites, claim, clock, sleep, now)
        if carries_tt(board):
            return await _check_pi_with_tt(board, ssh, sites, claim, clock, sleep, now, allow_unoffered)
        return await _check_ssh(board, ssh, claim, now, allow_unoffered=allow_unoffered)
    except Exception as exc:
        return _failed(exc)
