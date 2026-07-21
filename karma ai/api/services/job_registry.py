"""In-memory job registry for the build API.

Holds opaque per-build job records keyed by a server-generated build_id.
Mirrors api/services/session_store.py's shape (dict-backed, lazy expiry +
explicit sweep, store-level asyncio.Lock), narrowed where a build job's
concurrency profile genuinely differs from a session's - see JobRecord and
InMemoryJobRegistry docstrings. Never imports from agents/; state is held
as an opaque Any.
"""

from __future__ import annotations

import asyncio
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, fields as dataclass_fields
from datetime import datetime, timezone
from typing import Any, Literal

BuildStatus = Literal[
    "queued", "running",  # non-terminal
    "succeeded", "infeasible",  # terminal, domain outcomes
    "cannot_proceed", "failed",  # terminal (cannot_proceed structurally unreachable)
]

TERMINAL_STATUSES: frozenset[BuildStatus] = frozenset(
    {"succeeded", "infeasible", "cannot_proceed", "failed"}
)

# Terminal jobs are retained 24h from finished_at so the frontend can
# re-poll the result screen. queued/running jobs have no TTL - they are
# active and must not be evicted from under their own worker.
BUILD_TERMINAL_TTL_SECONDS = 86400

# LRU cap bounding unbounded growth alongside the TTL (plan section 1 / 8.4).
MAX_JOB_RECORDS = 500


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class JobRecord:
    """One build job.

    A plain dataclass, not a pydantic model, matching SessionRecord's
    rationale - though unlike SessionRecord this record carries no lock.
    A build job has exactly one writer (its own _run_and_store task) and
    is only ever read by pollers; clients never mutate an in-flight build
    (v1 has no PATCH /builds/{id}). A per-record lock would guard against
    a hazard that cannot occur here, so it is deliberately omitted - do
    not add one "for consistency" with SessionRecord.
    """

    build_id: str
    session_id: str
    status: BuildStatus
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    # The full final PipelineState, retained opaque so a dormant
    # refinement v2 has its inputs. Only set on succeeded / infeasible /
    # cannot_proceed - those are normal returns from run_from_brief with a
    # produced PipelineState. On "failed", run_from_brief raised before
    # producing one, so state stays None and error_code/error_message
    # carry the failure instead.
    state: Any
    error_code: str | None
    error_message: str | None
    warnings: list[str] = field(default_factory=list)


_JOB_RECORD_FIELD_NAMES = {f.name for f in dataclass_fields(JobRecord)}


class JobRegistry(ABC):
    """Abstract job registry. Implementations own expiry and cap policy."""

    @abstractmethod
    async def create(self, session_id: str) -> JobRecord:
        """Create a new job with a generated uuid4 build_id, status='queued'."""

    @abstractmethod
    async def get(self, build_id: str) -> JobRecord | None:
        """Return the live record, or None if missing or expired.

        Expiry only applies to terminal jobs (TTL measured from
        finished_at); non-terminal jobs never expire regardless of age.
        """

    @abstractmethod
    async def update(self, build_id: str, **fields: Any) -> JobRecord | None:
        """Update arbitrary fields on a job record. Returns None if missing."""

    @abstractmethod
    async def sweep_expired(self) -> int:
        """Evict all currently expired terminal jobs; return count evicted."""


