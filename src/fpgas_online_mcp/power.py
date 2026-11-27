"""Power cycle: toggle a board's PoE port through the site and wait until the host really rebooted.

The only destructive tool. Order: fresh in-use check (refuse unless idle; "in use" always refuses,
"unknown" only goes on under the claim's `override_reason`) -> record the boot to replace (refuse if
unreadable, unless the check read unknown under the override: a hung board has no boot to read) ->
close the ssh master (a Tiny Tapeout board's: its Pi's, when the board page names one) -> toggle (no
retry on 429) -> poll every 10 s for up to 300 s. A board is back only when its boot id changed
(Tiny Tapeout: `uptime_s` smaller than the time since the toggle) and it answers. With no old boot
id, a welland or ps1 host is back only when its boot id and `/proc/uptime` come in one probe and the
uptime is smaller than the time since the toggle: never "it answers" alone. Empty or null values
never count.
"""

import asyncio
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import inuse
from .boards import Board, BoardId, host_changed, switch_port, tt_host_of, tt_switch_port
from .claims import Claim
from .sites import SiteError
from .ssh import RUN_SLACK_S, clean_text

POLL_S = 10.0
GIVE_UP_S = 300.0
BOOT_ID_COMMAND = "cat /proc/sys/kernel/random/boot_id"
BOOT_UPTIME_COMMAND = "cat /proc/sys/kernel/random/boot_id /proc/uptime"
BOOT_ID_TIMEOUT_S = 20.0
MAX_ERROR_LEN = 300

NOT_POE = (
    "the switch cycled the port but this host did not lose power: it is probably not powered by PoE, "
    "so the site cannot reset it — ask the user to reset it by hand"
)
CANCELLED = "cancelled (or the claim ended) before the toggle; nothing was toggled"
NO_RETURN = "the board did not come back within 5 minutes: do not cycle it again, ask the user"
NO_FRESH_BOOT = (
    "after 5 minutes the board answers but has not restarted since the toggle (its uptime is older than the "
    "toggle): do not cycle it again, ask the user"
)
MAX_REASON_LEN = 200
OTHER_OVERRIDE_HINT = (
    '. The claim\'s override was given for "{given}", not for unknown: release this claim, claim it again '
    "(an override for unknown needs the user's agreement), then power_cycle"
)
NO_OLD_STEP = "board not answering before the toggle: back means it answers with a boot younger than the toggle"
UNKNOWN_HINT = (
    ". If the board is down and only if the user agrees, release this claim and claim it again with "
    "override_reason saying why; power_cycle then goes ahead on an unknown verdict (never on in use)"
)

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


async def _ssh_boot_id(
    ssh, board: Board, timeout_s: float = BOOT_ID_TIMEOUT_S, allow_unoffered: bool = False
) -> str | None:
    """The host's boot id over ssh, or None on any failure (expected while it reboots)."""
    try:
        r = await ssh.run(board.id, BOOT_ID_COMMAND, timeout_s, allow_unoffered=allow_unoffered)
    except Exception:  # noqa: BLE001 - a host that is rebooting fails in many ways
        return None
    if r.exit_status != 0 or r.timed_out or r.truncated:
        return None
    return _valid_boot_id(r.output)


def _uptime(text: str) -> float | None:
    """The first field of /proc/uptime when it is a finite, non-negative number, else None."""
    fields = text.split()
    try:
        value = float(fields[0]) if fields else math.nan
    except ValueError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


async def _ssh_boot_and_uptime(
    ssh, board: Board, timeout_s: float, allow_unoffered: bool
) -> tuple[str | None, float | None]:
    """(boot id, /proc/uptime seconds) from one probe. The boot id is None unless the output is the
    boot id alone or it and an uptime line (anything else, a banner say, is unreadable); the uptime is
    None when its line is missing or not a number."""
    try:
        r = await ssh.run(board.id, BOOT_UPTIME_COMMAND, timeout_s, allow_unoffered=allow_unoffered)
    except Exception:  # noqa: BLE001 - a host that is rebooting fails in many ways
        return None, None
    if r.truncated or r.timed_out:
        return None, None
    lines = (r.output or "").splitlines()
    if not 1 <= len(lines) <= 2:
        return None, None
    boot = _valid_boot_id(lines[0])
    if boot is None:
        return None, None
    # cat exits non-zero when /proc/uptime is unreadable, after printing the boot id: the host answered
    up = _uptime(lines[1]) if len(lines) == 2 and r.exit_status == 0 else None
    return boot, up


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


