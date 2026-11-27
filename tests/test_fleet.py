"""fleet.py: the fleet_details and switch_ports results, from the fixtures (no broker, no network)."""

import json
from pathlib import Path

import pytest

from fpgas_online_mcp import fleet
from fpgas_online_mcp.ops import compact

FIX = Path(__file__).parent / "fixtures"
UNTRUSTED = '<board-output untrusted="true">'
ACTION_KEYS = ("link_action", "poe_action", "can_power", "power_icon", "power_title", "powers", "admin_enabled")


def machine(name="welland_machine.json"):
    return json.loads((FIX / name).read_text())


def switches():
    return json.loads((FIX / "welland_switches_full.json").read_text())["switches"]


def registry():
    return json.loads((FIX / "welland_registry.json").read_text())["machines"]


HOSTNAMES = fleet.hostnames(registry())


def size(obj):
    return len(compact(obj).encode())


def no_actions(obj):
    text = compact(obj)
    for key in ACTION_KEYS:
        assert f'"{key}"' not in text, key
    assert "_action" not in text


# --- sections -------------------------------------------------------------------------------------


def test_sections_default_and_all():
    assert fleet.parse_sections(None) == ["machine", "boards", "network", "switch_port"]
    assert fleet.parse_sections("all") == list(fleet.SECTIONS)
    assert fleet.parse_sections(["all"]) == list(fleet.SECTIONS)
    assert fleet.parse_sections(["events", "machine", "events"]) == ["events", "machine"]
    assert fleet.parse_sections("console") == ["console"]
    with pytest.raises(ValueError, match="boot_checks"):  # the error lists the sections
        fleet.parse_sections(["nonsense"])


def test_default_sections():
    r = fleet.details(machine(), fleet.parse_sections(None), HOSTNAMES)
    assert set(r) >= {"machine", "boards", "network", "switch_port", "page", "serial", "as_of"}
    for absent in ("events", "console", "registrations", "boot_checks", "labels"):
        assert absent not in r
    assert r["page"] == "https://welland.fpgas.online/fleet/00000000cc479fd1/"
    assert r["machine"]["hostname"] == "pi-sw1-p2" and r["machine"]["offered"] is True
    assert r["boards"][0]["type"] == "Fomu"
    assert r["paging"]["boards"] == {"total": 1, "offset": 0, "shown": 1}
    assert size(r) <= fleet.LIMIT
    assert "cut" not in r


def test_switch_port_section_has_no_action_fields_and_names_hosts():
    r = fleet.details(machine(), ["switch_port"], HOSTNAMES)
    port = r["switch_port"]
    assert port["port"] == 2 and port["poe_state"] == "delivering" and port["watts"] == "4.2"
    assert port["machines"] == ["pi-sw1-p2"]
    assert set(port) <= set(fleet.PORT_COLUMNS)
    no_actions(r)


def test_all_sections_fit_and_say_what_was_cut():
    r = fleet.details(machine(), fleet.parse_sections("all"), HOSTNAMES)
    assert size(r) <= fleet.LIMIT
    no_actions(r)
    assert r["cut"], "the fixture does not fit 8 KB with every section"
    for name, note in r["cut"].items():
        assert name in fleet.SECTIONS
        assert "fleet_details" in note and f"'{name}'" in note  # how to get it
    # a cut list says how many it shows and where to go on
    for name, page in r["paging"].items():
        if name in r["cut"] and name in r:
            assert page["shown"] < page["total"] and page["next"] == page["offset"] + page["shown"]
            assert f"offset={page['next']}" in r["cut"][name]


