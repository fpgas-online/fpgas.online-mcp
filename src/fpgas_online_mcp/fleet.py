"""The fleet_details and switch_ports results: welland's fleet and switch pages, fitted to the tool limit.

Pure functions over what the site published (machine.json, registry.json, switches.json); the
broker op reads the site and calls these. Read-only: a switch port row keeps only the fields in
PORT_COLUMNS, so the page's action fields (PoE and link switching) never reach an agent.
"""

import json
from dataclasses import dataclass
from typing import Any

from .ssh import wrap_untrusted

LIMIT = 8192  # the tool output limit
DEFAULT_LIMIT = 20  # items per list section when the caller gives no limit
SECTIONS = (
    "machine",
    "boards",
    "network",
    "switch_port",
    "boot_checks",
    "labels",
    "registrations",
    "events",
    "console",
)
DEFAULT_SECTIONS = ("machine", "boards", "network", "switch_port")
# list sections: paged, newest first by this key (None: in the site's order)
PAGED = {"boards": None, "boot_checks": None, "registrations": "last_confirmed", "events": "ts", "console": "at"}
# Sections that are text the Pi sent (anyone can log in to a Pi and plant text): each is wrapped once.
UNTRUSTED = {"boards", "boot_checks", "registrations", "events", "console"}
UNTRUSTED_MACHINE = ("registered_model", "cameras")  # machine fields the Pi reports about itself
UNTRUSTED_PORT = ("lldp_name", "lldp_port")  # what the device on a switch port announces
FALLBACK_UNAVAILABLE = ("boot_checks", "labels", "registrations", "events", "console")
PORT_COLUMNS = [
    "switch",
    "port",
    "machines",
    "link_up",
    "speed",
    "poe_state",
    "watts",
    "vlan",
    "lldp_name",
    "lldp_port",
    "rx_errors",
    "tx_errors",
    "net_ok",
    "net_title",
    "badge_words",
    "empty",
]
SWITCH_KEYS = ("index", "name", "model", "status", "read_at", "known_issues", "empty_ports")
PAGE_URL = "https://welland.fpgas.online/fleet/{serial}/"
NOT_REGISTERED = "no host with this serial has registered"  # machine.json's 404 for a serial it does not know


