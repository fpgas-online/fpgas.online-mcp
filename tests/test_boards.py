"""Board identity and inventory, served from the Task 3 fixtures."""

import json
from datetime import UTC, datetime

import httpx
import pytest
from test_sites import FIX, client, clock, site  # noqa: F401  (fixtures)

from fpgas_online_mcp.boards import TT_HOST_SITE, BoardId, inventory, ssh_port, switch_port, tt_host
from fpgas_online_mcp.sites import SiteError


def by_id(inv):
    return {str(b.id): b for b in inv.boards}


async def test_welland_duplicate_hostname_prefers_live(client, site):  # noqa: F811
    reg = json.loads((FIX / "welland_registry.json").read_text())
    live = next(m for m in reg["machines"] if m["hostname"] == "pi-sw1-p2")
    old = {**live, "condition": "missing", "offered": False, "last_seen": "2025-01-01T00:00:00+00:00"}
    reg["machines"] = [old, *reg["machines"], {**old}]
    site.override[("welland.fpgas.online", "/fleet/registry.json")] = httpx.Response(200, json=reg)
    inv = await inventory(client, "welland")
    dup = [b for b in inv.boards if b.name == "pi-sw1-p2"]
    assert len(dup) == 1
    assert dup[0].condition == live["condition"] and dup[0].offered is live["offered"]
    assert dup[0].type == "Fomu"
    assert dup[0].boot_id == live["network"]["boot_id"]
    assert dup[0].last_seen == live["last_seen"]
    assert any(b.condition == "missing" for b in inv.boards)  # offline boards stay listed


async def test_boot_id_only_when_current(client, site):  # noqa: F811
    reg = json.loads((FIX / "welland_registry.json").read_text())
    reg["machines"][0]["network"]["current_boot"] = False
    site.override[("welland.fpgas.online", "/fleet/registry.json")] = httpx.Response(200, json=reg)
    inv = await inventory(client, "welland")
    assert by_id(inv)["welland:" + reg["machines"][0]["hostname"]].boot_id is None


async def test_ps1_offered(client):  # noqa: F811
    inv = await inventory(client, "ps1")
    assert inv.boards and all(b.condition == "offered" and b.offered is True for b in inv.boards)
    assert "pi3" in by_id(inv)["ps1:pi3"].name
    assert "absence says nothing" in inv.notes["ps1"]


async def test_tinytapeout_conditions(client):  # noqa: F811
    inv = await inventory(client, "tinytapeout")
    boards = by_id(inv)
    assert boards["tinytapeout:tt06"].condition == "not connected"
    up = boards["tinytapeout:tt-ab2a48a7ee008dd9"]
    assert up.condition == "connected" and up.type == "chip board"
    assert up.usb_serial == "ab2a48a7ee008dd9"
    # a slug with no fixture (status 404) is listed as unknown, not omitted
    assert boards["tinytapeout:fpga-2"].condition == "unknown"
    assert "catalogue" in inv.notes["tinytapeout"]


async def test_site_filter_and_all(client):  # noqa: F811
    one = await inventory(client, "ps1")
    assert {b.site for b in one.boards} == {"ps1"} and set(one.notes) == {"ps1"}
    everything = await inventory(client)
    assert {b.site for b in everything.boards} == {"welland", "ps1", "tinytapeout"}
    with pytest.raises(ValueError):
        await inventory(client, "nowhere")


async def test_one_site_down_keeps_others(client, site):  # noqa: F811
    site.override[("ps1.fpgas.online", "/fpgas/")] = httpx.Response(500)
    inv = await inventory(client)
    assert {b.site for b in inv.boards} == {"welland", "tinytapeout"}
    assert inv.notes["ps1"].startswith("unavailable: ") and "absence says nothing" in inv.notes["ps1"]


