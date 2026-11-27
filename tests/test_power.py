"""Power cycle: success per site, unchanged boot id, empty ids mid-reboot, 429, refusals. No live calls."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from fpgas_online_mcp import power
from fpgas_online_mcp.boards import Board, BoardId
from fpgas_online_mcp.cache import Entry
from fpgas_online_mcp.claims import Claim
from fpgas_online_mcp.inuse import Verdict
from fpgas_online_mcp.sites import BoardPage, SiteError, ToggleResult
from fpgas_online_mcp.ssh import RunResult

OLD = "11111111-1111-1111-1111-111111111111"
NEW = "22222222-2222-2222-2222-222222222222"
ENTRY = Entry(b"", 0.0, "2026-11-26T12:00:00Z")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.t += s


def board(site="welland", name="pi-sw1-p2"):
    return Board(BoardId(site, name), site, name, None, "ok", True, None, None, None, "2026-11-26T12:00:00Z", False)


CLAIM = Claim(
    "c-1", "welland:pi-sw1-p2", "me@host", "t", datetime(2026, 11, 26, tzinfo=UTC), datetime(2026, 11, 27, tzinfo=UTC)
)


def idle(*a, **k):
    async def f(*a, **k):
        return Verdict("idle", [], "x")

    return f(*a, **k)


class Sites:
    def __init__(self, clock, toggle=None, boot=OLD, tt=None, page=None, order=None):
        self.clock, self.calls, self.boot = clock, [], boot  # boot: str, or callable(now, toggled_at)
        self.order = order if order is not None else []
        self.page_live = []
        self._toggle = toggle or ToggleResult(200, {"2": ["off", "on"]}, None)
        self.tt = tt  # callable(clock.t) -> status dict
        self.page = page or BoardPage(tt_port="9", tt_switch="2")
        self.toggled_at = None

    async def welland_registry(self, live=False):
        assert live
        boot = self.boot(self.clock.t, self.toggled_at) if callable(self.boot) else self.boot
        net = {"boot_id": boot, "current_boot": True}
        return [{"hostname": "pi-sw1-p2", "network": net}], ENTRY

    async def tt_status(self, slug, live=False):
        assert live
        return self.tt(self.clock.t), ENTRY

    async def board_page(self, site, name, live=False):
        self.page_live.append(live)
        return self.page

    async def toggle(self, site, port, switch):
        self.calls.append((site, port, switch))
        self.order.append("toggle")
        self.toggled_at = self.clock.t
        if isinstance(self._toggle, Exception):
            raise self._toggle
        return self._toggle


class Ssh:
    """`answers(now, toggled_at)` gives the stdout of a boot-id read, or None to fail. `uptime(now,
    toggled_at)` gives the host's /proc/uptime seconds for the combined probe (None: no uptime line)."""

    def __init__(self, clock, sites, answers, uptime=None):
        self.clock, self.sites, self.answers, self.exits, self.runs = clock, sites, answers, 0, 0
        self.uptime = uptime
        self.timeouts = []
        self.exited = []
        self.allow = []  # allow_unoffered of each run
        self.commands = []

    async def exit(self, board):
        self.exits += 1
        self.exited.append(board)
        self.sites.order.append("exit")

    async def run(self, board, command, timeout_s, allow_unoffered=False):
        assert command in (power.BOOT_ID_COMMAND, power.BOOT_UPTIME_COMMAND)
        self.commands.append(command)
        self.allow.append(allow_unoffered)
        self.runs += 1
        self.timeouts.append((self.clock.t, timeout_s))
        out = self.answers(self.clock.t, self.sites.toggled_at)
        if out is None:
            raise OSError("connection refused")
        if isinstance(out, RunResult):
            return out
        if command == power.BOOT_UPTIME_COMMAND:
            up = self.uptime(self.clock.t, self.sites.toggled_at) if self.uptime else None
            out = out + "\n" + (f"{up:.2f} 1234.56\n" if up is not None else "")
        return RunResult(0, out, False, False)


def reboot_after(seconds, mid=("", "garbage")):
    """OLD until the toggle, then the failures/empties in `mid`, back as NEW `seconds` after."""

    def f(now, toggled):
        if toggled is None or now - toggled < 5:
            return OLD
        if now - toggled < seconds:
            return mid[int(now) % len(mid)] if mid else None
        return NEW

    return f


