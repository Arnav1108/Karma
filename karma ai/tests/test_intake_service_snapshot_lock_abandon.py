"""Unit tests for IntakeService.get_snapshot, lock_early, and abandon.

lock_brief is monkeypatched at the api.services.intake_service module level so
lock-brief calls can be counted precisely, matching the spying style used in
test_intake_service_submit_answer.py. FakeSessionStore.peek/get are wrapped
with local spies (not modified in the shared fakes module) to distinguish
which one a given method actually calls.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from agents.nodes.node1_intake import IntakeSessionState, blank_brief
from api.services import intake_service as intake_service_module
from api.services.exceptions import (
    BriefFloorNotMetError,
    SessionAlreadyLockedError,
    SessionNotFoundError,
    TurnInProgressError,
)
from api.services.intake_service import IntakeService
from tests.intake_service_fakes import FakeSessionStore

pytestmark = pytest.mark.asyncio


async def _seed_session(store, *, status: str = "asking"):
    brief = blank_brief(uuid4(), uuid4(), uuid4())
    state = IntakeSessionState(brief=brief, history=[])
    record = await store.create(state)
    record.status = status
    return record


async def _seed_floor_met_session(store, *, status: str = "asking"):
    brief = blank_brief(uuid4(), uuid4(), uuid4())
    brief.budget.comfortable_max = 60000
    brief.purpose.sub_case = "1080p esports gaming"
    state = IntakeSessionState(brief=brief, history=[])
    record = await store.create(state)
    record.status = status
    return record


def _spy_store_methods(monkeypatch, store, *names):
    calls = {name: [] for name in names}
    for name in names:
        original = getattr(store, name)

        async def spy(session_id, _name=name, _original=original):
            calls[_name].append(session_id)
            return await _original(session_id)

        monkeypatch.setattr(store, name, spy)
    return calls


# --- get_snapshot ------------------------------------------------------


async def test_get_snapshot_live_session_returns_it_via_peek(monkeypatch):
    store = FakeSessionStore()
    service = IntakeService(store)
    record = await _seed_session(store)

    calls = _spy_store_methods(monkeypatch, store, "peek", "get")

    result = await service.get_snapshot(record.session_id)

    assert result.session_id == record.session_id
    assert calls["peek"] == [record.session_id]
    assert calls["get"] == []


async def test_get_snapshot_missing_session_raises_not_found():
    store = FakeSessionStore()
    service = IntakeService(store)

    with pytest.raises(SessionNotFoundError):
        await service.get_snapshot("does-not-exist")


# --- lock_early ----------------------------------------------------------


async def test_lock_early_success_locks_and_calls_lock_brief_once(monkeypatch):
    store = FakeSessionStore()
    service = IntakeService(store)
    record = await _seed_floor_met_session(store)
    lock_brief_calls = []

    def spying_lock_brief(brief):
        lock_brief_calls.append(brief)
        data = brief.model_dump()
        data["status"] = "locked"
        return brief.model_validate(data)

    monkeypatch.setattr(intake_service_module, "lock_brief", spying_lock_brief)

    updated = await service.lock_early(record.session_id)

    assert updated.status == "locked"
    assert len(lock_brief_calls) == 1
    assert store.records[record.session_id].status == "locked"
    assert updated.state.brief.status == "locked"


async def test_lock_early_floor_not_met_missing_budget_only(monkeypatch):
    store = FakeSessionStore()
    service = IntakeService(store)
    brief = blank_brief(uuid4(), uuid4(), uuid4())
    brief.purpose.sub_case = "video editing rig"  # budget left at sentinel 0
    state = IntakeSessionState(brief=brief, history=[])
    record = await store.create(state)
    lock_brief_calls = []
    monkeypatch.setattr(
        intake_service_module,
        "lock_brief",
        lambda b: lock_brief_calls.append(b) or b,
    )

    with pytest.raises(BriefFloorNotMetError) as exc_info:
        await service.lock_early(record.session_id)

    assert exc_info.value.missing == ["budget"]
    assert lock_brief_calls == []
    assert store.records[record.session_id].status == "asking"


async def test_lock_early_floor_not_met_missing_both(monkeypatch):
    store = FakeSessionStore()
    service = IntakeService(store)
    record = await _seed_session(store)  # blank_brief: budget and sub_case both sentinel
    lock_brief_calls = []
    monkeypatch.setattr(
        intake_service_module,
        "lock_brief",
        lambda b: lock_brief_calls.append(b) or b,
    )

    with pytest.raises(BriefFloorNotMetError) as exc_info:
        await service.lock_early(record.session_id)

    assert exc_info.value.missing == ["budget", "primary_use_case"]
    assert lock_brief_calls == []
    assert store.records[record.session_id].status == "asking"


async def test_lock_early_already_locked_does_not_call_lock_brief(monkeypatch):
    store = FakeSessionStore()
    service = IntakeService(store)
    record = await _seed_floor_met_session(store, status="locked")
    lock_brief_calls = []
    monkeypatch.setattr(
        intake_service_module,
        "lock_brief",
        lambda b: lock_brief_calls.append(b) or b,
    )

    with pytest.raises(SessionAlreadyLockedError):
        await service.lock_early(record.session_id)

    assert lock_brief_calls == []


async def test_lock_early_turn_in_progress_fails_fast_not_queued():
    store = FakeSessionStore()
    service = IntakeService(store)
    record = await _seed_floor_met_session(store)

    await record.lock.acquire()
    try:
        with pytest.raises(TurnInProgressError):
            # Short timeout proves this returns fast; if TurnInProgressError
            # were replaced by blocking on the contended lock, this would
            # raise asyncio.TimeoutError instead of the expected error.
            await asyncio.wait_for(service.lock_early(record.session_id), timeout=0.2)
    finally:
        record.lock.release()


async def test_lock_early_missing_session_raises_not_found():
    store = FakeSessionStore()
    service = IntakeService(store)

    with pytest.raises(SessionNotFoundError):
        await service.lock_early("does-not-exist")


# --- abandon ---------------------------------------------------------------


async def test_abandon_existing_session_returns_none_and_deletes(monkeypatch):
    store = FakeSessionStore()
    service = IntakeService(store)
    record = await _seed_session(store)

    result = await service.abandon(record.session_id)

    assert result is None
    assert await store.get(record.session_id) is None


async def test_abandon_missing_session_does_not_raise():
    store = FakeSessionStore()
    service = IntakeService(store)

    result = await service.abandon("does-not-exist")

    assert result is None
