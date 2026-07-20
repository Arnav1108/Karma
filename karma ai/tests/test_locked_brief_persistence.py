"""Unit tests for locked-brief Postgres persistence.

Covers PostgresClient.persist_locked_brief (agents/db/postgres.py) with a fake
cursor — no real DB connection — and the IntakeService wiring in lock_early /
submit_answer's auto-lock branch, proving the same atomicity guarantee already
established for LlmUpstreamError: a failed Postgres write leaves the in-memory
session state untouched (still "asking"), because the write happens before the
only line that mutates shared state, self._store.update(...).
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from uuid import uuid4

import pytest

from agents.db import postgres as postgres_module
from agents.db.postgres import PostgresClient
from agents.nodes.node1_intake import IntakeSessionState, blank_brief
from api.services import intake_service as intake_service_module
from api.services.exceptions import BriefPersistenceError
from api.services.intake_service import IntakeService
from tests.intake_service_fakes import FakePostgresClient, FakeSessionStore


# ---------------------------------------------------------------------------
# PostgresClient.persist_locked_brief — fake cursor, no real DB
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple] = []

    def execute(self, query, params) -> None:
        self.executed.append((query, params))


def _fake_cursor_cm(cursor: _FakeCursor):
    @contextmanager
    def _cm():
        yield cursor
    return _cm


def _locked_brief():
    brief = blank_brief(uuid4(), uuid4(), uuid4())
    brief.budget.comfortable_max = 60000
    brief.purpose.sub_case = "competitive_fps"
    return brief


def test_persist_locked_brief_inserts_with_expected_params(monkeypatch):
    cursor = _FakeCursor()
    monkeypatch.setattr(postgres_module, "_cursor", _fake_cursor_cm(cursor))

    client = PostgresClient()
    brief = _locked_brief()
    client.persist_locked_brief(brief, "session-abc")

    assert len(cursor.executed) == 1
    query, params = cursor.executed[0]
    assert "INSERT INTO locked_briefs" in query
    assert "ON CONFLICT (brief_id) DO NOTHING" in query
    assert params == (
        str(brief.brief_id), "session-abc", str(brief.user_id), str(brief.chat_id),
        brief.schema_version, brief.model_dump_json(), brief.updated_at,
    )
    parsed = json.loads(params[5])
    assert parsed["brief_id"] == str(brief.brief_id)


def test_persist_locked_brief_on_conflict_do_nothing_does_not_raise_on_duplicate(monkeypatch):
    cursor = _FakeCursor()
    monkeypatch.setattr(postgres_module, "_cursor", _fake_cursor_cm(cursor))

    client = PostgresClient()
    brief = _locked_brief()

    # First insert, then a retry of the same brief_id/session_id — the ON CONFLICT
    # DO NOTHING clause is what makes a caller retry after a partial failure safe;
    # this proves the client-side call itself never raises on a repeat.
    client.persist_locked_brief(brief, "session-abc")
    client.persist_locked_brief(brief, "session-abc")

    assert len(cursor.executed) == 2
    for _, params in cursor.executed:
        assert params[0] == str(brief.brief_id)


# ---------------------------------------------------------------------------
# IntakeService.lock_early — Postgres write failure atomicity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lock_early_postgres_failure_raises_and_leaves_state_asking():
    store = FakeSessionStore()
    postgres = FakePostgresClient()
    service = IntakeService(store, postgres)

    brief = _locked_brief()
    state = IntakeSessionState(brief=brief, history=[])
    record = await store.create(state)
    snapshot_before = record.state.model_dump()

    postgres.raise_on_persist = RuntimeError("connection refused")

    with pytest.raises(BriefPersistenceError):
        await service.lock_early(record.session_id)

    assert store.records[record.session_id].state.model_dump() == snapshot_before
    assert store.records[record.session_id].status == "asking"
    assert store.update_calls == 0
    assert postgres.persist_calls == []


@pytest.mark.asyncio
async def test_lock_early_postgres_success_persists_before_store_update():
    store = FakeSessionStore()
    postgres = FakePostgresClient()
    service = IntakeService(store, postgres)

    brief = _locked_brief()
    state = IntakeSessionState(brief=brief, history=[])
    record = await store.create(state)

    updated = await service.lock_early(record.session_id)

    assert updated.status == "locked"
    assert store.records[record.session_id].status == "locked"
    assert len(postgres.persist_calls) == 1
    persisted_brief, persisted_session_id = postgres.persist_calls[0]
    assert persisted_session_id == record.session_id
    assert persisted_brief.status == "locked"


# ---------------------------------------------------------------------------
# IntakeService.submit_answer's auto-lock branch — same atomicity proof
# ---------------------------------------------------------------------------

async def _seed_asking_session(store):
    brief = blank_brief(uuid4(), uuid4(), uuid4())
    state = IntakeSessionState(
        brief=brief,
        history=[{"role": "assistant", "content": "Anything else?"}],
        asked_so_far=["hard_constraints"],
        current_question_id="hard_constraints",
    )
    record = await store.create(state)
    return record


@pytest.mark.asyncio
async def test_submit_answer_auto_lock_postgres_failure_raises_and_leaves_state_asking(monkeypatch):
    store = FakeSessionStore()
    postgres = FakePostgresClient()
    service = IntakeService(store, postgres)
    record = await _seed_asking_session(store)
    snapshot_before = record.state.model_dump()

    def fake_intake_step(state, answer, phrase_fn):
        state.history.append({"role": "user", "content": answer})
        return state, None, False  # sequence exhausted -> auto-lock branch

    monkeypatch.setattr(intake_service_module, "intake_step", fake_intake_step)
    postgres.raise_on_persist = RuntimeError("connection refused")

    with pytest.raises(BriefPersistenceError):
        await service.submit_answer(record.session_id, "final answer")

    assert store.records[record.session_id].state.model_dump() == snapshot_before
    assert store.records[record.session_id].status == "asking"
    assert store.update_calls == 0
    assert postgres.persist_calls == []


@pytest.mark.asyncio
async def test_submit_answer_auto_lock_postgres_success_persists_before_store_update(monkeypatch):
    store = FakeSessionStore()
    postgres = FakePostgresClient()
    service = IntakeService(store, postgres)
    record = await _seed_asking_session(store)

    def fake_intake_step(state, answer, phrase_fn):
        state.history.append({"role": "user", "content": answer})
        return state, None, False

    monkeypatch.setattr(intake_service_module, "intake_step", fake_intake_step)

    updated, question, locked = await service.submit_answer(record.session_id, "final answer")

    assert locked is True
    assert question is None
    assert store.records[record.session_id].status == "locked"
    assert len(postgres.persist_calls) == 1
    persisted_brief, persisted_session_id = postgres.persist_calls[0]
    assert persisted_session_id == record.session_id
    assert persisted_brief.status == "locked"


@pytest.mark.asyncio
async def test_submit_answer_explicit_intake_step_lock_also_persists(monkeypatch):
    """intake_step can lock the brief itself (its own "done"/"stop" + floor_met
    early-exit inside extract_turn, which calls lock_brief() and returns
    status="locked" before submit_answer's exhaustion-check branch ever runs —
    `not locked` is False so that branch is skipped). persist_locked_brief must
    still fire for this path: the single `if locked:` check after both possible
    sources of locked=True covers it, regardless of which path set locked=True."""
    store = FakeSessionStore()
    postgres = FakePostgresClient()
    service = IntakeService(store, postgres)
    record = await _seed_asking_session(store)

    def fake_intake_step(state, answer, phrase_fn):
        state.history.append({"role": "user", "content": answer})
        return state, None, True  # already locked by intake_step itself

    monkeypatch.setattr(intake_service_module, "intake_step", fake_intake_step)

    updated, question, locked = await service.submit_answer(record.session_id, "done")

    assert locked is True
    assert store.records[record.session_id].status == "locked"
    assert len(postgres.persist_calls) == 1
    persisted_brief, persisted_session_id = postgres.persist_calls[0]
    assert persisted_session_id == record.session_id


@pytest.mark.asyncio
async def test_submit_answer_explicit_intake_step_lock_postgres_failure_leaves_state_unchanged(monkeypatch):
    """Same atomicity proof as the exhaustion-case test, but for the OTHER source
    of locked=True — intake_step's own early-exit. Before the fix, this path
    skipped persist_locked_brief entirely, so a Postgres failure here couldn't
    even be detected; this test would have passed vacuously (persist_calls == [])
    prior to the fix, which is exactly why the gap was invisible."""
    store = FakeSessionStore()
    postgres = FakePostgresClient()
    service = IntakeService(store, postgres)
    record = await _seed_asking_session(store)
    snapshot_before = record.state.model_dump()

    def fake_intake_step(state, answer, phrase_fn):
        state.history.append({"role": "user", "content": answer})
        return state, None, True  # already locked by intake_step itself

    monkeypatch.setattr(intake_service_module, "intake_step", fake_intake_step)
    postgres.raise_on_persist = RuntimeError("connection refused")

    with pytest.raises(BriefPersistenceError):
        await service.submit_answer(record.session_id, "done")

    assert store.records[record.session_id].state.model_dump() == snapshot_before
    assert store.records[record.session_id].status == "asking"
    assert store.update_calls == 0
