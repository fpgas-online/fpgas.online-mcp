"""Power cycle: success per site, unchanged boot id, empty ids mid-reboot, 429, refusals. No live calls."""

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
    def __init__(self, clock, toggle=None, boot=OLD, registry_after=None, tt=None, page=None):
        self.clock, self.calls, self.boot = clock, [], boot
        self._toggle = toggle or ToggleResult(200, {"2": ["off", "on"]}, None)
        self.tt = tt  # callable(clock.t) -> status dict
        self.page = page or BoardPage(tt_port="9", tt_switch="2")
        self.toggled_at = None

    async def welland_registry(self, live=False):
        assert live
        net = {"boot_id": self.boot, "current_boot": True}
        return [{"hostname": "pi-sw1-p2", "network": net}], ENTRY

    async def tt_status(self, slug, live=False):
        assert live
        return self.tt(self.clock.t), ENTRY

    async def board_page(self, site, name):
        return self.page

    async def toggle(self, site, port, switch):
        self.calls.append((site, port, switch))
        self.toggled_at = self.clock.t
        return self._toggle


class Ssh:
    """`answers(now, toggled_at)` gives the stdout of a boot-id read, or None to fail."""

    def __init__(self, clock, sites, answers):
        self.clock, self.sites, self.answers, self.exits, self.runs = clock, sites, answers, 0, 0

    async def exit(self, board):
        self.exits += 1

    async def run(self, board, command, timeout_s):
        assert command == "cat /proc/sys/kernel/random/boot_id"
        self.runs += 1
        out = self.answers(self.clock.t, self.sites.toggled_at)
        if out is None:
            raise OSError("connection refused")
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


async def run(b, sites, ssh, clock, check=idle):
    return await power.power_cycle(b, CLAIM, sites, ssh, clock=clock, sleep=clock.sleep, inuse_check=check)


async def test_welland_success_waits_for_new_boot_id_not_empty():
    clock = Clock()
    sites = Sites(clock)
    ssh = Ssh(clock, sites, reboot_after(71))
    r = await run(board(), sites, ssh, clock)
    assert r.ok and r.seconds_to_back == 80  # first poll at 10 s steps after 71 s
    assert sites.calls == [("welland", "2", 1)]
    assert "boot id read" in r.steps and any(s.startswith("back at +") for s in r.steps)
    assert ssh.exits >= 2  # before the toggle, and fresh masters while polling


async def test_welland_registry_missing_falls_back_to_ssh():
    clock = Clock()
    sites = Sites(clock, boot=None)
    ssh = Ssh(clock, sites, reboot_after(30))
    r = await run(board(), sites, ssh, clock)
    assert r.ok


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


async def test_toggle_network_error_is_refusal():
    clock = Clock()
    sites = Sites(clock)

    async def boom(*a):
        raise SiteError("welland: down")

    sites.toggle = boom
    r = await run(board(), sites, Ssh(clock, sites, lambda n, t: OLD), clock)
    assert not r.ok and "welland: down" in r.message


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

    async def check(b, ssh, s, claim, clock=None, sleep=None):
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