async def _toggle_args(board: Board, sites) -> tuple[str, int | None, BoardId | None] | str:
    """(port, switch) for the toggle body and the host whose ssh master to close first (a Tiny Tapeout
    board's Pi, from the same live page; None when it names none), or a refusal message."""
    if board.site in ("welland", "ps1"):
        target = switch_port(board.site, board.name)
        if target is None:
            return f"cannot work out the switch port from the name {board.name!r}"
        return str(target[0]), target[1], board.id
    page = await sites.board_page("tinytapeout", board.name, live=True)
    if not page.tt_port or not page.tt_switch:
        return "the board page names no switch port: nothing to cycle"
    sp = tt_switch_port(page)
    if sp is None:
        return "the board page names no usable switch and port: nothing to cycle"
    return str(sp[1]), sp[0], tt_host_of(page)


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
    should_stop: Callable[[], bool] = lambda: False,
) -> CycleResult:
    """`should_stop()` is asked just before the master is closed and just before the toggle: true (the
    caller hung up, or the claim is no longer live) ends the cycle with nothing toggled.

    The claim's `allow_unoffered` (never an argument) lets the check and the boot id reads reach a
    welland Pi the site does not offer."""
    steps: list[str] = []
    is_tt = board.site == "tinytapeout"
    allow = claim.allow_unoffered

    verdict = await inuse_check(board, ssh, sites, claim, clock=clock, sleep=sleep, allow_unoffered=allow)
    has_override = (claim.override_reason or "").strip() != ""
    # Only an unknown verdict can be overridden, never "in use" (nor anything unexpected), and only by an
    # override the user gave for unknown: one given for "in use" or an idle claim does not stretch to it.
    on_override = (
        verdict.verdict == "unknown"
        and has_override
        and not verdict.not_offered
        and claim.verdict_at_claim == "unknown"
    )
    if verdict.verdict != "idle" and not on_override:
        signals = "; ".join(verdict.signals)
        steps.append(f"in-use check: {verdict.verdict}")
        hint = ""
        if verdict.verdict == "unknown" and not verdict.not_offered:
            if has_override:
                given = claim.verdict_at_claim or "an unrecorded verdict"
                hint = OTHER_OVERRIDE_HINT.format(given=given)
            else:
                hint = UNKNOWN_HINT
        return _refuse(
            f"not cycling: the in-use check says {verdict.verdict}" + (f" ({signals})" if signals else "") + hint,
            steps,
        )
    if on_override:
        reason = " ".join(clean_text(claim.override_reason).split())[:MAX_REASON_LEN]
        steps.append(f"in-use check: unknown; going ahead on the claim's override ({reason})")
    else:
        steps.append("in-use check: idle")

    try:
        target = await _toggle_args(board, sites)
    except SiteError as e:
        return _refuse(f"not cycling: {e}", steps)
    if isinstance(target, str):
        return _refuse(f"not cycling: {target}", steps)
    port, switch, master = target
    if is_tt and master is not None and str(master) != claim.host:
        return _refuse(f"not cycling: {host_changed(board.id, master, claim.host)}", steps)

    old_id: str | None = None
    old_uptime: float | None = None
    by_registry = False  # welland only: the old id came from the registry, so compare registry ids
    if is_tt:
        old_uptime = await _tt_uptime(sites, board.name)
        ok = old_uptime is not None
    else:
        # Read over ssh (the in-use check just used the master) so old and new ids are like for like.
        old_id = await _ssh_boot_id(ssh, board, allow_unoffered=allow)
        if old_id is None and board.site == "welland":
            old_id = await _registry_boot_id(board, sites)
            by_registry = old_id is not None
        ok = old_id is not None
    no_old = not ok
    if no_old and not on_override:
        return _refuse(
            "not cycling: could not read the board's current boot id, so a reboot could not be proven", steps
        )
    if no_old:
        steps.append(NO_OLD_STEP)
    else:
        steps.append("boot id read" if not is_tt else f"daemon uptime read: {old_uptime:.0f} s")
    if by_registry:
        steps.append("boot id from the registry (ssh failed)")

    if should_stop():
        steps.append("stopped before the toggle")
        return _refuse(CANCELLED, steps)
    if master is not None:
        await ssh.exit(master)  # the master would sit on a dead socket after the reboot
    if should_stop():
        steps.append("stopped before the toggle")
        return _refuse(CANCELLED, steps)

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
            # With no old value the fresh uptime is all the proof: bound it by the time before the fetch.
            since = clock() - t_toggle if no_old else None
            uptime = await _tt_uptime(sites, board.name)
            back = uptime is not None and uptime < (since if since is not None else clock() - t_toggle)
            answered_same = uptime is not None and not back
        else:
            # ssh.run waits RUN_SLACK_S past the remote timeout: keep the whole probe inside the cap.
            probe_s = max(1.0, min(BOOT_ID_TIMEOUT_S, remaining - RUN_SLACK_S))
            if no_old or by_registry:
                # The uptime is read during the probe, so the time since the toggle at its start is the
                # strictest bound: a boot younger than that began after the toggle.
                since = clock() - t_toggle
                new_id, up = await _ssh_boot_and_uptime(ssh, board, probe_s, allow)
                fresh = new_id is not None and up is not None and up < since
                if by_registry:  # a hung Pi's registry can keep its last boot as current: either proves it
                    reg = await _registry_boot_id(board, sites)
                    back = fresh or (new_id is not None and reg is not None and reg != old_id)
                else:
                    back = fresh
                # Only an uptime older than the toggle proves the host kept its power (not-PoE).
                answered_same = not back and new_id is not None and up is not None and up >= since
            else:
                new_id = await _ssh_boot_id(ssh, board, probe_s, allow)
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
    if not answered_same:
        return _refuse(NO_RETURN, steps)
    # With no old boot, a board that answers with an old one may simply have been up all along: the
    # not-PoE diagnosis needs the old boot it kept.
    return _refuse(NO_FRESH_BOOT if no_old else NOT_POE, steps)
