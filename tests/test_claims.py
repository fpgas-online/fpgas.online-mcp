"""Claims: advisory machine-local board reservations."""

import asyncio
import json
import logging
import os
import stat
from datetime import UTC, datetime, timedelta

import pytest

from fpgas_online_mcp.claims import Claim, ClaimConflict, ClaimGone, ClaimStore

BOARD = "welland:sw1-p1"


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def store(tmp_path, clock):
    return ClaimStore(tmp_path, clock)


async def test_default_30_minutes(store, clock):
    c = await store.claim(BOARD, None, "blink", "a@h")
    assert c.expires == clock.now + timedelta(minutes=30)
    assert c.started == clock.now
    assert c.board == BOARD and c.holder == "a@h" and c.purpose == "blink"
    assert c.claim_id.startswith("c-")
    assert c.override_reason is None and c.tt_state_at_claim is None
    assert store.get(c.claim_id) == c
    assert store.for_board(BOARD) == c


@pytest.mark.parametrize("minutes", [0, -5, 241])
async def test_minutes_out_of_range(store, minutes):
    with pytest.raises(ValueError, match="240"):
        await store.claim(BOARD, minutes, "x", "a@h")
    assert store.all() == []


async def test_max_240_ok(store, clock):
    c = await store.claim(BOARD, 240, "x", "a@h")
    assert c.expires == clock.now + timedelta(minutes=240)


async def test_renew_extends_keeps_started(store, clock):
    c = await store.claim(BOARD, 30, "one", "a@h")
    clock.advance(minutes=10)
    r = await store.claim(BOARD, 60, "two", "a@h", claim_id=c.claim_id)
    assert r.claim_id == c.claim_id
    assert r.started == c.started
    assert r.expires == clock.now + timedelta(minutes=60)
    assert r.purpose == "two"
    assert len(store.all()) == 1


async def test_renew_keeps_tt_state(store, clock, tmp_path):
    c = await store.claim(BOARD, 30, "one", "a@h")
    path = tmp_path / "claims" / "welland_sw1-p1.json"
    data = json.loads(path.read_text())
    data["tt_state_at_claim"] = "idle"
    path.write_text(json.dumps(data))
    r = await store.claim(BOARD, 30, "one", "a@h", claim_id=c.claim_id)
    assert r.tt_state_at_claim == "idle"


async def test_conflict(store):
    c = await store.claim(BOARD, 30, "blink", "a@h")
    with pytest.raises(ClaimConflict) as exc:
        await store.claim(BOARD, 30, "other", "b@h")
    assert exc.value.existing == c
    assert str(exc.value) == "claimed by a@h until 12:30 UTC (blink)"


async def test_override_reason_does_not_override_live_claim(store):
    await store.claim(BOARD, 30, "blink", "a@h")
    with pytest.raises(ClaimConflict):
        await store.claim(BOARD, 30, "x", "b@h", override_reason="urgent")


async def test_override_reason_stored(store):
    c = await store.claim(BOARD, 30, "x", "a@h", override_reason="owner said ok")
    assert store.get(c.claim_id).override_reason == "owner said ok"


async def test_claim_id_for_other_board(store):
    c = await store.claim(BOARD, 30, "x", "a@h")
    with pytest.raises(ValueError, match="other board|different board"):
        await store.claim("ps1:pi1", 30, "x", "a@h", claim_id=c.claim_id)


async def test_wrong_claim_id_on_claimed_board_conflicts(store):
    await store.claim(BOARD, 30, "x", "a@h")
    with pytest.raises(ClaimConflict):
        await store.claim(BOARD, 30, "x", "a@h", claim_id="c-nope")


async def test_release(store, tmp_path):
    c = await store.claim(BOARD, 30, "x", "a@h")
    await store.release(c.claim_id)
    assert store.get(c.claim_id) is None
    assert store.for_board(BOARD) is None
    assert not (tmp_path / "claims" / "welland_sw1-p1.json").exists()
    with pytest.raises(KeyError):
        await store.release(c.claim_id)


