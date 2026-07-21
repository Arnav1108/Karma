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

import asyncio
import uuid

import pytest

from api import main as api_main
from api.config import get_settings
from api.logging_config import request_id_var


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


def _find_dispatch(app):
    """Pull the raw dispatch function out of the @app.middleware("http")
    registration -- Starlette stores it as Middleware(BaseHTTPMiddleware,
    dispatch=fn) in app.user_middleware. CORSMiddleware (also registered)
    takes no `dispatch` kwarg, so filtering on that key is unambiguous.
    """
    for middleware in app.user_middleware:
        if "dispatch" in middleware.kwargs:
            return middleware.kwargs["dispatch"]
    raise AssertionError("no dispatch-style middleware registered on the app")


def test_request_id_middleware_sets_a_fresh_id_per_request_and_resets_after():
    """Calls the real registered dispatch function directly (not through
    TestClient) so everything -- the .set() before call_next, the .reset()
    in the finally, and this test's own assertions -- runs in the exact same
    thread/async context. Going through TestClient would run the app on a
    separate thread with its own root context, where reading request_id_var
    from the test's own thread would trivially read "-" regardless of
    whether main.py's finally block ever actually reset anything -- that
    would not be a meaningful test of the reset.
    """
    app = api_main.create_app()
    dispatch = _find_dispatch(app)

    captured_ids: list[str] = []

    async def fake_call_next(request):
        captured_ids.append(request_id_var.get())
        return "ok"

    async def _run():
        resp1 = await dispatch(object(), fake_call_next)
        assert resp1 == "ok"
        after_first = request_id_var.get()

        resp2 = await dispatch(object(), fake_call_next)
        assert resp2 == "ok"
        after_second = request_id_var.get()

        return after_first, after_second

    after_first, after_second = asyncio.run(_run())

    assert len(captured_ids) == 2
    # Each request got its own id, and both are real uuid4s, not placeholders.
    uuid.UUID(captured_ids[0])
    uuid.UUID(captured_ids[1])
    assert captured_ids[0] != captured_ids[1]

    # The var is back to the unset default after EACH request, not just
    # eventually -- proving .reset(token) actually ran, rather than the next
    # request's fresh .set() merely overwriting a leaked value.
    assert after_first == "-"
    assert after_second == "-"
