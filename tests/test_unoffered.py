"""allow_unoffered: reaching a welland Pi that is up but not offered, through the broker's ops.

The broker runs in process (the test_mcp stack: fixture pages, fake ssh, stub in-use check and power
cycle). welland:pi-sw1-p10 is `attention` and not offered in the registry fixture, and has no board
page (HTTP 404), like a real Pi welland does not offer.
"""

import contextlib
import json
import os
from pathlib import Path

import httpx
import pytest
from test_broker import env  # noqa: F401  (fixture)
from test_mcp import HERE, call, ok, stack  # noqa: F401  (fixture)

from fpgas_online_mcp import client, inuse, mcp_server, paths
from fpgas_online_mcp.boards import BoardId
from fpgas_online_mcp.client import BrokerError
from fpgas_online_mcp.inuse import Verdict

UNOFFERED = "welland:pi-sw1-p10"  # attention, offered false, no board page
OFFERED = "welland:pi-sw1-p2"  # operational, offered, has a board page
MISSING = "welland:pi-sw2-p18"
SETUP = "welland:pi-sw1-p12"  # attention; set on the setup port by `setup_port`
NOTE = (
    "not offered by the site (condition: attention): it may be under maintenance; use it only for what the user asked"
)
HINT = (
    "welland does not offer welland:pi-sw1-p10 (condition attention); if the user wants it anyway and it is up, "
    "pass allow_unoffered"
)


@contextlib.asynccontextmanager
async def broker(holder="me:1@host"):
    c = await client.connect(holder, spawn=False)
    try:
        yield c
    finally:
        await c.close()


async def refused(c, op, **args) -> BrokerError:
    with pytest.raises(BrokerError) as exc:
        await c.call(op, **args)
    return exc.value


def setup_port(stack, name="pi-sw1-p12"):  # noqa: F811
    registry = json.loads((HERE / "fixtures" / "welland_registry.json").read_text())
    for m in registry["machines"]:
        if m["hostname"] == name:
            m["setup_port"] = True
    stack.site.override[("welland.fpgas.online", "/fleet/registry.json")] = httpx.Response(200, json=registry)


def password_reads(stack):  # noqa: F811
    return stack.site.count("/fpgas/pi-sw1-p2.html")  # the page site_password reads


# --- default: unchanged refusal ------------------------------------------------------------------


async def test_without_the_option_an_unoffered_pi_is_refused_and_told_about_the_option(stack):  # noqa: F811
    async with broker() as c:
        e = await refused(c, "claim_board", board=UNOFFERED, purpose="look")
        assert e.code == "not_offered" and HINT in e.message and "not claimed" in e.message
        # an override would give a claim that still cannot reach the Pi: refused the same way
        e = await refused(c, "claim_board", board=UNOFFERED, purpose="look", override_reason="Tim said so")
        assert e.code == "not_offered"
    assert stack.state.claims.for_board(UNOFFERED) is None
    assert stack.inuse.calls == []  # refused from the inventory, before any ssh


async def seed_claim(stack, board, allow_unoffered=False):  # noqa: F811
    """A claim as an older broker (or an earlier state of the site) could have left it."""
    claim = await stack.state.claims.claim(board, 30, "old", "me:1@host", allow_unoffered=allow_unoffered)
    return claim.claim_id


async def test_a_claim_without_the_option_cannot_reach_the_pi(stack):  # noqa: F811
    claim_id = await seed_claim(stack, UNOFFERED)
    async with broker() as c:
        for op, args in (("connect", {}), ("run", {"command": "true"}), ("power_cycle", {})):
            # the option is the claim's: passing it here changes nothing
            e = await refused(c, op, board=UNOFFERED, claim_id=claim_id, allow_unoffered=True, **args)
            assert e.code == "not_offered" and HINT in e.message
            assert "claim the board again with allow_unoffered" in e.message
    assert password_reads(stack) == 0 and stack.power.calls == []
    assert "Host fpgas-welland-pi-sw1-p10" not in _config()


def _config() -> str:
    path = paths.config_dir() / "ssh_config"
    return path.read_text() if path.exists() else ""


def ssh_calls() -> int:
    log = Path(os.environ["FAKE_SSH_LOG"])
    return len(log.read_text().splitlines()) if log.exists() else 0


