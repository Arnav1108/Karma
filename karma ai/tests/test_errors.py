"""Unit tests for api/errors.py.

Builds a tiny throwaway FastAPI app, registers the handlers under test, and
adds one dummy route per exception type that just raises it. Asserts each
produces the exact status code and envelope body from
karma ai/docs/intake_routes_plan.md section 2.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel
import pytest

from api.config import get_settings
from api.errors import register_exception_handlers
from api.services.exceptions import (
    BriefFloorNotMetError,
    BriefPersistenceError,
    IntakeServiceError,
    LlmUpstreamError,
    SessionAlreadyLockedError,
    SessionNotFoundError,
    TurnInProgressError,
)


class _DummyBody(BaseModel):
    answer: str


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    # get_settings() is @lru_cache -- the TurnInProgressError handler now
    # reads it directly (docs/hardening_plan.md section 6), so env vars set
    # by the Retry-After tests below must not leak into (or be clobbered by)
    # other test modules relying on the process-wide cached Settings.
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/session-not-found")
    async def _session_not_found():
        raise SessionNotFoundError

    @app.get("/session-already-locked")
    async def _session_already_locked():
        raise SessionAlreadyLockedError

    @app.get("/turn-in-progress")
    async def _turn_in_progress():
        raise TurnInProgressError

    @app.get("/brief-floor-not-met")
    async def _brief_floor_not_met():
        raise BriefFloorNotMetError(["budget", "purpose"])

    @app.get("/llm-upstream-error")
    async def _llm_upstream_error():
        raise LlmUpstreamError(ValueError("boom"))

    @app.get("/brief-persistence-error")
    async def _brief_persistence_error():
        raise BriefPersistenceError(RuntimeError("connection refused"))

    @app.get("/intake-service-error-base")
    async def _intake_service_error_base():
        raise IntakeServiceError("some future subclass with no handler")

    @app.get("/bare-exception")
    async def _bare_exception():
        raise ValueError("unexpected bug")

    @app.post("/validate-me")
    async def _validate_me(body: _DummyBody):
        return {"ok": True}

    return TestClient(app, raise_server_exceptions=False)


def test_session_not_found(client: TestClient) -> None:
    resp = client.get("/session-not-found")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "SESSION_NOT_FOUND"
    assert body["error"]["retryable"] is False
    assert "details" not in body["error"]


def test_session_already_locked(client: TestClient) -> None:
    resp = client.get("/session-already-locked")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "SESSION_ALREADY_LOCKED"
    assert body["error"]["retryable"] is False
    assert "details" not in body["error"]


def test_turn_in_progress(client: TestClient) -> None:
    resp = client.get("/turn-in-progress")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "TURN_IN_PROGRESS"
    assert body["error"]["retryable"] is True
    assert "details" not in body["error"]
    # Default KARMA_TURN_RETRY_AFTER_S=1 -- docs/hardening_plan.md section 6.
    assert resp.headers["Retry-After"] == "1"


def test_turn_in_progress_retry_after_is_env_configurable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KARMA_TURN_RETRY_AFTER_S", "5")
    get_settings.cache_clear()

    resp = client.get("/turn-in-progress")

    assert resp.status_code == 409
    assert resp.headers["Retry-After"] == "5"


def test_brief_floor_not_met(client: TestClient) -> None:
    resp = client.get("/brief-floor-not-met")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "BRIEF_FLOOR_NOT_MET"
    assert body["error"]["retryable"] is False
    assert body["error"]["details"] == {"missing": ["budget", "purpose"]}


def test_llm_upstream_error(client: TestClient) -> None:
    resp = client.get("/llm-upstream-error")
    assert resp.status_code == 502
    body = resp.json()
    assert body["error"]["code"] == "LLM_UPSTREAM_ERROR"
    assert body["error"]["retryable"] is True
    assert "details" not in body["error"]
    # Never leak the raw OpenAI SDK exception internals into the response.
    assert "boom" not in body["error"]["message"]


def test_brief_persistence_error(client: TestClient) -> None:
    resp = client.get("/brief-persistence-error")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "DATABASE_UNAVAILABLE"
    assert body["error"]["retryable"] is True
    assert "details" not in body["error"]
    assert "connection refused" not in body["error"]["message"]


def test_intake_service_error_base_catch_all(client: TestClient) -> None:
    resp = client.get("/intake-service-error-base")
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert body["error"]["retryable"] is False
    assert "details" not in body["error"]


def test_request_validation_error(client: TestClient) -> None:
    resp = client.post("/validate-me", json={"answer": 123})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["retryable"] is False
    assert "details" not in body["error"]
    # Not FastAPI's default {"detail": [...]} body.
    assert "detail" not in body


def test_bare_exception_catch_all(client: TestClient) -> None:
    resp = client.get("/bare-exception")
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert body["error"]["retryable"] is False
    assert "details" not in body["error"]
    assert "unexpected bug" not in body["error"]["message"]
