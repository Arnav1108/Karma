from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from agents.db.postgres import PostgresClient
from api.config import get_settings
from api.errors import register_exception_handlers
from api.routers import health
from api.services.intake_service import IntakeService
from api.services.session_store import InMemorySessionStore


def get_intake_service(request: Request) -> IntakeService:
    return request.app.state.intake_service


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
    # Future routers attach auth at inclusion time, e.g.:
    #   from fastapi import Depends
    #   from api.middleware import require_api_key
    #   app.include_router(intake.router, dependencies=[Depends(require_api_key)])
    return app


app = create_app()