async def test_board_status_without_the_option_says_how_to_reach_the_pi(stack):  # noqa: F811
    async with broker() as c:
        status = await c.call("board_status", board=UNOFFERED)
    assert status["not_offered"] == HINT and "offered_note" not in status
    assert status["in_use"]["verdict"] == "unknown" and HINT in status["in_use"]["signals"][0]
    assert stack.inuse.calls == [] and ssh_calls() == 0


async def test_a_leftover_master_does_not_let_a_claim_without_the_option_in(stack):  # noqa: F811
    async with broker() as c:
        claim = await c.call("claim_board", board=UNOFFERED, purpose="look", allow_unoffered=True)
        await c.call("connect", board=UNOFFERED)  # opens the shared master (it persists for 30 minutes)
        await c.call("release_board", claim_id=claim["claim_id"])
    assert await stack.state.ssh.check(BoardId("welland", "pi-sw1-p10"))
    stack.state.inuse_check = inuse.check  # the real check: it would reuse that master if asked
    before = ssh_calls()
    async with broker("other:2@host") as other:
        e = await refused(other, "claim_board", board=UNOFFERED, purpose="sneak")
        assert e.code == "not_offered" and HINT in e.message
        status = await other.call("board_status", board=UNOFFERED)
        assert status["in_use"]["verdict"] == "unknown" and status["not_offered"] == HINT
        claim_id = await seed_claim(stack, UNOFFERED)
        for op, args in (("connect", {}), ("run", {"command": "true"}), ("power_cycle", {})):
            e = await refused(other, op, board=UNOFFERED, claim_id=claim_id, **args)
            assert e.code == "not_offered"
    assert ssh_calls() == before and stack.state.claims.for_board(UNOFFERED).allow_unoffered is False
    assert stack.power.calls == []


# --- with the option -----------------------------------------------------------------------------


async def test_the_option_reaches_an_attention_pi_and_is_kept_in_the_claim(stack):  # noqa: F811
    async with broker() as c:
        claim = await c.call("claim_board", board=UNOFFERED, purpose="look", allow_unoffered=True)
        assert stack.inuse.kw[-1]["allow_unoffered"] is True  # the check could run its script
        assert claim["allow_unoffered"] is True and claim["offered_note"] == NOTE
        stored = stack.state.claims.for_board(UNOFFERED)
        assert stored.allow_unoffered is True
        info = await c.call("connect", board=UNOFFERED)  # nobody passes the option here
        assert info["alias"] == "fpgas-welland-pi-sw1-p10" and info["offered_note"] == NOTE
        r = await c.call("run", board=UNOFFERED, command="echo up")
        assert r["exit_status"] == 0 and "up" in r["output"]
        await c.call("power_cycle", board=UNOFFERED)
    assert stack.power.claims[-1].allow_unoffered is True
    assert password_reads(stack) == 1  # the site-wide password, read once
    config = _config()
    assert "Host fpgas-welland-pi-sw1-p10" in config and "Port 11022" in config and "REDACTED" not in config


async def test_every_view_of_the_claim_shows_the_option(stack):  # noqa: F811
    async with broker() as c:
        await c.call("claim_board", board=UNOFFERED, purpose="look", allow_unoffered=True)
        await c.call("claim_board", board=OFFERED, purpose="blink")
    async with broker("other:2@host") as other:
        listed = await other.call("list_boards", site="welland")
        assert listed["claims"][UNOFFERED]["allow_unoffered"] is True
        assert "allow_unoffered" not in listed["claims"][OFFERED]
        claims = {c["board"]: c for c in (await other.call("claims"))["claims"]}
        assert claims[UNOFFERED]["allow_unoffered"] is True and "allow_unoffered" not in claims[OFFERED]
        status = await other.call("board_status", board=UNOFFERED, check_use=False)
        assert status["board"]["claim"]["allow_unoffered"] is True


async def test_board_status_with_the_option(stack):  # noqa: F811
    async with broker() as c:
        status = await c.call("board_status", board=UNOFFERED, allow_unoffered=True)
        assert status["allow_unoffered"] is True and status["offered_note"] == NOTE
        assert stack.inuse.kw[-1]["allow_unoffered"] is True
        offered = await c.call("board_status", board=OFFERED, allow_unoffered=True)
        assert offered["allow_unoffered"] is True and "offered_note" not in offered


