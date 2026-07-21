"""Integration tests for api/routers/builds.py.

Builds the REAL api.main.create_app() app end-to-end -- create_app() now
mounts builds.router itself and installs a real app.state.build_service
(BuildService + InMemoryJobRegistry + a dedicated ThreadPoolExecutor)
sharing the same InMemorySessionStore as app.state.intake_service, exactly
as production does. No manual router-mounting or BuildService construction
here anymore -- that workaround in the prior revision of this file was only
needed while create_app() didn't wire builds itself yet.

Sessions are seeded directly into the real store's internal dict
(store._sessions) rather than through a full multi-turn intake conversation
-- the same "reach past the public surface to seed test state" pattern
test_intake_routes.py uses for _postgres. run_from_brief is monkeypatched at
the api.services.build_service module level (never BuildService itself) so
tests exercise the real BuildService / registry / mapper stack without a
real LLM/DB/pipeline call. KARMA_API_KEYS is unset in the test environment,
so require_api_key no-ops, same as intake's tests.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from agents.nodes.node3_selector import SELECTION_ORDER
from agents.schemas.build_card import BuildCard, BuildCardPart
from api import main as api_main
from api.routers import builds
from api.services import build_service as build_service_module
from api.services.session_store import SessionRecord


@dataclass
class _FakeBrief:
    marker: str = "brief"


@dataclass
class _StateWithBrief:
    brief: _FakeBrief


def _seed_session(store, session_id: str, status: str) -> None:
    """Writes straight into the real InMemorySessionStore's backing dict.
    BuildService.start_build only ever reads record.status and
    record.state.brief, so a minimal fake state is enough -- a full intake
    conversation isn't needed to exercise the build routes."""
    now = datetime.now(timezone.utc)
    store._sessions[session_id] = SessionRecord(
        session_id=session_id,
        state=_StateWithBrief(_FakeBrief()),
        status=status,
        created_at=now,
        last_accessed_at=now,
    )


def _make_build_card(n_parts: int | None = None) -> BuildCard:
    """A real BuildCard (not a mock) with n_parts filled slots in
    SELECTION_ORDER order, defaulting to a full 9-slot card."""
    n_parts = len(SELECTION_ORDER) if n_parts is None else n_parts
    parts = [
        BuildCardPart(
            slot=slot,
            product_id=f"prod-{i}",
            name=f"Part {i}",
            price_inr=1000 * (i + 1),
            justification="test pick",
        )
        for i, slot in enumerate(SELECTION_ORDER[:n_parts])
    ]
    return BuildCard(
        parts=parts,
        total_price_inr=sum(p.price_inr for p in parts),
        summary="test build",
        warnings=[],
    )


