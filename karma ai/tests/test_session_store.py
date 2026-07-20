"""Unit tests for the in-memory intake session store.

Pure unit tests: no DB, no network, no imports from the core pipeline
(agents/). Time is controlled by monkeypatching the module-level
_utcnow() function with a fake clock - tests never sleep for real.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from api.services import session_store as store_module
from api.services.session_store import InMemorySessionStore

pytestmark = pytest.mark.asyncio


class FakeClock:
    """Controllable clock swapped in for the module's _utcnow()."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    fake = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    monkeypatch.setattr(store_module, "_utcnow", fake)
    return fake


async def test_create_returns_asking_session_with_valid_uuid(clock):
    store = InMemorySessionStore()
    record = await store.create(state={"foo": "bar"})

    assert record.status == "asking"
    assert record.created_at == record.last_accessed_at

    # Parse without forcing a version, then check the version bits and
    # that the string round-trips exactly - a real uuid4 does both.
    parsed = uuid.UUID(record.session_id)
    assert parsed.version == 4
    assert str(parsed) == record.session_id


async def test_get_on_live_session_refreshes_last_accessed_at(clock):
    store = InMemorySessionStore(asking_ttl_seconds=100)
    record = await store.create(state="x")
    created_at = record.created_at

    clock.advance(40)  # partway through the 100s asking TTL
    fetched = await store.get(record.session_id)

    assert fetched is not None
    assert fetched.last_accessed_at == clock.now
    assert fetched.last_accessed_at > created_at


async def test_asking_session_expires_and_is_lazily_evicted(clock):
    store = InMemorySessionStore(asking_ttl_seconds=1)
    record = await store.create(state="x")

    clock.advance(1.5)  # past the 1s asking TTL

    first = await store.get(record.session_id)
    assert first is None

    # A second call must not resurrect it - proves real eviction
    # happened, not just a None returned for this one call.
    second = await store.get(record.session_id)
    assert second is None
    assert record.session_id not in store._sessions


async def test_locked_session_survives_past_asking_ttl(clock):
    store = InMemorySessionStore(asking_ttl_seconds=1, locked_ttl_seconds=100)
    record = await store.create(state="x")
    await store.update(record.session_id, state="y", status="locked")

    clock.advance(5)  # past the asking TTL, well under the locked TTL

    fetched = await store.get(record.session_id)
    assert fetched is not None
    assert fetched.status == "locked"


async def test_locked_session_expires_past_locked_ttl(clock):
    store = InMemorySessionStore(asking_ttl_seconds=1, locked_ttl_seconds=10)
    record = await store.create(state="x")
    await store.update(record.session_id, state="y", status="locked")

    clock.advance(11)  # past the 10s locked TTL

    fetched = await store.get(record.session_id)
    assert fetched is None


async def test_update_on_expired_session_returns_none(clock):
    store = InMemorySessionStore(asking_ttl_seconds=1)
    record = await store.create(state="x")

    clock.advance(2)

    result = await store.update(record.session_id, state="new", status="asking")
    assert result is None


async def test_delete_is_idempotent(clock):
    store = InMemorySessionStore()
    record = await store.create(state="x")

    first = await store.delete(record.session_id)
    second = await store.delete(record.session_id)

    assert first is True
    assert second is False  # documented contract: True only if it existed


async def test_sweep_expired_removes_only_expired(clock):
    store = InMemorySessionStore(asking_ttl_seconds=10)
    fresh = await store.create(state="fresh")
    expired = await store.create(state="expired")

    clock.advance(5)
    # Touch "fresh" so its last_accessed_at stays recent; "expired" is
    # left untouched since creation.
    await store.get(fresh.session_id)

    clock.advance(6)  # expired: 11s since creation; fresh: 6s since touch

    count = await store.sweep_expired()

    assert count == 1
    assert await store.get(fresh.session_id) is not None
    assert await store.get(expired.session_id) is None


async def test_create_gives_each_record_a_distinct_lock(clock):
    store = InMemorySessionStore()
    a = await store.create(state="a")
    b = await store.create(state="b")

    assert a.lock is not b.lock
    assert isinstance(a.lock, asyncio.Lock)
