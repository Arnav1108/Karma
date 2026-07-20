"""Unit tests for IntakeService.submit_answer.

intake_step and lock_brief are monkeypatched at the api.services.intake_service
module level so no real OpenAI call is ever made and lock-brief calls can be
counted precisely. FakeSessionStore (shared with the create_session tests) is
asserted against directly, not just the returned value, so atomicity and
"not called at all" claims are checked against real store/spy state rather
than inferred from the return value alone.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import openai
import pytest

from agents.nodes.node1_intake import IntakeQuestion, IntakeSessionState, blank_brief
from api.services import intake_service as intake_service_module
from api.services.exceptions import (
    LlmUpstreamError,
    SessionAlreadyLockedError,
    SessionNotFoundError,
    TurnInProgressError,
)
from api.services.intake_service import IntakeService
from tests.intake_service_fakes import ExpiringMidTurnSessionStore, FakePostgresClient, FakeSessionStore

pytestmark = pytest.mark.asyncio


def _known_question() -> IntakeQuestion:
    return IntakeQuestion(question_id="primary_use_case", text="What will you use it for?", kind="sequence")


async def _seed_session(store, *, status: str = "asking") -> "SessionRecord":
    brief = blank_brief(uuid4(), uuid4(), uuid4())
    state = IntakeSessionState(
        brief=brief,
        history=[{"role": "assistant", "content": "What's your budget?"}],
        asked_so_far=["budget"],
        current_question_id="budget",
    )
    record = await store.create(state)
    record.status = status
    return record


async def test_submit_answer_normal_turn_stores_intake_step_result(monkeypatch):
    store = FakeSessionStore()
    service = IntakeService(store, FakePostgresClient())
    record = await _seed_session(store)
    expected_question = _known_question()

    def fake_intake_step(state, answer, phrase_fn):
        state.history.append({"role": "user", "content": answer})
        state.history.append({"role": "assistant", "content": expected_question.text})
        state.current_question_id = expected_question.question_id
        return state, expected_question, False

    monkeypatch.setattr(intake_service_module, "intake_step", fake_intake_step)

    updated, question, locked = await service.submit_answer(record.session_id, "60000")

    assert question == expected_question
    assert locked is False
    assert updated.status == "asking"
    assert store.records[record.session_id].status == "asking"
    assert store.records[record.session_id].state.current_question_id == "primary_use_case"
    assert store.records[record.session_id].state.history[-1] == {
        "role": "assistant",
        "content": expected_question.text,
    }
    assert store.update_calls == 1


async def test_submit_answer_llm_failure_leaves_stored_state_unchanged(monkeypatch):
    store = FakeSessionStore()
    service = IntakeService(store, FakePostgresClient())
    record = await _seed_session(store)
    snapshot_before = record.state.model_dump()

    def fake_intake_step(state, answer, phrase_fn):
        # Simulate extract_turn succeeding and mutating its argument before the
        # internal intake_begin phrasing call fails, per plan section 4.
        state.history.append({"role": "user", "content": "SHOULD NOT PERSIST"})
        raise openai.OpenAIError("upstream boom")

    monkeypatch.setattr(intake_service_module, "intake_step", fake_intake_step)

    with pytest.raises(LlmUpstreamError):
        await service.submit_answer(record.session_id, "60000")

    # The atomicity claim: the object actually sitting in the store is
    # byte-for-byte the same as before the failed turn, not just "an
    # exception was raised."
    assert store.records[record.session_id].state.model_dump() == snapshot_before
    assert store.records[record.session_id].status == "asking"
    assert store.update_calls == 0


async def test_submit_answer_sequence_exhaustion_auto_locks(monkeypatch):
    store = FakeSessionStore()
    service = IntakeService(store, FakePostgresClient())
    record = await _seed_session(store)
    lock_brief_calls = []

    def fake_intake_step(state, answer, phrase_fn):
        state.history.append({"role": "user", "content": answer})
        return state, None, False

    def spying_lock_brief(brief):
        lock_brief_calls.append(brief)
        data = brief.model_dump()
        data["status"] = "locked"
        return brief.model_validate(data)

    monkeypatch.setattr(intake_service_module, "intake_step", fake_intake_step)
    monkeypatch.setattr(intake_service_module, "lock_brief", spying_lock_brief)

    updated, question, locked = await service.submit_answer(record.session_id, "final answer")

    assert question is None
    assert locked is True
    assert len(lock_brief_calls) == 1
    assert updated.status == "locked"
    assert store.records[record.session_id].status == "locked"


async def test_submit_answer_explicit_early_lock_does_not_double_lock(monkeypatch):
    store = FakeSessionStore()
    service = IntakeService(store, FakePostgresClient())
    record = await _seed_session(store)
    lock_brief_calls = []

    def fake_intake_step(state, answer, phrase_fn):
        state.history.append({"role": "user", "content": answer})
        return state, None, True

    def spying_lock_brief(brief):
        lock_brief_calls.append(brief)
        return brief

    monkeypatch.setattr(intake_service_module, "intake_step", fake_intake_step)
    monkeypatch.setattr(intake_service_module, "lock_brief", spying_lock_brief)

    updated, question, locked = await service.submit_answer(record.session_id, "done")

    assert locked is True
    assert lock_brief_calls == []  # intake_step already locked -> service must not call lock_brief
    assert updated.status == "locked"


async def test_submit_answer_explicit_early_lock_persists_to_postgres(monkeypatch):
    """intake_step can lock the brief on its own (the "done"/"stop" + floor_met
    early-exit), never passing through the service's `if not locked and question
    is None:` exhaustion branch. persist_locked_brief must still fire for this
    path — the service's `if locked:` persistence check runs after both possible
    sources of locked=True are resolved, not only inside the exhaustion branch."""
    store = FakeSessionStore()
    postgres = FakePostgresClient()
    service = IntakeService(store, postgres)
    record = await _seed_session(store)

    def fake_intake_step(state, answer, phrase_fn):
        state.history.append({"role": "user", "content": answer})
        state.brief = state.brief.model_copy(update={"status": "locked"})
        return state, None, True

    monkeypatch.setattr(intake_service_module, "intake_step", fake_intake_step)

    updated, question, locked = await service.submit_answer(record.session_id, "done")

    assert locked is True
    assert len(postgres.persist_calls) == 1
    persisted_brief, persisted_session_id = postgres.persist_calls[0]
    assert persisted_session_id == record.session_id
    assert persisted_brief.status == "locked"
    assert persisted_brief is updated.state.brief


async def test_submit_answer_session_not_found():
    store = FakeSessionStore()
    service = IntakeService(store, FakePostgresClient())

    with pytest.raises(SessionNotFoundError):
        await service.submit_answer("does-not-exist", "42")


async def test_submit_answer_already_locked_does_not_call_intake_step(monkeypatch):
    store = FakeSessionStore()
    service = IntakeService(store, FakePostgresClient())
    record = await _seed_session(store, status="locked")
    call_count = {"n": 0}

    def fake_intake_step(state, answer, phrase_fn):
        call_count["n"] += 1
        return state, None, True

    monkeypatch.setattr(intake_service_module, "intake_step", fake_intake_step)

    with pytest.raises(SessionAlreadyLockedError):
        await service.submit_answer(record.session_id, "42")

    assert call_count["n"] == 0


async def test_submit_answer_turn_in_progress_fails_fast_not_queued():
    store = FakeSessionStore()
    service = IntakeService(store, FakePostgresClient())
    record = await _seed_session(store)

    await record.lock.acquire()
    try:
        with pytest.raises(TurnInProgressError):
            # A short timeout proves this returns fast; if TurnInProgressError
            # were replaced by blocking on the contended lock, this would
            # raise asyncio.TimeoutError instead, not the expected error.
            await asyncio.wait_for(service.submit_answer(record.session_id, "42"), timeout=0.2)
    finally:
        record.lock.release()


async def test_submit_answer_session_expires_mid_turn(monkeypatch):
    store = ExpiringMidTurnSessionStore()
    service = IntakeService(store, FakePostgresClient())
    record = await _seed_session(store)
    store.simulate_expiry_on_next_update = True

    def fake_intake_step(state, answer, phrase_fn):
        state.history.append({"role": "user", "content": answer})
        return state, _known_question(), False

    monkeypatch.setattr(intake_service_module, "intake_step", fake_intake_step)

    with pytest.raises(SessionNotFoundError):
        await service.submit_answer(record.session_id, "42")