async def test_expiry_drops_claim_and_file(store, clock, tmp_path):
    c = await store.claim(BOARD, 30, "x", "a@h")
    clock.advance(minutes=30)
    assert store.get(c.claim_id) is None
    assert store.all() == []
    assert not (tmp_path / "claims" / "welland_sw1-p1.json").exists()
    c2 = await store.claim(BOARD, 30, "y", "b@h")
    assert c2.holder == "b@h"


async def test_expired_claim_id_cannot_renew_but_board_free(store, clock):
    c = await store.claim(BOARD, 30, "x", "a@h")
    clock.advance(minutes=31)
    with pytest.raises(ClaimGone, match="expired; claim again"):
        await store.claim(BOARD, 30, "x", "a@h", claim_id=c.claim_id)
    assert store.for_board(BOARD) is None  # no claim was created
    c2 = await store.claim(BOARD, 30, "x", "a@h")
    assert c2.started == clock.now and c2.claim_id != c.claim_id


async def test_released_claim_id_cannot_renew(store):
    c = await store.claim(BOARD, 30, "x", "a@h")
    await store.release(c.claim_id)
    with pytest.raises(ClaimGone):
        await store.claim(BOARD, 30, "x", "a@h", claim_id=c.claim_id)


async def test_survives_new_store(tmp_path, clock):
    c = await ClaimStore(tmp_path, clock).claim(BOARD, 30, "x", "a@h")
    again = ClaimStore(tmp_path, clock).get(c.claim_id)
    assert again is not None
    assert (again.board, again.started, again.expires) == (BOARD, c.started, c.expires)


async def test_file_format_and_modes(store, tmp_path):
    c = await store.claim(BOARD, 30, "blink", "a@h")
    d = tmp_path / "claims"
    f = d / "welland_sw1-p1.json"
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    data = json.loads(f.read_text())
    assert data == {
        "claim_id": c.claim_id,
        "board": BOARD,
        "holder": "a@h",
        "purpose": "blink",
        "started": "2026-11-26T12:00:00Z",
        "expires": "2026-11-26T12:30:00Z",
        "override_reason": None,
        "tt_state_at_claim": None,
    }
    assert sorted(p.name for p in d.iterdir()) == [".lock", "welland_sw1-p1.json"]


async def test_concurrent_claims_one_conflict(store):
    results = await asyncio.gather(
        store.claim(BOARD, 30, "x", "a@h"),
        store.claim(BOARD, 30, "y", "b@h"),
        return_exceptions=True,
    )
    assert sum(isinstance(r, Claim) for r in results) == 1
    assert sum(isinstance(r, ClaimConflict) for r in results) == 1


async def test_holder_connected_in_memory(tmp_path, clock):
    store = ClaimStore(tmp_path, clock)
    c = await store.claim(BOARD, 30, "x", "a@h")
    assert c.holder_connected is False  # disconnected holder still holds
    store.set_connected("a@h", True)
    assert store.get(c.claim_id).holder_connected is True
    assert store.for_board(BOARD).holder_connected is True
    store.set_connected("a@h", False)
    assert store.get(c.claim_id).holder_connected is False
    store.set_connected("a@h", True)
    assert ClaimStore(tmp_path, clock).get(c.claim_id).holder_connected is False


async def test_active_count_and_all(store, clock):
    a = await store.claim(BOARD, 30, "x", "a@h")
    await store.claim("ps1:pi1", 5, "y", "b@h")
    assert store.active_count() == 2
    assert len(store.all()) == 2
    assert a.claim_id in {c.claim_id for c in store.all()}
    clock.advance(minutes=6)
    assert store.active_count() == 1
    assert [c.claim_id for c in store.all()] == [a.claim_id]


