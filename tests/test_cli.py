"""The CLI through main(argv), against the in-process broker of test_mcp's `stack` fixture."""

import asyncio
import json
import re
import tempfile
from pathlib import Path

import pytest
from test_mcp import REPO_TMP, UNTRUSTED, WELLAND
from test_mcp import stack as stack  # noqa: F401  (fixture)

import fpgas_online_mcp
from fpgas_online_mcp.cli import _presence, main
from fpgas_online_mcp.inuse import Verdict


async def cli(capsys, *argv):
    """Run main() in a thread (it starts its own event loop); (exit code, stdout, stderr)."""
    code = await asyncio.to_thread(main, list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


async def test_boards(stack, capsys):
    code, out, err = await cli(capsys, "boards", "--site", "welland")
    assert code == 0 and err == ""
    assert out.splitlines()[0].split() == ["BOARD", "TYPE", "CONDITION", "OFFERED", "SEEN", "CLAIM"]
    assert WELLAND in out and "welland:" in out and "as of 20" in out


async def test_boards_condition(stack, capsys):
    code, out, err = await cli(capsys, "boards", "--condition", "missing")
    assert code == 0 and err == ""
    board_rows = [re.split(r"  +", line) for line in out.splitlines()[1:] if re.match(r"\w+:\S+  ", line)]
    assert board_rows and {r[2] for r in board_rows} == {"missing"}
    assert WELLAND not in out  # pi-sw1-p2 is operational in the fixture


def test_fmt_boards_old_format_is_an_error():
    from fpgas_online_mcp.cli import fmt_boards
    from fpgas_online_mcp.client import BrokerError

    with pytest.raises(BrokerError, match="older format"):
        fmt_boards({"sites": {}, "boards": [{"id": "welland:pi-sw1-p2"}]})


async def test_boards_json(stack, capsys):
    code, out, _ = await cli(capsys, "boards", "--site", "welland", "--json")
    data = json.loads(out)
    assert code == 0 and set(data["sites"]) == {"welland"} and data["boards"]


async def test_status(stack, capsys):
    stack.inuse.verdict = Verdict("unknown", ["board says: missing tool"], "2026-11-26T12:00:00Z")
    code, out, _ = await cli(capsys, "status", WELLAND)
    assert code == 0
    assert f"{WELLAND}:" in out and "in use: unknown (as of 2026-11-26T12:00:00Z)" in out
    assert f"signals: {UNTRUSTED}board says: missing tool</board-output>" in out
    code, out, _ = await cli(capsys, "status", WELLAND, "--json")
    assert json.loads(out)["in_use"]["verdict"] == "unknown"


async def test_claim_claims_release(stack, capsys):
    code, out, _ = await cli(capsys, "claim", WELLAND, "--minutes", "10", "--purpose", "cli test")
    assert code == 0
    first = out.splitlines()[0]
    assert first.startswith("CLAIM ID: ")
    claim_id = first.split(": ")[1]
    assert "cli test" in out and f"fpgas-online release {claim_id}" in out

    code, out, _ = await cli(capsys, "claims")
    assert code == 0 and claim_id in out and WELLAND in out and "cli:" in out and "cli test" in out
    code, out, _ = await cli(capsys, "claims", "--json")
    assert json.loads(out)["claims"][0]["claim_id"] == claim_id
    code, out, _ = await cli(capsys, "boards", "--site", "welland")
    assert "cli test" in out  # the claim shows in the board list

    code, out, _ = await cli(capsys, "release", claim_id)
    assert code == 0 and out.strip() == f"released {claim_id}"
    code, out, _ = await cli(capsys, "claims")
    assert code == 0 and out.strip() == "no live claims"


async def test_claim_in_use_error_and_override(stack, capsys):
    stack.inuse.verdict = Verdict("in use", ["board says: tmux client"], "2026-11-26T12:00:00Z")
    code, out, err = await cli(capsys, "claim", WELLAND, "--purpose", "x")
    assert code == 1 and out == "" and err.startswith("error: ") and "override_reason" in err
    code, out, _ = await cli(capsys, "claim", WELLAND, "--purpose", "x", "--override-reason", "user said so")
    assert code == 0 and "override: user said so" in out


async def test_errors_go_to_stderr(stack, capsys):
    code, out, err = await cli(capsys, "status", "welland:no-such-host")
    assert code == 1 and out == "" and "list_boards" in err
    code, out, err = await cli(capsys, "release", "nope")
    assert code == 1 and out == "" and "not a live claim" in err


async def test_broker_status_and_stop_idle(stack, capsys):
    code, out, _ = await cli(capsys, "broker", "status")
    assert code == 0 and f"version {fpgas_online_mcp.__version__}" in out and "pid " in out
    started = out.split("started ", 1)[1].splitlines()[0]
    assert re.fullmatch(r"(\d{4}-\d\d-\d\d )?\d\d:\d\d UTC", started), started  # not a raw Unix timestamp
    code, out, _ = await cli(capsys, "broker", "status", "--json")
    assert json.loads(out)["version"] == fpgas_online_mcp.__version__
    code, out, _ = await cli(capsys, "broker", "stop")
    assert code == 0 and out.strip() == "stopped"


async def test_broker_stop_busy(stack, capsys):
    await cli(capsys, "claim", WELLAND, "--purpose", "held")
    code, out, err = await cli(capsys, "broker", "stop")
    assert code == 1 and "busy" in out and WELLAND in out and "held" in out
    code, out, _ = await cli(capsys, "broker", "stop", "--json")
    assert code == 1 and json.loads(out)["stopping"] is False


async def test_broker_stop_refused_while_a_session_is_connected(stack, capsys):
    async with stack.session() as c:
        await c.call_tool("guide", {})  # the session's pooled connection stays open
        code, out, _ = await cli(capsys, "broker", "stop")
    assert code == 1 and "busy" in out and "other client" in out
    await asyncio.sleep(0.1)
    code, out, _ = await cli(capsys, "broker", "stop")  # the CLI is the only client now
    assert code == 0 and out.strip() == "stopped"


@pytest.fixture
def no_broker(monkeypatch):
    REPO_TMP.mkdir(exist_ok=True)
    d = Path(tempfile.mkdtemp(dir=REPO_TMP, prefix="n"))
    for name, sub in (("XDG_RUNTIME_DIR", "r"), ("XDG_STATE_HOME", "s"), ("XDG_CACHE_HOME", "c")):
        (d / sub).mkdir()
        monkeypatch.setenv(name, str(d / sub))
    yield
    import shutil

    shutil.rmtree(d, ignore_errors=True)


async def test_broker_not_running(no_broker, capsys):
    code, out, _ = await cli(capsys, "broker", "status")
    assert code == 1 and out.strip() == "not running"
    code, out, _ = await cli(capsys, "broker", "stop")
    assert code == 0 and out.strip() == "not running"
    code, out, _ = await cli(capsys, "broker", "stop", "--json")
    assert code == 0 and json.loads(out) == {"running": False}


def test_bad_usage_exits_2(capsys):
    with pytest.raises(SystemExit) as e:
        main(["claim", WELLAND])  # --purpose is required
    assert e.value.code == 2


async def test_broker_version_mismatch(stack, capsys):
    stack.broker.version = "0.0.1"
    code, out, _ = await cli(capsys, "broker", "status")
    assert code == 0 and "version 0.0.1" in out and f"this CLI is version {fpgas_online_mcp.__version__}" in out
    code, out, _ = await cli(capsys, "broker", "status")  # status did not stop it
    assert code == 0 and "running" in out
    code, out, _ = await cli(capsys, "broker", "stop")
    assert code == 0 and out.strip() == "stopped"


async def test_broker_status_newer_broker(stack, capsys):
    stack.broker.version = "99.0.0"
    code, out, _ = await cli(capsys, "broker", "status")
    assert code == 0 and "version 99.0.0" in out and "uses the running (newer) broker" in out


async def test_minutes_out_of_range(stack, capsys):
    for bad in ("0", "241"):
        code, out, err = await cli(capsys, "claim", WELLAND, "--purpose", "x", "--minutes", bad)
        assert code == 1 and out == "" and err.startswith("error: ") and "Traceback" not in err


async def test_claim_default_minutes_and_cli_holder(stack, capsys):
    code, out, _ = await cli(capsys, "claim", WELLAND, "--purpose", "x")
    assert code == 0 and "UTC" in out and "T" not in out.split("until")[1].split("UTC")[0] and "(started" in out
    code, out, _ = await cli(capsys, "claims")
    assert "(gone)" not in out  # the claims call itself reconnects the same holder
    assert _presence({"holder": "cli:u@h", "holder_connected": False}) == " (cli)"
    assert _presence({"holder": "p:1@h", "holder_connected": False}) == " (not connected)"
    assert _presence({"holder": "p:1@h", "holder_connected": True}) == ""


async def test_oserror_is_an_error_line(no_broker, capsys, monkeypatch):
    async def boom(*a, **k):
        raise OSError("cannot spawn")

    monkeypatch.setattr("fpgas_online_mcp.cli.connect", boom)
    code, out, err = await cli(capsys, "boards")
    assert code == 1 and out == "" and err == "error: cannot spawn\n"


def test_when_epoch():
    from fpgas_online_mcp.cli import when_epoch

    assert when_epoch(1_700_000_000) == "2023-11-14 22:13 UTC"  # a past day carries its date
    assert when_epoch("1700000000.5") == "2023-11-14 22:13 UTC"
    for bad in (None, "x", float("nan"), 1e30):
        assert when_epoch(bad) == str(bad)


# --- fleet and switches ---------------------------------------------------------------------------


async def test_fleet(stack, capsys):
    code, out, err = await cli(capsys, "fleet", WELLAND)
    assert code == 0 and err == ""
    assert out.splitlines()[0].startswith(f"{WELLAND} (serial 00000000cc479fd1")
    assert "page: https://welland.fpgas.online/fleet/00000000cc479fd1/" in out
    assert "\nmachine:\n" in out and "  hostname: pi-sw1-p2" in out and "\nswitch_port:\n" in out
    assert "events" not in out and "_action" not in out


async def test_fleet_sections_paging_and_json(stack, capsys):
    code, out, _ = await cli(capsys, "fleet", WELLAND, "--section", "events", "--section", "console", "--limit", "3")
    assert code == 0
    assert "events (3 of 30; next: --offset 3):" in out and f"  {UNTRUSTED}" in out
    assert "console (3 of 40; next: --offset 3):" in out
    code, out, _ = await cli(
        capsys, "fleet", WELLAND, "--section", "events", "--limit", "2", "--offset", "28", "--json"
    )
    data = json.loads(out)
    assert data["paging"]["events"] == {"total": 30, "offset": 28, "shown": 2}
    code, out, err = await cli(capsys, "fleet", "ps1:pi3")
    assert code == 1 and "ps1 publishes no fleet or switch pages" in err


async def test_switches(stack, capsys):
    code, out, err = await cli(capsys, "switches")
    assert code == 0 and err == ""
    assert "switch 1: sw-netgear-gsm7252ps-s2 (gsm7252ps), answering" in out
    assert out.count("known issue: ") == 3
    header = next(line for line in out.splitlines() if line.startswith("SWITCH"))
    assert header.split() == [
        "SWITCH",
        "PORT",
        "MACHINES",
        "LINK",
        "SPEED",
        "POE",
        "WATTS",
        "VLAN",
        "LLDP",
        "ERRORS",
        "NET",
    ]
    assert "_action" not in out and "can_power" not in out
    code, out, _ = await cli(capsys, "switches", "--problems")
    rows = [line.split()[:2] for line in out.splitlines() if re.match(r"\d+ +\d+ ", line)]
    assert sorted(rows) == [["1", "11"], ["1", "14"], ["2", "2"], ["2", "42"]]
    assert "2/42: not as it should be" in out  # why a port is not as it should be
    code, out, _ = await cli(capsys, "switches", "--board", "tinytapeout:tt-ab2a48a7ee008dd9", "--json")
    data = json.loads(out)
    assert data["host"] == "welland:pi-sw2-p11" and [r[:2] for r in data["ports"]] == [[2, 11]]
    code, out, _ = await cli(capsys, "switches", "--switch", "1", "--port", "2")
    rows = [line.split()[:3] for line in out.splitlines() if re.match(r"\d+ +\d+ ", line)]
    assert rows == [["1", "2", "pi-sw1-p2"]]


async def test_fleet_against_an_older_broker(stack, capsys):
    stack.broker._handlers.pop("switch_ports")
    code, out, err = await cli(capsys, "switches")
    assert code == 1 and "older" in err and "fpgas-online broker stop" in err


def test_fmt_switches_takes_any_cell_type_and_short_summaries():
    from fpgas_online_mcp.cli import fmt_switches
    from fpgas_online_mcp.fleet import PORT_COLUMNS

    row = dict.fromkeys(PORT_COLUMNS)
    row |= {"switch": 1, "port": 2, "machines": ["pi-sw1-p2", None], "watts": 4.2, "vlan": 2102, "rx_errors": 3}
    row |= {"speed": 1000, "lldp_name": 7, "net_ok": True}
    r = {
        "switches": [
            {"index": 1, "name": "a", "known_issues": 2},
            {"index": 2, "name": "b", "known_issues": [{"issue": "poe #16", "url": "https://x/16"}]},
            {"index": 3, "name": "c"},
        ],
        "columns": PORT_COLUMNS,
        "ports": [[row[c] for c in PORT_COLUMNS]],
        "total": 1,
        "offset": 0,
    }
    out = fmt_switches(r)
    line = next(x for x in out.splitlines() if x.startswith("1 "))
    assert line.split()[:9] == ["1", "2", "pi-sw1-p2,-", "down", "1000", "-", "4.2", "2102", "7"]
    assert "2 known issues" in out and "known issue: poe #16 (https://x/16)" in out


def test_fleet_and_switches_text_strip_control_characters():
    """A Pi's strings (model, cameras, LLDP names) can carry terminal escapes: never print them."""
    from fpgas_online_mcp import fleet
    from fpgas_online_mcp.cli import fmt_fleet, fmt_switches

    fix = Path(__file__).parent / "fixtures"
    data = json.loads((fix / "welland_machine.json").read_text())
    data["machine"]["registered_model"] = "\x1b[31mPi\x1b[0m 3\x07\r"
    data["machine"]["cameras"] = ["\x1b]0;owned\x07ov5647"]
    out = fmt_fleet(fleet.details(data, ["machine"], {}, head={"board": WELLAND}))
    switches = json.loads((fix / "welland_switches_full.json").read_text())["switches"]
    switches[0]["ports"][1]["lldp_name"] = "\x1b[2Jpi-sw1-p2\x00"
    out += fmt_switches(fleet.switch_ports(switches, {}, switch=1, port=2))
    for bad in ("\x1b", "\x07", "\x00", "\r"):
        assert bad not in out
    assert "Pi 3" in out and "ov5647" in out and "pi-sw1-p2" in out
