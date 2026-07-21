"""Unit tests for BuildService.start_build and its background worker.

run_from_brief is monkeypatched at the api.services.build_service module
level so no real pipeline/LLM/DB call is ever made. Uses the real
InMemoryJobRegistry (already covered by tests/test_job_registry.py) and a
minimal FakeSessionStore so this file only has to fake what BuildService
doesn't already own. A real ThreadPoolExecutor drives the background work
so the timeout/capacity-reclamation tests exercise genuine thread timing,
not a mocked clock.
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

import openai
import pytest

from agents.llm.client import StructuredCallError
from api.services import build_service as build_service_module
from api.services.build_service import BuildService
from api.services.exceptions import BriefNotLockedError, BuildCapacityError, SessionNotFoundError
from api.services.job_registry import InMemoryJobRegistry
from api.services.session_store import SessionRecord, SessionStore

pytestmark = pytest.mark.asyncio


@dataclass
class _FakeBrief:
    """Stand-in for UserBuildBrief - BuildService only ever passes this
    through to run_from_brief untouched, so its real shape doesn't matter."""
    marker: str


@dataclass
class _StateWithBrief:
    """Stand-in for IntakeSessionState - BuildService only ever reads
    record.state.brief, so this is the minimal shape it needs."""
    brief: _FakeBrief


