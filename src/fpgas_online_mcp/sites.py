"""Client for the public pages and the power endpoint of the three fpgas.online sites."""

import base64
import binascii
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import httpx

from . import __version__
from .cache import Cache, Entry

log = logging.getLogger(__name__)

SITES = ("welland", "ps1", "tinytapeout")
USER_AGENT = f"fpgas-online-mcp/{__version__}"
TIMEOUT_S = 15.0

TTL_REGISTRY_S = 60
TTL_SWITCHES_S = 60
TTL_PS1_LIST_S = 60
TTL_TT_HOME_S = 300
TTL_TT_STATUS_S = 10
TTL_BOARD_PAGE_S = 86400

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class SiteError(Exception):
    """A site could not be read or answered badly. The message names the site.

    `status` is the HTTP status when the site answered with one (a 404 says the page does not
    exist, which callers treat differently from a site that is down), else None.
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class BoardPage:
    password: str | None = field(default=None, repr=False)
    ssh_port: int | None = None
    tt_port: str | None = None
    tt_switch: str | None = None


@dataclass(frozen=True)
class ToggleResult:
    status: int
    body: dict
    retry_after: int | None


def new_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT_S, headers={"User-Agent": USER_AGENT}, follow_redirects=True)


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _parse_registry(data: bytes) -> list[dict]:
    """The registry's machines; an entry that is not an object is skipped with a warning."""
    machines = json.loads(data)["machines"]
    if not isinstance(machines, list):
        raise ValueError("machines is not a list")
    good = [m for m in machines if isinstance(m, dict)]
    if len(good) < len(machines):
        log.warning("welland registry: skipped %d entries that are not objects", len(machines) - len(good))
    return good


def _parse_switches(data: bytes) -> list[dict]:
    """The switches of welland's management page (read only: the page's actions are never used)."""
    switches = json.loads(data)["switches"]
    if not isinstance(switches, list) or not all(isinstance(s, dict) for s in switches):
        raise ValueError("switches is not a list of objects")
    return switches


def _unique(items) -> list[str]:
    return list(dict.fromkeys(items))


def _parse_hosts(pattern: str, data: bytes) -> list[str]:
    text = _text(data)
    if "</html>" not in text.lower():
        raise ValueError("not an HTML page")
    return _unique(re.findall(rf'href="({pattern})\.html"', text))


def _parse_ps1_hosts(data: bytes) -> list[str]:
    return _parse_hosts(r"pi\d+", data)


def _parse_welland_hosts(data: bytes) -> list[str]:
    return _parse_hosts(r"pi-sw\d+-p\d+", data)


def _parse_tt_slugs(data: bytes) -> list[str]:
    text = _text(data)
    if "</html>" not in text.lower():
        raise ValueError("not an HTML page")
    return _unique(re.findall(r'/board/([^/"]+)/', text))


def _parse_tt_status(data: bytes) -> dict:
    status = json.loads(data)
    if not isinstance(status, dict) or "reachable" not in status:
        raise ValueError("no reachable key")
    return status


def _parse_board_page(site: str, data: bytes) -> BoardPage:
    text = _text(data)
    if site == "tinytapeout":
        port = re.search(r'data-port="([^"]*)"', text)
        switch = re.search(r'data-switch="([^"]*)"', text)
        if port is None or switch is None:
            raise ValueError("no data-port/data-switch")
        return BoardPage(tt_port=port[1] or None, tt_switch=switch[1] or None)
    match = re.search(r"/wssh/\?[^\"'\s]*?password=([^&\"'\s]+)", text)
    if match is None:
        raise ValueError("no password parameter")
    try:
        password = base64.b64decode(unquote(match[1]), validate=True).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError("password parameter is not base64") from exc
    if not password:
        raise ValueError("empty password")
    # Only the "Use your own ssh client ... host: <site>.fpgas.online, port N" line and the
    # "ssh [-4] -p N pi@<site>.fpgas.online" command count; they must agree.
    ports = set(re.findall(r"host:\s*[\w.-]+\.fpgas\.online,\s*port (\d+)\b", text))
    ports |= set(re.findall(r"\bssh (?:-\w+ )*-p (\d+) pi@[\w.-]+\.fpgas\.online\b", text))
    return BoardPage(password=password, ssh_port=int(ports.pop()) if len(ports) == 1 else None)


