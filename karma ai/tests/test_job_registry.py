"""Unit tests for the in-memory build job registry.

Pure unit tests: no DB, no network, no imports from the core pipeline
(agents/). Time is controlled by monkeypatching the module-level
_utcnow() function with a fake clock - tests never sleep for real.
Mirrors tests/test_session_store.py's FakeClock pattern.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from api.services import job_registry as registry_module
from api.services.job_registry import InMemoryJobRegistry, JobRecord

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
    monkeypatch.setattr(registry_module, "_utcnow", fake)
    return fake


async def test_create_returns_queued_job_with_valid_uuid(clock):
    registry = InMemoryJobRegistry()
    record = await registry.create(session_id="sess-1")

    assert record.status == "queued"
    assert record.session_id == "sess-1"
    assert record.created_at == clock.now
    assert record.started_at is None
    assert record.finished_at is None
    assert record.state is None
    assert record.warnings == []

    # Parse without forcing a version, then check the version bits and
    # that the string round-trips exactly - a real uuid4 does both.
    parsed = uuid.UUID(record.build_id)
    assert parsed.version == 4
    assert str(parsed) == record.build_id


async def test_get_on_terminal_job_past_ttl_evicts_and_returns_none(clock):
    registry = InMemoryJobRegistry(terminal_ttl_seconds=100)
    record = await registry.create(session_id="sess-1")
    await registry.update(
        record.build_id, status="succeeded", finished_at=clock.now
    )

    clock.advance(101)  # past the 100s terminal TTL, measured from finished_at

    first = await registry.get(record.build_id)
    assert first is None

    # A second call must not resurrect it - proves real eviction
    # happened, not just a None returned for this one call.
    second = await registry.get(record.build_id)
    assert second is None
    assert record.build_id not in registry._jobs


async def test_get_on_nonterminal_job_never_expires_regardless_of_age(clock):
    registry = InMemoryJobRegistry(terminal_ttl_seconds=100)
    record = await registry.create(session_id="sess-1")
    await registry.update(record.build_id, status="running", started_at=clock.now)

    # Advance far past the terminal TTL while status stays non-terminal.
    clock.advance(100_000)

    fetched = await registry.get(record.build_id)
    assert fetched is not None
    assert fetched.status == "running"
    assert fetched.build_id in registry._jobs


async def test_terminal_ttl_applied_uniformly_incorrectly_expires_running_job(clock):
    """Proves test 3 actually exercises the terminal-only TTL branch.

    Temporarily patches _is_expired to apply the TTL regardless of status
    (the bug this design deliberately avoids) and shows the non-terminal
    job WOULD be evicted under that (wrong) policy - i.e. the assertion
    in test_get_on_nonterminal_job_never_expires_regardless_of_age is not
    vacuously true.
    """
    registry = InMemoryJobRegistry(terminal_ttl_seconds=100)
    record = await registry.create(session_id="sess-1")
    await registry.update(record.build_id, status="running", started_at=clock.now)

    def _is_expired_uniform(self, rec, now):
        # Same age math as the real implementation, but WITHOUT the
        # terminal-status guard - measures age from created_at as a stand-in
        # "activity" timestamp since running jobs have no finished_at.
        reference = rec.finished_at or rec.created_at
        age = (now - reference).total_seconds()
        return age > self._terminal_ttl_seconds

    original = InMemoryJobRegistry._is_expired
    InMemoryJobRegistry._is_expired = _is_expired_uniform
    try:
        clock.advance(100_000)
        fetched = await registry.get(record.build_id)
        assert fetched is None  # wrong policy incorrectly evicts the running job
    finally:
        InMemoryJobRegistry._is_expired = original

    # Restore proves the fixture object itself is unaffected by the monkeypatch
    # of the class method (a fresh registry to avoid the now-corrupted dict).
    registry2 = InMemoryJobRegistry(terminal_ttl_seconds=100)
    record2 = await registry2.create(session_id="sess-1")
    await registry2.update(record2.build_id, status="running", started_at=clock.now)
    still_there = await registry2.get(record2.build_id)
    assert still_there is not None


async def test_update_running_to_succeeded_sets_finished_at(clock):
    registry = InMemoryJobRegistry()
    record = await registry.create(session_id="sess-1")
    await registry.update(record.build_id, status="running", started_at=clock.now)

    clock.advance(30)
    updated = await registry.update(
        record.build_id, status="succeeded", state={"build_card": "x"}
    )

    assert updated is not None
    assert updated.status == "succeeded"
    assert updated.finished_at == clock.now


async def test_update_to_nonterminal_status_does_not_set_finished_at(clock):
    registry = InMemoryJobRegistry()
    record = await registry.create(session_id="sess-1")

    updated = await registry.update(record.build_id, status="running", started_at=clock.now)

    assert updated is not None
    assert updated.status == "running"
    assert updated.finished_at is None


async def test_update_respects_explicitly_passed_finished_at(clock):
    registry = InMemoryJobRegistry()
    record = await registry.create(session_id="sess-1")
    explicit_time = clock.now - timedelta(seconds=5)

    updated = await registry.update(
        record.build_id, status="failed", finished_at=explicit_time, error_code="BUILD_TIMEOUT"
    )

    assert updated is not None
    assert updated.finished_at == explicit_time


async def test_update_unknown_field_raises_type_error(clock):
    registry = InMemoryJobRegistry()
    record = await registry.create(session_id="sess-1")

    with pytest.raises(TypeError):
        await registry.update(record.build_id, bogus_field="x")


async def test_update_on_missing_job_returns_none(clock):
    registry = InMemoryJobRegistry()

    result = await registry.update("nonexistent-id", status="running")
    assert result is None


async def test_sweep_expired_removes_only_expired_terminal_jobs(clock):
    registry = InMemoryJobRegistry(terminal_ttl_seconds=10)

    stale_terminal = await registry.create(session_id="sess-stale")
    await registry.update(
        stale_terminal.build_id, status="failed", finished_at=clock.now
    )

    long_running = await registry.create(session_id="sess-running")
    await registry.update(long_running.build_id, status="running", started_at=clock.now)

    clock.advance(6)  # stale_terminal now 6s old; not yet past the 10s TTL

    fresh_terminal = await registry.create(session_id="sess-fresh")
    await registry.update(
        fresh_terminal.build_id, status="succeeded", finished_at=clock.now
    )

    clock.advance(5)  # stale_terminal: 11s old (expired); fresh_terminal: 5s old (not)

    count = await registry.sweep_expired()

    assert count == 1
    assert stale_terminal.build_id not in registry._jobs
    assert await registry.get(fresh_terminal.build_id) is not None
    assert await registry.get(long_running.build_id) is not None


async def test_sweep_expired_never_touches_nonterminal_jobs_regardless_of_age(clock):
    registry = InMemoryJobRegistry(terminal_ttl_seconds=10)
    record = await registry.create(session_id="sess-1")
    await registry.update(record.build_id, status="running", started_at=clock.now)

    clock.advance(10_000)  # wildly past the terminal TTL

    count = await registry.sweep_expired()

    assert count == 0
    assert record.build_id in registry._jobs


async def test_lru_cap_evicts_oldest_finished_terminal_jobs_on_overflow(clock):
    registry = InMemoryJobRegistry(max_records=3)

    ids = []
    for i in range(3):
        record = await registry.create(session_id=f"sess-{i}")
        await registry.update(record.build_id, status="succeeded", finished_at=clock.now)
        ids.append(record.build_id)
        clock.advance(1)

    assert len(registry._jobs) == 3

    # A 4th job pushes the store over the cap; the oldest-finished
    # terminal job (ids[0]) should be evicted to bring it back to 3.
    overflow_record = await registry.create(session_id="sess-overflow")
    await registry.update(overflow_record.build_id, status="succeeded", finished_at=clock.now)

    assert len(registry._jobs) == 3
    assert ids[0] not in registry._jobs
    assert ids[1] in registry._jobs
    assert ids[2] in registry._jobs
    assert overflow_record.build_id in registry._jobs


async def test_lru_cap_never_evicts_nonterminal_jobs(clock):
    registry = InMemoryJobRegistry(max_records=2)

    running_a = await registry.create(session_id="sess-a")
    await registry.update(running_a.build_id, status="running", started_at=clock.now)
    running_b = await registry.create(session_id="sess-b")
    await registry.update(running_b.build_id, status="running", started_at=clock.now)

    # A 3rd job overflows the cap=2, but there are no terminal jobs to
    # evict - both running jobs must survive even though the store now
    # temporarily exceeds max_records.
    running_c = await registry.create(session_id="sess-c")
    await registry.update(running_c.build_id, status="running", started_at=clock.now)

    assert len(registry._jobs) == 3
    assert running_a.build_id in registry._jobs
    assert running_b.build_id in registry._jobs
    assert running_c.build_id in registry._jobs


async def test_job_record_has_no_lock_field(clock):
    field_names = {f.name for f in dataclasses.fields(JobRecord)}
    assert "lock" not in field_names

    registry = InMemoryJobRegistry()
    record = await registry.create(session_id="sess-1")
    assert not hasattr(record, "lock")
