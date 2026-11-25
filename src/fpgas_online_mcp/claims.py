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
      "tt_state_at_claim": null             # Tiny Tapeout state seen at claim time
    }

Expired claims are ignored and their file removed on read. A file that cannot
be parsed is logged and ignored (never deleted). Whether a holder's front end
is currently connected is kept in memory only, never in the file.
"""

import asyncio
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
    holder_connected: bool = False  # in memory only, not persisted


class ClaimConflict(Exception):
    """The board already has a live claim by someone else."""

    def __init__(self, existing: Claim):
        self.existing = existing
        until = existing.expires.astimezone(UTC).strftime("%H:%M UTC")
        super().__init__(f"claimed by {existing.holder} until {until} ({existing.purpose})")


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s).astimezone(UTC)


def _now() -> datetime:
    return datetime.now(UTC)


class ClaimStore:
    def __init__(self, dir: Path, clock: Callable[[], datetime] = _now):
        self._dir = Path(dir) / "claims"
        self._clock = clock
        self._lock = asyncio.Lock()
        self._connected: set[str] = set()

    # --- paths and file io -------------------------------------------------

    def _path(self, board: str) -> Path:
        return self._dir / (re.sub(r"[^A-Za-z0-9._-]", "_", board.replace(":", "_", 1)) + ".json")

    def _write(self, claim: Claim) -> None:
        self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._dir.chmod(0o700)
        data = {
            "claim_id": claim.claim_id,
            "board": claim.board,
            "holder": claim.holder,
            "purpose": claim.purpose,
            "started": _iso(claim.started),
            "expires": _iso(claim.expires),
            "override_reason": claim.override_reason,
            "tt_state_at_claim": claim.tt_state_at_claim,
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
            )
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            log.warning("ignoring unreadable claim file %s: %s", path, e)
            return None
        if claim.expires <= self._clock():
            try:
                path.unlink()
            except OSError:
                pass  # best effort; it is ignored on every read anyway
            return None
        return replace(claim, holder_connected=claim.holder in self._connected)

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
    ) -> Claim:
        minutes = DEFAULT_MINUTES if minutes is None else minutes
        if not 1 <= minutes <= MAX_MINUTES:
            raise ValueError(f"minutes must be between 1 and {MAX_MINUTES}, got {minutes}")
        async with self._lock:
            now = self._clock()
            expires = now + timedelta(minutes=minutes)
            if claim_id is not None:
                known = self.get(claim_id)
                if known is not None and known.board != board:
                    raise ValueError(f"claim {claim_id} is for a different board ({known.board})")
            existing = self.for_board(board)
            if existing is not None:
                if claim_id != existing.claim_id:
                    raise ClaimConflict(existing)
                claim = replace(existing, purpose=purpose, expires=expires)
            else:
                claim = Claim(
                    claim_id="c-" + secrets.token_urlsafe(9),
                    board=board,
                    holder=holder,
                    purpose=purpose,
                    started=now,
                    expires=expires,
                    override_reason=override_reason,
                    holder_connected=holder in self._connected,
                )
            self._write(claim)
            return claim

    async def release(self, claim_id: str) -> None:
        async with self._lock:
            claim = self.get(claim_id)
            if claim is None:
                raise KeyError(claim_id)
            self._path(claim.board).unlink(missing_ok=True)
