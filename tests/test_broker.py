"""Broker protocol, lifecycle and start-up."""

import asyncio
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest
from tmpdir import TEST_TMP

from fpgas_online_mcp import __version__, client, paths
from fpgas_online_mcp.broker import SOCK_NAME, Broker, Conn, serve
from fpgas_online_mcp.client import BrokerError

REPO_TMP = TEST_TMP


def _raw(sock_path: Path, *lines: dict) -> list[dict]:
    out = []
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(3)
        s.connect(str(sock_path))
        with s.makefile("rwb") as f:
            for line in lines:
                f.write(json.dumps(line).encode() + b"\n")
                f.flush()
                out.append(json.loads(f.readline()))
    return out


async def _wait_for_sock(sock: Path) -> None:
    async with asyncio.timeout(5):
        while not sock.exists():
            await asyncio.sleep(0.01)


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
    await _wait_for_sock(sock)
    yield broker, sock
    broker.stop()
    await task


async def test_ping_roundtrip(running):
    broker, _ = running
    c = await client.connect("me@host", spawn=False)
    info = await c.call("ping")
    assert info["version"] == __version__ and info["protocol"] == 1
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
    await writer.wait_closed()


async def test_overlong_line_closes(running):
    _, sock = running
    reader, writer = await asyncio.open_unix_connection(str(sock))
    writer.write(b"x" * (1024 * 1024 + 10) + b"\n")
    resp = json.loads(await reader.readline())
    assert resp["error"]["code"] == "bad_request"
    assert await reader.readline() == b""
    writer.close()
    await writer.wait_closed()


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
    await _wait_for_sock(sock)
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
    await _wait_for_sock(sock)
    return task


async def test_version_mismatch_restarts_when_idle(env):
    old = await _old_broker(env, 0)
    c = await client.connect("a@h")
    info = await c.call("ping")
    assert info["version"] == __version__ and info["pid"] != os.getpid()
    await asyncio.wait_for(old, timeout=5)
    await c.close()


async def test_older_broker_with_claims_is_used(env):
    old = await _old_broker(env, 1)
    c = await client.connect("a@h")
    assert c.broker_version == "0.0.1" and (await c.call("ping"))["pid"] == os.getpid()
    assert not old.done()
    await c.close()
    old.cancel()
    with pytest.raises(asyncio.CancelledError):
        await old


async def test_older_broker_with_other_client_is_kept(env):
    old = await _old_broker(env, 0)
    other = await client.connect("old@h", spawn=False)  # an old session's open connection
    c = await client.connect("new@h")
    assert c.broker_version == "0.0.1"
    assert (await other.call("ping"))["pid"] == os.getpid()  # the old session still works
    assert not old.done()
    await other.close()
    await c.close()
    old.cancel()
    with pytest.raises(asyncio.CancelledError):
        await old


async def test_newer_broker_is_used_not_replaced(env):
    sock = paths.runtime_dir() / SOCK_NAME
    broker = Broker()
    broker.version = "99.0.0"
    task = asyncio.create_task(serve(sock, broker))
    await _wait_for_sock(sock)
    c = await client.connect("a@h")
    assert c.broker_version == "99.0.0" and (await c.call("ping"))["pid"] == os.getpid()
    await c.close()
    assert not task.done()
    broker.stop()
    await task


async def test_protocol_mismatch_refused(env):
    sock = paths.runtime_dir() / SOCK_NAME
    broker = Broker()
    broker.version, broker.protocol = "99.0.0", 2
    task = asyncio.create_task(serve(sock, broker))
    await _wait_for_sock(sock)
    with pytest.raises(BrokerError) as e:
        await client.connect("a@h")
    assert e.value.code == "protocol" and "99.0.0" in e.value.message and "protocol 2" in e.value.message
    assert not task.done()
    broker.stop()
    await task


async def test_two_versions_alternate_without_restarts(env, monkeypatch):
    """An old and a new front end take turns: neither replaces the broker the other is using."""
    sock = paths.runtime_dir() / SOCK_NAME
    broker = Broker()  # the current version
    task = asyncio.create_task(serve(sock, broker))
    await _wait_for_sock(sock)
    new = await client.connect("new@h")
    for _ in range(3):
        monkeypatch.setattr(client, "__version__", "0.0.1")
        old = await client.connect("old@h")
        monkeypatch.setattr(client, "__version__", __version__)  # not undo(): that would drop env's XDG dirs
        assert old.broker_version == __version__
        assert (await old.call("ping"))["pid"] == os.getpid()
        assert (await new.call("ping"))["pid"] == os.getpid()
        await old.close()
    assert not task.done()
    await new.close()
    broker.stop()
    await task


