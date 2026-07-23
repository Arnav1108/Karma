"""Build API routes -- two handlers over BuildService.

Mount-agnostic by design (docs/build_service_plan.md section 7): this file
adds no /api/v1 prefix and no auth dependency of its own -- both are added at
mount time by api.main.create_app() (app.include_router(builds.router,
prefix="/api/v1", dependencies=[Depends(require_api_key)])), mirroring
api/routers/intake.py.

Every handler is `async def` (hard rule -- a sync `def` handler would run in
FastAPI's worker-thread pool off the event loop that BuildService's registry
lock depends on, same rationale as intake.py). BuildServiceError subclasses
are never caught here; they propagate to the handlers registered by
api.errors.register_exception_handlers.

Importing get_build_service from api.main here is safe despite api.main's
create_app() importing this module back: that import is a deferred
(function-scoped) import inside create_app() itself, done after
get_build_service is already defined at api.main's module level -- the same
trick api.routers.intake relies on for get_intake_service.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from api.dtos import BuildAcceptedDTO, BuildStatusResponse, StartBuildRequest
from api.errors import UNAUTHORIZED_RESPONSE, error_response
from api.main import get_build_service
from api.mappers import map_build_status
from api.rate_limit import rate_limit
from api.services.build_service import BuildService

router = APIRouter(prefix="/builds")


@router.post(
    "",
    response_model=BuildAcceptedDTO,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit("build_create"))],
    responses={
        **UNAUTHORIZED_RESPONSE,
        404: error_response("SESSION_NOT_FOUND — unknown or expired session."),
        409: error_response("BRIEF_NOT_LOCKED — the session's brief is not locked yet."),
        422: error_response("VALIDATION_ERROR — request body failed validation."),
        429: error_response(
            "BUILD_CAPACITY (at max concurrent builds) or RATE_LIMITED "
            "(build-create quota exceeded); both set Retry-After."
        ),
    },
)
async def start_build(
    body: StartBuildRequest,
    service: BuildService = Depends(get_build_service),
) -> BuildAcceptedDTO:
    build_id = await service.start_build(body.session_id)
    return BuildAcceptedDTO(build_id=build_id, status="queued", poll_after_ms=2000)


@router.get(
    "/{build_id}",
    response_model=BuildStatusResponse,
    responses={
        **UNAUTHORIZED_RESPONSE,
        404: error_response("BUILD_NOT_FOUND — unknown or evicted build id."),
    },
)
async def get_build(
    build_id: str,
    service: BuildService = Depends(get_build_service),
) -> BuildStatusResponse:
    record = await service.get_build_status(build_id)
    return map_build_status(record)
