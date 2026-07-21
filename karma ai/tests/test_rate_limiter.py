"""Unit tests for the in-process sliding-window-log rate limiter.

Pure unit tests: no FastAPI, no network. Time is controlled by monkeypatching
the stdlib time.monotonic() with a fake clock - same style as
test_session_store.py's FakeClock (there for datetime, here for a float
monotonic clock) - tests never sleep for real.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from api.rate_limit import RateLimiter

pytestmark = pytest.mark.asyncio


class FakeMonotonic:
    """Controllable clock swapped in for time.monotonic()."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch) -> FakeMonotonic:
    fake = FakeMonotonic()
    monkeypatch.setattr(time, "monotonic", fake)
    return fake


# ---------------------------------------------------------------------------
# 1. Admits up to max_hits within the window, rejects the next
# ---------------------------------------------------------------------------

async def test_allow_admits_up_to_max_hits_then_rejects(clock):
    limiter = RateLimiter({"build_create": (3, 3600)})

    assert await limiter.allow("key-a", "build_create") is True
    assert await limiter.allow("key-a", "build_create") is True
    assert await limiter.allow("key-a", "build_create") is True
    assert await limiter.allow("key-a", "build_create") is False


# ---------------------------------------------------------------------------
# 2. Old hits evicted once the window elapses
# ---------------------------------------------------------------------------

async def test_allow_true_again_after_window_elapses(clock):
    limiter = RateLimiter({"session_create": (2, 60)})

    assert await limiter.allow("key-a", "session_create") is True
    assert await limiter.allow("key-a", "session_create") is True
    assert await limiter.allow("key-a", "session_create") is False

    clock.advance(60.5)  # past the 60s window - both hits should evict

    assert await limiter.allow("key-a", "session_create") is True


# ---------------------------------------------------------------------------
# 3. A rejected call does not count against the window
# ---------------------------------------------------------------------------

async def test_rejected_call_does_not_inflate_the_window(clock):
    limiter = RateLimiter({"intake_turn": (2, 60)})

    assert await limiter.allow("key-a", "intake_turn") is True
    assert await limiter.allow("key-a", "intake_turn") is True
    # Limit reached - this call must be rejected AND must not append.
    assert await limiter.allow("key-a", "intake_turn") is False

    clock.advance(10)  # short of the 60s window - original 2 hits still live

    # If the rejection had counted, this would already be a 4th hit and
    # still rejected; instead we're only checking against the original 2.
    assert await limiter.allow("key-a", "intake_turn") is False

    clock.advance(50.5)  # now past 60s since the original 2 hits (10 + 50.5)

    assert await limiter.allow("key-a", "intake_turn") is True


# ---------------------------------------------------------------------------
# 4. Different categories, same bucket_key, are independent
# ---------------------------------------------------------------------------

async def test_categories_are_independent_for_same_bucket_key(clock):
    limiter = RateLimiter({
        "session_create": (1, 60),
        "intake_turn": (5, 60),
    })

    assert await limiter.allow("key-a", "session_create") is True
    assert await limiter.allow("key-a", "session_create") is False

    # intake_turn's budget for the same key is untouched.
    assert await limiter.allow("key-a", "intake_turn") is True
    assert await limiter.allow("key-a", "intake_turn") is True


# ---------------------------------------------------------------------------
# 5. Different bucket_keys, same category, are independent
# ---------------------------------------------------------------------------

async def test_bucket_keys_are_independent_for_same_category(clock):
    limiter = RateLimiter({"build_create": (1, 3600)})

    assert await limiter.allow("key-a", "build_create") is True
    assert await limiter.allow("key-a", "build_create") is False

    # A different API key gets its own bucket, unaffected by key-a.
    assert await limiter.allow("key-b", "build_create") is True


# ---------------------------------------------------------------------------
# 6. retry_after: 0 on an empty bucket, decreasing-but-positive as the
#    window's edge approaches
# ---------------------------------------------------------------------------

async def test_retry_after_zero_when_bucket_empty(clock):
    limiter = RateLimiter({"build_create": (3, 3600)})

    assert await limiter.retry_after("key-a", "build_create") == 0


async def test_retry_after_counts_down_toward_zero(clock):
    limiter = RateLimiter({"build_create": (1, 100)})

    assert await limiter.allow("key-a", "build_create") is True

    first = await limiter.retry_after("key-a", "build_create")
    assert first == 100  # ceil(100 - 0)

    clock.advance(40)
    second = await limiter.retry_after("key-a", "build_create")
    assert second == 60

    clock.advance(59)
    third = await limiter.retry_after("key-a", "build_create")
    assert third == 1  # minimum 1, never 0 while still technically in-window

    assert first > second > third > 0

    clock.advance(1.5)  # now past the window entirely
    assert await limiter.retry_after("key-a", "build_create") == 0


# ---------------------------------------------------------------------------
# 7. Concurrent access is genuinely serialized - exactly max_hits admitted
# ---------------------------------------------------------------------------

async def test_concurrent_allow_admits_exactly_max_hits(clock):
    limiter = RateLimiter({"build_create": (10, 3600)})
    max_hits = 10
    total_calls = max_hits + 5

    results = await asyncio.gather(
        *(limiter.allow("key-a", "build_create") for _ in range(total_calls))
    )

    assert sum(1 for r in results if r is True) == max_hits
    assert sum(1 for r in results if r is False) == total_calls - max_hits
