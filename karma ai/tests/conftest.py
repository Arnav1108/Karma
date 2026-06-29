"""Session-scoped fixtures for pipeline integration tests.

Loads the three fixture briefs once per test session and provides a db_available
flag so DB-dependent tests can skip cleanly when Postgres is unreachable.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add karma ai/ to sys.path so "agents" resolves without an install step.
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest

from agents.schemas.brief import UserBuildBrief
from agents.schemas.slots import ComponentSlot

_FIXTURES = _ROOT / "data" / "fixtures"


@pytest.fixture(scope="session")
def budget_gamer_brief() -> UserBuildBrief:
    return UserBuildBrief.model_validate_json(
        (_FIXTURES / "budget_gamer.json").read_text(encoding="utf-8")
    )


@pytest.fixture(scope="session")
def ml_workstation_brief() -> UserBuildBrief:
    return UserBuildBrief.model_validate_json(
        (_FIXTURES / "ml_workstation.json").read_text(encoding="utf-8")
    )


@pytest.fixture(scope="session")
def video_editor_brief() -> UserBuildBrief:
    return UserBuildBrief.model_validate_json(
        (_FIXTURES / "video_editor.json").read_text(encoding="utf-8")
    )


@pytest.fixture(scope="session")
def db_available() -> bool:
    """Return True if Postgres can serve a live price query, False otherwise.

    Used to skip tests that assert on LLM verdicts anchored to live catalog data.
    The underlying feasibility check degrades gracefully, but verdicts become less
    reliable without a real price anchor — so we skip rather than risk false fails.
    """
    try:
        from agents.db.postgres import PostgresClient
        PostgresClient().get_min_catalog_price(ComponentSlot.gpu)
        return True
    except Exception:
        return False
