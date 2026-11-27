"""Advisory, machine-local board claims shared by every session and the CLI.

A claim says "this holder is using this board until <expires>". Nothing here
enforces it against the site; callers (connect/run/power_cycle) check it.

Storage: one JSON file per claimed board, `<state_dir>/claims/<site>_<name>.json`
(directory mode 0700, files 0600), written atomically (temp file + rename).
Format (all times ISO 8601 UTC, `Z` suffix):

    {
      "claim_id": "c-AbC123xyZ9k",          # opaque id; renew/release with it
      "board": "welland:sw1-p1",            # "<site>:<name>"
      "holder": "my-session@myhost",        # "<label>@<hostname>"
      "purpose": "blink test",
      "started": "2026-11-26T12:00:00Z",    # kept across renewals
      "expires": "2026-11-26T12:30:00Z",    # claims last 1..240 minutes (default 30)
      "override_reason": null,              # why an in-use board was claimed anyway
      "tt_state_at_claim": null,            # Tiny Tapeout state seen at claim time
      "host": null,                         # Tiny Tapeout: the Pi it is plugged into ("welland:pi-sw2-p7")
      "allow_unoffered": false              # reach this welland Pi although the site does not offer it
    }

One claim per Pi: a claim's hardware key is its `host`, else its board. A new claim is refused
while another board with the same key has a live claim, so a Tiny Tapeout board and the welland Pi
it is plugged into exclude each other. Files written before `host` existed read as host None, and
before `allow_unoffered` as false (only a JSON `true` counts). A renewal keeps both as stored.

Mutations hold an flock on `claims/.lock` (cross-process) and an asyncio lock.
Naive times in a hand-edited file are read as UTC.
Expired claims are ignored and their file removed on read. A file that cannot
be parsed is logged (once per modification time) and ignored (never deleted). Whether a holder's front end
is currently connected is kept in memory only, never in the file.
"""

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import re
import secrets
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .paths import _private

log = logging.getLogger(__name__)

DEFAULT_MINUTES = 30
MAX_MINUTES = 240


@dataclass(frozen=True)
class Claim:
    claim_id: str
    board: str
    holder: str
    purpose: str
    started: datetime
    expires: datetime
    override_reason: str | None = None
    tt_state_at_claim: str | None = None
    host: str | None = None
    allow_unoffered: bool = False  # set at claim time (with its in-use check); connect/run/power_cycle use it
    holder_connected: bool = False  # in memory only, not persisted

    @property
    def key(self) -> str:
        """The hardware this claim holds: its Pi when known, else its board."""
        return self.host or self.board


class ClaimConflict(Exception):
    """The board, or another board on the same Pi (`shared` names that Pi), already has a live claim."""

    def __init__(self, existing: Claim, shared: str | None = None):
        self.existing = existing
        self.shared = shared
        until = existing.expires.astimezone(UTC).strftime("%H:%M UTC")
        same_pi = f", which is on the same Pi ({shared})" if shared else ""
        super().__init__(f"claimed by {existing.holder} until {until} ({existing.purpose}){same_pi}")


class ClaimGone(Exception):
    """A renewal named a claim that is no longer live (expired or released): it must not become a new claim."""

    def __init__(self, claim_id: str):
        self.claim_id = claim_id
        super().__init__(f"claim {claim_id} expired; claim again")


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s: str) -> datetime:
    t = datetime.fromisoformat(s)
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


def _now() -> datetime:
    return datetime.now(UTC)


