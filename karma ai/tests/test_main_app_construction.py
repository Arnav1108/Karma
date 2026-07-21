"""Confirms api.main.create_app() actually wires the Phase 5 TTL/cap Settings
fields into the real InMemorySessionStore/InMemoryJobRegistry constructors
(docs/hardening_plan.md section 3), rather than leaving them on the stores'
hardcoded module defaults (ASKING_TTL_SECONDS=1800 etc.).

No public accessor exists for a store's effective TTL (this plan's routers/intake.py
fix went with Depends(get_settings) at the route level, not a store-level
accessor -- see test_intake_routes.py's own note on this), so these tests read
the stores' private attributes directly. That's an acceptable, narrowly-scoped
exception for a test whose whole point is to guard the wiring at the
construction site.

get_settings() is @lru_cache -- the cache is cleared before/after each test so
env vars set here don't leak into (or get clobbered by) other test modules.
"""

from __future__ import annotations

import pytest

from api import main as api_main
from api.config import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_session_store_gets_settings_derived_ttls_not_module_defaults(monkeypatch):
    monkeypatch.setenv("KARMA_SESSION_TTL_MIN", "1")
    monkeypatch.setenv("KARMA_LOCKED_SESSION_TTL_H", "2")

    app = api_main.create_app()

    assert app.state.session_store._asking_ttl_seconds == 60
    assert app.state.session_store._locked_ttl_seconds == 7200


def test_job_registry_gets_settings_derived_ttl_and_cap_not_module_defaults(monkeypatch):
    monkeypatch.setenv("KARMA_BUILD_RESULT_TTL_H", "1")
    monkeypatch.setenv("KARMA_MAX_JOB_RECORDS", "7")

    app = api_main.create_app()

    assert app.state.job_registry._terminal_ttl_seconds == 3600
    assert app.state.job_registry._max_records == 7


def test_defaults_still_match_the_stores_own_hardcoded_defaults():
    """Sanity check: with no env override, the settings-derived values equal
    what the stores' own module constants would have produced -- so existing
    behavior (and any test constructing a bare store) is unaffected.
    """
    app = api_main.create_app()

    assert app.state.session_store._asking_ttl_seconds == 1800
    assert app.state.session_store._locked_ttl_seconds == 86400
    assert app.state.job_registry._terminal_ttl_seconds == 86400
    assert app.state.job_registry._max_records == 500