async def test_the_option_on_an_offered_pi_is_harmless(stack):  # noqa: F811
    async with broker() as c:
        claim = await c.call("claim_board", board=OFFERED, purpose="blink", allow_unoffered=True)
        assert claim["allow_unoffered"] is True and "offered_note" not in claim
        info = await c.call("connect", board=OFFERED)
        assert "offered_note" not in info
    assert password_reads(stack) == 1  # its own page, not the site-wide fallback
    assert stack.site.count("/fpgas/") == 0


# --- the option does not cover ... ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("board", "expected"),
    [
        (
            MISSING,
            "welland:pi-sw2-p18 is missing (not checking in): allow_unoffered reaches only a Pi that is up "
            "(condition operational or attention)",
        ),
        (SETUP, "welland:pi-sw1-p12 is on the setup port (it is being set up): allow_unoffered does not cover it"),
        ("ps1:pi3", "allow_unoffered covers only welland Pis: ps1 lists only the hosts it offers"),
        ("tinytapeout:tt06", "allow_unoffered covers only welland Pis: a Tiny Tapeout board already reaches its Pi"),
    ],
)
async def test_the_option_is_refused_where_it_does_not_apply(stack, board, expected):  # noqa: F811
    setup_port(stack)
    async with broker() as c:
        e = await refused(c, "claim_board", board=board, purpose="look", allow_unoffered=True)
        assert e.code == "unoffered_not_covered" and expected in e.message
        # board_status still answers, with the option ignored and the reason under not_offered
        status = await c.call("board_status", board=board, check_use=False, allow_unoffered=True)
        assert status["board"]["id"] == board and status["allow_unoffered"] is False
        assert status["not_offered"].startswith(f"allow_unoffered was ignored: {expected}")
        assert "offered_note" not in status
    assert stack.inuse.calls == [] and stack.state.claims.all() == []


async def test_board_status_ignoring_the_option_checks_as_without_it(stack):  # noqa: F811
    async with broker() as c:
        status = await c.call("board_status", board="ps1:pi3", allow_unoffered=True)
    assert status["in_use"]["verdict"] == "idle" and stack.inuse.kw[-1]["allow_unoffered"] is False


async def test_a_missing_or_setup_port_pi_is_told_the_option_will_not_help(stack):  # noqa: F811
    setup_port(stack)
    async with broker() as c:
        for board, why in ((MISSING, "not checking in"), (SETUP, "setup port")):
            e = await refused(c, "claim_board", board=board, purpose="x", override_reason="test")
            assert e.code == "not_offered" and "allow_unoffered will not help" in e.message and why in e.message
            claim_id = await seed_claim(stack, board)
            e = await refused(c, "connect", board=board, claim_id=claim_id)
            assert e.code == "not_offered" and "allow_unoffered will not help" in e.message and why in e.message
            assert "claim the board again" not in e.message
    assert stack.inuse.calls == [] and ssh_calls() == 0


# --- renewals and arguments ----------------------------------------------------------------------


async def test_a_renewal_keeps_the_option_and_never_adds_it(stack):  # noqa: F811
    async with broker() as c:
        flagged = await c.call("claim_board", board=UNOFFERED, purpose="look", allow_unoffered=True)
        renewed = await c.call("claim_board", board=UNOFFERED, purpose="more", claim_id=flagged["claim_id"])
        assert renewed["renewed"] is True and renewed["allow_unoffered"] is True
        plain = await c.call("claim_board", board=OFFERED, purpose="blink")
        e = await refused(c, "claim_board", board=OFFERED, purpose="blink", claim_id=plain["claim_id"],
                          allow_unoffered=True)  # fmt: skip
        assert e.code == "bad_request" and "release" in e.message
    assert stack.state.claims.for_board(OFFERED).allow_unoffered is False


@pytest.mark.parametrize("value", ["yes", 1])
async def test_the_option_must_be_a_boolean(stack, value):  # noqa: F811
    async with broker() as c:
        e = await refused(c, "claim_board", board=UNOFFERED, purpose="x", allow_unoffered=value)
        assert e.code == "bad_request" and "allow_unoffered" in e.message


# --- board_status: boot check, boards, fleet page and switch port (welland, read-only) ------------