def test_events_paged_newest_first_and_untrusted():
    data = machine()
    r = fleet.details(data, ["events"], HOSTNAMES, limit=5)
    assert r["paging"]["events"] == {"total": 30, "offset": 0, "shown": 5, "next": 5}
    text = r["events"]
    assert text.startswith(UNTRUSTED) and text.endswith("</board-output>")
    assert text.count("</board-output>") == 1  # the hostile event cannot close the wrapper
    lines = text[len(UNTRUSTED) : -len("</board-output>")].splitlines()
    assert [json.loads(line)["test"] for line in lines] == ["test-29", "test-28", "test-27", "test-26", "test-25"]
    later = fleet.details(data, ["events"], HOSTNAMES, limit=5, offset=25)
    assert later["paging"]["events"] == {"total": 30, "offset": 25, "shown": 5}
    assert '"test-0"' in later["events"] and '"test-29"' not in later["events"]


def test_console_and_registrations_are_untrusted():
    r = fleet.details(machine(), ["console", "registrations"], HOSTNAMES, limit=3)
    console = r["console"]
    assert console["since"] and console["receiver_late"] is False
    assert console["lines"].startswith(UNTRUSTED) and "kernel line 39" in console["lines"]
    assert "kernel line 36" not in console["lines"]
    assert r["paging"]["console"] == {"total": 40, "offset": 0, "shown": 3, "next": 3}
    assert r["registrations"].startswith(UNTRUSTED) and "SHA256:fp0" in r["registrations"]


def test_console_error_is_shown_without_lines():
    data = machine()
    data["console"] = None
    data["console_error"] = "the receiver is down"
    r = fleet.details(data, ["console"], HOSTNAMES)
    assert r["console"] == {"error": "the receiver is down"}


def test_network_error_replaces_a_missing_network():
    data = machine()
    data["network"] = None
    data["network_error"] = "no network record yet"
    assert fleet.details(data, ["network"], HOSTNAMES)["network"] == {"error": "no network record yet"}


def test_a_huge_section_is_cut_first_and_the_rest_kept():
    data = machine()
    data["events"] = [dict(data["events"][0], ts=f"2026-11-27T12:{i // 60:02d}:{i % 60:02d}+00:00") for i in range(500)]
    r = fleet.details(data, ["machine", "events"], HOSTNAMES, limit=500)
    assert size(r) <= fleet.LIMIT
    assert r["machine"]["hostname"] == "pi-sw1-p2"  # the small section is whole
    page = r["paging"]["events"]
    assert 1 <= page["shown"] < 500 and page["next"] == page["shown"]
    assert "events" in r["cut"] and "limit=" in r["cut"]["events"]


def test_a_section_too_big_to_cut_is_left_out_with_a_note():
    data = machine()
    data["labels"]["notes"] = ["x" * 9000]
    r = fleet.details(data, ["machine", "labels"], HOSTNAMES)
    assert size(r) <= fleet.LIMIT and "labels" not in r and "machine" in r
    assert "labels" in r["cut"] and "sections=['labels']" in r["cut"]["labels"] and r["page"] in r["cut"]["labels"]


# --- the fallback for a site without machine.json -------------------------------------------------


def test_fallback_from_the_registry_and_the_switch_row():
    entry = next(m for m in registry() if m["hostname"] == "pi-sw1-p2")
    data = fleet.fallback(entry, fleet.find_port(switches(), "00000000cc479fd1", "pi-sw1-p2"))
    r = fleet.details(data, fleet.parse_sections("all"), HOSTNAMES)
    assert r["machine"]["hostname"] == "pi-sw1-p2" and r["machine"]["condition"] == "operational"
    assert r["boards"][0]["type"] == "Fomu" and r["network"]["boot_id"]
    assert r["switch_port"]["port"] == 2 and r["switch_port"]["switch"] == 1
    assert r["page"] == "https://welland.fpgas.online/fleet/00000000cc479fd1/"
    assert set(r["unavailable"]) == {"boot_checks", "labels", "registrations", "events", "console"}
    assert "newer site" in r["note"] and r["page"] in r["note"]
    no_actions(r)


