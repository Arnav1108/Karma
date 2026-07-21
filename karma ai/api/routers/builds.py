"""Build API routes -- two handlers over BuildService.

Mount-agnostic by design (docs/build_service_plan.md section 7): this file
adds no /api/v1 prefix and no auth dependency of its own -- both are added at
mount time, e.g. `app.include_router(builds.router, prefix="/api/v1",
dependencies=[Depends(require_api_key)])`, mirroring api/routers/intake.py.
Not yet wired into api/main.py -- that mounting (plus instantiating
app.state.build_service) is a separate, later step.

Every handler is `async def` (hard rule -- a sync `def` handler would run in
FastAPI's worker-thread pool off the event loop that BuildService's registry
lock depends on, same rationale as intake.py). BuildServiceError subclasses
are never caught here; they propagate to the handlers registered by
api.errors.register_exception_handlers.

Unlike intake.py, importing get_build_service from api.main here carries no
circular-import risk: intake.py needed a deferred (function-scoped) import
because api.main's create_app() imports api.routers.intake, creating a cycle
back through api.main. api.main does not import this module yet (mounting is
deferred per the plan), so a plain top-level import is safe -- revisit this
once builds.router is actually wired into create_app().
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from api.dtos import BuildAcceptedDTO, BuildStatusResponse, StartBuildRequest
from api.main import get_build_service
from api.mappers import map_build_status
from api.services.build_service import BuildService

router = APIRouter(prefix="/builds")


@router.post("", response_model=BuildAcceptedDTO, status_code=status.HTTP_202_ACCEPTED)
async def start_build(
    body: StartBuildRequest,
    service: BuildService = Depends(get_build_service),
) -> BuildAcceptedDTO:
    build_id = await service.start_build(body.session_id)
    return BuildAcceptedDTO(build_id=build_id, status="queued", poll_after_ms=2000)


@router.get("/{build_id}", response_model=BuildStatusResponse)
async def get_build(
    build_id: str,
    service: BuildService = Depends(get_build_service),
) -> BuildStatusResponse:
    record = await service.get_build_status(build_id)
    return map_build_status(record)
