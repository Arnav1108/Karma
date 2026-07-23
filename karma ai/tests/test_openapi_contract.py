"""Phase 6 Step 3 — OpenAPI well-formedness + snapshot freeze.

Two guards on the exported contract (docs/frontend_contract_plan.md section 7):

1. The live /openapi.json is valid OpenAPI 3.x and its `paths` cover every mounted
   in-schema route — nothing the frontend can call is missing from the spec.
2. The committed snapshot at api/contract/openapi.json exactly equals the spec the
   real app produces now. Any DTO shape change (field rename/removal/type change)
   flows into the spec and fails this test — the section 3 freeze. Regenerating is a
   deliberate step: `python -m scripts.dump_openapi`, then commit the diff.
"""

from __future__ import annotations

import json

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from api import main as api_main
from scripts.dump_openapi import SNAPSHOT_PATH, render_openapi


def test_openapi_endpoint_well_formed_and_covers_all_routes():
    app = api_main.create_app()
    client = TestClient(app)

    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()  # raises if the body is not valid JSON

    assert spec["openapi"].startswith("3."), spec.get("openapi")

    mounted = {
        route.path
        for route in app.routes
        if isinstance(route, APIRoute) and route.include_in_schema
    }
    assert mounted, "expected at least the intake/builds/health routes to be mounted"
    assert mounted == set(spec["paths"].keys()), (
        f"spec paths drifted from mounted routes: "
        f"missing={mounted - set(spec['paths'])}, extra={set(spec['paths']) - mounted}"
    )


def test_committed_snapshot_matches_live_spec():
    assert SNAPSHOT_PATH.exists(), (
        f"missing {SNAPSHOT_PATH}; run `python -m scripts.dump_openapi` to create it"
    )
    committed = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    live = render_openapi()

    # Compare parsed JSON, not raw text, so CRLF/indentation never causes a false diff.
    assert committed == live, (
        "api/contract/openapi.json is stale relative to the live app. If the contract "
        "change was intentional, regenerate with `python -m scripts.dump_openapi` and "
        "commit the diff."
    )
