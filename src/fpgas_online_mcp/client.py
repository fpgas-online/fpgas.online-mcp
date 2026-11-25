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
from .paths import runtime_dir, state_dir
from .protocol import MAX_LINE, BrokerError, decode, encode

__all__ = ["BrokerClient", "BrokerError", "connect"]

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
    with open(state_dir() / "broker.log", "ab") as log:
        subprocess.run(
            [sys.executable, "-c", _DETACH],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            cwd="/",
            check=True,
        )


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


async def connect(holder: str, spawn: bool = True) -> BrokerClient:
    """Connect to the broker, starting it if needed and replacing one of a different version."""
    client = await _open(holder)
    if client is None:
        if not spawn:
            raise BrokerError("not_running", "the broker is not running")
        client = await _start(holder)
    info = await client.call("ping")
    if info["version"] == __version__:
        return client
    old = info["version"]
    try:
        await client.call("shutdown_if_idle")
    except BrokerError as e:
        await client.close()
        if e.code != "busy":
            raise
        raise BrokerError(
            "version",
            f"broker {old} is running with active claims but this client is {__version__}; "
            "release the claims (or let them expire) and retry, or stop the old broker by hand",
        ) from e
    await client.close()
    await _wait_gone()
    if not spawn:
        raise BrokerError("not_running", f"the old broker {old} stopped; none is running now")
    return await _start(holder)