async def test_board_status_shows_the_boot_check_boards_fleet_page_and_switch_port(stack):  # noqa: F811
    async with broker() as c:
        board = (await c.call("board_status", board=UNOFFERED, check_use=False))["board"]
    assert board["boot_check"] == {"result": "fail", "at": "2026-10-17T04:14:29Z"}
    assert board["boards"] == [
        {
            "type": "NeTV2",
            "result": "fail",
            "tests": {"jtag": "pass", "uart": "pass", "ddr": "pass", "flash": "pass", "wiring": "pass"},
        }
    ]
    assert board["fleet_page"] == "https://welland.fpgas.online/fleet/000000004fe3e7e4/"
    assert board["switch_port"] == {"link_up": True, "poe_state": "delivering", "watts": 8.6}
    assert all(r.method == "GET" for r in stack.site.requests)  # nothing but reads, never a switch action
    assert stack.site.count("/management/switches.json") == 1


async def test_board_status_without_switch_facts_or_on_other_sites(stack):  # noqa: F811
    stack.site.override[("welland.fpgas.online", "/management/switches.json")] = httpx.Response(500)
    async with broker() as c:
        board = (await c.call("board_status", board=UNOFFERED, check_use=False))["board"]
        assert "switch_port" not in board and board["boot_check"]["result"] == "fail"  # the rest is still there
        for other in ("ps1:pi3", "tinytapeout:tt06"):
            board = (await c.call("board_status", board=other, check_use=False))["board"]
            assert not {"switch_port", "boot_check", "boards", "fleet_page"} & set(board)


def huge_registry(stack, boards=8, tests=40):  # noqa: F811
    registry = json.loads((HERE / "fixtures" / "welland_registry.json").read_text())
    for m in registry["machines"]:
        if m["hostname"] == "pi-sw1-p10":
            m["boards"] = [
                {"type": f"Board type {i}", "result": "fail", "tests": {f"test-{j:02d}": "pass" for j in range(tests)}}
                for i in range(boards)
            ]
    stack.site.override[("welland.fpgas.online", "/fleet/registry.json")] = httpx.Response(200, json=registry)


def long_verdict():
    return Verdict("in use", [f"board says: active: signal {i} " + "x" * 180 for i in range(10)], "x")


async def test_board_status_cuts_the_tests_first_and_never_the_verdict(stack):  # noqa: F811
    huge_registry(stack, boards=4, tests=25)
    stack.inuse.verdict = long_verdict()
    async with stack.session() as c:
        status = ok(await call(c, "board_status", board=UNOFFERED, allow_unoffered=True))  # fits in 4 KB
    assert status["in_use"]["verdict"] == "in use"
    assert all(f"signal {i} " in status["in_use"]["signals"][0] for i in range(10))
    assert [b["type"] for b in status["board"]["boards"]] == [f"Board type {i}" for i in range(4)]
    assert all("tests" not in b for b in status["board"]["boards"])
    assert "fleet_page" in status["board"]["cut"] and status["board"]["boot_check"]["result"] == "fail"


async def test_board_status_drops_the_boards_when_even_that_is_too_long(stack):  # noqa: F811
    huge_registry(stack, boards=60, tests=3)
    stack.inuse.verdict = long_verdict()
    async with stack.session() as c:
        status = ok(await call(c, "board_status", board=UNOFFERED, allow_unoffered=True))
    assert "boards" not in status["board"] and "fleet_page" in status["board"]["cut"]
    assert status["in_use"]["verdict"] == "in use" and status["board"]["switch_port"]["watts"] == 8.6


async def test_board_status_that_fits_keeps_every_test(stack):  # noqa: F811
    huge_registry(stack, boards=2, tests=5)
    async with stack.session() as c:
        status = ok(await call(c, "board_status", board=UNOFFERED))
    assert all(len(b["tests"]) == 5 for b in status["board"]["boards"]) and "cut" not in status["board"]


# --- the MCP tools, the CLI, and brokers of other versions ----------------------------------------


async def test_only_board_status_and_claim_board_take_the_option(stack):  # noqa: F811
    async with stack.session() as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    for name in ("board_status", "claim_board"):
        prop = tools[name].input_schema["properties"]["allow_unoffered"]
        assert prop["type"] == "boolean" and prop["default"] is False
        assert "only when the user asks" in prop["description"].lower()
        assert "allow_unoffered" in tools[name].description
    for name in ("connect", "run", "power_cycle", "list_boards", "release_board"):
        assert "allow_unoffered" not in tools[name].input_schema["properties"]