# A Tiny Tapeout claim holds the Pi the board was on when claimed: the default page names switch 2 port 9.
TT_CLAIM = replace(CLAIM, board="tinytapeout:fpga-2", host="welland:pi-sw2-p9")


async def run(b, sites, ssh, clock, check=idle, claim=None):
    claim = claim or (TT_CLAIM if b.site == "tinytapeout" else CLAIM)
    return await power.power_cycle(b, claim, sites, ssh, clock=clock, sleep=clock.sleep, inuse_check=check)


async def test_welland_success_waits_for_new_boot_id_not_empty():
    clock = Clock()
    sites = Sites(clock)
    ssh = Ssh(clock, sites, reboot_after(71))
    r = await run(board(), sites, ssh, clock)
    assert r.ok and r.seconds_to_back == 80  # first poll at 10 s steps after 71 s
    assert sites.calls == [("welland", "2", 1)]
    assert "boot id read" in r.steps and any(s.startswith("back at +") for s in r.steps)
    assert ssh.exits == 1  # the master is closed once, before the toggle
    assert sites.order == ["exit", "toggle"]
    assert all(t + to <= sites.toggled_at + 300 for t, to in ssh.timeouts[1:])


async def test_welland_registry_id_differing_from_ssh_id_is_not_a_reboot():
    clock = Clock()
    sites = Sites(clock, boot=NEW)  # the registry stores another id than ssh reports
    ssh = Ssh(clock, sites, lambda n, t: OLD)
    r = await run(board(), sites, ssh, clock)
    assert not r.ok and r.message == power.NOT_POE


async def test_welland_ssh_unreadable_falls_back_to_registry_and_needs_ssh_too():
    clock = Clock()
    # registry shows the new boot after 40 s; ssh answers (new id) only from 60 s
    sites = Sites(clock, boot=lambda n, t: OLD if t is None or n - t < 40 else NEW)
    ssh = Ssh(clock, sites, lambda n, t: None if t is None or n - t < 60 else NEW)
    r = await run(board(), sites, ssh, clock)
    assert r.ok and r.seconds_to_back == 60
    assert "boot id from the registry (ssh failed)" in r.steps


async def test_welland_registry_fallback_without_ssh_never_comes_back():
    clock = Clock()
    sites = Sites(clock, boot=lambda n, t: OLD if t is None else NEW)
    ssh = Ssh(clock, sites, lambda n, t: None)
    r = await run(board(), sites, ssh, clock)
    assert not r.ok and "within 5 minutes" in r.message


async def test_ps1_success_port_is_string_and_no_switch():
    clock = Clock()
    sites = Sites(clock)
    ssh = Ssh(clock, sites, reboot_after(40, mid=(None,)))
    r = await run(board("ps1", "pi21"), sites, ssh, clock)
    assert r.ok
    assert sites.calls == [("ps1", "21", None)]


async def test_same_boot_id_throughout_gives_not_poe_message():
    clock = Clock()
    sites = Sites(clock)
    ssh = Ssh(clock, sites, lambda now, t: OLD)
    r = await run(board("ps1", "pi21"), sites, ssh, clock)
    assert not r.ok and r.message == power.NOT_POE and r.seconds_to_back is None
    assert clock.t - 1000 <= 310


async def test_never_answers_gives_no_return_message():
    clock = Clock()
    sites = Sites(clock)
    ssh = Ssh(clock, sites, lambda now, t: OLD if t is None else None)
    r = await run(board(), sites, ssh, clock)
    assert not r.ok and "within 5 minutes" in r.message and r.message != power.NOT_POE


async def test_empty_or_short_ids_never_count_as_back():
    clock = Clock()
    sites = Sites(clock)
    ssh = Ssh(clock, sites, lambda now, t: OLD if t is None else "")
    r = await run(board(), sites, ssh, clock)
    assert not r.ok and "within 5 minutes" in r.message


async def test_429_passes_retry_after_without_retrying():
    clock = Clock()
    sites = Sites(clock, toggle=ToggleResult(429, {"error": "wait"}, 59))
    ssh = Ssh(clock, sites, lambda now, t: OLD)
    r = await run(board(), sites, ssh, clock)
    assert not r.ok and r.retry_after == 59 and "59 seconds" in r.message
    assert len(sites.calls) == 1


