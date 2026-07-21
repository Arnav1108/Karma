"""BuildService — API-facing driver for the async build pipeline.

Implements start_build and its background worker (_run_and_store) per
karma ai/docs/build_service_plan.md sections 2-5. get_build_status is a
stub here — its real implementation (record -> JobRecord, BuildNotFoundError
on miss) lands in a later step alongside the poll-route DTO mapping.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import openai

from agents.graph_runner import run_from_brief
from agents.llm.client import StructuredCallError
from agents.schemas import UserBuildBrief
from api.services.exceptions import BriefNotLockedError, BuildCapacityError, SessionNotFoundError
from api.services.job_registry import BuildStatus, JobRecord, JobRegistry
from api.services.session_store import SessionStore


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BuildService:
    def __init__(
        self,
        registry: JobRegistry,
        session_store: SessionStore,
        executor: ThreadPoolExecutor,
        *,
        max_concurrent: int,
        timeout_s: float,
    ) -> None:
        self._registry = registry
        self._session_store = session_store
        self._executor = executor
        self._max_concurrent = max_concurrent
        self._timeout_s = timeout_s
        self._active_builds = 0
        self._active_lock = asyncio.Lock()
        # Strong refs so scheduled tasks aren't garbage-collected mid-flight.
        self._tasks: set[asyncio.Task] = set()

    async def start_build(self, session_id: str) -> str:
        record = await self._session_store.get(session_id)
        if record is None:
            raise SessionNotFoundError
        if record.status != "locked":
            raise BriefNotLockedError

        async with self._active_lock:
            if self._active_builds >= self._max_concurrent:
                raise BuildCapacityError
            self._active_builds += 1

        job = await self._registry.create(session_id)
        task = asyncio.create_task(self._run_and_store(job.build_id, record.state.brief))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job.build_id

    async def _run_and_store(self, build_id: str, brief: UserBuildBrief) -> None:
        await self._registry.update(build_id, status="running", started_at=_utcnow())
        loop = asyncio.get_running_loop()
        # Hold the raw executor future (not just the wait_for-wrapped one) - see
        # the finally block below for why this matters for capacity reclamation.
        cf = self._executor.submit(run_from_brief, brief)
        try:
            state = await asyncio.wait_for(asyncio.wrap_future(cf, loop=loop), timeout=self._timeout_s)
            status = self._classify(state)
            await self._registry.update(build_id, status=status, state=state)
        except asyncio.TimeoutError:
            await self._registry.update(
                build_id,
                status="failed",
                error_code="BUILD_TIMEOUT",
                error_message="The build did not complete within the allotted time.",
            )
        except (openai.OpenAIError, StructuredCallError) as exc:
            await self._registry.update(
                build_id,
                status="failed",
                error_code="LLM_UPSTREAM_ERROR",
                error_message=f"The upstream language model call failed: {type(exc).__name__}.",
            )
        except Exception as exc:
            # Distinguishing DATABASE_UNAVAILABLE from a generic INTERNAL_ERROR
            # needs a probe of PostgresClient (plan §6) — deferred to the result
            # mapping step. Everything uncaught here lands as INTERNAL_ERROR.
            await self._registry.update(
                build_id,
                status="failed",
                error_code="INTERNAL_ERROR",
                error_message=f"An internal error occurred: {type(exc).__name__}.",
            )
        finally:
            # Reclaim the capacity slot only when the executor future ACTUALLY
            # resolves - never on the wait_for timeout alone. On a timeout,
            # asyncio.wait_for calls .cancel() on the future it awaited, which
            # (via the chaining asyncio.wrap_future sets up) immediately flips
            # THAT wrapper to CANCELLED without stopping the underlying thread
            # (concurrent.futures.Future.cancel() on an already-running call is
            # a no-op returning False) - so cf itself is still genuinely running.
            # Wrapping cf again here produces a fresh, non-cancelled future
            # chained to the same thread, so awaiting it blocks (asynchronously,
            # off the loop) until run_from_brief has truly returned - only then
            # is the slot freed, so a new build can never contend with a
            # still-running orphaned thread for the same executor worker.
            if not cf.done():
                try:
                    await asyncio.wrap_future(cf, loop=loop)
                except Exception:
                    pass
            async with self._active_lock:
                self._active_builds -= 1

    def _classify(self, state) -> BuildStatus:
        """Minimal terminal-status classification per plan §6's table.

        Deliberately not the full result mapping (BuildCardDTO/VerdictDTO,
        Neo4j-degraded warnings, empty/partial-card probing) - that's a later
        task. This only determines succeeded / infeasible / cannot_proceed
        well enough for _run_and_store to set JobRecord.status correctly.
        """
        build_card = state.get("build_card")
        if build_card is not None and build_card.parts:
            return "succeeded"
        verdict = state.get("feasibility_verdict")
        if verdict is not None and verdict.verdict == "impossible":
            return "infeasible"
        return "cannot_proceed"

    async def get_build_status(self, build_id: str) -> JobRecord:
        raise NotImplementedError  # implemented in a later step