def _poll_until_terminal(client: TestClient, build_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/api/v1/builds/{build_id}")
        assert resp.status_code == 200
        body = resp.json()
        if body["status"] not in ("queued", "running"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"build {build_id} did not reach a terminal status within {timeout}s")


@pytest.fixture
def app_and_store(monkeypatch):
    monkeypatch.setattr(build_service_module, "_neo4j_up", lambda: True)
    monkeypatch.setattr(build_service_module, "_postgres_up", lambda: True)

    app = api_main.create_app()
    # The real, single, shared InMemorySessionStore -- the same instance
    # app.state.intake_service and app.state.build_service both read from
    # (create_app() constructs it once and passes it to both, per
    # build_service_plan.md's "don't split state" requirement).
    store = app.state.build_service._session_store

    yield app, store


@pytest.fixture
def client(app_and_store):
    """Context-managed TestClient. Without `with`, starlette's TestClient
    spins up a fresh portal/event loop per call and tears it down when the
    call returns, orphaning the fire-and-forget _run_and_store asyncio task
    BuildService schedules -- it would never progress between polls. `with`
    keeps one portal (and its background thread's running loop) alive for
    every request the fixture makes, so the scheduled task keeps advancing
    in real time between client.post()/client.get() calls, exactly like a
    real server process. Entering/exiting also fires the app's real
    startup/shutdown lifespan events, so app.state.build_executor is
    cleanly shut down when each test's client closes."""
    app, _ = app_and_store
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# 1. Happy path: locked session -> 202 -> poll to succeeded
# ---------------------------------------------------------------------------

def test_post_builds_locked_session_polls_to_succeeded_build_card(
    client: TestClient, app_and_store, monkeypatch
) -> None:
    _, store = app_and_store
    _seed_session(store, "sess-locked", "locked")
    card = _make_build_card()
    monkeypatch.setattr(build_service_module, "run_from_brief", lambda brief: {"build_card": card})

    resp = client.post("/api/v1/builds", json={"session_id": "sess-locked"})
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert body["poll_after_ms"] == 2000
    build_id = body["build_id"]
    assert build_id

    final = _poll_until_terminal(client, build_id)
    assert final["build_id"] == build_id
    assert final["status"] == "succeeded"
    assert final["error"] is None
    assert final["reason"] is None

    # Real BuildCardDTO shape, from a real mapped BuildCard.
    build = final["build"]
    assert build is not None
    assert set(build.keys()) == {"parts", "total_price_inr", "summary", "warnings"}
    assert len(build["parts"]) == len(SELECTION_ORDER)
    assert build["total_price_inr"] == card.total_price_inr
    assert build["summary"] == card.summary
    assert build["warnings"] == []
    part = build["parts"][0]
    assert set(part.keys()) == {"slot", "product_id", "name", "brand", "price_inr", "justification"}
    assert part["slot"] == SELECTION_ORDER[0].value


# ---------------------------------------------------------------------------
# 2. Unlocked session -> 409 BRIEF_NOT_LOCKED
# ---------------------------------------------------------------------------

def test_post_builds_unlocked_session_returns_409_brief_not_locked_envelope(
    client: TestClient, app_and_store
) -> None:
    _, store = app_and_store
    _seed_session(store, "sess-asking", "asking")

    resp = client.post("/api/v1/builds", json={"session_id": "sess-asking"})
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "BRIEF_NOT_LOCKED"
    assert body["error"]["retryable"] is False


# ---------------------------------------------------------------------------
# 3. Unknown session -> 404 SESSION_NOT_FOUND
# ---------------------------------------------------------------------------

def test_post_builds_unknown_session_returns_404_session_not_found_envelope(
    client: TestClient,
) -> None:
    resp = client.post("/api/v1/builds", json={"session_id": "does-not-exist"})
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "SESSION_NOT_FOUND"
    assert body["error"]["retryable"] is False


# ---------------------------------------------------------------------------
# 4. Unknown build_id -> 404 BUILD_NOT_FOUND
# ---------------------------------------------------------------------------

def test_get_unknown_build_id_returns_404_build_not_found_envelope(
    client: TestClient,
) -> None:
    resp = client.get("/api/v1/builds/does-not-exist")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "BUILD_NOT_FOUND"
    assert body["error"]["retryable"] is False


# ---------------------------------------------------------------------------
# 5. Capacity: max_concurrent=1, second concurrent build -> 429 + Retry-After
# ---------------------------------------------------------------------------

def test_second_concurrent_build_at_capacity_returns_429_with_retry_after(
    app_and_store, monkeypatch
) -> None:
    app, store = app_and_store
    _seed_session(store, "sess-a", "locked")
    _seed_session(store, "sess-b", "locked")
    # Real create_app() sizes this from settings (default 2); force 1 here so
    # a second concurrent POST is deterministically rejected rather than
    # depending on the env's KARMA_MAX_CONCURRENT_BUILDS value.
    app.state.build_service._max_concurrent = 1

    release_event = threading.Event()

    def slow_run_from_brief(brief):
        # Blocks the first build so the second POST is admitted while the
        # first is still occupying the sole (overridden) slot.
        release_event.wait(timeout=5.0)
        return {"build_card": _make_build_card()}

    monkeypatch.setattr(build_service_module, "run_from_brief", slow_run_from_brief)

    with TestClient(app, raise_server_exceptions=False) as client:
        try:
            resp1 = client.post("/api/v1/builds", json={"session_id": "sess-a"})
            assert resp1.status_code == 202

            resp2 = client.post("/api/v1/builds", json={"session_id": "sess-b"})
            assert resp2.status_code == 429
            body = resp2.json()
            assert body["error"]["code"] == "BUILD_CAPACITY"
            assert body["error"]["retryable"] is True
            assert resp2.headers.get("retry-after") == "30"
        finally:
            # Release before the `with` block exits -- app shutdown (fired on
            # __exit__) shuts the executor down with wait=True, which would
            # otherwise block on this still-running worker thread.
            release_event.set()


# ---------------------------------------------------------------------------
# 6. builds.router is mount-agnostic (proven independent of where it's mounted)
# ---------------------------------------------------------------------------

def test_builds_router_carries_no_prefix_of_its_own() -> None:
    # Same proof shape as intake's routes: the module-level router object
    # only knows its own "/builds" prefix, never "/api/v1" -- that's added
    # at inclusion time by create_app(), not baked into the router itself.
    assert builds.router.prefix == "/builds"
    paths = {route.path for route in builds.router.routes}
    assert paths == {"/builds", "/builds/{build_id}"}
