"""Shared FakeSessionStore for IntakeService unit tests.

One in-memory SessionStore implementation reused across
test_intake_service_create_session.py and test_intake_service_submit_answer.py
so the fake's behavior (and any future fix to it) can't drift between files.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from api.services.session_store import SessionRecord, SessionStore


class FakeSessionStore(SessionStore):
    """Minimal in-memory SessionStore that records create()/update() calls for assertions."""

    def __init__(self) -> None:
        self.records: dict[str, SessionRecord] = {}
        self.create_calls = 0
        self.update_calls = 0

    async def create(self, state) -> SessionRecord:
        self.create_calls += 1
        now = datetime.now(timezone.utc)
        record = SessionRecord(
            session_id=str(uuid4()),
            state=state,
            status="asking",
            created_at=now,
            last_accessed_at=now,
        )
        self.records[record.session_id] = record
        return record

    async def get(self, session_id: str):
        return self.records.get(session_id)

    async def peek(self, session_id: str):
        return self.records.get(session_id)

    async def update(self, session_id: str, state, status):
        self.update_calls += 1
        record = self.records.get(session_id)
        if record is None:
            return None
        record.state = state
        record.status = status
        return record

    async def delete(self, session_id: str) -> bool:
        return self.records.pop(session_id, None) is not None

    async def sweep_expired(self) -> int:
        return 0


class ExpiringMidTurnSessionStore(FakeSessionStore):
    """FakeSessionStore variant whose update() simulates the session expiring
    mid-turn: the very first update() call after construction returns None
    (as if the TTL lapsed while the LLM call was in flight), without actually
    removing the record from .records (mirrors a real store having already
    evicted it elsewhere, not a bug in the fake)."""

    def __init__(self) -> None:
        super().__init__()
        self.simulate_expiry_on_next_update = False

    async def update(self, session_id: str, state, status):
        self.update_calls += 1
        if self.simulate_expiry_on_next_update:
            self.simulate_expiry_on_next_update = False
            return None
        record = self.records.get(session_id)
        if record is None:
            return None
        record.state = state
        record.status = status
        return record