async def test_other_error_status_carries_site_message():
    clock = Clock()
    sites = Sites(clock, toggle=ToggleResult(502, {"error": "the switch did not confirm"}, None))
    r = await run(board(), sites, Ssh(clock, sites, lambda n, t: OLD), clock)
    assert not r.ok and "the switch did not confirm" in r.message and r.retry_after is None


async def test_lost_toggle_response_but_board_restarts_is_ok_with_note():
    clock = Clock()
    sites = Sites(clock, toggle=SiteError("welland: cannot reach the site (ReadTimeout)"))
    r = await run(board(), sites, Ssh(clock, sites, reboot_after(30)), clock)
    assert r.ok and "toggle response was lost" in r.message and len(sites.calls) == 1


async def test_lost_toggle_response_and_no_restart_says_wait_and_check():
    clock = Clock()
    sites = Sites(clock, toggle=SiteError("welland: cannot reach the site (ReadTimeout)"))
    r = await run(board(), sites, Ssh(clock, sites, lambda n, t: OLD), clock)
    assert not r.ok and "may have reached the switch" in r.message and "at least 60 s" in r.message
    assert len(sites.calls) == 1


async def test_429_without_retry_after():
    clock = Clock()
    sites = Sites(clock, toggle=ToggleResult(429, {}, None))
    r = await run(board(), sites, Ssh(clock, sites, lambda n, t: OLD), clock)
    assert not r.ok and r.retry_after is None and "about a minute" in r.message


async def test_unparseable_names_refuse():
    clock = Clock()
    for b in (board("welland", "pi-sw1-p2x"), board("ps1", "pi21 "), board("ps1", "raspberry")):
        sites = Sites(clock)
        r = await run(b, sites, Ssh(clock, sites, lambda n, t: OLD), clock)
        assert not r.ok and "switch port" in r.message and sites.calls == []


async def test_port_is_normalised():
    clock = Clock()
    sites = Sites(clock)
    await run(board("welland", "pi-sw01-p02"), sites, Ssh(clock, sites, reboot_after(20)), clock)
    assert sites.calls == [("welland", "2", 1)]


@pytest.mark.parametrize("bad", [RunResult(1, NEW, False, False), RunResult(None, NEW, False, True)])
async def test_ps1_failed_or_timed_out_probe_is_not_back(bad):
    clock = Clock()
    sites = Sites(clock)
    ssh = Ssh(clock, sites, lambda n, t: OLD if t is None else bad)
    r = await run(board("ps1", "pi21"), sites, ssh, clock)
    assert not r.ok and "within 5 minutes" in r.message


@pytest.mark.parametrize("verdict", ["in use", "unknown"])
async def test_refuses_unless_idle(verdict):
    clock = Clock()
    sites = Sites(clock)

    async def check(*a, **k):
        return Verdict(verdict, ["someone is here"], "x")

    r = await run(board(), sites, Ssh(clock, sites, lambda n, t: OLD), clock, check)
    assert not r.ok and verdict in r.message and sites.calls == []


async def test_in_use_check_gets_claim_and_clock():
    clock = Clock()
    sites = Sites(clock)
    seen = {}

    async def check(b, ssh, s, claim, clock=None, sleep=None, allow_unoffered=False):
        seen.update(claim=claim, clock=clock, sleep=sleep)
        return Verdict("idle", [], "x")

    await run(board(), sites, Ssh(clock, sites, reboot_after(20)), clock, check)
    assert seen["claim"] is CLAIM and seen["clock"] is clock


async def test_refuses_when_boot_id_unreadable():
    clock = Clock()
    sites = Sites(clock, boot="")
    ssh = Ssh(clock, sites, lambda n, t: None)
    r = await run(board(), sites, ssh, clock)
    assert not r.ok and "boot id" in r.message and sites.calls == []
    sites = Sites(clock)
    r = await run(board("ps1", "pi21"), sites, Ssh(clock, sites, lambda n, t: ""), clock)
    assert not r.ok and sites.calls == []