class InMemoryJobRegistry(JobRegistry):
    """Dict-backed store with terminal-only TTL expiry and an LRU cap.

    Expiry is lazy (checked on access) plus an explicit sweep_expired()
    for periodic cleanup - identical pattern to InMemorySessionStore.
    Unlike sessions, the TTL only ever applies to terminal jobs, measured
    from finished_at; queued/running jobs are never evicted by age since
    an active build must not disappear from under its own worker.

    A single store-level asyncio.Lock guards the dict across all
    operations (create/get/update/sweep) - not a per-record lock, since a
    build job has exactly one writer (see JobRecord's docstring).

    LRU cap: enforced at create() time. After inserting a new record, if
    the store exceeds MAX_JOB_RECORDS, the oldest-finished terminal jobs
    are evicted first (sorted by finished_at ascending) until back at the
    cap. This is chosen over cap enforcement inside sweep_expired() because
    sweep_expired() only removes jobs that are also TTL-expired - it would
    not bound growth from a burst of terminal jobs still inside the 24h
    window. Non-terminal jobs are never evicted for capacity; if the cap is
    exceeded entirely by active jobs, the store temporarily grows past the
    cap rather than dropping in-flight builds.
    """

    def __init__(
        self,
        terminal_ttl_seconds: float = BUILD_TERMINAL_TTL_SECONDS,
        max_records: int = MAX_JOB_RECORDS,
    ) -> None:
        self._terminal_ttl_seconds = terminal_ttl_seconds
        self._max_records = max_records
        self._jobs: dict[str, JobRecord] = {}
        # Guards _jobs mutation across concurrent requests.
        self._store_lock = asyncio.Lock()

    def _is_expired(self, record: JobRecord, now: datetime) -> bool:
        if record.status not in TERMINAL_STATUSES:
            return False
        if record.finished_at is None:
            return False
        age = (now - record.finished_at).total_seconds()
        return age > self._terminal_ttl_seconds

    def _evict_over_cap_locked(self) -> None:
        overflow = len(self._jobs) - self._max_records
        if overflow <= 0:
            return
        evictable = sorted(
            (
                r
                for r in self._jobs.values()
                if r.status in TERMINAL_STATUSES and r.finished_at is not None
            ),
            key=lambda r: r.finished_at,
        )
        for record in evictable[:overflow]:
            del self._jobs[record.build_id]

    async def create(self, session_id: str) -> JobRecord:
        now = _utcnow()
        record = JobRecord(
            build_id=str(uuid.uuid4()),
            session_id=session_id,
            status="queued",
            created_at=now,
            started_at=None,
            finished_at=None,
            state=None,
            error_code=None,
            error_message=None,
        )
        async with self._store_lock:
            self._jobs[record.build_id] = record
            self._evict_over_cap_locked()
        return record

    async def get(self, build_id: str) -> JobRecord | None:
        async with self._store_lock:
            record = self._jobs.get(build_id)
            if record is None:
                return None
            now = _utcnow()
            if self._is_expired(record, now):
                # Lazy expiry: evict so a stale terminal job never resurrects.
                del self._jobs[build_id]
                return None
            return record

    async def update(self, build_id: str, **fields: Any) -> JobRecord | None:
        """Set arbitrary fields on a job record.

        A **fields kwargs signature is used instead of a fixed set of
        named parameters (as SessionStore.update's state/status pair)
        because JobRecord has many more independently-optional fields
        (started_at, finished_at, state, error_code, error_message,
        warnings) than SessionRecord, and callers only ever set a subset
        per transition (queued->running sets started_at; running->terminal
        sets status/state/finished_at; running->failed sets status/
        error_code/error_message). A fixed signature would force every
        caller to pass None for fields it isn't touching, which is
        indistinguishable from "clear this field". Only known JobRecord
        field names are accepted; anything else raises TypeError, same
        failure mode as passing an unknown kwarg to a normal method.

        If 'status' is set to a terminal value and the caller did not
        also pass 'finished_at', finished_at is auto-populated with the
        current time - the common case (a terminal transition marks the
        moment TTL expiry starts counting from).
        """
        unknown = set(fields) - _JOB_RECORD_FIELD_NAMES
        if unknown:
            raise TypeError(f"update() got unexpected field(s): {sorted(unknown)}")

        async with self._store_lock:
            record = self._jobs.get(build_id)
            if record is None:
                return None
            now = _utcnow()
            if self._is_expired(record, now):
                del self._jobs[build_id]
                return None

            for name, value in fields.items():
                setattr(record, name, value)

            if (
                fields.get("status") in TERMINAL_STATUSES
                and "finished_at" not in fields
            ):
                record.finished_at = now

            return record

    async def sweep_expired(self) -> int:
        async with self._store_lock:
            now = _utcnow()
            expired = [
                build_id
                for build_id, record in self._jobs.items()
                if self._is_expired(record, now)
            ]
            for build_id in expired:
                del self._jobs[build_id]
            return len(expired)