async def test_the_tools_end_to_end(stack):  # noqa: F811
    async with stack.session() as c:
        claim = ok(await call(c, "claim_board", board=UNOFFERED, purpose="look", allow_unoffered=True))
        assert claim["allow_unoffered"] is True and claim["offered_note"] == NOTE
        info = ok(await call(c, "connect", board=UNOFFERED))
        assert info["offered_note"] == NOTE
        status = ok(await call(c, "board_status", board=UNOFFERED, check_use=False, allow_unoffered=True))
        assert status["offered_note"] == NOTE and status["board"]["claim"]["allow_unoffered"] is True


class RecordingConn:
    """A broker connection that answers like an older broker: it ignores allow_unoffered."""

    def __init__(self):
        self.broken, self.calls = False, []

    async def call(self, op, **args):
        self.calls.append((op, args))
        if op == "claim_board":
            return {"claim_id": "c-old", "board": args["board"], "expires": "x"}
        return {"board": {"id": args.get("board")}}

    async def close(self):
        pass


async def frontend():
    conn = RecordingConn()

    async def connect(holder):
        return conn

    return mcp_server.Frontend("me@host", connect), conn


async def test_the_front_end_sends_the_option_only_when_it_is_on():
    fe, conn = await frontend()
    await fe.tool("board_status", board=UNOFFERED, check_use=True, allow_unoffered=False)
    await fe.tool("claim_board", board=UNOFFERED, purpose="x", allow_unoffered=False)
    assert all("allow_unoffered" not in args for _, args in conn.calls)  # an older broker sees what it knows


async def test_an_older_broker_that_ignores_the_option_is_reported():
    fe, conn = await frontend()
    result = await fe.tool("claim_board", board=UNOFFERED, purpose="x", allow_unoffered=True)
    text = result.content[0].text
    data = json.loads(text)
    assert conn.calls[-1][1]["allow_unoffered"] is True
    assert data["warning"].startswith("release_board c-old: ")
    assert "older" in data["warning"] and "ignored allow_unoffered" in data["warning"]
    assert fe.claim_ids == ["c-old"]  # still remembered, so it can be released
    result = await fe.tool("board_status", board=UNOFFERED, allow_unoffered=True)
    assert "ignored allow_unoffered" in json.loads(result.content[0].text)["warning"]


async def test_cli_status_and_claim_with_the_option(stack, capsys):  # noqa: F811
    from test_cli import cli

    code, out, err = await cli(capsys, "status", UNOFFERED, "--allow-unoffered")
    assert code == 0 and NOTE in out and "boot check: fail" in out and "fleet page: https://welland" in out
    assert "switch port: link up, PoE delivering, 8.6 W" in out
    code, out, err = await cli(capsys, "claim", UNOFFERED, "--purpose", "look", "--allow-unoffered")
    assert code == 0 and NOTE in out and "allow_unoffered" in out
    code, out, err = await cli(capsys, "claims")
    assert code == 0 and "(allow_unoffered)" in out
    code, out, err = await cli(capsys, "claim", MISSING, "--purpose", "look", "--allow-unoffered")
    assert code == 1 and "is missing (not checking in)" in err


# --- a claim's allow_unoffered is checked again at use ------------------------------------------


@pytest.mark.parametrize("op", ["connect", "run", "power_cycle"])
@pytest.mark.parametrize(("board", "now"), [(SETUP, "on the setup port"), (MISSING, "missing (not checking in)")])
async def test_a_claim_whose_pi_is_no_longer_covered_is_refused(stack, op, board, now):  # noqa: F811
    setup_port(stack)
    claim_id = await seed_claim(stack, board, allow_unoffered=True)  # taken while the Pi was up
    args = {"command": "true"} if op == "run" else {}
    async with broker() as c:
        e = await refused(c, op, board=board, claim_id=claim_id, **args)
    assert e.code == "unoffered_not_covered"
    assert e.message == f"{board} is now {now}: allow_unoffered no longer covers it; release the claim"
    assert stack.power.calls == [] and ssh_calls() == 0 and password_reads(stack) == 0


async def test_board_status_without_the_in_use_check_still_says_not_offered(stack):  # noqa: F811
    async with broker() as c:
        status = await c.call("board_status", board=UNOFFERED, check_use=False)
        assert status["not_offered"] == HINT and "in_use" not in status
        assert "not_offered" not in await c.call("board_status", board=UNOFFERED, check_use=False,
                                                 allow_unoffered=True)  # fmt: skip
        assert "not_offered" not in await c.call("board_status", board=OFFERED, check_use=False)
