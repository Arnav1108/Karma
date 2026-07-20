"""IntakeService — API-facing wrapper around the core intake per-turn primitives.

All five methods (create_session, submit_answer, get_snapshot, lock_early,
abandon) are implemented per karma ai/docs/intake_service_plan.md section 1.
See that plan for the full contract, including the atomicity, locking, and
executor-dispatch rules each method follows.
"""

from __future__ import annotations

import asyncio
import functools
from uuid import uuid4

import openai

from agents.llm.client import StructuredCallError
from agents.nodes.node1_intake import (
    IntakeQuestion,
    IntakeSessionState,
    blank_brief,
    floor_met,
    intake_begin,
    intake_step,
    lock_brief,
)
from api.services.exceptions import (
    BriefFloorNotMetError,
    LlmUpstreamError,
    SessionAlreadyLockedError,
    SessionNotFoundError,
    TurnInProgressError,
)
from api.services.session_store import SessionRecord, SessionStore


class IntakeService:
    def __init__(self, store: SessionStore) -> None:
        self._store = store

    async def create_session(
        self, client_ref: str | None = None,
    ) -> tuple[SessionRecord, IntakeQuestion | None]:
        brief = blank_brief(uuid4(), uuid4(), uuid4())
        state = IntakeSessionState(brief=brief, history=[])

        loop = asyncio.get_running_loop()
        try:
            state, question = await loop.run_in_executor(None, intake_begin, state, None)
        except openai.OpenAIError as exc:
            raise LlmUpstreamError(exc) from exc

        record = await self._store.create(state)
        return record, question

    async def submit_answer(
        self, session_id: str, answer: str,
    ) -> tuple[SessionRecord, IntakeQuestion | None, bool]:
        record = await self._store.get(session_id)
        if record is None:
            raise SessionNotFoundError
        if record.status == "locked":
            raise SessionAlreadyLockedError
        if record.lock.locked():
            raise TurnInProgressError

        async with record.lock:
            working_state = record.state.model_copy(deep=True)

            loop = asyncio.get_running_loop()
            try:
                working_state, question, locked = await loop.run_in_executor(
                    None, functools.partial(intake_step, working_state, answer, None)
                )
            except (openai.OpenAIError, StructuredCallError) as exc:
                raise LlmUpstreamError(exc) from exc

            if not locked and question is None:
                working_state.brief = lock_brief(working_state.brief)
                locked = True

            status = "locked" if locked else "asking"
            updated = await self._store.update(session_id, working_state, status)
            if updated is None:
                raise SessionNotFoundError

            return updated, question, locked

    async def get_snapshot(self, session_id: str) -> SessionRecord:
        record = await self._store.peek(session_id)
        if record is None:
            raise SessionNotFoundError
        return record

    async def lock_early(self, session_id: str) -> SessionRecord:
        record = await self._store.get(session_id)
        if record is None:
            raise SessionNotFoundError
        if record.status == "locked":
            raise SessionAlreadyLockedError
        if record.lock.locked():
            raise TurnInProgressError

        async with record.lock:
            if not floor_met(record.state.brief):
                missing = []
                if record.state.brief.budget.comfortable_max <= 0:
                    missing.append("budget")
                if not record.state.brief.purpose.sub_case:
                    missing.append("primary_use_case")
                raise BriefFloorNotMetError(missing)

            working_state = record.state.model_copy(deep=True)
            working_state.brief = lock_brief(working_state.brief)

            updated = await self._store.update(session_id, working_state, "locked")
            if updated is None:
                raise SessionNotFoundError

            return updated

    async def abandon(self, session_id: str) -> None:
        await self._store.delete(session_id)
