import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    version: str
    api_keys: frozenset[str]
    cors_origins: tuple[str, ...]


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
    )
