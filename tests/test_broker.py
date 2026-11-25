"""Broker protocol, lifecycle and start-up."""

import asyncio
import json
import os
import shutil
import signal
import socket
import stat
import tempfile
from pathlib import Path

import pytest

from fpgas_online_mcp import __version__, client, paths
from fpgas_online_mcp.broker import SOCK_NAME, Broker, serve
from fpgas_online_mcp.client import BrokerError

REPO_TMP = Path(__file__).resolve().parent.parent / "tmp"


def _raw(sock_path: Path, *lines: dict) -> list[dict]:
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(3)
    s.connect(str(sock_path))
    f = s.makefile("rwb")
    out = []
    for line in lines:
        f.write(json.dumps(line).encode() + b"\n")
        f.flush()
        out.append(json.loads(f.readline()))
    s.close()
    return out


@pytest.fixture
def env(monkeypatch):
    """Short XDG dirs (AF_UNIX paths are limited to 108 bytes); stops any broker spawned."""
    REPO_TMP.mkdir(exist_ok=True)
    d = Path(tempfile.mkdtemp(dir=REPO_TMP, prefix="b"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(d / "r"))
    monkeypatch.setenv("XDG_STATE_HOME", str(d / "s"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(d / "c"))
    monkeypatch.setenv("HOME", str(d / "h"))
    (d / "r").mkdir()
    yield d
    sock = d / "r" / "fpgas-online" / SOCK_NAME
    if sock.exists():
        try:
            pid = _raw(sock, {"id": 1, "op": "ping"})[0]["result"]["pid"]
            _raw(sock, {"id": 2, "op": "shutdown_if_idle"})
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        except OSError:
            pass
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
async def running(env):
    """An in-process broker on the runtime socket."""
    sock = paths.runtime_dir() / SOCK_NAME
    broker = Broker()
    task = asyncio.create_task(serve(sock, broker))
    while not sock.exists():
        await asyncio.sleep(0.01)
    yield broker, sock
    broker.stop()
    await task


async def test_ping_roundtrip(running):
    broker, _ = running
    c = await client.connect("me@host", spawn=False)
    info = await c.call("ping")
    assert info["version"] == __version__
    assert info["pid"] == os.getpid()
    await c.close()


async def test_unknown_op_error_result(running):
    c = await client.connect("me@host", spawn=False)
    with pytest.raises(BrokerError) as e:
        await c.call("nope")
    assert e.value.code == "unknown_op"
    await c.close()


async def test_handler_errors_and_holder(running):
    broker, _ = running
    seen = []

    async def boom(args, conn):
        raise RuntimeError("secret detail")

    async def refuse(args, conn):
        raise BrokerError("nope", "not allowed")

    async def who(args, conn):
        return conn.holder

    broker.register("boom", boom)
    broker.register("refuse", refuse)
    broker.register("who", who)
    broker.on_connect = lambda conn: seen.append("c")
    broker.on_disconnect = lambda conn: seen.append(f"d:{conn.holder}")
    c = await client.connect("me@host", spawn=False)
    with pytest.raises(BrokerError) as e:
        await c.call("boom")
    assert e.value.code == "internal" and "secret" not in e.value.message
    with pytest.raises(BrokerError) as e:
        await c.call("refuse")
    assert (e.value.code, e.value.message) == ("nope", "not allowed")
    assert await c.call("who") == "me@host"
    await c.close()
    await asyncio.sleep(0.1)
    assert seen == ["c", "d:me@host"]


async def test_bad_json_keeps_connection(running):
    _, sock = running
    reader, writer = await asyncio.open_unix_connection(str(sock))
    writer.write(b"{not json\n")
    resp = json.loads(await reader.readline())
    assert resp["ok"] is False and resp["id"] is None
    assert resp["error"]["code"] == "bad_request"
    writer.write(b'{"id": 5, "op": "ping"}\n')
    assert json.loads(await reader.readline())["ok"] is True
    writer.close()


async def test_overlong_line_closes(running):
    _, sock = running
    reader, writer = await asyncio.open_unix_connection(str(sock))
    writer.write(b"x" * (1024 * 1024 + 10) + b"\n")
    resp = json.loads(await reader.readline())
    assert resp["error"]["code"] == "bad_request"
    assert await reader.readline() == b""
    writer.close()


async def test_socket_mode_0600(running):
    _, sock = running
    assert stat.S_IMODE(sock.stat().st_mode) == 0o600


async def test_idle_exit(env):
    sock = paths.runtime_dir() / SOCK_NAME
    await asyncio.wait_for(serve(sock, Broker(), idle_exit_s=0.2), timeout=5)
    assert not sock.exists()


async def test_idle_exit_waits_for_claims(env):
    sock = paths.runtime_dir() / SOCK_NAME
    claims = [1]
    task = asyncio.create_task(serve(sock, Broker(active_claims=lambda: claims[0]), idle_exit_s=0.2))
    await asyncio.sleep(0.6)
    assert not task.done()
    claims[0] = 0
    await asyncio.wait_for(task, timeout=5)


async def test_shutdown_if_idle(env):
    sock = paths.runtime_dir() / SOCK_NAME
    claims = [1]
    task = asyncio.create_task(serve(sock, Broker(active_claims=lambda: claims[0])))
    while not sock.exists():
        await asyncio.sleep(0.01)
    c = await client.connect("h", spawn=False)
    with pytest.raises(BrokerError) as e:
        await c.call("shutdown_if_idle")
    assert e.value.code == "busy"
    claims[0] = 0
    await c.call("shutdown_if_idle")
    await asyncio.wait_for(task, timeout=5)
    assert not sock.exists()
    await c.close()


async def test_two_clients_spawn_one_broker(env):
    a, b = await asyncio.gather(client.connect("a@h"), client.connect("b@h"))
    pa, pb = (await a.call("ping"))["pid"], (await b.call("ping"))["pid"]
    assert pa == pb != os.getpid()
    assert (paths.state_dir() / "broker.log").exists()
    await a.close()
    await b.close()


async def test_stale_socket_replaced(env):
    sock = paths.runtime_dir() / SOCK_NAME
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(sock))
    s.close()  # leaves a socket file nobody listens on
    c = await client.connect("a@h")
    assert (await c.call("ping"))["pid"] != os.getpid()
    await c.close()


async def _old_broker(env, claims):
    sock = paths.runtime_dir() / SOCK_NAME
    broker = Broker(active_claims=lambda: claims)
    broker.version = "0.0.1"
    task = asyncio.create_task(serve(sock, broker))
    while not sock.exists():
        await asyncio.sleep(0.01)
    return task


async def test_version_mismatch_restarts_when_idle(env):
    old = await _old_broker(env, 0)
    c = await client.connect("a@h")
    info = await c.call("ping")
    assert info["version"] == __version__ and info["pid"] != os.getpid()
    await asyncio.wait_for(old, timeout=5)
    await c.close()


async def test_version_mismatch_refused_with_claims(env):
    old = await _old_broker(env, 1)
    with pytest.raises(BrokerError) as e:
        await client.connect("a@h")
    assert e.value.code == "version" and "0.0.1" in e.value.message
    assert not old.done()
    old.cancel()