class SiteClient:
    def __init__(self, http: httpx.AsyncClient, cache: Cache):
        self._http = http
        self._cache = cache

    def _url(self, site: str, path: str) -> str:
        if site not in SITES:
            raise SiteError(f"unknown site {site!r}")
        return f"https://{site}.fpgas.online{path}"

    async def _fetch(self, site: str, path: str) -> bytes:
        try:
            resp = await self._http.get(self._url(site, path))
        except httpx.HTTPError as exc:
            raise SiteError(f"{site}: cannot reach the site ({type(exc).__name__})") from exc
        if resp.status_code != 200:
            raise SiteError(f"{site}: HTTP {resp.status_code} for {path}", status=resp.status_code)
        return resp.content

    async def _get(
        self,
        site: str,
        path: str,
        ttl_s: float,
        parse: Callable[[bytes], Any],
        live: bool = False,
    ) -> tuple[Any, Entry]:
        try:
            entry = await self._cache.get(
                f"{site}{path}",
                ttl_s,
                lambda: self._fetch(site, path),
                live=live,
                validate=parse,
            )
            return parse(entry.data), entry
        except SiteError:
            raise
        except Exception as exc:
            raise SiteError(f"{site}: unexpected page layout for {path}") from exc

    async def welland_registry(self, live: bool = False) -> tuple[list[dict], Entry]:
        return await self._get("welland", "/fleet/registry.json", TTL_REGISTRY_S, _parse_registry, live)

    async def welland_switches(self, live: bool = False) -> tuple[list[dict], Entry]:
        """welland's switch ports (link, PoE state, watts, the machines on each), cached like the registry.
        A plain GET of the public management page; nothing here ever switches a port."""
        return await self._get("welland", "/management/switches.json", TTL_SWITCHES_S, _parse_switches, live)

    async def ps1_hosts(self, live: bool = False) -> tuple[list[str], Entry]:
        return await self._get("ps1", "/fpgas/", TTL_PS1_LIST_S, _parse_ps1_hosts, live)

    async def welland_hosts(self, live: bool = False) -> tuple[list[str], Entry]:
        """The Pis welland's `/fpgas/` page lists (offered, with a board that site shows), in page order."""
        return await self._get("welland", "/fpgas/", TTL_PS1_LIST_S, _parse_welland_hosts, live)

    async def site_password(self, site: str) -> str:
        """The site-wide ssh password, from the page of the first Pi the site lists.

        Every Pi of a site has the same login (the site publishes it on each listed Pi's page), so
        this serves a Pi the site does not list, such as one that carries only a Tiny Tapeout board.
        The page's own ssh port must match its own name, as for any board page. Never logged.
        """
        from .boards import ssh_port  # boards imports this module

        listers = {"welland": self.welland_hosts, "ps1": self.ps1_hosts}
        if site not in listers:
            raise SiteError(f"{site}: no site-wide ssh password (this site has no ssh)")
        hosts, _ = await listers[site]()
        if not hosts:
            raise SiteError(f"{site}: the /fpgas/ page lists no Pi, so there is no page to read the password from")
        page = await self.board_page(site, hosts[0])
        expected = ssh_port(site, hosts[0])
        if not page.password or page.ssh_port is None or page.ssh_port != expected:
            raise SiteError(
                f"{site}: the page of {hosts[0]} gives ssh port {page.ssh_port} but its name means {expected}; "
                "not using its password"
            )
        return page.password

    async def tt_slugs(self, live: bool = False) -> tuple[list[str], Entry]:
        return await self._get("tinytapeout", "/", TTL_TT_HOME_S, _parse_tt_slugs, live)

    async def tt_status(self, slug: str, live: bool = False) -> tuple[dict, Entry]:
        self._check_name("tinytapeout", slug)
        return await self._get(
            "tinytapeout",
            f"/board/{slug}/status.json",
            TTL_TT_STATUS_S,
            _parse_tt_status,
            live,
        )

    async def board_page(self, site: str, name: str, live: bool = False) -> BoardPage:
        """live=True fetches now, stores the result and never serves a cached or stale page."""
        self._check_name(site, name)
        path = f"/board/{name}/" if site == "tinytapeout" else f"/fpgas/{name}.html"
        page, _ = await self._get(site, path, TTL_BOARD_PAGE_S, lambda data: _parse_board_page(site, data), live)
        return page

    async def toggle(self, site: str, port: str, switch: int | None) -> ToggleResult:
        """POST a power toggle. Never cached. The port is always sent as a string."""
        body: dict = {"port": str(port)}
        if switch is not None:
            body["switch"] = switch
        try:
            resp = await self._http.post(self._url(site, "/snmp/toggle"), json=body, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise SiteError(f"{site}: cannot reach the site ({type(exc).__name__})") from exc
        if 300 <= resp.status_code < 400:
            raise SiteError(f"{site}: toggle was redirected (HTTP {resp.status_code}); not followed or retried")
        try:
            answer = resp.json()
            if not isinstance(answer, dict):
                raise ValueError
        except ValueError:
            answer = {"error": resp.text[:200]}
        try:
            retry_after = int(resp.headers["Retry-After"])
        except (KeyError, ValueError):
            retry_after = None
        return ToggleResult(resp.status_code, answer, retry_after)

    @staticmethod
    def _check_name(site: str, name: str) -> None:
        if not _NAME.fullmatch(name):
            raise SiteError(f"{site}: invalid board name")
