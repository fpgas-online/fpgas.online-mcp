"""Power cycle: toggle a board's PoE port through the site and wait until the host really rebooted.

The only destructive tool. Order: fresh in-use check (refuse unless idle) -> record the boot to
replace (refuse if unreadable) -> close the ssh master -> toggle (no retry on 429) -> poll every
10 s for up to 300 s. A board is back only when its boot id changed (Tiny Tapeout: `uptime_s`
smaller than the time since the toggle) and it answers. Empty or null values never count.
"""

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import inuse
from .boards import Board
from .claims import Claim
from .sites import SiteError

POLL_S = 10.0
GIVE_UP_S = 300.0
BOOT_ID_COMMAND = "cat /proc/sys/kernel/random/boot_id"
BOOT_ID_TIMEOUT_S = 20.0
MAX_ERROR_LEN = 300

NOT_POE = (
    "the switch cycled the port but this host did not lose power: it is probably not powered by PoE, "
    "so the site cannot reset it — ask the user to reset it by hand"
)
NO_RETURN = "the board did not come back within 5 minutes: do not cycle it again, ask the user"

_WELLAND_NAME = re.compile(r"pi-sw([0-9]+)-p([0-9]+)")
_PS1_NAME = re.compile(r"pi([0-9]+)")
_BOOT_ID = re.compile(r"^[0-9a-fA-F-]{36}$")


@dataclass(frozen=True)
class CycleResult:
    ok: bool
    seconds_to_back: float | None
    message: str
    retry_after: int | None = None
    steps: list[str] = field(default_factory=list)


def _valid_boot_id(text: str | None) -> str | None:
    text = (text or "").strip()
    return text if len(text) == 36 and _BOOT_ID.match(text) else None


def _number(value) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


async def _ssh_boot_id(ssh, board: Board, timeout_s: float = BOOT_ID_TIMEOUT_S) -> str | None:
    """The host's boot id over ssh, or None on any failure (expected while it reboots)."""
    try:
        r = await ssh.run(board.id, BOOT_ID_COMMAND, timeout_s)
    except Exception:  # noqa: BLE001 - a host that is rebooting fails in many ways
        return None
    if r.exit_status != 0 or r.timed_out or r.truncated:
        return None
    return _valid_boot_id(r.output)


async def _registry_boot_id(board: Board, sites) -> str | None:
    """The live registry's boot id for a welland host (current boot only), or None."""
    try:
        machines, _ = await sites.welland_registry(live=True)
    except SiteError:
        return None
    found = None
    for m in machines:
        net = m.get("network") or {}
        if m.get("hostname") == board.name and net.get("current_boot") is True:
            found = _valid_boot_id(net.get("boot_id")) or found
    return found


async def _tt_uptime(sites, name: str) -> float | None:
    """`uptime_s` of a reachable, present Tiny Tapeout board, else None."""
    try:
        status, _ = await sites.tt_status(name, live=True)
    except SiteError:
        return None
    if status.get("reachable") is not True or (status.get("board") or {}).get("present") is not True:
        return None
    return _number(status.get("uptime_s"))


async def _toggle_args(board: Board, sites) -> tuple[str, int | None] | str:
    """(port, switch) for the toggle body, or a refusal message."""
    if board.site == "welland":
        m = _WELLAND_NAME.fullmatch(board.name)
        if not m:
            return f"cannot work out the switch port from the name {board.name!r}"
        return str(int(m[2])), int(m[1])
    if board.site == "ps1":
        m = _PS1_NAME.fullmatch(board.name)
        if not m:
            return f"cannot work out the switch port from the name {board.name!r}"
        return str(int(m[1])), None
    page = await sites.board_page("tinytapeout", board.name, live=True)
    if not page.tt_port or not page.tt_switch:
        return "the board page names no switch port: nothing to cycle"
    try:
        return page.tt_port, int(page.tt_switch)
    except ValueError:
        return "the board page names no usable switch: nothing to cycle"


def _refuse(message: str, steps: list[str]) -> CycleResult:
    return CycleResult(False, None, message, None, steps)


