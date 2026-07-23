"""Phase 6 Step 1 — auth-envelope normalization + OpenAPI grouping/metadata.

Confirms three things wired in Step 1:
- 401 UNAUTHORIZED now returns the shared {"error": {...}} envelope with
  retryable=false, instead of FastAPI's default {"detail": ...} body (which
  omitted retryable and double-wrapped the envelope).
- create_app()'s generated OpenAPI spec groups routes under intake/builds/health
  tags and carries app-level version/contact metadata.
- POST /intake/sessions/{id}/answers advertises an explicit (union) response
  schema, not just an inferred one.

All assertions run against the REAL app from api.main.create_app() and the real
app.openapi() output — no live server needed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api import main as api_main
from api.config import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    # get_settings() is @lru_cache; clear around each test so KARMA_API_KEYS set
    # here neither leaks into nor is clobbered by other modules' cached Settings.
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_missing_key_returns_shared_envelope_with_retryable(monkeypatch):
    monkeypatch.setenv("KARMA_API_KEYS", "secret-key")
    app = api_main.create_app()
    client = TestClient(app, raise_server_exceptions=False)

    # No X-API-Key header at all -> require_api_key rejects.
    resp = client.post("/api/v1/intake/sessions", json={"client_ref": None})

    assert resp.status_code == 401
    body = resp.json()
    # Shared envelope shape, NOT FastAPI's default {"detail": ...}.
    assert "detail" not in body
    assert set(body.keys()) == {"error"}
    err = body["error"]
    assert err["code"] == "UNAUTHORIZED"
    assert err["retryable"] is False  # the field that was previously absent
    assert isinstance(err["message"], str) and err["message"]


def test_wrong_key_returns_same_enveloped_401(monkeypatch):
    monkeypatch.setenv("KARMA_API_KEYS", "secret-key")
    app = api_main.create_app()
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        "/api/v1/intake/sessions",
        json={"client_ref": None},
        headers={"X-API-Key": "not-the-right-key"},
    )

    assert resp.status_code == 401
    assert resp.json()["error"] == {
        "code": "UNAUTHORIZED",
        "message": "Invalid or missing API key.",
        "retryable": False,
    }


def test_openapi_tags_group_routes():
    app = api_main.create_app()
    spec = app.openapi()

    def tags_for(path: str, method: str) -> set[str]:
        return set(spec["paths"][path][method].get("tags", []))

    assert tags_for("/api/v1/intake/sessions", "post") == {"intake"}
    assert tags_for("/api/v1/intake/sessions/{session_id}/answers", "post") == {"intake"}
    assert tags_for("/api/v1/builds", "post") == {"builds"}
    assert tags_for("/api/v1/builds/{build_id}", "get") == {"builds"}
    assert tags_for("/healthz", "get") == {"health"}
    assert tags_for("/readyz", "get") == {"health"}


def test_openapi_carries_version_and_contact_metadata():
    app = api_main.create_app()
    info = app.openapi()["info"]

    assert info["version"] == get_settings().version
    assert info["contact"]["name"]  # non-empty contact block
    assert info["description"]  # self-describing spec


def test_answers_route_advertises_explicit_union_response_model():
    app = api_main.create_app()
    op = app.openapi()["paths"]["/api/v1/intake/sessions/{session_id}/answers"]["post"]

    schema = op["responses"]["200"]["content"]["application/json"]["schema"]
    refs = {branch.get("$ref", "") for branch in schema.get("anyOf", [])}
    assert "#/components/schemas/AnswerAskingResponse" in refs
    assert "#/components/schemas/AnswerLockedResponse" in refs
