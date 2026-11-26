"""The broker's ops: every tool call of the MCP front end (and the CLI) ends up here.

Handlers take (state, args, conn). Errors are BrokerError with a message that says what to do next;
the front end shows the message to the agent as an error result.

Claim resolution (connect, run, power_cycle): an explicit `claim_id` must be the live claim on that
board (the id is the authority: any holder may pass it). Without one, the board's live claim counts if
its id is among `claim_ids` (the ids the front end created and remembers) or was created on this
connection. No claim -> an error naming claim_board.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from . import guide, inuse, power
from .boards import Board, BoardId, inventory, switch_port
from .broker import Broker, Conn
from .claims import Claim, ClaimConflict, ClaimGone, ClaimStore
from .protocol import BrokerError
from .sites import SiteClient, SiteError
from .ssh import PortMismatch, SshManager, defuse, wrap_untrusted

log = logging.getLogger(__name__)

LIMIT = 4096
LIST_LIMIT = 8192  # list_boards: one site (welland ~46 boards) and most of all three fit uncut
LIST_COLUMNS = ["id", "type", "condition", "offered", "last_seen"]
SSH_DETAIL_LIMIT = 1024
RUN_LIMIT = 8192
RUN_HEADER_RESERVE = 512  # room for the run result's other fields next to the output
MAX_RUN_TIMEOUT_S = 600
OFFLINE = {"missing", "not connected"}


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> str:
    return _iso(datetime.now(UTC))


def _short(s: str | None) -> str | None:
    """A site's ISO time to whole seconds UTC (shorter lists); left alone if it does not parse."""
    if not s:
        return s
    try:
        t = datetime.fromisoformat(s)
    except ValueError:
        return s
    return _iso(t if t.tzinfo else t.replace(tzinfo=UTC))