async def power_cycle(
    board: Board,
    claim: Claim,
    sites,
    ssh,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], object] = asyncio.sleep,
    inuse_check=inuse.check,
) -> CycleResult:
    steps: list[str] = []
    is_tt = board.site == "tinytapeout"

    verdict = await inuse_check(board, ssh, sites, claim, clock=clock, sleep=sleep)
    if verdict.verdict != "idle":
        signals = "; ".join(verdict.signals)
        steps.append(f"in-use check: {verdict.verdict}")
        return _refuse(
            f"not cycling: the in-use check says {verdict.verdict}" + (f" ({signals})" if signals else ""),
            steps,
        )
    steps.append("in-use check: idle")

    try:
        target = await _toggle_args(board, sites)
    except SiteError as e:
        return _refuse(f"not cycling: {e}", steps)
    if isinstance(target, str):
        return _refuse(f"not cycling: {target}", steps)
    port, switch = target

    old_id: str | None = None
    old_uptime: float | None = None
    by_registry = False  # welland only: the old id came from the registry, so compare registry ids
    if is_tt:
        old_uptime = await _tt_uptime(sites, board.name)
        ok = old_uptime is not None
    else:
        # Read over ssh (the in-use check just used the master) so old and new ids are like for like.
        old_id = await _ssh_boot_id(ssh, board)
        if old_id is None and board.site == "welland":
            old_id = await _registry_boot_id(board, sites)
            by_registry = old_id is not None
        ok = old_id is not None
    if not ok:
        return _refuse(
            "not cycling: could not read the board's current boot id, so a reboot could not be proven", steps
        )
    steps.append("boot id read" if not is_tt else f"daemon uptime read: {old_uptime:.0f} s")
    if by_registry:
        steps.append("boot id from the registry (ssh failed)")

    if not is_tt:
        await ssh.exit(board.id)  # the master would sit on a dead socket after the reboot

    t_toggle = clock()
    toggle_lost = None
    try:
        result = await sites.toggle(board.site, port, switch)
    except SiteError as e:
        # The POST may have reached the switch (a read timeout, say): never retry, watch the board.
        toggle_lost = str(e)
        steps.append("toggle response lost")
    else:
        steps.append(f"toggled at +{clock() - t_toggle:.1f} s")
        if result.status == 429:
            wait = result.retry_after
            when = f"in {wait} seconds" if wait is not None else "in about a minute"
            return CycleResult(
                False, None, f"the site is holding this port after a recent switch: try again {when}", wait, steps
            )
        if not 200 <= result.status < 300:
            error = result.body.get("error") if isinstance(result.body, dict) else None
            text = str(error)[:MAX_ERROR_LEN] if error else "no message"
            return _refuse(f"the site refused the toggle (HTTP {result.status}): {text}", steps)

    answered_same = False
    while True:
        if GIVE_UP_S - (clock() - t_toggle) < POLL_S:
            break
        await sleep(POLL_S)
        remaining = GIVE_UP_S - (clock() - t_toggle)
        if remaining <= 0:
            break  # a probe started now could not finish inside the cap
        if is_tt:
            uptime = await _tt_uptime(sites, board.name)
            back = uptime is not None and uptime < clock() - t_toggle
            answered_same = uptime is not None and not back
        else:
            probe_s = min(BOOT_ID_TIMEOUT_S, remaining)
            new_id = await _ssh_boot_id(ssh, board, probe_s)
            if by_registry:
                reg = await _registry_boot_id(board, sites)
                back = new_id is not None and reg is not None and reg != old_id
                answered_same = new_id is not None and not back
            else:
                back = new_id is not None and new_id != old_id
                answered_same = new_id is not None and not back
        if back:
            elapsed = clock() - t_toggle
            steps.append(f"back at +{elapsed:.0f} s")
            note = "; the toggle response was lost, but the board did restart" if toggle_lost else ""
            return CycleResult(
                True, elapsed, f"the board restarted and answers again after {elapsed:.0f} s{note}", None, steps
            )

    steps.append("gave up at +300 s")
    if toggle_lost:
        return _refuse(
            f"the toggle request failed after it may have reached the switch ({toggle_lost}) and the board did not "
            "restart: wait at least 60 s and check the board before trying again",
            steps,
        )
    return CycleResult(False, None, NOT_POE if answered_same else NO_RETURN, None, steps)