def test_fallback_without_a_switch_row():
    entry = next(m for m in registry() if m["hostname"] == "pi-sw1-p2")
    r = fleet.details(fleet.fallback(entry, None), ["switch_port"], HOSTNAMES)
    assert r["switch_port"] is None


# --- switch_ports ---------------------------------------------------------------------------------


def rows(r):
    return [dict(zip(r["columns"], row, strict=True)) for row in r["ports"]]


def test_switch_ports_every_port():
    r = fleet.switch_ports(switches(), HOSTNAMES)
    assert [s["index"] for s in r["switches"]] == [1, 2]
    s1 = r["switches"][0]
    assert set(s1) == {"index", "name", "model", "status", "read_at", "known_issues", "empty_ports"}
    assert s1["name"] == "sw-netgear-gsm7252ps-s2" and s1["known_issues"][0]["issue"] == "poe #16"
    assert r["columns"] == fleet.PORT_COLUMNS
    assert r["total"] == 8 and len(r["ports"]) == 8 and "next" not in r
    p2 = next(p for p in rows(r) if (p["switch"], p["port"]) == (1, 2))
    assert p2 == {
        "switch": 1,
        "port": 2,
        "machines": ["pi-sw1-p2"],
        "link_up": True,
        "speed": "1G",
        "poe_state": "delivering",
        "watts": "4.2",
        "vlan": "2102",
        "lldp_name": "pi-sw1-p2",
        "lldp_port": "eth-uplink",
        "rx_errors": "0",
        "tx_errors": "0",
        "net_ok": True,
        "net_title": p2["net_title"],
        "badge_words": "link up at 1G",
        "empty": False,
    }
    no_actions(r)


def test_switch_ports_filters():
    one = fleet.switch_ports(switches(), HOSTNAMES, switch=2)
    assert {p["switch"] for p in rows(one)} == {2} and [s["index"] for s in one["switches"]] == [2]
    by_name = fleet.switch_ports(switches(), HOSTNAMES, switch="sw-netgear-s3300-1")
    assert by_name["ports"] == one["ports"]
    port = fleet.switch_ports(switches(), HOSTNAMES, switch=1, port=10)
    assert [(p["switch"], p["port"]) for p in rows(port)] == [(1, 10)]
    across = fleet.switch_ports(switches(), HOSTNAMES, port=2)
    assert [(p["switch"], p["port"]) for p in rows(across)] == [(1, 2), (2, 2)]
    problems = fleet.switch_ports(switches(), HOSTNAMES, problems=True)
    found = {(p["switch"], p["port"]) for p in rows(problems)}
    # 2/2 and 2/42 are not as they should be; 1/11 and 1/14 have errors counted
    assert found == {(1, 11), (1, 14), (2, 2), (2, 42)}
    with pytest.raises(LookupError, match="no switch 7"):
        fleet.switch_ports(switches(), HOSTNAMES, switch=7)


def test_switch_ports_of_a_board():
    r = fleet.switch_ports(switches(), HOSTNAMES, board=("00000000665fad00", "pi-sw2-p11"))
    assert [(p["switch"], p["port"], p["machines"]) for p in rows(r)] == [(2, 11, ["pi-sw2-p11"])]
    assert [s["index"] for s in r["switches"]] == [2]
    with pytest.raises(LookupError, match="no switch port"):
        fleet.switch_ports(switches(), HOSTNAMES, board=("ffff", "pi-sw9-p9"))


def test_switch_ports_paged_and_cut_at_the_limit(monkeypatch):
    page = fleet.switch_ports(switches(), HOSTNAMES, limit=3, offset=2)
    assert page["total"] == 8 and page["offset"] == 2 and len(page["ports"]) == 3 and page["next"] == 5
    monkeypatch.setattr(fleet, "LIMIT", 2500)
    cut = fleet.switch_ports(switches(), HOSTNAMES)
    assert size(cut) <= 2500 and len(cut["ports"]) < 8
    assert cut["next"] == len(cut["ports"]) and f"offset={cut['next']}" in cut["more"]
