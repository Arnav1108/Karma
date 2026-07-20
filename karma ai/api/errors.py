"""Centralized exception-to-HTTP mapping for the intake API routes.

Registers one @app.exception_handler per IntakeServiceError subclass (plus the
base class as a catch-all for any future subclass a handler forgets to add
explicitly), FastAPI's RequestValidationError, and a bare Exception catch-all,
so every route returns the same {"error": {...}} envelope. See
karma ai/docs/intake_routes_plan.md section 2 for the mapping this implements.

BriefPersistenceError is not in that section's table (it postdates the plan,
added when locked briefs started persisting to Postgres). Per the plan's own
§8 item 4 note that it "will need its own new IntakeServiceError subclass...
and a corresponding new row in §2 - not designed here", this file maps it to
503 DATABASE_UNAVAILABLE, retryable=true, matching the DATABASE_UNAVAILABLE
convention already used for Postgres-unavailability elsewhere (api_design.md
row: "Postgres unreachable at request time -> 503 DATABASE_UNAVAILABLE, yes").
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from api.dtos import ErrorBody, ErrorEnvelope
from api.services.exceptions import (
    BriefFloorNotMetError,
    BriefPersistenceError,
    IntakeServiceError,
    LlmUpstreamError,
    SessionAlreadyLockedError,
    SessionNotFoundError,
    TurnInProgressError,
)

logger = logging.getLogger(__name__)


def _envelope(code: str, message: str, retryable: bool, details: dict | None = None) -> dict:
    envelope = ErrorEnvelope(
        error=ErrorBody(code=code, message=message, retryable=retryable, details=details)
    )
    return envelope.model_dump(exclude_none=True)


def register_exception_handlers(app: FastAPI) -> None:
    """Register all intake-route exception handlers on `app`. Called once from create_app()."""

    @app.exception_handler(SessionNotFoundError)
    async def _session_not_found(request: Request, exc: SessionNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content=_envelope("SESSION_NOT_FOUND", "Session not found or has expired.", False),
        )

    @app.exception_handler(SessionAlreadyLockedError)
    async def _session_already_locked(
        request: Request, exc: SessionAlreadyLockedError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content=_envelope(
                "SESSION_ALREADY_LOCKED", "Session is already locked.", False
            ),
        )

    @app.exception_handler(TurnInProgressError)
    async def _turn_in_progress(request: Request, exc: TurnInProgressError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content=_envelope(
                "TURN_IN_PROGRESS",
                "A turn is already in progress for this session. Retry shortly.",
                True,
            ),
        )

    @app.exception_handler(BriefFloorNotMetError)
    async def _brief_floor_not_met(
        request: Request, exc: BriefFloorNotMetError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content=_envelope(
                "BRIEF_FLOOR_NOT_MET",
                "Budget and/or primary use case must be answered before locking.",
                False,
                details={"missing": exc.missing},
            ),
        )

    @app.exception_handler(LlmUpstreamError)
    async def _llm_upstream_error(request: Request, exc: LlmUpstreamError) -> JSONResponse:
        # exc.cause is a raw openai SDK exception (or StructuredCallError) - log it
        # server-side only, never serialize its internals into the response body.
        logger.exception("LLM upstream call failed", exc_info=exc.cause)
        return JSONResponse(
            status_code=502,
            content=_envelope(
                "LLM_UPSTREAM_ERROR",
                "The upstream language model call failed. Please retry.",
                True,
            ),
        )

    @app.exception_handler(BriefPersistenceError)
    async def _brief_persistence_error(
        request: Request, exc: BriefPersistenceError
    ) -> JSONResponse:
        logger.exception("Failed to persist locked brief to Postgres", exc_info=exc.cause)
        return JSONResponse(
            status_code=503,
            content=_envelope(
                "DATABASE_UNAVAILABLE",
                "Failed to persist the locked brief. Please retry.",
                True,
            ),
        )

    @app.exception_handler(IntakeServiceError)
    async def _intake_service_error(request: Request, exc: IntakeServiceError) -> JSONResponse:
        # Catch-all safety net for any IntakeServiceError subclass without its own
        # handler above - Starlette dispatches by walking __mro__, so this only
        # fires when none of the more specific handlers registered above matched.
        logger.exception("Unhandled IntakeServiceError subclass")
        return JSONResponse(
            status_code=500,
            content=_envelope("INTERNAL_ERROR", "An internal error occurred.", False),
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=_envelope("VALIDATION_ERROR", "Request validation failed.", False),
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception in request handler")
        return JSONResponse(
            status_code=500,
            content=_envelope("INTERNAL_ERROR", "An internal error occurred.", False),
        )
