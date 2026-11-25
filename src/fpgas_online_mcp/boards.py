"""Board identity and the merged inventory of the three sites. Read-only: no ssh, no in-use checks."""

import asyncio
from dataclasses import dataclass, field

from .cache import Entry
from .sites import SITES, SiteClient, SiteError

NOTES = {
    "welland": "every registered machine, including missing ones",
    "ps1": "only hosts offered now; absence says nothing",
    "tinytapeout": "catalogue; condition from status.json",
}


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


async def _welland(sites: SiteClient) -> list[Board]:
    machines, entry = await sites.welland_registry()
    chosen: dict[str, dict] = {}
    for m in machines:
        name = m.get("hostname")
        if isinstance(name, str) and name and (name not in chosen or _rank(m) > _rank(chosen[name])):
            chosen[name] = m
    boards = []
    for name, m in chosen.items():
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


async def _tt_board(sites: SiteClient, slug: str, cat: Entry) -> Board:
    try:
        status, entry = await sites.tt_status(slug)
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


async def _tinytapeout(sites: SiteClient) -> list[Board]:
    slugs, cat = await sites.tt_slugs()
    return list(await asyncio.gather(*(_tt_board(sites, s, cat) for s in slugs)))


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
