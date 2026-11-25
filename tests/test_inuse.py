"""In-use check: script parity, verdict parsing, the Tiny Tapeout rule, holder discount, failures."""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fpgas_online_mcp import inuse
from fpgas_online_mcp.boards import Board, BoardId
from fpgas_online_mcp.claims import Claim
from fpgas_online_mcp.ssh import RunResult

ROOT = Path(__file__).parent.parent
NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


def board(site="welland", name="pi-sw1-p2", type_=None):
    return Board(BoardId(site, name), site, name, type_, "ok", True, None, None, None, "2026-11-26T12:00:00Z", False)


def claim(started_ago_s=600, tt_state=None):
    return Claim("c-1", "welland:pi-sw1-p2", "me@host", "t", NOW - timedelta(seconds=started_ago_s),
                 NOW + timedelta(minutes=30), tt_state_at_claim=tt_state)  # fmt: skip


class FakeSsh:
    def __init__(self, text="", status=0, timed_out=False, exc=None):
        self.result = RunResult(None if timed_out else status, text, False, timed_out)
        self.exc, self.calls = exc, []

    async def run(self, b, command, timeout_s):
        self.calls.append((b, command, timeout_s))
        if self.exc:
            raise self.exc
        return self.result


class FakeSites:
    def __init__(self, *statuses, exc=None):
        self.statuses, self.exc, self.reads = list(statuses), exc, 0

    async def tt_status(self, slug, live=False):
        assert live
        if self.exc:
            raise self.exc
        self.reads += 1
        return self.statuses[min(self.reads, len(self.statuses)) - 1], None


class FakeTime:
    def __init__(self):
        self.t, self.sleeps = 1000.0, []

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def tt(state, clients=0, reachable=True):
    return {"reachable": reachable, "clients": clients, "idle_display": {"state": state} if state else None}


async def run_tt(*statuses, c=None, b=None):
    ft = FakeTime()
    sites = FakeSites(*statuses)
    v = await inuse.check(b or board("tinytapeout", "fpga-2"), None, sites, c, clock=ft, sleep=ft.sleep)
    return v, ft, sites


def script_out(verdict, *extra, uploads=None):
    lines = ["you: 1.2.3.4 port 5", "tmux: no clients (no server running)"]
    lines += list(extra)
    return "\n".join([*lines, f"verdict: {verdict}"]) + "\n"


# --- script ---------------------------------------------------------------------------------


def test_script_matches_in_use_md():
    md = (ROOT / "skills/fpgas-online/references/in-use.md").read_text()
    block = re.search(r"```sh\n(.*?)```", md.split("## welland and ps1", 1)[1], re.S)[1]
    shipped = (ROOT / "src/fpgas_online_mcp/data/in_use.sh").read_text()
    assert shipped.rstrip("\n") == block.rstrip("\n")


def test_remote_script_is_the_heredoc_body():
    s = inuse.remote_script()
    assert s.startswith("now=$(date +%s)") and s.rstrip().endswith('echo "verdict: $verdict"')
    assert "<<'EOF'" not in s and "ssh " not in s.splitlines()[0]


async def test_ssh_command_and_idle():
    ssh = FakeSsh(script_out("idle"))
    v = await inuse.check(board(), ssh, None, None)
    assert v.verdict == "idle" and v.signals == []
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", v.as_of)
    _, command, timeout = ssh.calls[0]
    assert command.startswith("sh -c ") and "verdict: $verdict" in command and timeout == 30.0


async def test_in_use_lines():
    out = script_out("in use", "active: tmux client, 12 s ago", "active: 1 command(s) running for another login")
    v = await inuse.check(board(), FakeSsh(out), None, None)
    assert v.verdict == "in use"
    assert v.signals == ["tmux client, 12 s ago", "1 command(s) running for another login"]


@pytest.mark.parametrize(
    "extra",
    [
        ["missing tool on host: tmux"],
        ["tmux: unexpected: boom"],
        ["ssh terminals (who -u):", "  ? pts/0"],
        ["  could not find your own login in the process list"],
    ],
)
async def test_unknown_forms(extra):
    out = script_out("unknown (treat as in use)", *extra)
    v = await inuse.check(board(), FakeSsh(out), None, None)
    assert v.verdict == "unknown" and v.signals


async def test_unknown_bare():
    v = await inuse.check(board(), FakeSsh(script_out("unknown (treat as in use)")), None, None)
    assert v.verdict == "unknown" and v.signals


@pytest.mark.parametrize(
    "ssh",
    [
        FakeSsh("garbage\n"),
        FakeSsh(""),
        FakeSsh(script_out("idle"), status=1),
        FakeSsh(script_out("idle"), timed_out=True),
        FakeSsh(exc=RuntimeError("no master\nTraceback (most recent call last):")),
        FakeSsh(script_out("idle") + "trailing junk\n"),
    ],
)
async def test_failures_are_unknown(ssh):
    v = await inuse.check(board(), ssh, None, None)
    assert v.verdict == "unknown" and v.signals
    assert all("\n" not in s and len(s) <= 200 for s in v.signals)


