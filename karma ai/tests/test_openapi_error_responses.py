"""Phase 6 Step 2 — per-route error responses in the OpenAPI spec.

FastAPI cannot infer the custom exception handlers' response shapes, so each route
declares a responses={} map (api/routers/*.py) documenting exactly the error codes it
can actually raise, per the catalog in docs/frontend_contract_plan.md section 5.

These tests assert the spec now advertises those errors, that every documented error
body references the shared ErrorEnvelope model, that no route documents a 504 (designed
but never emitted — section 8 item 5), and that BUILD_ALREADY_ACTIVE (registered but
never raised) is not documented on POST /builds.
"""

from __future__ import annotations

import pytest

from api import main as api_main

_ENV_REF = "#/components/schemas/ErrorEnvelope"

# Only the statuses each route explicitly documents via error_response() — i.e. errors
# it can genuinely raise. (Param-only routes also carry FastAPI's auto-generated 422
# HTTPValidationError, which is a framework default and deliberately not listed here.)
_EXPECTED_ERROR_STATUSES = {
    ("/api/v1/intake/sessions", "post"): {401, 422, 429, 502},
    ("/api/v1/intake/sessions/{session_id}/answers", "post"): {401, 404, 409, 422, 429, 502, 503},
    ("/api/v1/intake/sessions/{session_id}", "get"): {401, 404},
    ("/api/v1/intake/sessions/{session_id}/lock", "post"): {401, 404, 409, 503},
    ("/api/v1/intake/sessions/{session_id}", "delete"): {401},
    ("/api/v1/builds", "post"): {401, 404, 409, 422, 429},
    ("/api/v1/builds/{build_id}", "get"): {401, 404},
}


@pytest.fixture(scope="module")
def spec():
    return api_main.create_app().openapi()


def _error_schema(op: dict, status: int) -> dict:
    return op["responses"][str(status)]["content"]["application/json"]["schema"]


@pytest.mark.parametrize("route,statuses", _EXPECTED_ERROR_STATUSES.items())
def test_documented_error_statuses_reference_error_envelope(spec, route, statuses):
    path, method = route
    op = spec["paths"][path][method]
    for status in statuses:
        assert str(status) in op["responses"], f"{method} {path} missing {status}"
        assert _error_schema(op, status)["$ref"] == _ENV_REF, (
            f"{method} {path} {status} does not reference ErrorEnvelope"
        )


def test_no_route_documents_504(spec):
    offenders = [
        (path, method)
        for path, methods in spec["paths"].items()
        for method, op in methods.items()
        if "504" in op.get("responses", {})
    ]
    assert offenders == [], f"504 documented but never emitted: {offenders}"


def test_answers_documents_full_intake_error_surface(spec):
    op = spec["paths"]["/api/v1/intake/sessions/{session_id}/answers"]["post"]
    documented = {c for c in op["responses"] if c[0] in "45"}
    # Every transport error submit_answer can raise per the service method.
    assert {"401", "404", "409", "422", "429", "502", "503"} <= documented


def test_lock_409_description_enumerates_brief_floor_not_met(spec):
    op = spec["paths"]["/api/v1/intake/sessions/{session_id}/lock"]["post"]
    assert "BRIEF_FLOOR_NOT_MET" in op["responses"]["409"]["description"]


def test_builds_429_covers_both_capacity_and_rate_limit(spec):
    desc = spec["paths"]["/api/v1/builds"]["post"]["responses"]["429"]["description"]
    assert "BUILD_CAPACITY" in desc and "RATE_LIMITED" in desc


def test_builds_does_not_document_never_raised_build_already_active(spec):
    # BuildAlreadyActiveError has a handler in api/errors.py but no code path raises it,
    # so its BUILD_ALREADY_ACTIVE code must not appear in the build route's 409 contract.
    op = spec["paths"]["/api/v1/builds"]["post"]
    desc = op["responses"]["409"]["description"]
    assert "BUILD_ALREADY_ACTIVE" not in desc
    assert "BRIEF_NOT_LOCKED" in desc


def test_snapshot_does_not_document_rate_limit_or_llm_errors(spec):
    # GET snapshot is read-only: not rate-limited, makes no LLM call.
    op = spec["paths"]["/api/v1/intake/sessions/{session_id}"]["get"]
    assert "429" not in op["responses"]
    assert "502" not in op["responses"]