class FakeSessionStore(SessionStore):
    """Minimal in-memory SessionStore; records are inserted directly via
    put_locked/put_asking rather than through create(), since BuildService
    only ever calls get()."""

    def __init__(self) -> None:
        self.records: dict[str, SessionRecord] = {}

    def put_locked(self, session_id: str, brief_marker: str = "brief") -> None:
        now = datetime.now(timezone.utc)
        self.records[session_id] = SessionRecord(
            session_id=session_id,
            state=_StateWithBrief(_FakeBrief(brief_marker)),
            status="locked",
            created_at=now,
            last_accessed_at=now,
        )

    def put_asking(self, session_id: str) -> None:
        now = datetime.now(timezone.utc)
        self.records[session_id] = SessionRecord(
            session_id=session_id,
            state=_StateWithBrief(_FakeBrief("brief")),
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


async def _wait_for_terminal(registry: InMemoryJobRegistry, build_id: str, timeout: float = 2.0):
    """Poll the registry until the job reaches a terminal status."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = await registry.get(build_id)
        if record is not None and record.status in (
            "succeeded", "infeasible", "cannot_proceed", "failed",
        ):
            return record
        await asyncio.sleep(0.01)
    raise AssertionError(f"build {build_id} did not reach a terminal status within {timeout}s")


def _make_service(store, *, max_concurrent=2, timeout_s=5.0, executor=None):
    registry = InMemoryJobRegistry()
    executor = executor or ThreadPoolExecutor(max_workers=max_concurrent + 1)
    service = BuildService(
        registry, store, executor, max_concurrent=max_concurrent, timeout_s=timeout_s,
    )
    return service, registry, executor


# ---------------------------------------------------------------------------
# 1. start_build success
# ---------------------------------------------------------------------------

async def test_start_build_success_creates_queued_job_and_schedules_task(monkeypatch):
    store = FakeSessionStore()
    store.put_locked("sess-1")
    service, registry, executor = _make_service(store)
    monkeypatch.setattr(build_service_module, "run_from_brief", lambda brief: {})

    try:
        build_id = await service.start_build("sess-1")

        record = await registry.get(build_id)
        assert record is not None
        assert record.status == "queued"
        assert record.session_id == "sess-1"
        assert len(service._tasks) == 1
    finally:
        executor.shutdown(wait=False)


# ---------------------------------------------------------------------------
# 2. SessionNotFoundError
# ---------------------------------------------------------------------------

async def test_start_build_raises_session_not_found(monkeypatch):
    store = FakeSessionStore()
    service, registry, executor = _make_service(store)
    monkeypatch.setattr(build_service_module, "run_from_brief", lambda brief: {})

    try:
        with pytest.raises(SessionNotFoundError):
            await service.start_build("nonexistent")
    finally:
        executor.shutdown(wait=False)


# ---------------------------------------------------------------------------
# 3. BriefNotLockedError
# ---------------------------------------------------------------------------

async def test_start_build_raises_brief_not_locked(monkeypatch):
    store = FakeSessionStore()
    store.put_asking("sess-1")
    service, registry, executor = _make_service(store)
    monkeypatch.setattr(build_service_module, "run_from_brief", lambda brief: {})

    try:
        with pytest.raises(BriefNotLockedError):
            await service.start_build("sess-1")
    finally:
        executor.shutdown(wait=False)


# ---------------------------------------------------------------------------
# 4. BuildCapacityError - and prove admission atomicity under concurrency
# ---------------------------------------------------------------------------

async def test_start_build_capacity_error_is_atomic_under_concurrent_admission(monkeypatch):
    max_concurrent = 3
    store = FakeSessionStore()
    for i in range(max_concurrent + 1):
        store.put_locked(f"sess-{i}")

    release_event = threading.Event()

    def slow_run_from_brief(brief):
        # Blocks every admitted build until the test explicitly releases it,
        # so none can finish before the capacity check below runs.
        release_event.wait(timeout=5.0)
        return {}

    monkeypatch.setattr(build_service_module, "run_from_brief", slow_run_from_brief)

    service, registry, executor = _make_service(
        store, max_concurrent=max_concurrent, timeout_s=5.0,
        executor=ThreadPoolExecutor(max_workers=max_concurrent),
    )

    try:
        # Fire exactly max_concurrent concurrent start_build calls - all must
        # be admitted (this is the race the store-level lock must survive:
        # every coroutine reads self._active_builds and increments it while
        # interleaved with the others via asyncio.gather).
        results = await asyncio.gather(
            *(service.start_build(f"sess-{i}") for i in range(max_concurrent))
        )
        assert len(results) == max_concurrent
        assert all(isinstance(build_id, str) for build_id in results)
        assert service._active_builds == max_concurrent

        # One more, beyond capacity, must be rejected - not silently queued.
        with pytest.raises(BuildCapacityError):
            await service.start_build(f"sess-{max_concurrent}")

        # Capacity count is unaffected by the rejected attempt.
        assert service._active_builds == max_concurrent
    finally:
        release_event.set()
        await asyncio.gather(*list(service._tasks), return_exceptions=True)
        executor.shutdown(wait=True)


# ---------------------------------------------------------------------------
# 5. Capacity slot reclamation happens on real completion, not on timeout
# ---------------------------------------------------------------------------

async def test_capacity_slot_reclaimed_only_on_real_completion_not_on_timeout(monkeypatch):
    completion_event = threading.Event()
    TIMEOUT_S = 0.1
    MOCK_DURATION_S = 0.5

    def slow_completing_run_from_brief(brief):
        time.sleep(MOCK_DURATION_S)
        completion_event.set()
        return {}

    monkeypatch.setattr(build_service_module, "run_from_brief", slow_completing_run_from_brief)

    store = FakeSessionStore()
    store.put_locked("sess-1")
    service, registry, executor = _make_service(
        store, max_concurrent=1, timeout_s=TIMEOUT_S,
        executor=ThreadPoolExecutor(max_workers=1),
    )

    try:
        build_id = await service.start_build("sess-1")
        assert service._active_builds == 1

        # Well past TIMEOUT_S (0.1s) but well before MOCK_DURATION_S (0.5s):
        # the job must already be reported failed/BUILD_TIMEOUT, but the
        # underlying thread is still running and the slot must NOT be freed.
        await asyncio.sleep(0.25)
        record = await registry.get(build_id)
        assert record is not None
        assert record.status == "failed"
        assert record.error_code == "BUILD_TIMEOUT"
        assert not completion_event.is_set(), (
            "mock hadn't finished yet - this assertion is what makes the "
            "next one meaningful"
        )
        assert service._active_builds == 1, (
            "capacity slot must not be reclaimed while the underlying "
            "thread is still genuinely running"
        )

        # Now let it actually finish and confirm the slot is freed only then.
        await asyncio.sleep(0.5)
        assert completion_event.is_set()
        assert service._active_builds == 0
    finally:
        executor.shutdown(wait=True)


# ---------------------------------------------------------------------------
# 6. LLM failure during build
# ---------------------------------------------------------------------------

async def test_llm_failure_during_build_marks_job_failed_with_llm_error_code(monkeypatch):
    def raising_run_from_brief(brief):
        raise openai.OpenAIError("upstream boom")

    monkeypatch.setattr(build_service_module, "run_from_brief", raising_run_from_brief)

    store = FakeSessionStore()
    store.put_locked("sess-1")
    service, registry, executor = _make_service(store, timeout_s=5.0)

    try:
        build_id = await service.start_build("sess-1")
        record = await _wait_for_terminal(registry, build_id)

        assert record.status == "failed"
        assert record.error_code == "LLM_UPSTREAM_ERROR"
        assert service._active_builds == 0
    finally:
        executor.shutdown(wait=True)


async def test_structured_call_error_during_build_marks_job_failed_with_llm_error_code(monkeypatch):
    def raising_run_from_brief(brief):
        raise StructuredCallError(ValueError("bad schema"), raw_output="not json")

    monkeypatch.setattr(build_service_module, "run_from_brief", raising_run_from_brief)

    store = FakeSessionStore()
    store.put_locked("sess-1")
    service, registry, executor = _make_service(store, timeout_s=5.0)

    try:
        build_id = await service.start_build("sess-1")
        record = await _wait_for_terminal(registry, build_id)

        assert record.status == "failed"
        assert record.error_code == "LLM_UPSTREAM_ERROR"
    finally:
        executor.shutdown(wait=True)


# ---------------------------------------------------------------------------
# 7. Generic exception during build - doesn't crash the task/event loop
# ---------------------------------------------------------------------------

async def test_generic_exception_during_build_marks_job_failed_without_crashing(monkeypatch):
    def raising_run_from_brief(brief):
        raise ValueError("kaboom")

    monkeypatch.setattr(build_service_module, "run_from_brief", raising_run_from_brief)

    store = FakeSessionStore()
    store.put_locked("sess-1")
    service, registry, executor = _make_service(store, timeout_s=5.0)

    try:
        build_id = await service.start_build("sess-1")
        record = await _wait_for_terminal(registry, build_id)

        assert record.status == "failed"
        assert record.error_code == "INTERNAL_ERROR"
        assert service._active_builds == 0

        # Prove the task itself didn't propagate the exception anywhere -
        # the event loop and this test are still alive to reach this line,
        # and the scheduled task is done and holds no exception.
        assert len(service._tasks) == 0
    finally:
        executor.shutdown(wait=True)