def compact(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _size(obj) -> int:
    return len(compact(obj).encode())


@dataclass
class State:
    sites: SiteClient
    claims: ClaimStore
    ssh: SshManager
    inuse_check: Callable = inuse.check
    power_cycle: Callable = power.power_cycle
    conn_claims: dict[Conn, set[str]] = field(default_factory=dict)
    holder_conns: dict[str, set[Conn]] = field(default_factory=dict)

    async def check(self, board: Board, ssh, sites, claim: Claim | None, **kw):
        """The in-use check with its signals joined and wrapped once as untrusted board text."""
        v = await self.inuse_check(board, ssh, sites, claim, **kw)
        return replace(v, signals=[wrap_untrusted("; ".join(v.signals))] if v.signals else [])

    # --- connected holders ------------------------------------------------------------------------

    def seen(self, conn: Conn) -> None:
        """Count conn for its holder (the holder is known only after the first request)."""
        if conn.holder is None:
            return
        conns = self.holder_conns.setdefault(conn.holder, set())
        if conn not in conns:
            conns.add(conn)
            self.claims.set_connected(conn.holder, True)

    def gone(self, conn: Conn) -> None:
        """A connection closed: the holder is disconnected when its last connection closes."""
        self.conn_claims.pop(conn, None)
        conns = self.holder_conns.get(conn.holder) if conn.holder else None
        if conns is None:
            return
        conns.discard(conn)
        if not conns:
            del self.holder_conns[conn.holder]
            self.claims.set_connected(conn.holder, False)


# --- argument helpers -----------------------------------------------------------------------------


def _str(args: dict, key: str, required: bool = False) -> str | None:
    value = args.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise BrokerError("bad_request", f"{key} is required")
        return None
    if not isinstance(value, str):
        raise BrokerError("bad_request", f"{key} must be a string")
    return value.strip()


def _int(args: dict, key: str, default: int, lo: int, hi: int) -> int:
    value = args.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise BrokerError("bad_request", f"{key} must be a whole number from {lo} to {hi}")
    return value


def _remembered(state: State, args: dict, conn: Conn) -> set[str]:
    ids = args.get("claim_ids") or []
    if not isinstance(ids, list):
        raise BrokerError("bad_request", "claim_ids must be a list")
    return {i for i in ids if isinstance(i, str)} | state.conn_claims.get(conn, set())


# --- views ----------------------------------------------------------------------------------------


def _claim_view(c: Claim, conn: Conn) -> dict:
    view = {
        "holder": c.holder,
        "purpose": c.purpose,
        "expires": _iso(c.expires),
        "holder_connected": c.holder_connected,
    }
    if c.override_reason:
        view["override_reason"] = c.override_reason
    view["claim_id"] = c.claim_id  # the id is the authority; shown to everyone (spec)
    if conn.holder is not None and c.holder == conn.holder:
        view["yours"] = True
    return view


def _board_view(b: Board, claim: Claim | None, conn: Conn, full: bool = False) -> dict:
    view = {
        "id": str(b.id),
        "type": b.type,
        "condition": b.condition,
        "offered": b.offered,
        "last_seen": _short(b.last_seen),
    }
    if full:
        view.update({"boot_id": b.boot_id, "usb_serial": b.usb_serial, "as_of": _short(b.as_of)})
    view = {k: v for k, v in view.items() if v is not None}
    if b.stale:
        view["stale"] = True
    if claim is not None:
        view["claim"] = _claim_view(claim, conn)
    return view


def _verdict_view(v) -> dict:
    view = {"verdict": v.verdict, "signals": v.signals, "as_of": v.as_of}
    if v.verdict != "idle":
        view["note"] = "treat this board as in use"
    return view


def _conflict(e: ClaimConflict) -> BrokerError:
    return BrokerError("claimed", f"{e.existing.board} is {e}; ask the user or pick another board")


# --- board lookup ---------------------------------------------------------------------------------


async def _resolve(state: State, args: dict) -> Board:
    s = _str(args, "board", required=True)
    site = s.partition(":")[0] if ":" in s else None
    try:
        inv = await inventory(state.sites, site)
    except ValueError as e:
        raise BrokerError("bad_board", str(e)) from e
    down = [f"{k} ({v})" for k, v in inv.notes.items() if v.startswith("unavailable")]
    name = s.partition(":")[2]
    if (
        site == "ps1"
        and not down
        and switch_port("ps1", name)
        and BoardId("ps1", name) not in {b.id for b in inv.boards}
    ):
        raise BrokerError(
            "not_offered",
            f"ps1:{name} is not offered by ps1 right now (ps1 lists only the hosts it offers: it may be rebooting "
            "or taken on the website); try again later or pick another board",
        )
    try:
        board_id = BoardId.parse(s, [b.id for b in inv.boards])
    except ValueError as e:
        extra = f"; site(s) not answering now: {', '.join(down)}" if down else ""
        raise BrokerError("bad_board", f"{e}{extra}") from e
    found = next((b for b in inv.boards if b.id == board_id), None)
    if found is None:
        if down:
            raise BrokerError(
                "site_unavailable", f"cannot look up {board_id}: {'; '.join(down)}; try again in a minute"
            )
        raise BrokerError("bad_board", f"no such board {board_id}; use list_boards")
    return found


def _claim_for(state: State, board: Board, args: dict, conn: Conn) -> Claim:
    live = state.claims.for_board(str(board.id))
    explicit = _str(args, "claim_id")
    if explicit is not None:
        if live is not None and live.claim_id == explicit:
            return live
        raise BrokerError(
            "no_claim",
            f"{explicit} is not a live claim on {board.id} (expired, released or for another board); "
            "call claim_board to take a claim",
        )
    if live is not None and live.claim_id in _remembered(state, args, conn):
        return live
    if live is not None:
        raise _conflict(ClaimConflict(live))
    raise BrokerError(
        "no_claim",
        f"you hold no claim on {board.id}: call claim_board first (it checks that nobody is using the board), "
        "or pass the claim_id you were given",
    )


# --- ops ------------------------------------------------------------------------------------------


def _row(b: Board) -> list:
    """One dense list_boards row in LIST_COLUMNS order (stale is a site-level flag)."""
    return [str(b.id), b.type, b.condition, b.offered, _short(b.last_seen)]


async def op_list_boards(state: State, args: dict, conn: Conn) -> dict:
    site = _str(args, "site")
    board_type = (_str(args, "board_type") or "").lower()
    condition = (_str(args, "condition") or "").lower()
    include_offline = args.get("include_offline", True) is not False
    try:
        inv = await inventory(state.sites, site)
    except ValueError as e:
        raise BrokerError("bad_request", str(e)) from e
    claims = {c.board: c for c in state.claims.all()}
    sites = {}
    for s, note in inv.notes.items():
        mine = [b for b in inv.boards if b.site == s]
        info = {"note": note, "boards": len(mine)}
        if mine:
            info["as_of"] = _short(min(b.as_of for b in mine))
        if any(b.stale for b in mine):
            info["stale"] = True
        sites[s] = info
    chosen = [
        b
        for b in inv.boards
        if (include_offline or b.condition not in OFFLINE)
        and board_type in (b.type or "").lower()
        and (not condition or b.condition.lower() == condition)
    ]
    conditions = sorted({b.condition for b in inv.boards})

    def build(n: int) -> dict:
        shown = chosen[:n]
        result = {"sites": sites, "conditions": conditions, "columns": LIST_COLUMNS, "boards": [_row(b) for b in shown]}
        mine = {str(b.id): _claim_view(claims[str(b.id)], conn) for b in shown if str(b.id) in claims}
        if mine:
            result["claims"] = mine
        if n < len(chosen):
            result["more"] = f"… {len(chosen) - n} more boards not shown; filter with site, condition or board_type"
        return result

    # Too long: keep as many boards as fit (binary search), then say how many were left out.
    lo, hi = 0, len(chosen)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _size(build(mid)) <= LIST_LIMIT:
            lo = mid
        else:
            hi = mid - 1
    return build(lo)


async def op_board_status(state: State, args: dict, conn: Conn) -> dict:
    board = await _resolve(state, args)
    claim = state.claims.for_board(str(board.id))
    result = {"board": _board_view(board, claim, conn, full=True)}
    if args.get("check_use", True) is not False:
        result["in_use"] = _verdict_view(await state.check(board, state.ssh, state.sites, None))
    return result


async def _tt_state(state: State, board: Board) -> str | None:
    try:
        status, _ = await state.sites.tt_status(board.name, live=True)
    except SiteError:
        return None
    display = status.get("idle_display")
    value = display.get("state") if isinstance(display, dict) else None
    return value if isinstance(value, str) else None


async def op_claim_board(state: State, args: dict, conn: Conn) -> dict:
    board = await _resolve(state, args)
    purpose = _str(args, "purpose", required=True)
    minutes = _int(args, "minutes", 30, 1, 240)
    override = _str(args, "override_reason")
    explicit = _str(args, "claim_id")
    existing = state.claims.for_board(str(board.id))
    renew_id = explicit
    if renew_id is None and existing is not None and existing.claim_id in _remembered(state, args, conn):
        renew_id = existing.claim_id
    if existing is not None and existing.claim_id != renew_id:
        raise _conflict(ClaimConflict(existing))
    if renew_id is not None and existing is None:
        raise BrokerError(
            "no_claim",
            f"{renew_id} is not a live claim on {board.id} (it may have expired); call claim_board without "
            "claim_id to take a new claim",
        )

    verdict = None
    tt_state = None
    if renew_id is None:
        verdict = await state.check(board, state.ssh, state.sites, None)
        if verdict.verdict != "idle" and override is None:
            signals = "; ".join(verdict.signals)
            raise BrokerError(
                "in_use",
                f"not claimed: the in-use check of {board.id} says {verdict.verdict}"
                + (f" ({signals})" if signals else "")
                + f" as of {verdict.as_of}. Pick another board or ask the user; only if the user says it is fine, "
                "claim again with override_reason saying why",
            )
        if board.site == "tinytapeout":
            tt_state = await _tt_state(state, board)
    if conn.closed():  # the caller gave up during the check: a claim now would be one it never learns of
        log.info("not claiming %s: the client hung up during the in-use check", board.id)
        raise BrokerError("cancelled", "the claim call was cancelled; nothing was claimed")
    try:
        claim = await state.claims.claim(
            str(board.id),
            minutes,
            purpose,
            conn.holder or "unknown",
            claim_id=renew_id,
            override_reason=override if renew_id is None else None,
            tt_state_at_claim=tt_state,
        )
    except ClaimConflict as e:
        raise _conflict(e) from e
    except ClaimGone as e:
        raise BrokerError(
            "no_claim",
            f"{e}: {e.claim_id} on {board.id} ended before it could be renewed; call claim_board "
            "without claim_id (it runs the in-use check again)",
        ) from e
    except ValueError as e:
        raise BrokerError("bad_request", str(e)) from e
    state.conn_claims.setdefault(conn, set()).add(claim.claim_id)
    result = {
        "claim_id": claim.claim_id,
        "board": claim.board,
        "holder": claim.holder,
        "purpose": claim.purpose,
        "started": _iso(claim.started),
        "expires": _iso(claim.expires),
        "renewed": renew_id is not None,
        "as_of": _now(),
        "next": "connect / run / power_cycle use this claim; keep claim_id to pass it after a restart; "
        "release_board when done",
    }
    if claim.override_reason:
        result["override_reason"] = claim.override_reason
    if verdict is not None:
        result["in_use"] = _verdict_view(verdict)
    return result


async def op_release_board(state: State, args: dict, conn: Conn) -> dict:
    claim_id = _str(args, "claim_id", required=True)
    try:
        await state.claims.release(claim_id)
    except KeyError:
        raise BrokerError(
            "no_claim", f"{claim_id} is not a live claim (expired or already released); list_boards shows claims"
        ) from None
    for ids in state.conn_claims.values():
        ids.discard(claim_id)
    return {"released": claim_id, "as_of": _now()}


def _ssh_error(board: Board, e: Exception, next_step: str) -> BrokerError:
    """ssh's own text can carry a host's pre-auth banner: log it all, show at most 1 KB of it wrapped."""
    detail = str(e)
    log.warning("ssh to %s failed: %s", board.id, detail)
    shown, _ = _fit_bytes(defuse(detail), SSH_DETAIL_LIMIT)
    return BrokerError("ssh", f"ssh to {board.id} failed: {wrap_untrusted(shown)}; {next_step}")


async def op_connect(state: State, args: dict, conn: Conn) -> dict:
    board = await _resolve(state, args)
    claim = _claim_for(state, board, args, conn)
    try:
        info = await state.ssh.ensure_master(board.id)
    except PortMismatch as e:
        raise BrokerError("port_mismatch", f"{e}; tell the user") from e
    except ValueError as e:
        raise BrokerError("no_ssh", f"{e}; use board_status and the guide topic tinytapeout") from e
    except (RuntimeError, SiteError) as e:
        raise _ssh_error(board, e, "check board_status and retry later, or pick another board") from e
    return {
        "board": str(board.id),
        "claim_id": claim.claim_id,
        "alias": info.alias,
        "config": info.config_path,
        "ssh": info.ssh_line,
        "scp": info.scp_line,
        "include": f"to use plain `ssh {info.alias}`, the user can add this line at the top of ~/.ssh/config "
        f"(before any Host block, or ssh applies it only inside that block): {info.include_line}",
        "rules": "non-interactive commands only: never ssh -t, never kill or exit the shared tmux. The shared "
        "connection closes after 30 minutes unused (and on power_cycle): if the ssh line asks for a password or "
        "fails, call connect again",
        "as_of": _now(),
    }


def _fit_bytes(text: str, budget: int) -> tuple[str, bool]:
    data = text.encode()
    if len(data) <= budget:
        return text, False
    half = max(0, (budget - 64) // 2)
    head = data[:half].decode(errors="ignore")
    tail = data[len(data) - half :].decode(errors="ignore")
    return f"{head}\n[… {len(data) - 2 * half} bytes omitted …]\n{tail}", True


async def op_run(state: State, args: dict, conn: Conn) -> dict:
    board = await _resolve(state, args)
    command = _str(args, "command", required=True)
    timeout_s = _int(args, "timeout_s", 60, 1, MAX_RUN_TIMEOUT_S)
    _claim_for(state, board, args, conn)
    try:
        r = await state.ssh.run(board.id, command, timeout_s)
    except PortMismatch as e:
        raise BrokerError("port_mismatch", f"{e}; tell the user") from e
    except ValueError as e:
        raise BrokerError("no_ssh", str(e)) from e
    except (RuntimeError, SiteError) as e:
        raise _ssh_error(board, e, "check board_status and retry later") from e
    budget = RUN_LIMIT - RUN_HEADER_RESERVE - len(wrap_untrusted("").encode())
    output, cut = _fit_bytes(defuse(r.output), budget)
    return {
        "board": str(board.id),
        "exit_status": r.exit_status,
        "timed_out": r.timed_out,
        "truncated": r.truncated or cut,
        "as_of": _now(),
        "output": wrap_untrusted(output),
    }


async def op_power_cycle(state: State, args: dict, conn: Conn) -> dict:
    board = await _resolve(state, args)
    claim = _claim_for(state, board, args, conn)
    r = await state.power_cycle(board, claim, state.sites, state.ssh, inuse_check=state.check)
    return {
        "board": str(board.id),
        "ok": r.ok,
        "message": r.message,
        "seconds_to_back": None if r.seconds_to_back is None else round(r.seconds_to_back),
        "retry_after": r.retry_after,
        "steps": r.steps,
        "as_of": _now(),
    }


async def op_guide(state: State, args: dict, conn: Conn) -> dict:
    try:
        return {"text": guide.text(_str(args, "topic"))}
    except ValueError as e:
        raise BrokerError("bad_request", str(e)) from e


async def op_claims(state: State, args: dict, conn: Conn) -> dict:
    """Every live claim (for the CLI)."""
    claims = []
    for c in state.claims.all():
        view = _claim_view(c, conn)
        view["board"] = c.board
        view["started"] = _iso(c.started)
        claims.append(view)
    return {"claims": claims, "as_of": _now()}


OPS = {
    "list_boards": op_list_boards,
    "board_status": op_board_status,
    "claim_board": op_claim_board,
    "release_board": op_release_board,
    "connect": op_connect,
    "run": op_run,
    "power_cycle": op_power_cycle,
    "guide": op_guide,
    "claims": op_claims,
}


def register(broker: Broker, state: State) -> None:
    """Register every op and the connection hooks that keep `holder_connected` true."""

    def bind(fn):
        async def handler(args: dict, conn: Conn):
            state.seen(conn)
            return await fn(state, args, conn)

        return handler

    for name, fn in OPS.items():
        broker.register(name, bind(fn))
    broker.on_connect = state.seen
    broker.on_disconnect = state.gone
