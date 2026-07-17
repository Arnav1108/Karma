from fastapi import FastAPI

from api.routers import health


def create_app() -> FastAPI:
    app = FastAPI(title="Karma Advisor API")
    # Health endpoints (/healthz, /readyz) are liveness/readiness probes — never gated.
    app.include_router(health.router)
    # Future routers attach auth at inclusion time, e.g.:
    #   from fastapi import Depends
    #   from api.middleware import require_api_key
    #   app.include_router(intake.router, dependencies=[Depends(require_api_key)])
    return app


app = create_app()
