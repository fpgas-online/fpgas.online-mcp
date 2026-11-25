"""Small persistent byte cache with single-flight fetches and stale-on-error.

Entries hold the bytes plus the UTC time they were fetched. Files live under the cache
directory, named by a hash of the key, mode 0600 (board pages contain the site password).
"""

import asyncio
import hashlib
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path


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
        self._clock = clock
        self._mem: dict[str, Entry] = {}
        self._locks: dict[str, asyncio.Lock] = {}

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
        self._mem[key] = entry
        path = self._path(key)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(entry.as_of.isoformat().encode() + b"\n" + entry.data)
        os.replace(tmp, path)

    async def get(
        self,
        key: str,
        ttl_s: float,
        fetch: Callable[[], Awaitable[bytes]],
        live: bool = False,
        validate: Callable[[bytes], object] | None = None,
    ) -> Entry:
        """Return the entry for key, fetching when missing or older than ttl_s.

        Concurrent calls for one key share one fetch. live=True always fetches and never
        serves a cached or stale entry. If the fetch fails or validate (which raises on
        bad bytes) rejects the fetched bytes, the previous entry is kept and served with
        stale=True; with no previous entry the error is raised.
        """
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            old = self._load(key)
            if not live and old is not None:
                age = (self._clock() - old.as_of).total_seconds()
                if age < ttl_s:
                    return old
            try:
                data = await fetch()
                if validate is not None:
                    validate(data)
            except Exception:
                if live or old is None:
                    raise
                return replace(old, stale=True)
            entry = Entry(data, self._clock())
            self._store(key, entry)
            return entry
