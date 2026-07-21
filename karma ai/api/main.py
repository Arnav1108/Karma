from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from agents.db.postgres import PostgresClient
from api.config import get_settings
from api.errors import register_exception_handlers
from api.routers import health
from api.services.build_service import BuildService
from api.services.intake_service import IntakeService
from api.services.session_store import InMemorySessionStore


def get_intake_service(request: Request) -> IntakeService:
    return request.app.state.intake_service


def get_build_service(request: Request) -> BuildService:
    return request.app.state.build_service


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Karma Advisor API")
    register_exception_handlers(app)
    # Empty KARMA_CORS_ORIGINS => empty allow list => browsers block all
    # cross-origin requests. Never default to "*".
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.intake_service = IntakeService(InMemorySessionStore(), PostgresClient())
    # Health endpoints (/healthz, /readyz) are liveness/readiness probes — never gated.
    app.include_router(health.router)
    # Deferred (function-scoped) imports: api.routers.intake imports get_intake_service
    # back from this module, which isn't set on api.main until this point in its own
    # top-to-bottom execution — importing it at module level here would cycle back into
    # this file before get_intake_service exists. By the time create_app() actually runs
    # (app = create_app() at the bottom of this module), api.main is already fully
    # initialized, so the deferred import resolves cleanly.
    from api.middleware import require_api_key
    from api.routers import intake
    app.include_router(intake.router, prefix="/api/v1", dependencies=[Depends(require_api_key)])
    return app


app = create_app()
