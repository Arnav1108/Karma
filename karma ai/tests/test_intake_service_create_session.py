"""Unit tests for IntakeService.create_session.

intake_begin is monkeypatched at the api.services.intake_service module level
so no real OpenAI call is ever made. Store state is asserted against a fake
in-memory SessionStore, not just the returned value, so the "nothing stored
on failure" claim is actually checked against store state.
"""

from __future__ import annotations

from uuid import UUID

import openai
import pytest

from agents.nodes.node1_intake import IntakeQuestion, IntakeSessionState, blank_brief
from api.services import intake_service as intake_service_module
from api.services.exceptions import LlmUpstreamError
from api.services.intake_service import IntakeService
from tests.intake_service_fakes import FakePostgresClient, FakeSessionStore

pytestmark = pytest.mark.asyncio


def _known_question() -> IntakeQuestion:
    return IntakeQuestion(question_id="budget", text="What's your budget?", kind="sequence")


async def test_create_session_stores_and_returns_on_success(monkeypatch):
    store = FakeSessionStore()
    service = IntakeService(store, FakePostgresClient())
    expected_question = _known_question()

    def fake_intake_begin(state, phrase_fn):
        state.history.append({"role": "assistant", "content": expected_question.text})
        state.current_question_id = expected_question.question_id
        return state, expected_question

    monkeypatch.setattr(intake_service_module, "intake_begin", fake_intake_begin)

    record, question = await service.create_session()

    assert question == expected_question
    assert store.create_calls == 1
    assert record.session_id in store.records
    assert store.records[record.session_id] is record
    assert isinstance(record.state, IntakeSessionState)
    assert record.state.current_question_id == "budget"
    assert record.state.history == [{"role": "assistant", "content": expected_question.text}]


async def test_create_session_raises_llm_upstream_error_and_stores_nothing(monkeypatch):
    store = FakeSessionStore()
    service = IntakeService(store, FakePostgresClient())

    def fake_intake_begin(state, phrase_fn):
        raise openai.OpenAIError("upstream boom")

    monkeypatch.setattr(intake_service_module, "intake_begin", fake_intake_begin)

    with pytest.raises(LlmUpstreamError):
        await service.create_session()

    # This is the atomicity claim: nothing was stored when intake_begin failed.
    assert store.create_calls == 0
    assert store.records == {}


async def test_create_session_calls_blank_brief_with_three_distinct_uuids(monkeypatch):
    store = FakeSessionStore()
    service = IntakeService(store, FakePostgresClient())
    captured_ids: list[UUID] = []

    def spying_blank_brief(brief_id, user_id, chat_id, schema_version="1.0"):
        captured_ids.extend([brief_id, user_id, chat_id])
        return blank_brief(brief_id, user_id, chat_id, schema_version)

    def fake_intake_begin(state, phrase_fn):
        return state, _known_question()

    monkeypatch.setattr(intake_service_module, "blank_brief", spying_blank_brief)
    monkeypatch.setattr(intake_service_module, "intake_begin", fake_intake_begin)

    await service.create_session()

    assert len(captured_ids) == 3
    assert all(isinstance(u, UUID) for u in captured_ids)
    assert len(set(captured_ids)) == 3