def tt_status(restart_at=None, present=True):
    def f(now):
        if restart_at is None:
            up = 5000 + (now - 1000)
        else:
            up = 5000 + (now - 1000) if now < 1000 + restart_at else now - (1000 + restart_at)
        return {"reachable": True, "board": {"present": present}, "uptime_s": up}

    return f


async def test_tt_success_uses_uptime_below_elapsed():
    clock = Clock()
    # daemon restarts 12 s after the toggle; uptime then is small
    sites = Sites(
        clock,
        tt=lambda now: (
            tt_status(None)(now) if sites.toggled_at is None else tt_status(sites.toggled_at - 1000 + 12)(now)
        ),
    )
    ssh = Ssh(clock, sites, lambda n, t: None)
    r = await run(board("tinytapeout", "fpga-2"), sites, ssh, clock)
    assert r.ok and sites.calls == [("tinytapeout", "9", 2)] and ssh.runs == 0
    assert sites.page_live == [True]  # the port is read live, never from the cache
    # the Pi's ssh master (the board page's switch 2 port 9) is closed before the toggle
    assert ssh.exited == [BoardId("welland", "pi-sw2-p9")] and sites.order == ["exit", "toggle"]
    assert r.seconds_to_back == 20


async def test_tt_daemon_never_restarts_is_not_poe():
    clock = Clock()
    sites = Sites(clock, tt=tt_status(None))
    r = await run(board("tinytapeout", "fpga-2"), sites, Ssh(clock, sites, lambda n, t: None), clock)
    assert not r.ok and r.message == power.NOT_POE


async def test_tt_unreadable_uptime_refuses_and_empty_port_refuses():
    clock = Clock()
    sites = Sites(clock, tt=lambda now: {"reachable": True, "board": {"present": True}, "uptime_s": None})
    r = await run(board("tinytapeout", "fpga-2"), sites, Ssh(clock, sites, lambda n, t: None), clock)
    assert not r.ok and sites.calls == []
    sites = Sites(clock, tt=tt_status(None), page=BoardPage(tt_port="", tt_switch=""))
    r = await run(board("tinytapeout", "fpga-2"), sites, Ssh(clock, sites, lambda n, t: None), clock)
    assert not r.ok and "nothing to cycle" in r.message and sites.calls == []
    for port, switch in (("9", "x"), ("09", "2")):  # the same parser as the Pi lookup: "09" is not a port
        sites = Sites(clock, tt=tt_status(None), page=BoardPage(tt_port=port, tt_switch=switch))
        ssh = Ssh(clock, sites, lambda n, t: None)
        r = await run(board("tinytapeout", "fpga-2"), sites, ssh, clock)
        assert not r.ok and "no usable switch" in r.message and sites.calls == [] and ssh.exits == 0


async def test_should_stop_before_toggle_toggles_nothing():
    clock = Clock()
    sites = Sites(clock)
    ssh = Ssh(clock, sites, reboot_after(20))
    r = await power.power_cycle(
        board(), CLAIM, sites, ssh, clock=clock, sleep=clock.sleep, inuse_check=idle, should_stop=lambda: True
    )
    assert not r.ok and r.message == power.CANCELLED and sites.calls == [] and ssh.exits == 0


async def test_claim_released_during_in_use_check_stops_before_toggle():
    clock = Clock()
    sites = Sites(clock)
    ssh = Ssh(clock, sites, reboot_after(20))
    live = [True]

    async def check_then_release(*a, **k):
        live[0] = False  # someone released the claim while the check ran
        return Verdict("idle", [], "x")

    r = await power.power_cycle(
        board(),
        CLAIM,
        sites,
        ssh,
        clock=clock,
        sleep=clock.sleep,
        inuse_check=check_then_release,
        should_stop=lambda: not live[0],
    )
    assert not r.ok and "nothing was toggled" in r.message and sites.calls == []


@pytest.mark.parametrize("held", ["welland:pi-sw2-p8", None])
async def test_tt_board_on_another_pi_than_its_claim_is_not_cycled(held):
    clock = Clock()
    sites = Sites(clock, tt=tt_status(None))
    ssh = Ssh(clock, sites, lambda n, t: None)
    r = await run(board("tinytapeout", "fpga-2"), sites, ssh, clock, claim=replace(TT_CLAIM, host=held))
    assert not r.ok and "is now on welland:pi-sw2-p9" in r.message and f"holds {held or 'no Pi'}" in r.message
    assert "claim the board again" in r.message
    assert sites.calls == [] and ssh.exits == 0


