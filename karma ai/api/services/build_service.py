"""BuildService — API-facing driver for the async build pipeline.

Implements start_build, get_build_status, and the real result
classification (_classify) per karma ai/docs/build_service_plan.md
sections 2-6.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import openai

from agents.db.neo4j import Neo4jClient
from agents.db.postgres import PostgresClient
from agents.graph_runner import run_from_brief
from agents.llm.client import StructuredCallError
from agents.nodes.node3_selector import SELECTION_ORDER
from agents.schemas import ComponentSlot, UserBuildBrief
from api.services.exceptions import (
    BriefNotLockedError,
    BuildCapacityError,
    BuildNotFoundError,
    SessionNotFoundError,
)
from api.services.job_registry import BuildStatus, JobRecord, JobRegistry
from api.services.session_store import SessionStore

# Plan section 6's exact wording for the "not silent" Neo4j-degraded notice.
NEO4J_DEGRADED_WARNING = (
    "Compatibility graph was unavailable; parts were selected on catalog data "
    "only -- cross-compatibility and fitness checks were skipped."
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _postgres_up() -> bool:
    """Same connectivity probe as api/routers/health.py's /readyz — there is
    no dedicated PostgresClient.ping(); a live catalog query is core's
    existing way of confirming the pool can actually serve Postgres."""
    try:
        PostgresClient().get_min_catalog_price(ComponentSlot.gpu)
        return True
    except Exception:
        return False


def _neo4j_up() -> bool:
    """Same probe as api/routers/health.py's /readyz."""
    try:
        return Neo4jClient().ping()
    except Exception:
        return False


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
            # The Postgres/Neo4j probes _classify performs are genuine I/O and
            # run here, inside the try block, before the finally's capacity
            # reclamation — see that block's comment for why this ordering is
            # safe (cf is already done by this point; reclamation timing is
            # untouched by how long classification itself takes).
            status, error_code, warnings = await self._classify(state)
            await self._registry.update(
                build_id, status=status, state=state, error_code=error_code, warnings=warnings,
            )
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
            # This is run_from_brief raising outright (e.g. a raw psycopg2
            # error), not the build_card-shape-based classification _classify
            # performs below. Plan §6 doesn't specify a DATABASE_UNAVAILABLE
            # probe for this raised-exception path (only for an empty/partial
            # build_card that returned normally), so everything uncaught here
            # still lands as INTERNAL_ERROR.
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

    async def _classify(self, state) -> tuple[BuildStatus, str | None, list[str]]:
        """Full terminal-status classification per plan §6's table.

        Returns (status, error_code, warnings). error_code is only set for
        the "failed" status; warnings carries build_card.warnings (passed
        through unchanged for genuine dead-ends) plus, when applicable, the
        synthesized Neo4j-degraded notice.

        Row (a) — build_card present: "parts count < len(SELECTION_ORDER)"
        covers both partial and fully-empty parts (0 < 9). Degraded ⇒ probe
        Postgres (via api/routers/health.py's exact /readyz pattern, run off
        the event loop on the build executor since it's blocking I/O):
          - probe fails -> failed / DEGRADED_DEPENDENCY (infra flap, retryable)
          - probe succeeds, parts empty AND build_card.warnings empty ->
            failed / INTERNAL_ERROR (the one "shouldn't occur" shape plan §6
            calls out explicitly)
          - probe succeeds otherwise -> succeeded; build_card.warnings passed
            through as-is, nothing synthesized for this case.
        Full parts (no degradation) -> succeeded outright, no Postgres probe.

        Row (b) — feasibility_verdict.verdict == "impossible", no build_card
        -> infeasible.

        Row (c) — error_message present, no verdict, no card -> cannot_proceed.
        Structurally near-unreachable via run_from_brief (plan §6 / ground-
        truth table), kept for completeness; also the fallback for any other
        unexpected state shape.

        Row (d) — the post-run Neo4j "not silent" probe runs in EVERY branch
        above, including the failed/DEGRADED_DEPENDENCY one, not only on
        succeeded: plan §6 explicitly allows an infeasible/cannot_proceed/
        failed result to also note the compatibility graph was down "if
        relevant", so this always checks and appends rather than gating on
        the branch reached. Best-effort signal — can disagree with
        mid-build availability (plan §6 caveat).
        """
        loop = asyncio.get_running_loop()
        build_card = state.get("build_card")
        verdict = state.get("feasibility_verdict")

        status: BuildStatus
        error_code: str | None = None
        warnings: list[str] = []

        if build_card is not None:
            warnings = list(build_card.warnings)
            parts_count = len(build_card.parts)
            degraded = parts_count < len(SELECTION_ORDER)
            if degraded:
                postgres_ok = await loop.run_in_executor(self._executor, _postgres_up)
                if not postgres_ok:
                    status, error_code = "failed", "DEGRADED_DEPENDENCY"
                elif parts_count == 0 and not warnings:
                    status, error_code = "failed", "INTERNAL_ERROR"
                else:
                    status = "succeeded"
            else:
                status = "succeeded"
        elif verdict is not None and verdict.verdict == "impossible":
            status = "infeasible"
        elif state.get("error_message") is not None:
            status = "cannot_proceed"
        else:
            status = "cannot_proceed"

        neo4j_ok = await loop.run_in_executor(self._executor, _neo4j_up)
        if not neo4j_ok:
            warnings = warnings + [NEO4J_DEGRADED_WARNING]

        return status, error_code, warnings

    async def get_build_status(self, build_id: str) -> JobRecord:
        record = await self._registry.get(build_id)
        if record is None:
            raise BuildNotFoundError
        return record
