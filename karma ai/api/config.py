import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    version: str


@lru_cache
def get_settings() -> Settings:
    return Settings(
        version=os.environ.get("KARMA_API_VERSION", "0.1.0"),
    )
