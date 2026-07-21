"""Unit tests for the background sweep loop in api/main.py.

Tests _sweep_loop directly against a fake app (a SimpleNamespace with a
.state carrying fake session_store/job_registry objects whose
sweep_expired() is an AsyncMock), never spinning up a real FastAPI app or
InMemorySessionStore/InMemoryJobRegistry -- the loop's own control flow
(interval, per-store try/except, cancellation) is what's under test, not
the stores themselves (those have their own test_session_store.py /
test_job_registry.py coverage).
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.main import _sweep_loop

pytestmark = pytest.mark.asyncio


def _make_app(interval: float, session_sweep, job_sweep) -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(
            settings=SimpleNamespace(sweep_interval_s=interval),
            session_store=SimpleNamespace(sweep_expired=session_sweep),
            job_registry=SimpleNamespace(sweep_expired=job_sweep),
        )
    )


async def test_sweep_loop_calls_both_stores_after_interval():
    session_sweep = AsyncMock(return_value=0)
    job_sweep = AsyncMock(return_value=0)
    app = _make_app(0.05, session_sweep, job_sweep)

    task = asyncio.create_task(_sweep_loop(app))
    await asyncio.sleep(0.17)  # ~3 intervals
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert session_sweep.await_count >= 2
    assert job_sweep.await_count >= 2


async def test_sweep_loop_survives_a_raising_store_and_keeps_going(caplog):
    session_sweep = AsyncMock(side_effect=[RuntimeError("boom"), 0, 0, 0])
    job_sweep = AsyncMock(return_value=0)
    app = _make_app(0.05, session_sweep, job_sweep)

    with caplog.at_level(logging.ERROR, logger="api.main"):
        task = asyncio.create_task(_sweep_loop(app))
        await asyncio.sleep(0.35)  # first iteration raises, later ones don't
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert session_sweep.await_count >= 2  # raised once, then called again -- loop kept going
    assert any(
        "sweep failed for sessions" in record.message for record in caplog.records
    )
    # job_registry's sweep is independent of session_store's failure -- it kept running too.
    assert job_sweep.await_count >= 2


async def test_sweep_loop_exits_cleanly_on_cancellation():
    session_sweep = AsyncMock(return_value=0)
    job_sweep = AsyncMock(return_value=0)
    app = _make_app(0.05, session_sweep, job_sweep)

    task = asyncio.create_task(_sweep_loop(app))
    await asyncio.sleep(0.12)
    task.cancel()

    # Awaiting the cancelled task must not raise anything other than
    # CancelledError, and that's the only exception we expect/allow here --
    # proves the loop doesn't leak some other unhandled exception on teardown.
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled() or task.done()
