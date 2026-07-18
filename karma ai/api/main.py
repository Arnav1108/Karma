from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.config import get_settings
from api.routers import health


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Karma Advisor API")
    # Empty KARMA_CORS_ORIGINS => empty allow list => browsers block all
    # cross-origin requests. Never default to "*".
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Health endpoints (/healthz, /readyz) are liveness/readiness probes — never gated.
    app.include_router(health.router)
    # Future routers attach auth at inclusion time, e.g.:
    #   from fastapi import Depends
    #   from api.middleware import require_api_key
    #   app.include_router(intake.router, dependencies=[Depends(require_api_key)])
    return app


app = create_app()
