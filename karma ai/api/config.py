import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    version: str
    api_keys: frozenset[str]
    cors_origins: tuple[str, ...]
    max_concurrent_builds: int
    build_timeout_s: float
    sweep_interval_s: float
    session_ttl_min: int
    locked_session_ttl_h: int
    build_result_ttl_h: int
    max_job_records: int
    rate_limit_enabled: bool
    rl_session_create_per_min: int
    rl_intake_turn_per_min: int
    rl_build_create_per_hour: int


def _parse_api_keys(raw: str) -> frozenset[str]:
    return frozenset(key.strip() for key in raw.split(",") if key.strip())


def _parse_cors_origins(raw: str) -> tuple[str, ...]:
    return tuple(origin.strip() for origin in raw.split(",") if origin.strip())


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() not in ("false", "0", "no", "off", "")


@lru_cache
def get_settings() -> Settings:
    return Settings(
        version=os.environ.get("KARMA_API_VERSION", "0.1.0"),
        api_keys=_parse_api_keys(os.environ.get("KARMA_API_KEYS", "")),
        cors_origins=_parse_cors_origins(os.environ.get("KARMA_CORS_ORIGINS", "")),
        # Default 2 -- the Postgres pool is maxconn=10, shared with intake, and
        # each build makes many sequential DB calls (build_service_plan.md section 4).
        max_concurrent_builds=int(os.environ.get("KARMA_MAX_CONCURRENT_BUILDS", "2")),
        # Default 300s -- reported watchdog timeout, not a hard cancellation
        # (build_service_plan.md section 5).
        build_timeout_s=float(os.environ.get("KARMA_BUILD_TIMEOUT_S", "300")),
        # Default 300s (5 min) -- backstops the lazy TTL eviction on
        # SessionStore/JobRegistry; see docs/hardening_plan.md section 1.
        sweep_interval_s=float(os.environ.get("KARMA_SWEEP_INTERVAL_S", "300")),
        # Raw minutes/hours, NOT pre-converted to seconds -- each call site
        # (InMemorySessionStore/InMemoryJobRegistry construction in main.py,
        # expires_at computation in routers/intake.py) multiplies by 60/3600
        # itself. See docs/hardening_plan.md section 3.
        session_ttl_min=int(os.environ.get("KARMA_SESSION_TTL_MIN", "30")),
        locked_session_ttl_h=int(os.environ.get("KARMA_LOCKED_SESSION_TTL_H", "24")),
        build_result_ttl_h=int(os.environ.get("KARMA_BUILD_RESULT_TTL_H", "24")),
        max_job_records=int(os.environ.get("KARMA_MAX_JOB_RECORDS", "500")),
        # See docs/hardening_plan.md section 2. Disabling makes rate_limit()'s
        # dependency a genuine no-op (api/rate_limit.py) - useful for load
        # tests and local dev.
        rate_limit_enabled=_parse_bool(os.environ.get("KARMA_RATE_LIMIT_ENABLED", "true")),
        rl_session_create_per_min=int(os.environ.get("KARMA_RL_SESSION_CREATE_PER_MIN", "5")),
        rl_intake_turn_per_min=int(os.environ.get("KARMA_RL_INTAKE_TURN_PER_MIN", "20")),
        rl_build_create_per_hour=int(os.environ.get("KARMA_RL_BUILD_CREATE_PER_HOUR", "3")),
    )