async def test_corrupt_file_ignored_not_deleted(store, tmp_path, caplog):
    await store.claim(BOARD, 30, "x", "a@h")
    bad = tmp_path / "claims" / "ps1_pi1.json"
    bad.write_text("{not json")
    with caplog.at_level(logging.WARNING):
        assert len(store.all()) == 1
        assert store.for_board("ps1:pi1") is None
    assert bad.exists()
    assert "ps1_pi1.json" in caplog.text
    # a corrupt file does not block claiming... it is replaced by the new claim
    c = await store.claim("ps1:pi1", 30, "z", "a@h")
    assert store.for_board("ps1:pi1") == c


def test_corrupt_file_warned_once_per_mtime(store, tmp_path, caplog):
    bad = tmp_path / "claims" / "ps1_pi1.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("{not json")
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            store.active_count()
        assert caplog.text.count("ps1_pi1.json") == 1
        st = bad.stat()
        bad.write_text("{still not json")
        os.utime(bad, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
        store.active_count()
        store.active_count()
        assert caplog.text.count("ps1_pi1.json") == 2


async def test_tt_state_at_claim_set_and_kept(store):
    c = await store.claim(BOARD, 30, "x", "a@h", tt_state_at_claim="idle")
    assert store.get(c.claim_id).tt_state_at_claim == "idle"
    r = await store.claim(BOARD, 30, "x", "a@h", claim_id=c.claim_id)
    assert r.tt_state_at_claim == "idle"
    r = await store.claim(BOARD, 30, "x", "a@h", claim_id=c.claim_id, tt_state_at_claim="busy")
    assert store.get(c.claim_id).tt_state_at_claim == "busy"


async def test_two_stores_same_dir_one_conflict(tmp_path, clock):
    a, b = ClaimStore(tmp_path, clock), ClaimStore(tmp_path, clock)
    results = await asyncio.gather(
        a.claim(BOARD, 30, "x", "a@h"),
        b.claim(BOARD, 30, "y", "b@h"),
        return_exceptions=True,
    )
    assert sum(isinstance(r, ClaimConflict) for r in results) == 1


async def test_dirs_are_private(tmp_path, clock):
    state = tmp_path / "new" / "state"
    await ClaimStore(state, clock).claim(BOARD, 30, "x", "a@h")
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE((state / "claims").stat().st_mode) == 0o700


async def test_naive_times_read_as_utc(store, tmp_path):
    c = await store.claim(BOARD, 30, "x", "a@h")
    f = tmp_path / "claims" / "welland_sw1-p1.json"
    data = json.loads(f.read_text())
    data["started"], data["expires"] = "2026-11-26T12:00:00", "2026-11-26T12:30:00"
    f.write_text(json.dumps(data))
    got = store.get(c.claim_id)
    assert got.expires == datetime(2026, 11, 26, 12, 30, tzinfo=UTC)


async def test_expiry_does_not_delete_fresh_claim(store, clock, tmp_path):
    c = await store.claim(BOARD, 30, "x", "a@h")
    f = tmp_path / "claims" / "welland_sw1-p1.json"
    clock.advance(minutes=31)
    # another process replaced the file with a fresh claim after we parsed the old one
    fresh = json.loads(f.read_text())
    fresh.update(claim_id="c-fresh", expires="2026-11-26T14:00:00Z")
    f.write_text(json.dumps(fresh))
    store._drop_expired(f, c.claim_id)
    assert f.exists()


async def test_expired_read_does_not_block_while_other_store_holds_lock(tmp_path, clock):
    a, b = ClaimStore(tmp_path, clock), ClaimStore(tmp_path, clock)
    c = await a.claim(BOARD, 30, "x", "a@h")
    clock.advance(minutes=31)
    f = tmp_path / "claims" / "welland_sw1-p1.json"
    async with a._locked():
        assert b.all() == []  # returns promptly (would deadlock with a blocking flock)
        assert f.exists()  # drop skipped while the lock is busy
    assert b.get(c.claim_id) is None
    assert not f.exists()
