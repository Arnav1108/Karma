"""IntakeService — API-facing wrapper around the core intake per-turn primitives.

Only create_session is implemented in this pass; submit_answer, get_snapshot,
lock_early, and abandon are stubbed per karma ai/docs/intake_service_plan.md
section 1 (implemented in a later step). See that plan for the full contract,
including the atomicity, locking, and executor-dispatch rules the stubbed
methods will need to follow.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import openai

from agents.nodes.node1_intake import (
    IntakeQuestion,
    IntakeSessionState,
    blank_brief,
    intake_begin,
)
from api.services.exceptions import LlmUpstreamError
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
        raise NotImplementedError  # implemented in a later step

    async def get_snapshot(self, session_id: str) -> SessionRecord:
        raise NotImplementedError  # implemented in a later step

    async def lock_early(self, session_id: str) -> SessionRecord:
        raise NotImplementedError  # implemented in a later step

    async def abandon(self, session_id: str) -> None:
        raise NotImplementedError  # implemented in a later step