def test_version_tuple():
    assert client.version_tuple("0.10.2") > client.version_tuple("0.9.9")
    assert client.version_tuple("1.0") == (1, 0)
    assert client.version_tuple("1.2rc1") == (1, 2)
    assert client.version_tuple("dev") == (0,)
    assert client.version_tuple("0.0.85+dirty") == (0, 0, 85)


async def test_shutdown_refused_while_other_clients_connected(running):
    broker, _ = running
    a = await client.connect("a@h", spawn=False)
    b = await client.connect("b@h", spawn=False)
    with pytest.raises(BrokerError) as e:
        await a.call("shutdown_if_idle")
    assert e.value.code == "busy" and "other client" in e.value.message
    await b.close()
    await asyncio.sleep(0.1)
    assert await a.call("shutdown_if_idle") == {"stopping": True}
    await a.close()


async def test_shutdown_refused_while_requests_in_flight():
    broker = Broker()
    gate = asyncio.Event()

    async def slow(args, conn):
        await gate.wait()
        return "done"

    broker.register("slow", slow)
    a, b = Conn(), Conn()
    t = asyncio.create_task(broker.dispatch({"id": 1, "op": "slow"}, a))
    await asyncio.sleep(0)
    resp = await broker.dispatch({"id": 2, "op": "shutdown_if_idle"}, b)
    assert resp["error"]["code"] == "busy" and "other requests" in resp["error"]["message"]
    gate.set()
    assert (await t)["result"] == "done"
    assert (await broker.dispatch({"id": 3, "op": "shutdown_if_idle"}, b))["ok"] is True


async def test_shutdown_keeps_successor_socket(env):
    sock = paths.runtime_dir() / SOCK_NAME
    broker = Broker()
    task = asyncio.create_task(serve(sock, broker))
    await _wait_for_sock(sock)
    sock.unlink()  # a successor broker B takes over the path
    successor = socket.socket(socket.AF_UNIX)
    successor.bind(str(sock))
    try:
        broker.stop()
        await task
        assert sock.exists()
    finally:
        successor.close()


async def test_cancelled_call_does_not_desync(running):
    broker, _ = running

    async def slow(args, conn):
        await asyncio.sleep(0.3)
        return "slow"

    broker.register("slow", slow)
    c = await client.connect("h", spawn=False)
    t = asyncio.create_task(c.call("slow"))
    await asyncio.sleep(0.1)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    with pytest.raises(BrokerError) as e:
        await c.call("ping")
    assert e.value.code == "disconnected"


async def test_stale_reply_is_skipped(env):
    sock = paths.runtime_dir() / SOCK_NAME

    async def fake(reader, writer):
        await reader.readline()
        writer.write(b'{"id":99,"ok":true,"result":"stale"}\n{"id":1,"ok":true,"result":"fresh"}\n')
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(fake, path=str(sock))
    reader, writer = await asyncio.open_unix_connection(str(sock))
    c = client.BrokerClient(reader, writer, "h")
    assert await c.call("x") == "fresh"
    await c.close()
    server.close()
    await server.wait_closed()


def test_failed_spawn_is_a_broker_error(env, monkeypatch):
    def fail(*a, **k):
        raise subprocess.CalledProcessError(1, a[0])

    paths.state_dir()  # created as the real _spawn expects
    monkeypatch.setattr(client.subprocess, "run", fail)
    with pytest.raises(BrokerError) as e:
        client._spawn()
    assert e.value.code == "start_failed" and "broker.log" in e.value.message


async def test_ping_says_the_last_activity_window(running, own_config_dir):
    c = await client.connect("me@host", spawn=False)
    assert (await c.call("ping"))["last_activity_s"] == 300
    (own_config_dir / "fpgas-online").mkdir(mode=0o700, exist_ok=True)
    (own_config_dir / "fpgas-online" / "config.toml").write_text('last_activity = "10m"\n')
    assert (await c.call("ping"))["last_activity_s"] == 600
    (own_config_dir / "fpgas-online" / "config.toml").write_text('last_activity = "soon"\n')
    said = (await c.call("ping"))["last_activity_s"]
    assert isinstance(said, str) and "config.toml" in said and "soon" in said
    await c.close()