class ClaimStore:
    def __init__(self, dir: Path, clock: Callable[[], datetime] = _now):
        self._dir = Path(dir) / "claims"
        self._clock = clock
        self._lock = asyncio.Lock()
        self._connected: set[str] = set()
        self._holding = False  # this process holds the flock
        self._warned: set[tuple[str, int]] = set()  # (path, mtime_ns) of unreadable files already logged

    async def _acquire(self):
        """Take the cross-process flock, polling so the event loop is never blocked."""
        self._ensure_dir()
        fd = os.open(self._dir / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                await asyncio.sleep(0.01)
            except BaseException:
                os.close(fd)
                raise

    @contextlib.asynccontextmanager
    async def _locked(self):
        async with self._lock:
            fd = await self._acquire()
            self._holding = True
            try:
                yield
            finally:
                self._holding = False
                os.close(fd)  # closing releases the flock

    def _ensure_dir(self) -> None:
        _private(self._dir, self._dir.parent)

    # --- paths and file io -------------------------------------------------

    def _path(self, board: str) -> Path:
        return self._dir / (re.sub(r"[^A-Za-z0-9._-]", "_", board.replace(":", "_", 1)) + ".json")

    def _write(self, claim: Claim) -> None:
        self._ensure_dir()
        data = {
            "claim_id": claim.claim_id,
            "board": claim.board,
            "holder": claim.holder,
            "purpose": claim.purpose,
            "started": _iso(claim.started),
            "expires": _iso(claim.expires),
            "override_reason": claim.override_reason,
            "tt_state_at_claim": claim.tt_state_at_claim,
            "host": claim.host,
            "allow_unoffered": claim.allow_unoffered,
        }
        fd, tmp = tempfile.mkstemp(dir=self._dir, prefix=".claim-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path(claim.board))
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _load(self, path: Path) -> Claim | None:
        """Parse one file; None if corrupt or expired (expired files are removed)."""
        try:
            d = json.loads(path.read_text())
            claim = Claim(
                claim_id=d["claim_id"],
                board=d["board"],
                holder=d["holder"],
                purpose=d["purpose"],
                started=_parse(d["started"]),
                expires=_parse(d["expires"]),
                override_reason=d.get("override_reason"),
                tt_state_at_claim=d.get("tt_state_at_claim"),
                host=d.get("host"),
                allow_unoffered=d.get("allow_unoffered") is True,
            )
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            self._warn_once(path, e)
            return None
        if claim.expires <= self._clock():
            self._drop_expired(path, claim.claim_id)
            return None
        return replace(claim, holder_connected=claim.holder in self._connected)

    def _warn_once(self, path: Path, error: Exception) -> None:
        """Log an unreadable file once per version of it (the broker reads every claim about once a second)."""
        try:
            key = (str(path), path.stat().st_mtime_ns)
        except OSError:
            key = (str(path), -1)
        if key not in self._warned:
            self._warned.add(key)
            log.warning("ignoring unreadable claim file %s: %s", path, error)

    def _drop_expired(self, path: Path, claim_id: str) -> None:
        """Best-effort unlink, under the flock and only if the file is still that expired claim."""
        fd = None
        try:
            if not self._holding:
                fd = os.open(self._dir / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # never block; skip if busy
            d = json.loads(path.read_text())
            if d.get("claim_id") == claim_id and _parse(d["expires"]) <= self._clock():
                path.unlink()
        except BlockingIOError:
            pass  # another holder has the lock; the expired claim is still ignored
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass  # ignored on every read anyway
        finally:
            if fd is not None:
                os.close(fd)

    # --- reads -------------------------------------------------------------

    def all(self) -> list[Claim]:
        if not self._dir.is_dir():
            return []
        claims = (self._load(p) for p in sorted(self._dir.glob("*.json")))
        return [c for c in claims if c is not None]

    def get(self, claim_id: str) -> Claim | None:
        return next((c for c in self.all() if c.claim_id == claim_id), None)

    def for_board(self, board: str) -> Claim | None:
        return self._load(self._path(board))

    def shared(self, board: str, host: str | None) -> Claim | None:
        """A live claim on another board that holds the same hardware (the same Pi) as board/host would."""
        key = host or board
        return next((c for c in self.all() if c.board != board and c.key == key), None)

    def active_count(self) -> int:
        return len(self.all())

    def set_connected(self, holder: str, connected: bool) -> None:
        if connected:
            self._connected.add(holder)
        else:
            self._connected.discard(holder)

    # --- mutations (serialized) -------------------------------------------

    async def claim(
        self,
        board: str,
        minutes: int | None,
        purpose: str,
        holder: str,
        claim_id: str | None = None,
        override_reason: str | None = None,
        tt_state_at_claim: str | None = None,
        host: str | None = None,
        allow_unoffered: bool = False,
    ) -> Claim:
        """Claim board, or renew claim_id. `host`: the Pi the board is plugged into; `allow_unoffered`:
        the board may be reached although its site does not offer it. Both for new claims only: a
        renewal keeps the stored ones (it skips the in-use check, so it must not widen the claim)."""
        minutes = DEFAULT_MINUTES if minutes is None else minutes
        if not 1 <= minutes <= MAX_MINUTES:
            raise ValueError(f"minutes must be between 1 and {MAX_MINUTES}, got {minutes}")
        async with self._locked():
            now = self._clock()
            expires = now + timedelta(minutes=minutes)
            if claim_id is not None:
                known = self.get(claim_id)
                if known is not None and known.board != board:
                    raise ValueError(f"claim {claim_id} is for a different board ({known.board})")
            existing = self.for_board(board)
            await asyncio.sleep(0)  # yield point: only the locks make check-then-write safe
            if existing is None and claim_id is not None:
                # A fresh claim needs the caller's in-use check, which a renewal skipped.
                raise ClaimGone(claim_id)
            if existing is not None:
                if claim_id != existing.claim_id:
                    raise ClaimConflict(existing)
                claim = replace(
                    existing,
                    purpose=purpose,
                    expires=expires,
                    tt_state_at_claim=tt_state_at_claim or existing.tt_state_at_claim,
                )
            else:
                other = self.shared(board, host)
                if other is not None:
                    raise ClaimConflict(other, shared=host or board)
                claim = Claim(
                    claim_id="c-" + secrets.token_urlsafe(9),
                    board=board,
                    holder=holder,
                    purpose=purpose,
                    started=now,
                    expires=expires,
                    override_reason=override_reason,
                    tt_state_at_claim=tt_state_at_claim,
                    host=host,
                    allow_unoffered=allow_unoffered,
                    holder_connected=holder in self._connected,
                )
            self._write(claim)
            return claim

    async def release(self, claim_id: str) -> None:
        async with self._locked():
            claim = self.get(claim_id)
            if claim is None:
                raise KeyError(claim_id)
            self._path(claim.board).unlink(missing_ok=True)
