"""Integration tests for api/routers/intake.py.

Builds the REAL app via api.main.create_app() (not a throwaway FastAPI()) so
DI (app.state.intake_service), the real InMemorySessionStore, the real
IntakeService, the real exception handlers, and now the real intake router
mount (create_app() calls register_exception_handlers(app) and
app.include_router(intake.router, prefix="/api/v1",
dependencies=[Depends(require_api_key)]) itself) are all wired exactly as
production does -- no test-local handler registration or router mounting
needed anymore. KARMA_API_KEYS is unset in the test environment, so
require_api_key no-ops and these tests don't send an API key.

intake_begin/intake_step are monkeypatched at the api.services.intake_service
module level (same style as tests/test_intake_service_submit_answer.py) so no
real OpenAI call happens. The real IntakeService singleton's Postgres client
is also swapped for FakePostgresClient (tests/intake_service_fakes.py) --
unavoidable to exercise the lock paths standalone, since IntakeService
persists every newly-locked brief to Postgres synchronously
(docs/intake_routes_plan.md section 8 item 4) and this test must not touch a
real database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from agents.nodes.node1_intake import IntakeQuestion
from api import main as api_main
from api.config import get_settings
from api.services import intake_service as intake_service_module
from tests.intake_service_fakes import FakePostgresClient


@pytest.fixture
def app_and_service(monkeypatch):
    call_counts = {"intake_begin": 0, "intake_step": 0}

    def fake_intake_begin(state, phrase_fn):
        call_counts["intake_begin"] += 1
        state.history.append({"role": "assistant", "content": "What's your budget?"})
        state.current_question_id = "budget"
        return state, IntakeQuestion(
            question_id="budget", text="What's your budget?", kind="sequence"
        )

    turns_before_lock = 3

    def fake_intake_step(state, answer, phrase_fn):
        call_counts["intake_step"] += 1
        state.history.append({"role": "user", "content": answer})
        if state.current_question_id and state.current_question_id not in state.asked_so_far:
            state.asked_so_far.append(state.current_question_id)

        if call_counts["intake_step"] < turns_before_lock:
            next_id = f"q{call_counts['intake_step']}"
            state.history.append({"role": "assistant", "content": f"Next question {next_id}"})
            state.current_question_id = next_id
            return (
                state,
                IntakeQuestion(question_id=next_id, text=f"Next question {next_id}", kind="sequence"),
                False,
            )

        state.brief = state.brief.model_copy(update={"status": "locked"})
        state.current_question_id = None
        return state, None, True

    monkeypatch.setattr(intake_service_module, "intake_begin", fake_intake_begin)
    monkeypatch.setattr(intake_service_module, "intake_step", fake_intake_step)

    app = api_main.create_app()
    fake_postgres = FakePostgresClient()
    app.state.intake_service._postgres = fake_postgres

    return app, call_counts, fake_postgres


@pytest.fixture
def client(app_and_service) -> TestClient:
    app, _, _ = app_and_service
    return TestClient(app, raise_server_exceptions=False)


def test_full_happy_path_answers_through_to_lock(client: TestClient) -> None:
    created = client.post("/api/v1/intake/sessions", json={"client_ref": None})
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "asking"
    assert body["question"]["question_id"] == "budget"
    session_id = body["session_id"]

    resp = client.post(f"/api/v1/intake/sessions/{session_id}/answers", json={"answer": "60000"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "asking"

    resp = client.post(f"/api/v1/intake/sessions/{session_id}/answers", json={"answer": "gaming"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "asking"

    resp = client.post(f"/api/v1/intake/sessions/{session_id}/answers", json={"answer": "done"})
    assert resp.status_code == 200
    final = resp.json()
    assert final["status"] == "locked"
    assert "brief_summary" in final
    summary = final["brief_summary"]
    # Real BriefSummaryDTO fields, from a real mapped UserBuildBrief -- not a
    # hand-rolled dict.
    assert "budget" in summary and set(summary["budget"]) == {
        "comfortable_min", "comfortable_max", "ceiling", "scope", "currency", "notes",
    }
    assert "completeness" in summary
    assert summary["answered_fields"] == ["budget"]
    assert "progress" in final


def test_submit_answer_unknown_session_returns_404_envelope(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/intake/sessions/does-not-exist/answers", json={"answer": "42"}
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "SESSION_NOT_FOUND"
    assert body["error"]["retryable"] is False


def test_lock_before_floor_met_returns_409_with_missing(client: TestClient) -> None:
    created = client.post("/api/v1/intake/sessions", json={"client_ref": None})
    session_id = created.json()["session_id"]

    resp = client.post(f"/api/v1/intake/sessions/{session_id}/lock")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "BRIEF_FLOOR_NOT_MET"
    assert set(body["error"]["details"]["missing"]) == {"budget", "primary_use_case"}


def test_delete_unknown_session_is_idempotent_204(client: TestClient) -> None:
    resp = client.delete("/api/v1/intake/sessions/does-not-exist")
    assert resp.status_code == 204
    assert resp.content == b""


def test_get_snapshot_mid_conversation_reconstructs_question_without_llm_call(
    app_and_service,
) -> None:
    app, call_counts, _ = app_and_service
    client = TestClient(app, raise_server_exceptions=False)

    created = client.post("/api/v1/intake/sessions", json={"client_ref": None})
    session_id = created.json()["session_id"]
    assert call_counts["intake_begin"] == 1

    client.post(f"/api/v1/intake/sessions/{session_id}/answers", json={"answer": "60000"})

    resp = client.get(f"/api/v1/intake/sessions/{session_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "asking"
    assert body["question"] is not None
    assert body["question"]["question_id"] == "q1"
    assert body["question"]["text"] == "Next question q1"
    assert body["brief_summary"] is None

    # The whole point of the snapshot route: no extra phrasing LLM call.
    # intake_begin was called exactly once, by the original create_session --
    # never again from this GET.
    assert call_counts["intake_begin"] == 1


def test_create_session_expires_at_reflects_configured_session_ttl_min(monkeypatch) -> None:
    """Guards the docs/hardening_plan.md section 3 expires_at ripple: with
    KARMA_SESSION_TTL_MIN overridden to a small custom value, the expires_at
    the route actually returns must reflect that value (via Depends(get_settings)),
    not the hardcoded 1800s (30 min) ASKING_TTL_SECONDS default.
    """
    get_settings.cache_clear()
    monkeypatch.setenv("KARMA_SESSION_TTL_MIN", "1")

    def fake_intake_begin(state, phrase_fn):
        state.history.append({"role": "assistant", "content": "What's your budget?"})
        state.current_question_id = "budget"
        return state, IntakeQuestion(
            question_id="budget", text="What's your budget?", kind="sequence"
        )

    monkeypatch.setattr(intake_service_module, "intake_begin", fake_intake_begin)

    app = api_main.create_app()
    client = TestClient(app, raise_server_exceptions=False)

    before = datetime.now(timezone.utc)
    resp = client.post("/api/v1/intake/sessions", json={"client_ref": None})
    assert resp.status_code == 201

    expires_at = datetime.fromisoformat(resp.json()["expires_at"])
    delta = expires_at - before

    # 1 min TTL => ~60s, with generous slack for test execution time -- but
    # nowhere near the 1800s (30 min) default, which is the regression this
    # test exists to catch.
    assert timedelta(seconds=30) < delta < timedelta(seconds=300)

    get_settings.cache_clear()
