"""Integration tests for api/routers/builds.py.

Builds the REAL api.main.create_app() app (so DI, CORS, and the real
exception handlers registered by api.errors.register_exception_handlers are
all wired exactly as production does) but mounts api.routers.builds and
installs app.state.build_service in the test fixture itself -- create_app()
does not do either yet (build_service_plan.md section 7: mounting is a
separate, later step; only the get_build_service accessor exists so far).
This mirrors the pre-mount era of test_intake_routes.py, before
intake.router was folded into create_app() itself.

run_from_brief is monkeypatched at the api.services.build_service module
level (never BuildService itself, per the task's instruction) so these tests
exercise the real BuildService / InMemoryJobRegistry / mapper stack
end-to-end without a real LLM/DB/pipeline call. KARMA_API_KEYS is unset in
the test environment, so require_api_key no-ops, same as intake's tests.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from agents.nodes.node3_selector import SELECTION_ORDER
from agents.schemas.build_card import BuildCard, BuildCardPart
from api import main as api_main
from api.middleware import require_api_key
from api.routers import builds
from api.services import build_service as build_service_module
from api.services.build_service import BuildService
from api.services.job_registry import InMemoryJobRegistry
from api.services.session_store import SessionRecord, SessionStore


@dataclass
class _FakeBrief:
    marker: str = "brief"


@dataclass
class _StateWithBrief:
    brief: _FakeBrief


class FakeSessionStore(SessionStore):
    """Minimal SessionStore -- records are inserted directly via put_locked/
    put_asking (as test_build_service_start_build.py's fake does) since
    BuildService.start_build only ever calls get()."""

    def __init__(self) -> None:
        self.records: dict[str, SessionRecord] = {}

    def put_locked(self, session_id: str) -> None:
        now = datetime.now(timezone.utc)
        self.records[session_id] = SessionRecord(
            session_id=session_id,
            state=_StateWithBrief(_FakeBrief()),
            status="locked",
            created_at=now,
            last_accessed_at=now,
        )

    def put_asking(self, session_id: str) -> None:
        now = datetime.now(timezone.utc)
        self.records[session_id] = SessionRecord(
            session_id=session_id,
            state=_StateWithBrief(_FakeBrief()),
            status="asking",
            created_at=now,
            last_accessed_at=now,
        )

    async def create(self, state) -> SessionRecord:
        raise NotImplementedError("unused by BuildService")

    async def get(self, session_id: str):
        return self.records.get(session_id)

    async def peek(self, session_id: str):
        return self.records.get(session_id)

    async def update(self, session_id: str, state, status):
        raise NotImplementedError("unused by BuildService")

    async def delete(self, session_id: str) -> bool:
        return self.records.pop(session_id, None) is not None

    async def sweep_expired(self) -> int:
        return 0


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
    store = FakeSessionStore()
    registry = InMemoryJobRegistry()
    executor = ThreadPoolExecutor(max_workers=1)
    app.state.build_service = BuildService(
        registry, store, executor, max_concurrent=1, timeout_s=5.0,
    )
    # builds.router is mount-agnostic (docstring); mounted here exactly the
    # way create_app() will eventually mount it, since that step is deferred.
    app.include_router(
        builds.router, prefix="/api/v1", dependencies=[Depends(require_api_key)]
    )

    yield app, store
    executor.shutdown(wait=True)


@pytest.fixture
def client(app_and_store):
    """A context-managed TestClient. Without `with`, starlette's TestClient
    spins up a fresh portal/event loop per call and tears it down when the
    call returns, orphaning any asyncio.create_task scheduled during the
    request (BuildService's fire-and-forget _run_and_store task, in
    particular) -- it would never progress between polls. `with` keeps one
    portal (and its background thread's running loop) alive for every
    request the fixture makes, so the scheduled task keeps advancing in
    real time between client.post()/client.get() calls, exactly like a real
    server process."""
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
    store.put_locked("sess-locked")
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
    store.put_asking("sess-asking")

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
    store.put_locked("sess-a")
    store.put_locked("sess-b")

    release_event = threading.Event()

    def slow_run_from_brief(brief):
        # Blocks the first build so the second POST is admitted while the
        # first is still occupying the sole (max_concurrent=1) slot.
        release_event.wait(timeout=5.0)
        return {"build_card": _make_build_card()}

    monkeypatch.setattr(build_service_module, "run_from_brief", slow_run_from_brief)

    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            resp1 = client.post("/api/v1/builds", json={"session_id": "sess-a"})
            assert resp1.status_code == 202

            resp2 = client.post("/api/v1/builds", json={"session_id": "sess-b"})
            assert resp2.status_code == 429
            body = resp2.json()
            assert body["error"]["code"] == "BUILD_CAPACITY"
            assert body["error"]["retryable"] is True
            assert resp2.headers.get("retry-after") == "30"
    finally:
        release_event.set()


# ---------------------------------------------------------------------------
# 6. builds.router is mount-agnostic; main.py is left untouched for mounting
# ---------------------------------------------------------------------------

def test_builds_router_carries_no_prefix_of_its_own() -> None:
    # Same proof shape as intake's routes: the module-level router object
    # only knows its own "/builds" prefix, never "/api/v1" -- that's added
    # at inclusion time (here in the fixture; in production, at the deferred
    # mounting step).
    assert builds.router.prefix == "/builds"
    paths = {route.path for route in builds.router.routes}
    assert paths == {"/builds", "/builds/{build_id}"}