async def test_signals_capped():
    many = [f"active: {'x' * 300} {i}" for i in range(30)]
    v = await inuse.check(board(), FakeSsh(script_out("in use", *many)), None, None)
    assert len(v.signals) == 10 and all(len(s) <= 200 for s in v.signals)


# --- holder discount (ssh) ---------------------------------------------------------------------


async def test_holder_uploads_ignored():
    out = script_out("in use", "active: ~/Uploads change, 100 s ago")
    v = await inuse.check(board(), FakeSsh(out), None, claim(600), now=lambda: NOW)
    assert v.verdict == "idle" and "ignored" in v.signals[0]


async def test_uploads_older_than_claim_still_counts():
    out = script_out("in use", "active: ~/Uploads change, 700 s ago")
    v = await inuse.check(board(), FakeSsh(out), None, claim(600), now=lambda: NOW)
    assert v.verdict == "in use"


async def test_no_claim_no_discount():
    out = script_out("in use", "active: ~/Uploads change, 100 s ago")
    v = await inuse.check(board(), FakeSsh(out), None, None, now=lambda: NOW)
    assert v.verdict == "in use" and v.signals == ["~/Uploads change, 100 s ago"]


async def test_another_login_not_ignored_for_holder():
    out = script_out("in use", "active: ~/Uploads change, 100 s ago", "active: 1 command(s) running for another login")
    v = await inuse.check(board(), FakeSsh(out), None, claim(600), now=lambda: NOW)
    assert v.verdict == "in use" and v.signals[0] == "1 command(s) running for another login"


async def test_discount_does_not_hide_unknown():
    out = script_out("in use", "missing tool on host: tmux", "active: ~/Uploads change, 100 s ago")
    v = await inuse.check(board(), FakeSsh(out), None, claim(600), now=lambda: NOW)
    assert v.verdict == "unknown"


# --- Tiny Tapeout -----------------------------------------------------------------------------


async def test_tt_idle_needs_two_loaded_reads_15s_apart():
    v, ft, sites = await run_tt(tt("loaded"))
    assert v.verdict == "idle" and sites.reads == 2 and sum(ft.sleeps) >= 15


async def test_tt_clients_in_use():
    v, _, sites = await run_tt(tt("loaded", clients=1))
    assert v.verdict == "in use" and "1 serial client" in v.signals[0] and sites.reads == 1


async def test_tt_waiting_rereads_twice_then_in_use():
    v, ft, sites = await run_tt(tt("waiting"))
    assert v.verdict == "in use" and sites.reads == 3 and ft.sleeps == [75.0, 75.0]


async def test_tt_waiting_then_loaded_is_idle():
    v, _, sites = await run_tt(tt("waiting"), tt("loaded"), tt("loaded"))
    assert v.verdict == "idle" and sites.reads == 3


async def test_tt_loaded_then_waiting_is_not_idle_yet():
    v, _, _ = await run_tt(tt("loaded"), tt("waiting"))
    assert v.verdict == "in use"


@pytest.mark.parametrize("state", ["in use", "left: my_design", "left: the SDK is not running"])
async def test_tt_in_use_states(state):
    v, _, _ = await run_tt(tt(state))
    assert v.verdict == "in use"


@pytest.mark.parametrize("st", [tt("not an fpga board"), tt("board not present"), tt("failed: x"), tt(None)])
async def test_tt_unknown_states(st):
    v, _, _ = await run_tt(st)
    assert v.verdict == "unknown"


async def test_tt_unreachable_unknown():
    v, _, _ = await run_tt({"reachable": False, "error": "not connected"})
    assert v.verdict == "unknown"


async def test_tt_chip_board_unknown_without_reading():
    v, _, sites = await run_tt(tt("loaded"), b=board("tinytapeout", "tt06", "chip board"))
    assert v.verdict == "unknown" and sites.reads == 0


async def test_tt_site_error_unknown():
    ft = FakeTime()
    v = await inuse.check(
        board("tinytapeout", "fpga-2"), None, FakeSites(exc=OSError("down")), None, clock=ft, sleep=ft.sleep
    )
    assert v.verdict == "unknown" and "OSError" in v.signals[0]


async def test_tt_holder_state_change_ignored():
    v, _, _ = await run_tt(tt("waiting"), c=claim(tt_state="loaded"))
    assert v.verdict == "idle"
    v, _, _ = await run_tt(tt("left: mine"), c=claim(tt_state="loaded"))
    assert v.verdict == "idle"


async def test_tt_holder_discount_needs_loaded_at_claim_and_does_not_cover_clients():
    v, _, _ = await run_tt(tt("left: old"), c=claim(tt_state="left: old"))
    assert v.verdict == "in use"
    v, _, _ = await run_tt(tt("waiting"), c=claim(tt_state=None))
    assert v.verdict == "in use"
    v, _, _ = await run_tt(tt("waiting", clients=2), c=claim(tt_state="loaded"))
    assert v.verdict == "in use"
