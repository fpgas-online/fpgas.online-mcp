"""Board identity and the merged inventory of the three sites. Read-only: no ssh, no in-use checks."""

import asyncio
import re
from dataclasses import dataclass, field

from .cache import Entry
from .sites import SITES, BoardPage, SiteClient, SiteError

NOTES = {
    "welland": "every registered machine, including missing ones; fleet_details says why one is not offered",
    "ps1": "only hosts offered now; absence says nothing",
    "tinytapeout": "catalogue; condition from status.json",
}

# Every Tiny Tapeout board is plugged into a Pi of this site; the board page names which one.
TT_HOST_SITE = "welland"

_WELLAND_NAME = re.compile(r"pi-sw([0-9]+)-p([0-9]+)")
_PS1_NAME = re.compile(r"pi([0-9]+)")
_POSITIVE = re.compile(r"[1-9][0-9]*")


@dataclass(frozen=True)
class BoardId:
    site: str
    name: str

    def __str__(self) -> str:
        return f"{self.site}:{self.name}"

    @classmethod
    def parse(cls, s: str, known: list["BoardId"]) -> "BoardId":
        s = s.strip()
        if ":" in s:
            site, _, name = s.partition(":")
            if site not in SITES:
                raise ValueError(f"unknown site {site!r}; sites are {', '.join(SITES)}")
            board_id = cls(site, name)
            if known and board_id not in known:
                raise ValueError(f"no such board {board_id}; use list_boards")
            return board_id
        candidates = [b for b in known if b.name == s]
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise ValueError(f"no such board {s!r}; use list_boards")
        raise ValueError(f"{s!r} is ambiguous; use one of: {', '.join(str(b) for b in candidates)}")


def switch_port(site: str, name: str) -> tuple[int, int | None] | None:
    """(port, switch) a welland or ps1 board sits on, from its name (ps1 has one switch: None); else None.

    welland `pi-sw<switch>-p<port>`, ps1 `pi<port>`. The power toggle and the ssh port both come from
    here, so the port that is cycled and the host that ssh reaches can never disagree.
    """
    if site == "welland" and (m := _WELLAND_NAME.fullmatch(name)):
        return int(m[2]), int(m[1])
    if site == "ps1" and (m := _PS1_NAME.fullmatch(name)):
        return int(m[1]), None
    return None


def ssh_port(site: str, name: str) -> int | None:
    """The ssh port the site forwards to this board: welland `<switch><2-digit port>22` (pi-sw1-p2 ->
    10222), ps1 `(100+N)*100+22` (pi3 -> 10322). None when the name does not follow the pattern."""
    sp = switch_port(site, name)
    if sp is None:
        return None
    port, switch = sp
    if switch is None:
        return (100 + port) * 100 + 22
    return int(f"{switch}{port:02d}22") if switch > 0 and port < 100 else None


async def tt_host(sites: SiteClient, slug: str) -> BoardId | None:
    """The welland Pi a Tiny Tapeout board is plugged into, from its live board page; None when the page
    names none (no Pi reports the board: it is not connected).

    The page's switch and port come from the Pi's own registration; the Pi is `pi-sw<s>-p<p>`. Read
    live every time: boards move between Pis, so the 1-day page cache could name the wrong Pi. A
    SiteError propagates (the caller decides what an unreadable page means).
    """
    return tt_host_of(await sites.board_page("tinytapeout", slug, live=True))


def tt_switch_port(page: BoardPage) -> tuple[int, int] | None:
    """(switch, port) a Tiny Tapeout board page names, or None unless both are plain positive integers.

    The one parser for these attributes: the Pi lookup and the power toggle must agree on what the
    page says, so a value like "07" is refused by both rather than read differently.
    """
    switch, port = page.tt_switch or "", page.tt_port or ""
    if not (_POSITIVE.fullmatch(switch) and _POSITIVE.fullmatch(port)):
        return None
    return int(switch), int(port)


def tt_host_of(page: BoardPage) -> BoardId | None:
    """The welland Pi a Tiny Tapeout board page names (its switch and port), or None."""
    sp = tt_switch_port(page)
    return None if sp is None else BoardId(TT_HOST_SITE, f"pi-sw{sp[0]}-p{sp[1]}")


def carries_tt(board: "Board") -> bool:
    """A welland Pi whose registry boards include a Tiny Tapeout one (type "TT ...")."""
    return board.site == "welland" and any(t.strip().startswith("TT") for t in (board.type or "").split(","))


def host_changed(board: BoardId, live: BoardId, held: str | None) -> str:
    """Why a Tiny Tapeout claim no longer covers its board: the board is on another Pi than the claim holds.

    A claim holds the Pi the board was on when it was claimed (that Pi passed the in-use check and
    is the one other claims are kept off); a claim is never moved to a new Pi behind the holder's back.
    """
    return (
        f"{board} is now on {live} but this claim holds {held or 'no Pi'}; release this claim and claim the "
        "board again (that checks the new Pi)"
    )


@dataclass(frozen=True)
class Board:
    id: BoardId
    site: str
    name: str
    type: str | None
    condition: str
    offered: bool | None
    last_seen: str | None
    boot_id: str | None
    usb_serial: str | None
    as_of: str
    stale: bool

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "site": self.site,
            "name": self.name,
            "type": self.type,
            "condition": self.condition,
            "offered": self.offered,
            "last_seen": self.last_seen,
            "boot_id": self.boot_id,
            "usb_serial": self.usb_serial,
            "as_of": self.as_of,
            "stale": self.stale,
        }


