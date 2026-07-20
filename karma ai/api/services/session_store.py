"""In-memory session store for the intake API.

Holds opaque per-session state keyed by a server-generated session id.
This module must stay decoupled from the core pipeline: it never imports
from agents/ and treats the stored state as an opaque value.
"""

from __future__ import annotations

import asyncio
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

SessionStatus = Literal["asking", "locked"]

ASKING_TTL_SECONDS = 1800  # 30 minutes of inactivity
LOCKED_TTL_SECONDS = 86400  # 24 hours of inactivity


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SessionRecord:
    """One intake session.

    A plain dataclass rather than a pydantic model: the record carries a
    live asyncio.Lock, which pydantic cannot validate or serialize, and
    the record itself never crosses a serialization boundary.
    """

    session_id: str
    state: Any
    status: SessionStatus
    created_at: datetime
    last_accessed_at: datetime
    # Per-session turn lock so only one /message turn mutates a session
    # at a time. Runtime-only; never serialized.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)


class SessionStore(ABC):
    """Abstract session store. Implementations own expiry policy."""

    @abstractmethod
    async def create(self, state: Any) -> SessionRecord:
        """Create a new session with a generated uuid4 id."""

    @abstractmethod
    async def get(self, session_id: str) -> SessionRecord | None:
        """Return the live record, or None if missing or expired."""

    @abstractmethod
    async def update(
        self, session_id: str, state: Any, status: SessionStatus
    ) -> SessionRecord | None:
        """Replace state and status. Returns None if missing or expired."""

    @abstractmethod
    async def delete(self, session_id: str) -> bool:
        """Remove the session. Idempotent; True if it existed."""

    @abstractmethod
    async def sweep_expired(self) -> int:
        """Evict all currently expired sessions; return the count evicted."""


class InMemorySessionStore(SessionStore):
    """Dict-backed store with sliding TTL expiry.

    Expiry is lazy (checked on access) plus an explicit sweep_expired()
    for periodic cleanup. TTLs are measured from last_accessed_at and
    depend on status: short for "asking" sessions, long for "locked".
    """

    def __init__(
        self,
        asking_ttl_seconds: float = ASKING_TTL_SECONDS,
        locked_ttl_seconds: float = LOCKED_TTL_SECONDS,
    ) -> None:
        self._asking_ttl_seconds = asking_ttl_seconds
        self._locked_ttl_seconds = locked_ttl_seconds
        self._sessions: dict[str, SessionRecord] = {}
        # Guards _sessions mutation across concurrent requests.
        self._store_lock = asyncio.Lock()

    def _ttl_seconds(self, record: SessionRecord) -> float:
        if record.status == "locked":
            return self._locked_ttl_seconds
        return self._asking_ttl_seconds

    def _is_expired(self, record: SessionRecord, now: datetime) -> bool:
        age = (now - record.last_accessed_at).total_seconds()
        return age > self._ttl_seconds(record)

    async def create(self, state: Any) -> SessionRecord:
        now = _utcnow()
        record = SessionRecord(
            session_id=str(uuid.uuid4()),
            state=state,
            status="asking",
            created_at=now,
            last_accessed_at=now,
        )
        async with self._store_lock:
            self._sessions[record.session_id] = record
        return record

    async def get(self, session_id: str) -> SessionRecord | None:
        async with self._store_lock:
            record = self._sessions.get(session_id)
            if record is None:
                return None
            now = _utcnow()
            if self._is_expired(record, now):
                # Lazy expiry: evict so a stale session never resurrects.
                del self._sessions[session_id]
                return None
            record.last_accessed_at = now
            return record

    async def update(
        self, session_id: str, state: Any, status: SessionStatus
    ) -> SessionRecord | None:
        async with self._store_lock:
            record = self._sessions.get(session_id)
            if record is None:
                return None
            now = _utcnow()
            if self._is_expired(record, now):
                del self._sessions[session_id]
                return None
            record.state = state
            record.status = status
            record.last_accessed_at = now
            return record

    async def delete(self, session_id: str) -> bool:
        async with self._store_lock:
            return self._sessions.pop(session_id, None) is not None

    async def sweep_expired(self) -> int:
        async with self._store_lock:
            now = _utcnow()
            expired = [
                sid
                for sid, record in self._sessions.items()
                if self._is_expired(record, now)
            ]
            for sid in expired:
                del self._sessions[sid]
            return len(expired)