@pytest.mark.parametrize("allow", [True, False])
async def test_the_claims_allow_unoffered_reaches_the_check_and_every_boot_id_read(allow):
    clock = Clock()
    sites = Sites(clock)
    ssh = Ssh(clock, sites, reboot_after(20))
    seen = {}

    async def check(b, ssh, s, claim, clock=None, sleep=None, allow_unoffered=False):
        seen["allow"] = allow_unoffered
        return Verdict("idle", [], "x")

    r = await run(board(), sites, ssh, clock, check, claim=replace(CLAIM, allow_unoffered=allow))
    assert r.ok and seen["allow"] is allow
    assert ssh.allow and set(ssh.allow) == {allow}


# --- a hung board: an unknown verdict under the claim's override_reason ----------------------------

REASON = "the user says the Pi hung after a deploy"
OVERRIDE_STEP = f"in-use check: unknown; going ahead on the claim's override ({REASON})"
NO_OLD_STEP = "board not answering before the toggle: back means it answers with a boot younger than the toggle"


def verdict_of(v, signals=("ssh: No route to host",), not_offered=False):
    async def check(*a, **k):
        return Verdict(v, list(signals), "x", not_offered)

    return check


unknown = verdict_of("unknown")


def overridden(claim=CLAIM, reason=REASON):
    return replace(claim, override_reason=reason)


def booted_at(delay):
    """/proc/uptime of a host that booted `delay` s after the toggle (a large one before it)."""
    return lambda now, t: 9000.0 + now if t is None or now - t < delay else now - (t + delay)


def dead_until(seconds, boot=NEW):
    """ssh fails until `seconds` after the toggle (and always before it), then answers `boot`."""
    return lambda now, t: None if t is None or now - t < seconds else boot


@pytest.mark.parametrize("claim", [CLAIM, overridden()])
async def test_in_use_refuses_even_with_an_override(claim):
    clock = Clock()
    sites = Sites(clock)
    ssh = Ssh(clock, sites, lambda n, t: OLD)
    r = await run(board(), sites, ssh, clock, verdict_of("in use", ["1 ssh session"]), claim=claim)
    assert not r.ok and "in use" in r.message and "1 ssh session" in r.message
    assert sites.calls == [] and ssh.exits == 0 and r.steps == ["in-use check: in use"]


@pytest.mark.parametrize("reason", [None, "", "   "])
async def test_unknown_without_an_override_refuses_and_says_how(reason):
    clock = Clock()
    sites = Sites(clock)
    ssh = Ssh(clock, sites, lambda n, t: OLD)
    r = await run(board(), sites, ssh, clock, unknown, claim=replace(CLAIM, override_reason=reason))
    assert not r.ok and "in-use check says unknown" in r.message and "No route to host" in r.message
    assert "override_reason" in r.message and "only if the user agrees" in r.message
    assert sites.calls == [] and ssh.exits == 0 and r.steps == ["in-use check: unknown"]


async def test_unknown_because_not_offered_refuses_even_with_an_override():
    clock = Clock()
    sites = Sites(clock)
    ssh = Ssh(clock, sites, lambda n, t: OLD)
    check = verdict_of("unknown", ["welland does not offer this Pi"], not_offered=True)
    r = await run(board(), sites, ssh, clock, check, claim=overridden())
    assert not r.ok and "unknown" in r.message and sites.calls == [] and ssh.exits == 0


async def test_unknown_with_an_override_and_a_readable_boot_id_proves_as_before():
    clock = Clock()
    sites = Sites(clock)
    ssh = Ssh(clock, sites, reboot_after(71))
    r = await run(board(), sites, ssh, clock, unknown, claim=overridden())
    assert r.ok and r.seconds_to_back == 80
    assert r.steps[:2] == [OVERRIDE_STEP, "boot id read"] and NO_OLD_STEP not in r.steps
    assert set(ssh.commands) == {power.BOOT_ID_COMMAND}  # the old-value proof is unchanged
    assert sites.calls == [("welland", "2", 1)]


