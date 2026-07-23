import asyncio
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from agents.db.postgres import PostgresClient
from api.config import get_settings
from api.errors import register_exception_handlers
from api.logging_config import configure_logging, request_id_var
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
    # Called at the very top of create_app(), not at module level, so it runs
    # exactly once per app construction (including in tests that call
    # create_app() directly) and before any log statement create_app() itself
    # can trigger further down (e.g. middleware.py's auth-disabled warning,
    # fired lazily on first request but wired here). configure_logging() is
    # itself idempotent (api/logging_config.py), so this is also safe on the
    # rare path where create_app() is invoked more than once in one process.
    configure_logging()
    settings = get_settings()
    app = FastAPI(
        title="Karma Advisor API",
        description=(
            "HTTP layer over the Karma Advisor LangGraph pipeline — conversational "
            "intake, feasibility, budget allocation, and part selection. The frozen "
            "frontend contract (DTOs, error catalog, per-screen fixtures) is documented "
            "in docs/frontend_contract_plan.md."
        ),
        version=settings.version,
        contact={"name": "Karma Computers"},
        lifespan=_lifespan,
    )
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

    @app.middleware("http")
    async def _request_id_middleware(request: Request, call_next):
        # contextvars need the token/.reset(token) pattern, not a bare
        # reassignment, to actually undo the .set() afterwards -- a plain
        # request_id_var.set("-") in the finally block would just be another
        # write, indistinguishable from a real (if coincidental) request id
        # of "-"; token.reset restores the exact prior value/absence instead.
        token = request_id_var.set(str(uuid.uuid4()))
        try:
            return await call_next(request)
        finally:
            request_id_var.reset(token)

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
    app.include_router(health.router, tags=["health"])
    # Deferred (function-scoped) imports: api.routers.intake/builds import
    # get_intake_service/get_build_service back from this module, which isn't
    # set on api.main until this point in its own top-to-bottom execution —
    # importing them at module level here would cycle back into this file
    # before those accessors exist. By the time create_app() actually runs
    # (app = create_app() at the bottom of this module), api.main is already
    # fully initialized, so the deferred import resolves cleanly.
    from api.middleware import require_api_key
    from api.routers import builds, intake
    app.include_router(
        intake.router,
        prefix="/api/v1",
        tags=["intake"],
        dependencies=[Depends(require_api_key)],
    )
    app.include_router(
        builds.router,
        prefix="/api/v1",
        tags=["builds"],
        dependencies=[Depends(require_api_key)],
    )

    return app


app = create_app()
