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


def _parse_api_keys(raw: str) -> frozenset[str]:
    return frozenset(key.strip() for key in raw.split(",") if key.strip())


def _parse_cors_origins(raw: str) -> tuple[str, ...]:
    return tuple(origin.strip() for origin in raw.split(",") if origin.strip())


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
    )
