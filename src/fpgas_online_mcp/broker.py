"""The per-user broker: one process serving every MCP front end and the CLI over a Unix socket."""

import asyncio
import contextlib
import logging
import os
import signal
import socket
import struct
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .paths import cache_dir, runtime_dir, state_dir
from .protocol import MAX_LINE, BrokerError, decode, encode, error_response

SOCK_NAME = "broker.sock"
log = logging.getLogger("fpgas_online_mcp.broker")


@dataclass(eq=False)
class Conn:
    """One connected client. `holder` is set by the first request that carries one."""

    holder: str | None = None


Handler = Callable[[dict, Conn], Awaitable[Any]]


class Broker:
    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        active_claims: Callable[[], int] = lambda: 0,
    ) -> None:
        self.clock = clock
        self.active_claims = active_claims
        self.version = __version__
        self.started = time.time()
        self.on_connect: Callable[[Conn], None] | None = None
        self.on_disconnect: Callable[[Conn], None] | None = None
        self._handlers: dict[str, Handler] = {}
        self._stop = asyncio.Event()
        self._stop_after_response = False
        self.register("ping", self._ping)
        self.register("shutdown_if_idle", self._shutdown_if_idle)

    def register(self, op: str, handler: Handler) -> None:
        self._handlers[op] = handler

    def stop(self) -> None:
        self._stop.set()

    async def _ping(self, args: dict, conn: Conn) -> dict:
        return {"version": self.version, "pid": os.getpid(), "started": self.started}

    async def _shutdown_if_idle(self, args: dict, conn: Conn) -> dict:
        if self.active_claims() > 0:
            raise BrokerError("busy", "the broker has active claims")
        self._stop_after_response = True
        return {"stopping": True}

    async def dispatch(self, request: dict, conn: Conn) -> dict:
        req_id = request.get("id")
        op = request.get("op")
        args = request.get("args", {})
        holder = request.get("holder")
        if conn.holder is None and isinstance(holder, str) and holder:
            conn.holder = holder
        if not isinstance(op, str) or not isinstance(args, dict):
            return error_response(req_id, "bad_request", "need string op and object args")
        handler = self._handlers.get(op)
        if handler is None:
            return error_response(req_id, "unknown_op", f"unknown op {op!r}")
        try:
            result = await handler(args, conn)
        except BrokerError as e:
            return error_response(req_id, e.code, e.message)
        except Exception:
            log.exception("handler for op %r failed", op)
            return error_response(req_id, "internal", "internal error; see the broker log")
        return {"id": req_id, "ok": True, "result": result}


def _peer_uid(writer: asyncio.StreamWriter) -> int | None:
    sock = writer.get_extra_info("socket")
    if sock is None:
        return None
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", creds)[1]


async def _serve_conn(
    broker: Broker,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    writers: set[asyncio.StreamWriter],
) -> None:
    if _peer_uid(writer) != os.getuid():
        writer.close()
        return
    conn = Conn()
    writers.add(writer)
    if broker.on_connect:
        broker.on_connect(conn)
    try:
        while True:
            try:
                line = await reader.readline()
            except ValueError:  # line longer than the stream limit
                writer.write(encode(error_response(None, "bad_request", "request too long")))
                await writer.drain()
                return
            if not line:
                return
            try:
                response = await broker.dispatch(decode(line), conn)
            except BrokerError as e:
                response = error_response(None, e.code, e.message)
            writer.write(encode(response))
            await writer.drain()
            if broker._stop_after_response:
                broker.stop()
                return
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        writers.discard(writer)
        if broker.on_disconnect:
            broker.on_disconnect(conn)
        writer.close()


def _is_live(path: Path) -> bool:
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect(str(path))
    except OSError:
        return False
    finally:
        s.close()
    return True


async def serve(sock_path: Path, broker: Broker, idle_exit_s: float = 1800) -> None:
    """Serve until shutdown_if_idle, stop(), or idle_exit_s with no clients and no claims."""
    if sock_path.exists():
        if _is_live(sock_path):
            raise SystemExit(f"another broker is already serving {sock_path}")
        sock_path.unlink()
    writers: set[asyncio.StreamWriter] = set()
    old_umask = os.umask(0o177)
    try:
        server = await asyncio.start_unix_server(
            lambda r, w: _serve_conn(broker, r, w, writers), path=str(sock_path), limit=MAX_LINE
        )
    finally:
        os.umask(old_umask)
    sock_path.chmod(0o600)
    st = sock_path.stat()
    ours = (st.st_dev, st.st_ino)
    idle_since = broker.clock()
    poll = min(idle_exit_s / 4, 1.0)
    try:
        while not broker._stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(broker._stop.wait(), timeout=poll)
            if writers or broker.active_claims() > 0:
                idle_since = broker.clock()
            elif broker.clock() - idle_since >= idle_exit_s:
                log.info("idle for %.0fs, exiting", idle_exit_s)
                break
    finally:
        server.close()
        for w in list(writers):
            w.close()
        # A successor may already have replaced the socket; only remove our own.
        with contextlib.suppress(FileNotFoundError):
            now = sock_path.stat()
            if (now.st_dev, now.st_ino) == ours:
                sock_path.unlink()
        await server.wait_closed()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    async def run() -> None:
        from .cache import Cache
        from .claims import ClaimStore
        from .ops import State, register
        from .sites import SiteClient, new_http_client
        from .ssh import SshManager

        http = new_http_client()
        sites = SiteClient(http, Cache(cache_dir()))
        claims = ClaimStore(state_dir())
        broker = Broker(active_claims=claims.active_count)
        register(broker, State(sites, claims, SshManager(sites)))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, broker.stop)
        try:
            await serve(runtime_dir() / SOCK_NAME, broker)
        finally:
            await http.aclose()

    asyncio.run(run())


if __name__ == "__main__":
    main()