async def test_unknown_with_an_override_and_the_same_boot_id_is_still_not_poe():
    clock = Clock()
    sites = Sites(clock)
    r = await run(board("ps1", "pi21"), sites, Ssh(clock, sites, lambda n, t: OLD), clock, unknown, overridden())
    assert not r.ok and r.message == power.NOT_POE


async def test_idle_with_an_override_still_needs_the_old_boot():
    """The missing-old-value path is only for a board that read unknown: an idle one answered the check."""
    clock = Clock()
    sites = Sites(clock, boot="")
    ssh = Ssh(clock, sites, lambda n, t: None)
    r = await run(board(), sites, ssh, clock, claim=overridden())
    assert not r.ok and "boot id" in r.message and sites.calls == [] and ssh.exits == 0


async def test_unknown_unreadable_old_boot_without_override_refuses_unchanged():
    clock = Clock()
    sites = Sites(clock, boot="")

    async def check(*a, **k):  # an idle claim whose board then stops answering the boot id read
        return Verdict("idle", [], "x")

    ssh = Ssh(clock, sites, lambda n, t: None)
    r = await run(board(), sites, ssh, clock, check)
    assert not r.ok and "a reboot could not be proven" in r.message and sites.calls == [] and ssh.exits == 0


async def test_welland_dead_board_under_override_is_back_on_a_boot_younger_than_the_toggle():
    clock = Clock()
    sites = Sites(clock, boot="")  # ssh down and no registry id: nothing to compare against
    ssh = Ssh(clock, sites, dead_until(60), uptime=booted_at(30))
    r = await run(board(), sites, ssh, clock, unknown, claim=overridden())
    assert r.ok and r.seconds_to_back == 60
    assert r.steps[:2] == [OVERRIDE_STEP, NO_OLD_STEP] and "boot id read" not in r.steps
    assert sites.calls == [("welland", "2", 1)] and sites.order == ["exit", "toggle"]
    # boot id and uptime come from the same probe after the toggle
    assert ssh.commands[0] == power.BOOT_ID_COMMAND and set(ssh.commands[1:]) == {power.BOOT_UPTIME_COMMAND}


async def test_ps1_dead_board_under_override_is_back_on_a_boot_younger_than_the_toggle():
    clock = Clock()
    sites = Sites(clock)
    ssh = Ssh(clock, sites, dead_until(40), uptime=booted_at(20))
    r = await run(board("ps1", "pi21"), sites, ssh, clock, unknown, claim=overridden())
    assert r.ok and r.seconds_to_back == 40 and NO_OLD_STEP in r.steps
    assert sites.calls == [("ps1", "21", None)]


async def test_no_old_boot_answering_alone_is_never_back():
    """It answers from the start of the polling, but its uptime is older than the toggle: no reboot."""
    clock = Clock()
    sites = Sites(clock, boot="")
    ssh = Ssh(clock, sites, dead_until(10), uptime=lambda n, t: 9000.0 + n)
    r = await run(board(), sites, ssh, clock, unknown, claim=overridden())
    assert not r.ok and r.message != power.NOT_POE and r.message == power.NO_FRESH_BOOT
    assert "do not cycle it again" in r.message and len(sites.calls) == 1


async def test_no_old_boot_without_an_uptime_line_is_not_back():
    clock = Clock()
    sites = Sites(clock, boot="")
    ssh = Ssh(clock, sites, dead_until(30), uptime=lambda n, t: None)
    r = await run(board(), sites, ssh, clock, unknown, claim=overridden())
    assert not r.ok and r.message != power.NOT_POE and len(sites.calls) == 1


async def test_no_old_boot_uptime_equal_to_the_time_since_the_toggle_is_not_back():
    clock = Clock()
    sites = Sites(clock, boot="")
    # uptime always equals the time since the toggle at the probe: never strictly younger
    ssh = Ssh(clock, sites, dead_until(20), uptime=lambda n, t: n - t)
    r = await run(board(), sites, ssh, clock, unknown, claim=overridden())
    assert not r.ok


@pytest.mark.parametrize("bad", ["abc 1.0", "-5.00 1.0", "nan 1.0", "inf 1.0", ""])
async def test_no_old_boot_unreadable_uptime_is_not_back(bad):
    clock = Clock()
    sites = Sites(clock, boot="")
    ssh = Ssh(clock, sites, lambda n, t: None if t is None else RunResult(0, f"{NEW}\n{bad}\n", False, False))
    r = await run(board(), sites, ssh, clock, unknown, claim=overridden())
    assert not r.ok


