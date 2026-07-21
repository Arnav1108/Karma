"""Unit tests for the Phase 5 TTL/job-cap Settings fields (docs/hardening_plan.md
section 3): session_ttl_min, locked_session_ttl_h, build_result_ttl_h,
max_job_records.

get_settings() is @lru_cache, so every test clears the cache before and after
to avoid leaking a memoized Settings across tests that set different env vars.
Settings stores these fields in their RAW unit (minutes/hours), not
pre-converted to seconds -- each call site (main.py's store construction,
routers/intake.py's expires_at) does the multiplication itself. These tests
cover both: that the raw value parses correctly, and that the multiplication
a caller would do produces the right number of seconds.
"""

from __future__ import annotations

import pytest

from api.config import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_session_ttl_min_default():
    settings = get_settings()
    assert settings.session_ttl_min == 30


def test_session_ttl_min_from_env(monkeypatch):
    monkeypatch.setenv("KARMA_SESSION_TTL_MIN", "5")
    settings = get_settings()
    assert settings.session_ttl_min == 5
    assert settings.session_ttl_min * 60 == 300


def test_locked_session_ttl_h_default():
    settings = get_settings()
    assert settings.locked_session_ttl_h == 24


def test_locked_session_ttl_h_from_env(monkeypatch):
    monkeypatch.setenv("KARMA_LOCKED_SESSION_TTL_H", "2")
    settings = get_settings()
    assert settings.locked_session_ttl_h == 2
    assert settings.locked_session_ttl_h * 3600 == 7200


def test_build_result_ttl_h_default():
    settings = get_settings()
    assert settings.build_result_ttl_h == 24


def test_build_result_ttl_h_from_env(monkeypatch):
    monkeypatch.setenv("KARMA_BUILD_RESULT_TTL_H", "1")
    settings = get_settings()
    assert settings.build_result_ttl_h == 1
    assert settings.build_result_ttl_h * 3600 == 3600


def test_max_job_records_default():
    settings = get_settings()
    assert settings.max_job_records == 500


def test_max_job_records_from_env(monkeypatch):
    monkeypatch.setenv("KARMA_MAX_JOB_RECORDS", "50")
    settings = get_settings()
    assert settings.max_job_records == 50
