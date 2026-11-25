"""Site client and cache, served from fixtures captured on 2026-11-26."""

import asyncio
import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

import fpgas_online_mcp
from fpgas_online_mcp.cache import Cache
from fpgas_online_mcp.sites import BoardPage, SiteClient, SiteError

FIX = Path(__file__).parent / "fixtures"

PAGES = {
    ("welland.fpgas.online", "/fleet/registry.json"): "welland_registry.json",
    ("welland.fpgas.online", "/fpgas/"): "welland_fpgas.html",
    ("welland.fpgas.online", "/fpgas/pi-sw1-p2.html"): "welland_board.html",
    ("ps1.fpgas.online", "/fpgas/"): "ps1_fpgas.html",
    ("ps1.fpgas.online", "/fpgas/pi3.html"): "ps1_board.html",
    ("tinytapeout.fpgas.online", "/"): "tt_home.html",
    ("tinytapeout.fpgas.online", "/board/tt-ab2a48a7ee008dd9/status.json"): ("tt_status_reachable.json"),
    ("tinytapeout.fpgas.online", "/board/tt06/status.json"): "tt_status_unreachable.json",
    ("tinytapeout.fpgas.online", "/board/tt-ab2a48a7ee008dd9/"): "tt_board.html",
}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class Site:
    """MockTransport handler serving fixtures; records requests; can be told to fail."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.override: dict[tuple[str, str], httpx.Response] = {}
        self.fail = False
        self.delay = 0.0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise httpx.ConnectError("boom", request=request)
        key = (request.url.host, request.url.path)
        if key in self.override:
            return self.override[key]
        name = PAGES.get(key)
        if name is None:
            return httpx.Response(404, text="Not Found")
        return httpx.Response(200, content=(FIX / name).read_bytes())

    def count(self, path):
        return sum(r.url.path == path for r in self.requests)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def site():
    return Site()


@pytest.fixture
async def client(tmp_path, clock, site):
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(site),
        headers={"User-Agent": f"fpgas-online-mcp/{fpgas_online_mcp.__version__}"},
    )
    yield SiteClient(http, Cache(tmp_path / "cache", clock))
    await http.aclose()


async def test_welland_registry(client):
    machines, entry = await client.welland_registry()
    assert any(m["hostname"] == "pi-sw1-p2" for m in machines)
    assert {"condition", "offered", "last_seen"} <= machines[0].keys()
    assert entry.stale is False


async def test_ps1_hosts(client):
    hosts, _ = await client.ps1_hosts()
    assert "pi3" in hosts and all(h.startswith("pi") for h in hosts)
    assert len(hosts) == len(set(hosts))


async def test_tt_slugs(client):
    slugs, _ = await client.tt_slugs()
    assert {"fpga-2", "tt06", "tt-ab2a48a7ee008dd9"} <= set(slugs)


async def test_tt_status(client):
    up, _ = await client.tt_status("tt-ab2a48a7ee008dd9")
    assert up["reachable"] is True and up["board"]["present"] is True
    down, _ = await client.tt_status("tt06")
    assert down["reachable"] is False


async def test_board_page_welland(client):
    page = await client.board_page("welland", "pi-sw1-p2")
    assert page == BoardPage(password="REDACTED", ssh_port=10222)


async def test_board_page_ps1(client):
    page = await client.board_page("ps1", "pi3")
    assert page.password == "REDACTED" and page.ssh_port == 10322


async def test_board_page_tinytapeout(client):
    page = await client.board_page("tinytapeout", "tt-ab2a48a7ee008dd9")
    assert page == BoardPage(tt_port="11", tt_switch="2")


async def test_password_not_in_repr(client):
    page = await client.board_page("welland", "pi-sw1-p2")
    assert "REDACTED" not in repr(page)


async def test_board_page_cache_file_is_private(client, tmp_path):
    await client.board_page("welland", "pi-sw1-p2")
    files = list((tmp_path / "cache").glob("*.entry"))
    assert files and all(stat.S_IMODE(f.stat().st_mode) == 0o600 for f in files)


async def test_invalid_name_rejected(client, site):
    with pytest.raises(SiteError, match="welland"):
        await client.board_page("welland", "../x")
    assert not site.requests


async def test_user_agent_header(client, site):
    await client.ps1_hosts()
    assert site.requests[0].headers["user-agent"] == (f"fpgas-online-mcp/{fpgas_online_mcp.__version__}")


async def test_cache_ttl(client, site, clock):
    await client.tt_status("tt06")
    clock.advance(9)
    await client.tt_status("tt06")
    assert site.count("/board/tt06/status.json") == 1
    clock.advance(2)
    _, entry = await client.tt_status("tt06")
    assert site.count("/board/tt06/status.json") == 2
    assert entry.as_of == clock.now


async def test_persisted_across_instances(tmp_path, clock, site):
    async with httpx.AsyncClient(transport=httpx.MockTransport(site)) as http:
        await SiteClient(http, Cache(tmp_path, clock)).ps1_hosts()
        _, entry = await SiteClient(http, Cache(tmp_path, clock)).ps1_hosts()
    assert site.count("/fpgas/") == 1
    assert entry.as_of == clock.now


async def test_single_flight(client, site):
    site.delay = 0.05
    results = await asyncio.gather(*(client.welland_registry() for _ in range(10)))
    assert site.count("/fleet/registry.json") == 1
    assert len({id(r[1]) for r in results}) == 1


async def test_stale_on_error(client, site, clock):
    await client.welland_registry()
    clock.advance(120)
    site.fail = True
    machines, entry = await client.welland_registry()
    assert machines and entry.stale is True
    assert entry.as_of == clock.now - timedelta(seconds=120)


async def test_fetch_failure_without_cache_raises_site_error(client, site):
    site.fail = True
    with pytest.raises(SiteError, match="welland"):
        await client.welland_registry()


async def test_http_error_status_raises_site_error(client):
    with pytest.raises(SiteError, match="HTTP 404"):
        await client.tt_status("nope")


async def test_live_bypasses_cache(client, site):
    await client.tt_status("tt06")
    await client.tt_status("tt06", live=True)
    assert site.count("/board/tt06/status.json") == 2
    await client.tt_status("tt06")
    assert site.count("/board/tt06/status.json") == 2  # live result was stored


async def test_live_failure_is_not_served_stale(client, site):
    await client.tt_status("tt06")
    site.fail = True
    with pytest.raises(SiteError):
        await client.tt_status("tt06", live=True)


async def test_parse_failure_keeps_previous(client, site, clock):
    good, first = await client.welland_registry()
    clock.advance(120)
    site.override[("welland.fpgas.online", "/fleet/registry.json")] = httpx.Response(200, text="<html>garbage</html>")
    machines, entry = await client.welland_registry()
    assert machines == good
    assert entry.stale is True and entry.as_of == first.as_of
    # The bad body did not replace the stored entry.
    site.override.clear()
    site.fail = True
    machines, entry = await client.welland_registry()
    assert machines == good and entry.stale is True


async def test_parse_failure_without_previous_raises(client, site):
    site.override[("welland.fpgas.online", "/fleet/registry.json")] = httpx.Response(200, text="not json")
    with pytest.raises(SiteError, match="welland.*layout"):
        await client.welland_registry()


async def test_board_page_without_password_is_a_parse_failure(client, site):
    site.override[("ps1.fpgas.online", "/fpgas/pi3.html")] = httpx.Response(200, text="<html></html>")
    with pytest.raises(SiteError, match="ps1"):
        await client.board_page("ps1", "pi3")


def toggle_site(site, response):
    seen = []

    def handler(request):
        seen.append(request)
        return response

    return seen, handler


async def make(tmp_path, handler):
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"User-Agent": "fpgas-online-mcp/test"},
    )
    return http, SiteClient(http, Cache(tmp_path, Clock()))


async def test_toggle_ok(tmp_path):
    seen, handler = toggle_site(None, httpx.Response(200, json={"2": ["off", "on"]}))
    http, c = await make(tmp_path, handler)
    result = await c.toggle("welland", "2", 1)
    await http.aclose()
    assert (result.status, result.body, result.retry_after) == (200, {"2": ["off", "on"]}, None)
    req = seen[0]
    assert req.method == "POST" and str(req.url) == "https://welland.fpgas.online/snmp/toggle"
    assert req.headers["content-type"] == "application/json"
    assert json.loads(req.content) == {"port": "2", "switch": 1}


async def test_toggle_429_retry_after(tmp_path):
    response = httpx.Response(429, headers={"Retry-After": "59"}, json={"error": "try again in 59 seconds"})
    _, handler = toggle_site(None, response)
    http, c = await make(tmp_path, handler)
    result = await c.toggle("welland", "2", 1)
    await http.aclose()
    assert result.status == 429 and result.retry_after == 59
    assert "59" in result.body["error"]


async def test_ps1_port_sent_as_string(tmp_path):
    seen, handler = toggle_site(None, httpx.Response(200, json={"21": ["off", "on"]}))
    http, c = await make(tmp_path, handler)
    await c.toggle("ps1", 21, None)  # even an int input goes out as a string
    await http.aclose()
    assert json.loads(seen[0].content) == {"port": "21"}


async def test_toggle_non_json_answer(tmp_path):
    _, handler = toggle_site(None, httpx.Response(502, text="Bad Gateway"))
    http, c = await make(tmp_path, handler)
    result = await c.toggle("tinytapeout", "11", 2)
    await http.aclose()
    assert result.status == 502 and result.body == {"error": "Bad Gateway"}


async def test_toggle_network_error(tmp_path):
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    http, c = await make(tmp_path, handler)
    with pytest.raises(SiteError, match="ps1"):
        await c.toggle("ps1", "21", None)
    await http.aclose()


async def test_failed_fetch_is_shared_without_cache(client, site):
    site.fail = True
    site.delay = 0.05
    results = await asyncio.gather(*(client.welland_registry() for _ in range(10)), return_exceptions=True)
    assert site.count("/fleet/registry.json") == 1
    assert all(isinstance(r, SiteError) for r in results)


async def test_failed_fetch_is_shared_with_old_entry(client, site, clock):
    await client.welland_registry()
    clock.advance(120)
    site.fail = True
    site.delay = 0.05
    results = await asyncio.gather(*(client.welland_registry() for _ in range(10)))
    assert site.count("/fleet/registry.json") == 2  # the first fetch plus one shared failure
    assert all(entry.stale for _, entry in results)


async def test_cache_write_failure_is_best_effort(tmp_path, clock, site, monkeypatch, caplog):
    import os

    cache = Cache(tmp_path / "c", clock)

    def boom(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", boom)
    async with httpx.AsyncClient(transport=httpx.MockTransport(site)) as http:
        c = SiteClient(http, cache)
        machines, entry = await c.welland_registry()
        again, _ = await c.welland_registry()
    assert machines and again == machines and not entry.stale
    assert site.count("/fleet/registry.json") == 1
    assert "cache write failed" in caplog.text
    assert list((tmp_path / "c").iterdir()) == []  # temp file removed


async def test_unwritable_cache_dir_still_serves(tmp_path, clock, site):
    cache = Cache(tmp_path / "c", clock)
    (tmp_path / "c").chmod(0o500)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(site)) as http:
            machines, _ = await SiteClient(http, cache).welland_registry()
    finally:
        (tmp_path / "c").chmod(0o700)
    assert machines


def test_cache_tightens_existing_dir(tmp_path, clock):
    d = tmp_path / "c"
    d.mkdir(mode=0o755)
    Cache(d, clock)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


async def test_toggle_redirect_is_not_followed(tmp_path):
    seen, handler = toggle_site(None, httpx.Response(307, headers={"Location": "https://welland.fpgas.online/x"}))
    http, c = await make(tmp_path, handler)
    http.follow_redirects = True
    with pytest.raises(SiteError, match="welland.*redirected"):
        await c.toggle("welland", "2", 1)
    await http.aclose()
    assert len(seen) == 1
