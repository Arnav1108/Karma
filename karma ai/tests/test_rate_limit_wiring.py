"""Integration tests for the Phase 5 rate-limit wiring (docs/hardening_plan.md
section 2): api/rate_limit.py's rate_limit() dependency factory attached to
POST /intake/sessions (session_create), POST /intake/sessions/{id}/answers
(intake_turn), and POST /builds (build_create); the KARMA_RATE_LIMIT_ENABLED
bypass; and the RATE_LIMITED vs BUILD_CAPACITY 429 distinction.

Builds the REAL api.main.create_app() app end-to-end -- same pattern as
test_build_routes.py / test_intake_routes.py: intake_begin is monkeypatched
at the api.services.intake_service module level (no live LLM call) and the
real IntakeService's Postgres client is swapped for FakePostgresClient;
build-side reachability checks are monkeypatched the same way
test_build_routes.py does it, and run_from_brief is monkeypatched to return
instantly.

get_settings() is @lru_cache, so each test sets its own low rate-limit env
vars via monkeypatch BEFORE calling create_app() (through the make_app
factory fixture below), clearing the cache immediately before construction
so the limiter reads the test's values, not a prior test's cached Settings.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from agents.nodes.node1_intake import IntakeQuestion
from agents.nodes.node3_selector import SELECTION_ORDER
from agents.schemas.build_card import BuildCard, BuildCardPart
from api import main as api_main
from api.config import get_settings
from api.services import build_service as build_service_module
from api.services import intake_service as intake_service_module
from api.services.session_store import SessionRecord
from tests.intake_service_fakes import FakePostgresClient


@dataclass
class _FakeBrief:
    marker: str = "brief"


@dataclass
class _StateWithBrief:
    brief: _FakeBrief


def _seed_locked_session(store, session_id: str) -> None:
    now = datetime.now(timezone.utc)
    store._sessions[session_id] = SessionRecord(
        session_id=session_id,
        state=_StateWithBrief(_FakeBrief()),
        status="locked",
        created_at=now,
        last_accessed_at=now,
    )


def _fake_intake_begin(state, phrase_fn):
    state.history.append({"role": "assistant", "content": "What's your budget?"})
    state.current_question_id = "budget"
    return state, IntakeQuestion(
        question_id="budget", text="What's your budget?", kind="sequence"
    )


def _make_build_card() -> BuildCard:
    parts = [
        BuildCardPart(
            slot=slot,
            product_id=f"prod-{i}",
            name=f"Part {i}",
            price_inr=1000 * (i + 1),
            justification="test pick",
        )
        for i, slot in enumerate(SELECTION_ORDER)
    ]
    return BuildCard(
        parts=parts,
        total_price_inr=sum(p.price_inr for p in parts),
        summary="test build",
        warnings=[],
    )


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def make_app(monkeypatch):
    """Returns a factory building a fresh, fully-wired create_app() app.

    Deferred (not called eagerly) so each test can monkeypatch.setenv its own
    KARMA_RL_*/KARMA_RATE_LIMIT_ENABLED values first -- get_settings() is
    read once at construction time inside create_app().
    """
    monkeypatch.setattr(intake_service_module, "intake_begin", _fake_intake_begin)
    monkeypatch.setattr(build_service_module, "_neo4j_up", lambda: True)
    monkeypatch.setattr(build_service_module, "_postgres_up", lambda: True)

    def _make():
        get_settings.cache_clear()
        application = api_main.create_app()
        application.state.intake_service._postgres = FakePostgresClient()
        return application

    return _make


# ---------------------------------------------------------------------------
# 1. Same key, low session_create limit -> 3rd request 429 RATE_LIMITED
# ---------------------------------------------------------------------------

def test_third_session_create_same_key_returns_429_rate_limited(make_app, monkeypatch):
    monkeypatch.setenv("KARMA_RL_SESSION_CREATE_PER_MIN", "2")
    application = make_app()
    headers = {"X-API-Key": "key-1"}

    with TestClient(application) as client:
        r1 = client.post("/api/v1/intake/sessions", json={"client_ref": None}, headers=headers)
        r2 = client.post("/api/v1/intake/sessions", json={"client_ref": None}, headers=headers)
        r3 = client.post("/api/v1/intake/sessions", json={"client_ref": None}, headers=headers)

    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r3.status_code == 429
    body = r3.json()
    assert body["error"]["code"] == "RATE_LIMITED"
    assert body["error"]["code"] != "BUILD_CAPACITY"
    assert body["error"]["retryable"] is True
    retry_after = r3.headers.get("retry-after")
    assert retry_after is not None
    assert int(retry_after) >= 1


# ---------------------------------------------------------------------------
# 2. A different API key gets its own bucket
# ---------------------------------------------------------------------------

def test_different_api_key_gets_its_own_session_create_bucket(make_app, monkeypatch):
    monkeypatch.setenv("KARMA_RL_SESSION_CREATE_PER_MIN", "2")
    application = make_app()

    with TestClient(application) as client:
        for _ in range(2):
            resp = client.post(
                "/api/v1/intake/sessions",
                json={"client_ref": None},
                headers={"X-API-Key": "key-a"},
            )
            assert resp.status_code == 201

        exhausted = client.post(
            "/api/v1/intake/sessions", json={"client_ref": None}, headers={"X-API-Key": "key-a"}
        )
        assert exhausted.status_code == 429

        # key-a's bucket is exhausted, but key-b has never been hit -- its own,
        # independent bucket still has headroom.
        other_key_resp = client.post(
            "/api/v1/intake/sessions", json={"client_ref": None}, headers={"X-API-Key": "key-b"}
        )
        assert other_key_resp.status_code == 201


# ---------------------------------------------------------------------------
# 3. GET/DELETE are not rate-limited, even after exhausting session_create
# ---------------------------------------------------------------------------

def test_get_and_delete_session_unaffected_by_exhausted_session_create_limit(
    make_app, monkeypatch
):
    monkeypatch.setenv("KARMA_RL_SESSION_CREATE_PER_MIN", "1")
    application = make_app()
    headers = {"X-API-Key": "key-1"}

    with TestClient(application) as client:
        created = client.post(
            "/api/v1/intake/sessions", json={"client_ref": None}, headers=headers
        )
        assert created.status_code == 201
        session_id = created.json()["session_id"]

        # Exhaust the session_create bucket (limit=1, already consumed above).
        exhausted = client.post(
            "/api/v1/intake/sessions", json={"client_ref": None}, headers=headers
        )
        assert exhausted.status_code == 429

        # GET and DELETE carry no rate_limit dependency -- unaffected by the
        # exhausted session_create bucket.
        snap = client.get(f"/api/v1/intake/sessions/{session_id}", headers=headers)
        assert snap.status_code == 200

        deleted = client.delete(f"/api/v1/intake/sessions/{session_id}", headers=headers)
        assert deleted.status_code == 204


# ---------------------------------------------------------------------------
# 4. KARMA_RATE_LIMIT_ENABLED=false is a genuine bypass, not a high limit
# ---------------------------------------------------------------------------

def test_rate_limit_disabled_flag_is_a_genuine_bypass(make_app, monkeypatch):
    monkeypatch.setenv("KARMA_RL_SESSION_CREATE_PER_MIN", "2")
    monkeypatch.setenv("KARMA_RATE_LIMIT_ENABLED", "false")
    application = make_app()
    headers = {"X-API-Key": "key-1"}

    with TestClient(application) as client:
        for _ in range(3):
            resp = client.post(
                "/api/v1/intake/sessions", json={"client_ref": None}, headers=headers
            )
            assert resp.status_code == 201


# ---------------------------------------------------------------------------
# 5. build_create's own 429 RATE_LIMITED is distinct from 429 BUILD_CAPACITY
# ---------------------------------------------------------------------------

def test_build_create_rate_limit_distinct_from_build_capacity(make_app, monkeypatch):
    monkeypatch.setenv("KARMA_RL_BUILD_CREATE_PER_HOUR", "2")
    # High enough that concurrency capacity is never the blocker here -- only
    # the hourly quota should reject the 3rd request.
    monkeypatch.setenv("KARMA_MAX_CONCURRENT_BUILDS", "10")
    application = make_app()

    store = application.state.build_service._session_store
    for session_id in ("sess-1", "sess-2", "sess-3"):
        _seed_locked_session(store, session_id)

    card = _make_build_card()
    monkeypatch.setattr(
        build_service_module, "run_from_brief", lambda brief: {"build_card": card}
    )
    headers = {"X-API-Key": "key-1"}

    with TestClient(application, raise_server_exceptions=False) as client:
        r1 = client.post("/api/v1/builds", json={"session_id": "sess-1"}, headers=headers)
        r2 = client.post("/api/v1/builds", json={"session_id": "sess-2"}, headers=headers)
        r3 = client.post("/api/v1/builds", json={"session_id": "sess-3"}, headers=headers)

    assert r1.status_code == 202
    assert r2.status_code == 202
    assert r3.status_code == 429
    body = r3.json()
    assert body["error"]["code"] == "RATE_LIMITED"
    assert body["error"]["code"] != "BUILD_CAPACITY"
    assert body["error"]["retryable"] is True
    assert r3.headers.get("retry-after") is not None
