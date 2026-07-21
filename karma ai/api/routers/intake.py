"""Intake API routes -- five handlers over IntakeService.

Mount-agnostic by design (docs/intake_routes_plan.md section 6): this file adds
no /api/v1 prefix and no auth dependency of its own -- both are added at mount
time, e.g. `app.include_router(intake.router, prefix="/api/v1",
dependencies=[Depends(require_api_key)])`. Not yet wired into api/main.py --
that mounting is a separate, later step.

Every handler is `async def` and awaits IntakeService directly (never a sync
`def`, which FastAPI would run in a worker thread off the event loop that
IntakeService's per-session lock depends on -- see plan section 7).
IntakeServiceError subclasses are never caught here; they propagate to the
handlers registered by api.errors.register_exception_handlers.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Response, status

from agents.nodes.node1_intake import IntakeSessionState
from api.config import Settings, get_settings
from api.dtos import (
    AnswerAskingResponse,
    AnswerLockedResponse,
    CreateSessionRequest,
    CreateSessionResponse,
    LockResponse,
    QuestionDTO,
    SnapshotResponse,
    SubmitAnswerRequest,
)
from api.errors import UNAUTHORIZED_RESPONSE, error_response
from api.main import get_intake_service
from api.mappers import map_brief_summary, map_progress, map_question
from api.rate_limit import rate_limit
from api.services.intake_service import IntakeService

router = APIRouter(prefix="/intake")


def _reconstruct_question(state: IntakeSessionState) -> QuestionDTO | None:
    """Rebuild the pending question from stored session state, without calling
    intake_begin -- that would spend a live phrasing LLM call on this
    read-only endpoint, contradicting the "sync, no LLM" snapshot contract
    (plan section 1.3).

    Known limitation (plan section 8 item 3): no `last_question` field exists
    yet on IntakeSessionState, so this hand-copies intake_begin's branch
    condition (node1_intake.py) instead of reading a stored IntakeQuestion
    back verbatim. If intake_begin's branching ever grows a new case, this
    copy can silently drift out of sync with no test forcing agreement. The
    clean fix is a core-side `last_question` field on IntakeSessionState, set
    wherever intake_begin sets current_question_id -- not attempted here, per
    the plan's own note to work within the current fields only.
    """
    if state.current_question_id is None and not state.brief.open_questions:
        return None

    text = state.history[-1]["content"] if state.history else ""
    if state.brief.open_questions:
        oq = state.brief.open_questions[0]
        attempts = state.open_question_attempts.get(oq, 0)
        kind = "confirm_default" if attempts == 1 else "clarification"
    else:
        kind = "sequence"
    return QuestionDTO(question_id=state.current_question_id, text=text, kind=kind)


@router.post(
    "/sessions",
    response_model=CreateSessionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("session_create"))],
    responses={
        **UNAUTHORIZED_RESPONSE,
        422: error_response("VALIDATION_ERROR — request body failed validation."),
        429: error_response(
            "RATE_LIMITED — session-create quota exceeded (Retry-After set)."
        ),
        502: error_response("LLM_UPSTREAM_ERROR — the intake phrasing LLM call failed."),
    },
)
async def create_session(
    body: CreateSessionRequest,
    service: IntakeService = Depends(get_intake_service),
    settings: Settings = Depends(get_settings),
) -> CreateSessionResponse:
    record, question = await service.create_session(body.client_ref)
    return CreateSessionResponse(
        session_id=record.session_id,
        status="asking",
        question=map_question(question),
        progress=map_progress(record.state, record.state.brief),
        expires_at=record.created_at + timedelta(minutes=settings.session_ttl_min),
    )


@router.post(
    "/sessions/{session_id}/answers",
    response_model=AnswerAskingResponse | AnswerLockedResponse,
    dependencies=[Depends(rate_limit("intake_turn"))],
    responses={
        **UNAUTHORIZED_RESPONSE,
        404: error_response("SESSION_NOT_FOUND — unknown or expired session."),
        409: error_response(
            "SESSION_ALREADY_LOCKED (session already locked) or TURN_IN_PROGRESS "
            "(a concurrent turn is running; Retry-After set)."
        ),
        422: error_response("VALIDATION_ERROR — request body failed validation."),
        429: error_response(
            "RATE_LIMITED — intake-turn quota exceeded (Retry-After set)."
        ),
        502: error_response("LLM_UPSTREAM_ERROR — an intake LLM call failed."),
        503: error_response(
            "DATABASE_UNAVAILABLE — persisting the auto-locked brief to Postgres failed."
        ),
    },
)
async def submit_answer(
    session_id: str,
    body: SubmitAnswerRequest,
    service: IntakeService = Depends(get_intake_service),
    settings: Settings = Depends(get_settings),
) -> AnswerAskingResponse | AnswerLockedResponse:
    record, question, locked = await service.submit_answer(session_id, body.answer)

    if locked:
        return AnswerLockedResponse(
            status="locked",
            brief_summary=map_brief_summary(record.state.brief, record.state.asked_so_far),
            progress=map_progress(record.state, record.state.brief),
        )

    return AnswerAskingResponse(
        status="asking",
        question=map_question(question),
        progress=map_progress(record.state, record.state.brief),
        expires_at=record.last_accessed_at + timedelta(minutes=settings.session_ttl_min),
    )


@router.get(
    "/sessions/{session_id}",
    response_model=SnapshotResponse,
    responses={
        **UNAUTHORIZED_RESPONSE,
        404: error_response("SESSION_NOT_FOUND — unknown or expired session."),
    },
)
async def get_snapshot(
    session_id: str,
    service: IntakeService = Depends(get_intake_service),
    settings: Settings = Depends(get_settings),
) -> SnapshotResponse:
    record = await service.get_snapshot(session_id)
    state = record.state

    if record.status == "locked":
        question = None
        brief_summary = map_brief_summary(state.brief, state.asked_so_far)
        ttl_delta = timedelta(hours=settings.locked_session_ttl_h)
    else:
        question = _reconstruct_question(state)
        brief_summary = None
        ttl_delta = timedelta(minutes=settings.session_ttl_min)

    return SnapshotResponse(
        status=record.status,
        question=question,
        progress=map_progress(state, state.brief),
        brief_summary=brief_summary,
        expires_at=record.last_accessed_at + ttl_delta,
    )


@router.post(
    "/sessions/{session_id}/lock",
    response_model=LockResponse,
    responses={
        **UNAUTHORIZED_RESPONSE,
        404: error_response("SESSION_NOT_FOUND — unknown or expired session."),
        409: error_response(
            "SESSION_ALREADY_LOCKED (already locked), TURN_IN_PROGRESS (a concurrent "
            "turn is running; Retry-After set), or BRIEF_FLOOR_NOT_MET (budget/primary "
            "use case unanswered; details.missing lists the fields)."
        ),
        503: error_response(
            "DATABASE_UNAVAILABLE — persisting the locked brief to Postgres failed."
        ),
    },
)
async def lock_session(
    session_id: str,
    service: IntakeService = Depends(get_intake_service),
) -> LockResponse:
    record = await service.lock_early(session_id)
    return LockResponse(
        status="locked",
        brief_summary=map_brief_summary(record.state.brief, record.state.asked_so_far),
    )


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**UNAUTHORIZED_RESPONSE},
)
async def abandon_session(
    session_id: str,
    service: IntakeService = Depends(get_intake_service),
) -> Response:
    await service.abandon(session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