def _compact(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _size(obj) -> int:
    return len(_compact(obj).encode())


def parse_sections(value: Any) -> list[str]:
    """The sections asked for: None the defaults, "all" (alone or in a list) every one."""
    if value is None:
        return list(DEFAULT_SECTIONS)
    names = [value] if isinstance(value, str) else value
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ValueError("sections must be a list of section names")
    names = [n.strip() for n in names]
    if "all" in names:
        return list(SECTIONS)
    unknown = [n for n in names if n not in SECTIONS]
    if unknown:
        raise ValueError(f"unknown section(s) {', '.join(unknown)}; sections are {', '.join(SECTIONS)} or all")
    return list(dict.fromkeys(names))


def hostnames(machines: list[dict]) -> dict[str, str]:
    """serial -> hostname for every registry entry (switch rows name machines by serial only)."""
    return {
        m["serial"]: m["hostname"]
        for m in machines
        if isinstance(m.get("serial"), str) and isinstance(m.get("hostname"), str)
    }


def port_view(row: dict, names: dict[str, str], switch: int | None = None) -> dict:
    """A switch port row cut to PORT_COLUMNS; machines by host name (else serial)."""
    view: dict = {} if switch is None else {"switch": switch}
    for key in PORT_COLUMNS[1:]:
        if key not in row:
            continue
        value = row[key]
        if key == "machines":
            value = [names.get(m.get("serial"), m.get("serial")) if isinstance(m, dict) else m for m in value or []]
        elif key in UNTRUSTED_PORT and value not in (None, ""):
            value = wrap_untrusted(value if isinstance(value, str) else _compact(value))
        view[key] = value
    return view


def find_port(switches: list[dict], serial: str | None, hostname: str | None) -> tuple[int | None, dict] | None:
    """(switch index, row) of the port a machine is on: the row listing its serial, else the row whose
    LLDP name is the host name; None when no row has it."""
    by_name = None
    for s in switches:
        for row in s.get("ports") or []:
            if not isinstance(row, dict):
                continue
            serials = {m.get("serial") for m in row.get("machines") or [] if isinstance(m, dict)}
            if serial and serial in serials:
                return s.get("index"), row
            if by_name is None and hostname and row.get("lldp_name") == hostname:
                by_name = (s.get("index"), row)
    return by_name


def fallback(entry: dict, port: tuple[int | None, dict] | None, port_error: str | None = None) -> dict:
    """A machine.json-shaped dict from the registry entry and its switch port, for a site without
    machine.json (it answers 404). The sections only machine.json has are listed as unavailable."""
    serial = entry.get("serial")
    data = {
        "serial": serial,
        "page": PAGE_URL.format(serial=serial),
        "machine": {k: v for k, v in entry.items() if k not in ("boards", "network")},
        "boards": entry.get("boards") or [],
        "network": entry.get("network"),
        "switch_port": None,
        "unavailable": list(FALLBACK_UNAVAILABLE),
    }
    if port is not None:
        data["switch_port"] = port[1]
        data["switch_index"] = port[0]
    else:
        data["switch_port_error"] = port_error or "no switch port lists this Pi now"
    return data


CLIP_START = 1024  # a string longer than this is the first cut when one item is too big
CLIP_MIN = 64  # below this a section is left out instead
_OPEN, _CLOSE = wrap_untrusted("").split("</", 1)[0], "</board-output>"


def _clip(obj: Any, cap: int, page: str) -> Any:
    """obj with every string longer than cap bytes cut to cap, marked with how much and where the rest is.
    A wrapped (untrusted) string keeps its wrapper whole."""
    if isinstance(obj, dict):
        return {k: _clip(v, cap, page) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clip(v, cap, page) for v in obj]
    if not isinstance(obj, str):
        return obj
    wrapped = obj.startswith(_OPEN) and obj.endswith(_CLOSE)
    inner = obj[len(_OPEN) : -len(_CLOSE)] if wrapped else obj
    data = inner.encode()
    mark = f"[\u2026 {len(data) - cap} bytes cut; full text on the page {page}]"
    if len(data) <= cap + len(mark.encode()):
        return obj
    inner = data[:cap].decode(errors="ignore") + mark
    return f"{_OPEN}{inner}{_CLOSE}" if wrapped else inner


@dataclass
class _Section:
    name: str
    value: Any
    total: int = 0
    offset: int = 0
    shown: int = 0
    asked: int = 0  # how many the caller asked for (shown before any cut)
    extra: dict | None = None  # console: its other fields
    dropped: bool = False
    cap: int | None = None  # strings are cut to this many bytes (an item too big for the limit)
    page: str = ""

    @property
    def paged(self) -> bool:
        return self.name in PAGED and isinstance(self.value, list)

    def items(self) -> list:
        items = self.value[: self.shown]
        return items if self.cap is None else [_clip(i, self.cap, self.page) for i in items]

    def largest_item(self) -> int:
        return max((_size(i) for i in self.value[: self.shown]), default=0)

    def render(self) -> Any:
        if not self.paged:
            return self.value if self.cap is None else _clip(self.value, self.cap, self.page)
        items = self.items()
        if self.name in UNTRUSTED:
            items = wrap_untrusted("\n".join(_compact(i) for i in items)) if items else []
        if self.extra is not None:
            return {**self.extra, "lines": items}
        return items


def _newest_first(items: list, key: str | None) -> list:
    if key is None:
        return list(items)
    return sorted(items, key=lambda i: str(i.get(key) or "") if isinstance(i, dict) else "", reverse=True)


def _section(name: str, data: dict, names: dict[str, str], offset: int, limit: int) -> _Section:
    value = data.get(name)
    extra = None
    if name == "machine" and isinstance(value, dict):
        value = {
            k: wrap_untrusted(v if isinstance(v, str) else _compact(v))
            if k in UNTRUSTED_MACHINE and v is not None
            else v
            for k, v in value.items()
        }
    elif name == "network" and value is None and data.get("network_error"):
        value = {"error": data["network_error"]}
    elif name == "switch_port" and isinstance(value, dict):
        value = port_view(value, names, data.get("switch_index"))
    elif name == "switch_port" and value is None and data.get("switch_port_error"):
        value = {"error": data["switch_port_error"]}
    elif name == "console":
        if isinstance(value, dict):
            extra = {k: v for k, v in value.items() if k != "lines"}
            value = value.get("lines") or []
        else:
            value = {k: data[f"console_{k}"] for k in ("error", "not_set_up") if data.get(f"console_{k}")} or None
    if name in PAGED and isinstance(value, list):
        items = _newest_first(value, PAGED[name])
        shown = max(0, min(limit, len(items) - offset))
        return _Section(name, items[offset:], len(items), offset, shown, shown, extra)
    return _Section(name, value)


def details(
    data: dict,
    sections: list[str],
    names: dict[str, str],
    offset: int = 0,
    limit: int | None = None,
    head: dict | None = None,
) -> dict:
    """The fleet_details result for these sections, at most LIMIT bytes.

    List sections are paged (newest first) with `paging` saying how many there are and the next
    offset. Too long: the longest section is cut first (a list to fewer items; text longer than the
    rest allows cut, marked, with the page for the full text; left out only when that is not enough),
    and `cut` says what was cut and how to get it. `head` goes first (the board, its Pi, as_of).
    """
    limit = DEFAULT_LIMIT if limit is None else limit
    serial = data.get("serial")
    page = data.get("page") or PAGE_URL.format(serial=serial)
    head = {**(head or {}), "serial": serial, "page": page}
    if data.get("generated_at"):
        head["as_of"] = data["generated_at"]
    unavailable = [s for s in sections if s in (data.get("unavailable") or [])]
    secs = [_section(n, data, names, offset, limit) for n in sections if n not in unavailable]
    for s in secs:
        s.page = page

    def build() -> dict:
        result = dict(head)
        paging = {}
        for s in secs:
            if s.dropped:
                continue
            result[s.name] = s.render()
            if s.paged:
                paging[s.name] = {"total": s.total, "offset": s.offset, "shown": s.shown}
                if s.offset + s.shown < s.total:
                    paging[s.name]["next"] = s.offset + s.shown
        if paging:
            result["paging"] = paging
        cut = {}
        for s in secs:
            if s.dropped:
                cut[s.name] = (
                    f"left out to fit {LIMIT // 1024} KB; call fleet_details with sections=['{s.name}'] alone, "
                    f"or see the page {page}"
                )
            else:
                notes = []
                if s.cap is not None:
                    notes.append(
                        f"long text cut to {s.cap} bytes per field to fit {LIMIT // 1024} KB; the full text is on "
                        f"the page {page}"
                    )
                if s.paged and s.shown < s.asked:
                    notes.append(
                        f"{s.shown} of {s.total} shown to fit {LIMIT // 1024} KB; for more call fleet_details with "
                        f"sections=['{s.name}'], offset={s.offset + s.shown}, limit={max(1, s.shown)}"
                    )
                if notes:
                    cut[s.name] = "; ".join(notes)
        if cut:
            result["cut"] = cut
        if unavailable:
            result["unavailable"] = unavailable
        if data.get("unavailable"):
            result["note"] = (
                f"welland does not publish /fleet/{serial}/machine.json yet: these are the registry entry and its "
                f"switch port row. The full details (boot checks, labels, registrations, events, console) need "
                f"the newer site; the page shows them: {page}"
            )
        return result

    while _size(build()) > LIMIT:
        live = [s for s in secs if not s.dropped]
        if not live:
            break
        biggest = max(live, key=lambda s: _size(s.render()))
        if biggest.paged and biggest.cap is None and biggest.largest_item() > LIMIT // 4:
            biggest.cap = CLIP_START  # one item alone would crowd out the rest: cut its long text first
        elif biggest.paged and biggest.shown > 1:
            _shrink(biggest, build)
        elif biggest.cap is None or biggest.cap > CLIP_MIN:
            biggest.cap = CLIP_START if biggest.cap is None else biggest.cap // 2
        else:
            biggest.dropped = True
    return build()


def _shrink(section: _Section, build) -> None:
    """Show as many items of section as fit (binary search); if even one does not, halve it."""
    lo, hi, full = 1, section.shown - 1, section.shown
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        section.shown = mid
        if _size(build()) <= LIMIT:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    section.shown = best if best is not None else max(1, full // 2)


# --- switch_ports ---------------------------------------------------------------------------------


def _counted(value: Any) -> bool:
    """An error counter that is not zero (the site gives counters as strings; '' is unknown)."""
    text = str(value if value is not None else "").strip()
    return text not in ("", "0")


def _problem(row: dict) -> bool:
    return row.get("net_ok") is False or _counted(row.get("rx_errors")) or _counted(row.get("tx_errors"))


def _switch_matches(s: dict, switch: int | str | None) -> bool:
    if switch is None:
        return True
    return s.get("index") == switch or (isinstance(switch, str) and s.get("name") == switch)


def switch_ports(
    switches: list[dict],
    names: dict[str, str],
    *,
    switch: int | str | None = None,
    port: int | None = None,
    board: tuple[str | None, str] | None = None,
    problems: bool = False,
    offset: int = 0,
    limit: int | None = None,
    head: dict | None = None,
) -> dict:
    """Every switch (summary) and its ports (compact rows in PORT_COLUMNS order), filtered, paged and
    at most LIMIT bytes. board is (serial, hostname). LookupError when the switch or board is not found."""
    chosen = [s for s in switches if _switch_matches(s, switch)]
    if not chosen:
        known = ", ".join(f"{s.get('index')} ({s.get('name')})" for s in switches)
        raise LookupError(f"no switch {switch}; the switches are {known}")
    target = None
    if board is not None:
        target = find_port(chosen, *board)
        if target is None:
            raise LookupError(f"no switch port lists {board[1]} now")
    rows, used = [], []
    for s in chosen:
        mine = []
        for row in s.get("ports") or []:
            if not isinstance(row, dict):
                continue
            if port is not None and row.get("port") != port:
                continue
            if target is not None and (s.get("index"), row) != target:
                continue
            if problems and not _problem(row):
                continue
            view = port_view(row, names, s.get("index"))
            mine.append([view.get(c) for c in PORT_COLUMNS])
        rows += mine
        if mine or (target is None and port is None and not problems):
            used.append({k: s.get(k) for k in SWITCH_KEYS})
    if not used:
        used = [{k: s.get(k) for k in SWITCH_KEYS} for s in chosen]
    rest = rows[offset:]
    asked = len(rest) if limit is None else min(limit, len(rest))
    level = 0  # how far the switch summaries are shortened (_summaries)

    def build(n: int) -> dict:
        result = {
            "site": "welland",
            **(head or {}),
            "switches": _summaries(used, level),
            "columns": PORT_COLUMNS,
            "ports": rest[:n],
            "total": len(rows),
            "offset": offset,
        }
        if offset + n < len(rows):
            result["next"] = offset + n
        if n < asked:
            result["more"] = (
                f"{n} of {len(rest)} ports shown to fit {LIMIT // 1024} KB; for more call switch_ports again with "
                f"offset={offset + n}, or filter by switch, port, board or problems"
            )
        if level:
            result["note"] = (
                "the switches' known issues are shortened to fit; switch_ports with switch=<index> and port=<n> "
                "has room for them, and https://welland.fpgas.online/management/switches/ shows them"
            )
        return result

    # The summaries alone must leave room for at least one port (else `more` would repeat this offset).
    while level < len(_SUMMARY_LEVELS) - 1 and _size(build(min(1, asked))) > LIMIT:
        level += 1
    lo, hi = 0, asked
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _size(build(mid)) <= LIMIT:
            lo = mid
        else:
            hi = mid - 1
    return build(max(lo, min(1, asked)))  # never 0 of a non-empty page: the front end's backstop cuts text


_SUMMARY_LEVELS = ("full", "issue links", "issue count", "index and name")


def _summaries(used: list[dict], level: int) -> list[dict]:
    """The switch summaries, shortened: known_issues to issue/url, then to a count, then index and name only."""
    if level == 0:
        return used
    if level == 3:
        return [{"index": s.get("index"), "name": s.get("name")} for s in used]
    out = []
    for s in used:
        issues = s.get("known_issues") or []
        if level == 1:
            short = [{"issue": i.get("issue"), "url": i.get("url")} if isinstance(i, dict) else i for i in issues]
        else:
            short = len(issues)
        out.append({**s, "known_issues": short})
    return out
