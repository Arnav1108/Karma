import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from agents.db.postgres import PostgresClient
from api.config import get_settings
from api.errors import register_exception_handlers
from api.rate_limit import RateLimiter
from api.routers import health
from api.services.build_service import BuildService
from api.services.intake_service import IntakeService
from api.services.job_registry import InMemoryJobRegistry
from api.services.session_store import InMemorySessionStore

logger = logging.getLogger(__name__)


def get_intake_service(request: Request) -> IntakeService:
    return request.app.state.intake_service


def get_build_service(request: Request) -> BuildService:
    return request.app.state.build_service


async def _sweep_loop(app: FastAPI) -> None:
    interval = app.state.settings.sweep_interval_s
    while True:
        await asyncio.sleep(interval)
        for name, store in (
            ("sessions", app.state.session_store),
            ("jobs", app.state.job_registry),
        ):
            try:
                n = await store.sweep_expired()
                if n:
                    logger.info("sweep evicted %d expired %s", n, name)
            except Exception:
                logger.exception("sweep failed for %s", name)  # never let the loop die


@asynccontextmanager
async def _lifespan(app: FastAPI):
    sweeper = asyncio.create_task(_sweep_loop(app))
    try:
        yield
    finally:
        sweeper.cancel()
        try:
            await sweeper
        except asyncio.CancelledError:
            pass
        # app.state.build_executor is set inside create_app(), well before the
        # app is ever actually started, so it's always present by shutdown.
        app.state.build_executor.shutdown(wait=True)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Karma Advisor API", lifespan=_lifespan)
    app.state.settings = settings
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
    # Constructed once, shared by every rate_limit(category) dependency via
    # request.app.state.rate_limiter (docs/hardening_plan.md section 2).
    app.state.rate_limiter = RateLimiter(
        {
            "session_create": (settings.rl_session_create_per_min, 60),
            "intake_turn": (settings.rl_intake_turn_per_min, 60),
            "build_create": (settings.rl_build_create_per_hour, 3600),
        }
    )
    # Constructed once and shared between IntakeService and BuildService --
    # BuildService.start_build reads the locked brief straight out of the
    # same in-memory session store intake writes to; a second instance would
    # split state and every build would 404 with SessionNotFoundError.
    session_store = InMemorySessionStore(
        asking_ttl_seconds=settings.session_ttl_min * 60,
        locked_ttl_seconds=settings.locked_session_ttl_h * 3600,
    )
    app.state.session_store = session_store
    app.state.intake_service = IntakeService(session_store, PostgresClient())

    # Dedicated pool, not the default run_in_executor(None, ...) pool intake
    # shares, so long builds cannot starve intake's short LLM turns -- and so
    # bounding max_workers *is* the concurrency cap (build_service_plan.md
    # section 2 / section 4).
    build_executor = ThreadPoolExecutor(max_workers=settings.max_concurrent_builds)
    app.state.build_executor = build_executor
    job_registry = InMemoryJobRegistry(
        terminal_ttl_seconds=settings.build_result_ttl_h * 3600,
        max_records=settings.max_job_records,
    )
    app.state.job_registry = job_registry
    app.state.build_service = BuildService(
        job_registry,
        session_store,
        build_executor,
        max_concurrent=settings.max_concurrent_builds,
        timeout_s=settings.build_timeout_s,
    )

    # Health endpoints (/healthz, /readyz) are liveness/readiness probes — never gated.
    app.include_router(health.router)
    # Deferred (function-scoped) imports: api.routers.intake/builds import
    # get_intake_service/get_build_service back from this module, which isn't
    # set on api.main until this point in its own top-to-bottom execution —
    # importing them at module level here would cycle back into this file
    # before those accessors exist. By the time create_app() actually runs
    # (app = create_app() at the bottom of this module), api.main is already
    # fully initialized, so the deferred import resolves cleanly.
    from api.middleware import require_api_key
    from api.routers import builds, intake
    app.include_router(intake.router, prefix="/api/v1", dependencies=[Depends(require_api_key)])
    app.include_router(builds.router, prefix="/api/v1", dependencies=[Depends(require_api_key)])

    return app


app = create_app()
