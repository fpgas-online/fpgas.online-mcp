"""Client side of the broker protocol, including start-up of the broker on demand."""

import asyncio
import contextlib
import fcntl
import itertools
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .paths import PathTooLong, runtime_dir, state_dir
from .protocol import MAX_LINE, PROTOCOL, BrokerError, decode, encode

__all__ = ["BrokerClient", "BrokerError", "connect", "version_tuple"]

START_TIMEOUT_S = 5.0
POLL_S = 0.05


class BrokerClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, holder: str):
        self._reader = reader
        self._writer = writer
        self.holder = holder
        self._ids = itertools.count(1)
        self._lock = asyncio.Lock()
        self._broken = False
        self.broker_version: str | None = None  # set by connect()
        self.broker_protocol: int | None = None  # set by connect()

    @property
    def broken(self) -> bool:
        """True once a call failed mid-way; the connection is closed and must not be reused."""
        return self._broken

    async def call(self, op: str, **args: Any) -> Any:
        async with self._lock:
            if self._broken:
                raise BrokerError("disconnected", "the connection to the broker was dropped")
            req_id = next(self._ids)
            req = {
                "id": req_id,
                "op": op,
                "args": args,
                "version": __version__,
                "holder": self.holder,
            }
            try:
                self._writer.write(encode(req))
                await self._writer.drain()
                while True:
                    line = await self._reader.readline()
                    if not line:
                        raise BrokerError("disconnected", "the broker closed the connection")
                    resp = decode(line)
                    if resp.get("id") in (req_id, None):
                        break  # skip replies to earlier abandoned requests
            except BaseException:
                # Cancelled or failed mid-call: replies could now pair with the wrong request.
                self._broken = True
                self._writer.close()
                raise
        if resp.get("ok"):
            return resp.get("result")
        err = resp.get("error") or {}
        raise BrokerError(err.get("code", "internal"), err.get("message", ""))

    async def close(self) -> None:
        self._writer.close()
        with contextlib.suppress(ConnectionError):
            await self._writer.wait_closed()


def _sock_path() -> Path:
    return runtime_dir() / "broker.sock"


async def _open(holder: str) -> BrokerClient | None:
    try:
        reader, writer = await asyncio.open_unix_connection(str(_sock_path()), limit=MAX_LINE)
    except (FileNotFoundError, ConnectionRefusedError):
        return None
    return BrokerClient(reader, writer, holder)


# Run by an intermediate process so the broker is reparented to init: the caller keeps no
# Popen handle (no ResourceWarning) and never gets a zombie when the broker exits.
_DETACH = (
    "import os, subprocess, sys\n"
    "subprocess.Popen([sys.executable, '-m', 'fpgas_online_mcp.broker'],"
    " stdin=subprocess.DEVNULL, start_new_session=True, cwd='/')\n"
    "os._exit(0)\n"
)


def _spawn() -> None:
    log_path = state_dir() / "broker.log"
    try:
        with open(log_path, "ab") as log:
            subprocess.run(
                [sys.executable, "-c", _DETACH],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                cwd="/",
                check=True,
            )
    except subprocess.CalledProcessError as e:
        raise BrokerError("start_failed", f"could not start the broker (exit {e.returncode}); see {log_path}") from e


async def _start(holder: str) -> BrokerClient:
    """Under the start-up lock: connect to a broker another client started, or start one."""
    lock_fd = os.open(runtime_dir() / "broker.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(POLL_S)
        client = await _open(holder)
        if client is not None:
            return client
        _sock_path().unlink(missing_ok=True)
        _spawn()
        deadline = asyncio.get_running_loop().time() + START_TIMEOUT_S
        while asyncio.get_running_loop().time() < deadline:
            client = await _open(holder)
            if client is not None:
                with contextlib.suppress(BrokerError):
                    await client.call("ping")
                    return client
                await client.close()
            await asyncio.sleep(POLL_S)
        raise BrokerError(
            "start_failed",
            f"the broker did not start within {START_TIMEOUT_S:.0f}s; see {state_dir() / 'broker.log'}",
        )
    finally:
        os.close(lock_fd)


async def _wait_gone() -> None:
    deadline = asyncio.get_running_loop().time() + START_TIMEOUT_S
    while _sock_path().exists() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(POLL_S)


# Versions written by hand before they came from `git describe` (0.0.<commits>): older than every
# git-describe version although the tuples say otherwise, so new clients replace such a broker. (A
# 0.1.1 client still sees itself as newer and replaces an idle new broker; that ends once the old
# plugin copies are updated.)
PRE_GIT_DESCRIBE = {"0.1.1"}


def version_tuple(version: str) -> tuple[int, ...]:
    """'0.10.2' -> (0, 10, 2); each part counts by its leading digits ('1rc2' -> 1, 'dev' -> 0).

    A version from PRE_GIT_DESCRIBE counts as (0,), older than any other.
    """
    if version in PRE_GIT_DESCRIBE:
        return (0,)
    parts = []
    for part in version.split("."):
        digits = len(part) - len(part.lstrip("0123456789"))
        parts.append(int(part[:digits]) if digits else 0)
    return tuple(parts)


async def _identify(client: BrokerClient) -> BrokerClient:
    info = await client.call("ping")
    client.broker_version = str(info["version"])
    client.broker_protocol = int(info.get("protocol", 1))  # brokers before the field speak protocol 1
    return client


def _protocol_error(client: BrokerClient) -> BrokerError:
    return BrokerError(
        "protocol",
        f"the running broker is version {client.broker_version} (protocol {client.broker_protocol}) but this "
        f"client is {__version__} (protocol {PROTOCOL}); they cannot talk to each other. Update or restart "
        "the older fpgas-online sessions, or wait until the broker is idle",
    )


async def connect(holder: str, spawn: bool = True) -> BrokerClient:
    """Connect to the broker, starting it if needed.

    A client newer than the broker asks it to stop when it is idle (no claims, no other clients, no
    other requests) and starts its own; when the broker is busy, or not older, the client uses it as
    long as both speak the same protocol. Versions are compared as tuples of ints, so two versions
    never take turns replacing each other's broker.

    With spawn=False nothing is started or stopped; `client.broker_version` says what is running.
    """
    try:
        runtime_dir()  # a too-long socket path is reported here, not as "the broker did not start"
    except PathTooLong as e:
        raise BrokerError("path_too_long", str(e)) from e
    client = await _open(holder)
    if client is None:
        if not spawn:
            raise BrokerError("not_running", "the broker is not running")
        client = await _start(holder)
    await _identify(client)
    if not spawn:
        return client  # without spawn a client never shuts a broker down (the CLI inspects/stops it)
    if version_tuple(__version__) > version_tuple(client.broker_version):
        try:
            await client.call("shutdown_if_idle")
        except BrokerError as e:
            if e.code != "busy":
                await client.close()
                raise
            # Busy: keep using the older broker; it restarts when it is idle and someone newer connects.
        else:
            await client.close()
            await _wait_gone()
            client = await _identify(await _start(holder))
    if client.broker_protocol != PROTOCOL:
        await client.close()
        raise _protocol_error(client)
    return client
