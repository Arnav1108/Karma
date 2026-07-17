from fastapi import FastAPI

from api.routers import health


def create_app() -> FastAPI:
    app = FastAPI(title="Karma Advisor API")
    app.include_router(health.router)
    return app


app = create_app()