async def test_stale_site_marks_boards(client, site, clock):  # noqa: F811
    await inventory(client, "ps1")
    first = datetime(2026, 11, 26, 12, 0, tzinfo=UTC).isoformat()
    clock.advance(120)
    site.fail = True
    inv = await inventory(client, "ps1")
    assert inv.boards and all(b.stale and b.as_of == first for b in inv.boards)
    assert inv.boards[0].to_dict()["id"] == "ps1:" + inv.boards[0].name


KNOWN = [BoardId("welland", "pi3"), BoardId("ps1", "pi3"), BoardId("ps1", "pi4"), BoardId("tinytapeout", "fpga-2")]


def test_parse():
    assert str(BoardId.parse("ps1:pi4", KNOWN)) == "ps1:pi4"
    assert BoardId.parse("fpga-2", KNOWN) == BoardId("tinytapeout", "fpga-2")
    assert BoardId.parse("ps1:anything", []) == BoardId("ps1", "anything")


def test_parse_errors():
    with pytest.raises(ValueError, match="welland:pi3, ps1:pi3"):
        BoardId.parse("pi3", KNOWN)
    with pytest.raises(ValueError, match="no such board; use list_boards|use list_boards"):
        BoardId.parse("nope", KNOWN)
    with pytest.raises(ValueError, match="use list_boards"):
        BoardId.parse("ps1:nope", KNOWN)
    with pytest.raises(ValueError, match="unknown site"):
        BoardId.parse("mars:pi3", KNOWN)


def test_ports_from_names():
    assert ssh_port("welland", "pi-sw1-p2") == 10222
    assert ssh_port("welland", "pi-sw2-p14") == 21422
    assert ssh_port("welland", "pi-sw01-p02") == 10222
    assert ssh_port("ps1", "pi3") == 10322 and ssh_port("ps1", "pi23") == 12322
    for s, name in (("welland", "pi-sw1-p2x"), ("ps1", "raspberry"), ("tinytapeout", "fpga-2"), ("welland", "pi3")):
        assert ssh_port(s, name) is None and switch_port(s, name) is None
    assert switch_port("welland", "pi-sw2-p9") == (9, 2) and switch_port("ps1", "pi7") == (7, None)


async def test_inventory_survives_registry_entries_that_are_not_objects(client, site):  # noqa: F811
    reg = json.loads((FIX / "welland_registry.json").read_text())
    reg["machines"].insert(0, "junk")
    site.override[("welland.fpgas.online", "/fleet/registry.json")] = httpx.Response(200, json=reg)
    inv = await inventory(client, "welland")
    assert "welland:pi-sw1-p2" in by_id(inv) and not inv.notes["welland"].startswith("unavailable")


# --- the welland Pi of a Tiny Tapeout board ------------------------------------------------------


async def test_tt_host_from_the_live_board_page(client, site):  # noqa: F811
    assert await tt_host(client, "tt-ab2a48a7ee008dd9") == BoardId(TT_HOST_SITE, "pi-sw2-p11")
    assert await tt_host(client, "tt-ab2a48a7ee008dd9") == BoardId("welland", "pi-sw2-p11")
    assert site.count("/board/tt-ab2a48a7ee008dd9/") == 2  # read live every time: boards move between Pis


@pytest.mark.parametrize(
    ("port", "switch"), [("", ""), ("7", ""), ("", "2"), ("x", "2"), ("7", "0"), ("-7", "2"), ("7.0", "2"), ("٧", "2")]
)
async def test_tt_host_none_without_a_usable_switch_and_port(client, site, port, switch):  # noqa: F811
    html = f'<html><div data-port="{port}" data-switch="{switch}"></div></html>'
    site.override[("tinytapeout.fpgas.online", "/board/fpga-2/")] = httpx.Response(200, text=html)
    assert await tt_host(client, "fpga-2") is None


async def test_tt_host_site_error_propagates(client):  # noqa: F811
    with pytest.raises(SiteError):
        await tt_host(client, "tt06")  # no fixture: 404