@dataclass
class Inventory:
    boards: list[Board] = field(default_factory=list)
    notes: dict[str, str] = field(default_factory=dict)


def _board(site: str, name: str, entry: Entry, *, stale: bool | None = None, **fields) -> Board:
    values = {"type": None, "offered": None, "last_seen": None, "boot_id": None, "usb_serial": None}
    values.update(fields)
    return Board(
        BoardId(site, name),
        site,
        name,
        as_of=entry.as_of.isoformat(),
        stale=entry.stale if stale is None else stale,
        **values,
    )


def _rank(machine: dict) -> tuple:
    """Order duplicates: a non-missing entry beats a missing one, then the most recently seen."""
    return (machine.get("condition") != "missing", str(machine.get("last_seen") or ""))


def _chosen(machines: list[dict]) -> dict[str, dict]:
    """One registry entry per hostname (see _rank)."""
    chosen: dict[str, dict] = {}
    for m in machines:
        name = m.get("hostname")
        if isinstance(name, str) and name and (name not in chosen or _rank(m) > _rank(chosen[name])):
            chosen[name] = m
    return chosen


async def _welland(sites: SiteClient) -> list[Board]:
    machines, entry = await sites.welland_registry()
    boards = []
    for name, m in _chosen(machines).items():
        types = [b["type"] for b in m.get("boards") or [] if isinstance(b, dict) and b.get("type")]
        network = m.get("network") or {}
        boot_id = network.get("boot_id") if network.get("current_boot") is True else None
        boards.append(
            _board(
                "welland",
                name,
                entry,
                type=", ".join(dict.fromkeys(types)) or None,
                condition=str(m.get("condition") or "unknown"),
                offered=m.get("offered"),
                last_seen=m.get("last_seen"),
                boot_id=boot_id,
            )
        )
    return boards


async def _ps1(sites: SiteClient) -> list[Board]:
    hosts, entry = await sites.ps1_hosts()
    return [_board("ps1", h, entry, condition="offered", offered=True) for h in hosts]


def _tt_type(status: dict) -> str | None:
    chip = (status.get("board") or {}).get("chip")
    if status.get("kind") == "fpga":
        return "FPGA board"
    if chip == "asic":
        return "chip board"
    return None


async def _tt_board(sites: SiteClient, slug: str, cat: Entry, live: bool = False) -> Board:
    try:
        status, entry = await sites.tt_status(slug, live=live)
    except SiteError:
        return _board("tinytapeout", slug, cat, condition="unknown")
    stale = cat.stale or entry.stale
    if status.get("reachable") is not True:
        return _board("tinytapeout", slug, entry, stale=stale, condition="not connected")
    return _board(
        "tinytapeout",
        slug,
        entry,
        stale=stale,
        type=_tt_type(status),
        condition="connected",
        usb_serial=(status.get("board") or {}).get("usb_serial"),
    )


async def _tinytapeout(sites: SiteClient, live: bool = False) -> list[Board]:
    slugs, cat = await sites.tt_slugs(live=live)
    return list(await asyncio.gather(*(_tt_board(sites, s, cat, live) for s in slugs)))


async def tt_serials_on(sites: SiteClient, name: str) -> list[str | None]:
    """The ids (USB serials) of the Tiny Tapeout boards (type "TT ...") the live welland registry puts on
    Pi `name`; [] when it carries none. An id is None when the Pi did not report one."""
    machines, _ = await sites.welland_registry(live=True)
    entry = _chosen(machines).get(name) or {}
    return [
        b.get("id")
        for b in entry.get("boards") or []
        if isinstance(b, dict) and str(b.get("type") or "").startswith("TT")
    ]


async def tt_board_with_serial(sites: SiteClient, serials: list[str | None], live: bool = False) -> Board | None:
    """The Tiny Tapeout site's board whose USB serial is one of `serials`, or None when the site lists
    none of them. SiteError when the catalogue, or the status of a board that could be the one, cannot
    be read: "not listed" must not be said of a board the site could not show."""
    boards = await _tinytapeout(sites, live)
    found = next((b for b in boards if b.usb_serial and b.usb_serial in serials), None)
    if found is None and any(b.condition == "unknown" for b in boards):
        raise SiteError("tinytapeout: could not read every board's status.json, so cannot tell which is on this Pi")
    return found


_LOADERS = {"welland": _welland, "ps1": _ps1, "tinytapeout": _tinytapeout}


async def inventory(sites: SiteClient, site: str | None = None) -> Inventory:
    if site is not None and site not in SITES:
        raise ValueError(f"unknown site {site!r}; sites are {', '.join(SITES)}")
    wanted = [site] if site else list(SITES)
    results = await asyncio.gather(*(_LOADERS[s](sites) for s in wanted), return_exceptions=True)
    inv = Inventory()
    for s, result in zip(wanted, results, strict=True):
        if isinstance(result, SiteError):
            inv.notes[s] = f"unavailable: {result}; {NOTES[s]}"
        elif isinstance(result, BaseException):
            raise result
        else:
            inv.boards.extend(sorted(result, key=lambda b: b.name))
            inv.notes[s] = NOTES[s]
    return inv
