"""Small persistent byte cache with single-flight fetches and stale-on-error.

Entries hold the bytes plus the UTC time they were fetched. Files live under the cache
directory, named by a hash of the key, mode 0600 (board pages contain the site password).
"""

import asyncio
import contextlib
import hashlib
import logging
import os
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Entry:
    data: bytes
    as_of: datetime
    stale: bool = False


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Cache:
    def __init__(self, dir: Path, clock: Callable[[], datetime] = _utc_now):
        self.dir = Path(dir)
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.dir.chmod(0o700)
        self._clock = clock
        self._mem: dict[str, Entry] = {}
        self._flights: dict[tuple[str, bool], asyncio.Future[Entry]] = {}

    def _path(self, key: str) -> Path:
        return self.dir / (hashlib.sha256(key.encode()).hexdigest() + ".entry")

    def _load(self, key: str) -> Entry | None:
        entry = self._mem.get(key)
        if entry is not None:
            return entry
        try:
            raw = self._path(key).read_bytes()
            head, _, data = raw.partition(b"\n")
            entry = Entry(data, datetime.fromisoformat(head.decode()))
        except (OSError, ValueError):
            return None
        if entry.as_of.tzinfo is None:
            return None
        self._mem[key] = entry
        return entry

    def _store(self, key: str, entry: Entry) -> None:
        """Remember the entry; persisting it is best effort (a failure is logged, not raised)."""
        self._mem[key] = entry
        path = self._path(key)
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".tmp-")  # mode 0600
            with os.fdopen(fd, "wb") as f:
                f.write(entry.as_of.isoformat().encode() + b"\n" + entry.data)
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("cache write failed (%s)", exc.strerror or type(exc).__name__)
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)

    async def _fetch_and_store(
        self,
        key: str,
        fetch: Callable[[], Awaitable[bytes]],
        validate: Callable[[bytes], object] | None,
    ) -> Entry:
        data = await fetch()
        if validate is not None:
            validate(data)
        entry = Entry(data, self._clock())
        self._store(key, entry)
        return entry

    async def get(
        self,
        key: str,
        ttl_s: float,
        fetch: Callable[[], Awaitable[bytes]],
        live: bool = False,
        validate: Callable[[bytes], object] | None = None,
    ) -> Entry:
        """Return the entry for key, fetching when missing or older than ttl_s.

        Concurrent calls for one key share one in-flight fetch, whether it succeeds or
        fails. live=True always fetches and never serves a cached or stale entry. If the
        fetch fails or validate (which raises on bad bytes) rejects the fetched bytes, the
        previous entry is kept and served with stale=True; with no previous entry (or with
        live=True) the error is raised to every waiting caller.
        """
        if not live:
            old = self._load(key)
            # A negative age (the clock went back, or an entry from the future) counts as expired.
            if old is not None and 0 <= (self._clock() - old.as_of).total_seconds() < ttl_s:
                return old
        flight_key = (key, live)
        flight = self._flights.get(flight_key)
        if flight is None:
            flight = asyncio.ensure_future(self._fetch_and_store(key, fetch, validate))
            self._flights[flight_key] = flight
            flight.add_done_callback(lambda _: self._flights.pop(flight_key, None))
        try:
            return await asyncio.shield(flight)
        except Exception:
            old = None if live else self._load(key)
            if old is None:
                raise
            return replace(old, stale=True)