async def test_no_old_boot_that_never_answers_did_not_come_back_not_not_poe():
    clock = Clock()
    sites = Sites(clock, boot="")
    ssh = Ssh(clock, sites, lambda n, t: None)
    r = await run(board(), sites, ssh, clock, unknown, claim=overridden())
    assert not r.ok and r.message == power.NO_RETURN and len(sites.calls) == 1
    assert r.steps[-1] == "gave up at +300 s"


async def test_no_old_boot_lost_toggle_response_then_fresh_boot_is_ok():
    clock = Clock()
    sites = Sites(clock, boot="", toggle=SiteError("welland: cannot reach the site (ReadTimeout)"))
    ssh = Ssh(clock, sites, dead_until(50), uptime=booted_at(25))
    r = await run(board(), sites, ssh, clock, unknown, claim=overridden())
    assert r.ok and "toggle response was lost" in r.message and len(sites.calls) == 1


def tt_down_then(restart_at):
    """Not reachable before the toggle; after it, the daemon restarts `restart_at` s later."""

    def f(sites):
        def g(now):
            if sites.toggled_at is None:
                return {"reachable": False, "board": {"present": False}, "uptime_s": None}
            return tt_status(sites.toggled_at - 1000 + restart_at)(now)

        return g

    return f


async def test_tt_dead_board_under_override_is_back_when_uptime_is_below_the_time_since_toggle():
    clock = Clock()
    sites = Sites(clock)
    sites.tt = tt_down_then(12)(sites)
    ssh = Ssh(clock, sites, lambda n, t: None)
    r = await run(board("tinytapeout", "fpga-2"), sites, ssh, clock, unknown, claim=overridden(TT_CLAIM))
    assert r.ok and r.seconds_to_back == 20 and ssh.runs == 0
    assert r.steps[:2] == [OVERRIDE_STEP, NO_OLD_STEP]
    assert sites.calls == [("tinytapeout", "9", 2)]


async def test_tt_dead_board_answering_with_an_old_uptime_is_not_back_nor_not_poe():
    clock = Clock()
    sites = Sites(clock)
    sites.tt = lambda now: (
        {"reachable": False, "board": {"present": False}} if sites.toggled_at is None else tt_status(None)(now)
    )
    r = await run(
        board("tinytapeout", "fpga-2"),
        sites,
        Ssh(clock, sites, lambda n, t: None),
        clock,
        unknown,
        overridden(TT_CLAIM),
    )
    assert not r.ok and r.message == power.NO_FRESH_BOOT


async def test_tt_unreadable_uptime_without_override_refuses():
    clock = Clock()
    sites = Sites(clock, tt=lambda now: {"reachable": False})
    ssh = Ssh(clock, sites, lambda n, t: None)
    r = await run(board("tinytapeout", "fpga-2"), sites, ssh, clock, unknown, claim=TT_CLAIM)
    assert not r.ok and "unknown" in r.message and sites.calls == [] and ssh.exits == 0


async def test_override_does_not_skip_the_tt_host_check():
    clock = Clock()
    sites = Sites(clock)
    sites.tt = tt_down_then(12)(sites)
    ssh = Ssh(clock, sites, lambda n, t: None)
    claim = overridden(replace(TT_CLAIM, host="welland:pi-sw2-p8"))
    r = await run(board("tinytapeout", "fpga-2"), sites, ssh, clock, unknown, claim=claim)
    assert not r.ok and "is now on welland:pi-sw2-p9" in r.message and sites.calls == [] and ssh.exits == 0


async def test_override_with_no_old_boot_still_stops_before_the_toggle():
    clock = Clock()
    sites = Sites(clock, boot="")
    ssh = Ssh(clock, sites, lambda n, t: None)
    r = await power.power_cycle(
        board(), overridden(), sites, ssh, clock=clock, sleep=clock.sleep, inuse_check=unknown, should_stop=lambda: True
    )
    assert not r.ok and r.message == power.CANCELLED and sites.calls == [] and ssh.exits == 0
