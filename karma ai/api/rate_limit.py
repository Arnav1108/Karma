"""In-process sliding-window-log rate limiter.

Per docs/hardening_plan.md §2: a deque of hit timestamps per (bucket_key,
category), evicting timestamps older than that category's window on each
check. Exact (no fixed-window edge burst), single-process only (state is
an in-memory dict - see hardening_plan.md §7 item 7).

Uses time.monotonic(), never datetime/wall-clock, so the window is immune
to wall-clock adjustment (NTP step, DST, manual clock changes).

This module intentionally has no FastAPI route/dependency wiring - it is
a standalone limiter. Wiring (a dependency factory bound per-route) is a
separate, later step.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque


class RateLimitError(Exception):
    """Raised by callers when a rate limit check rejects a request.

    Deliberately NOT an IntakeServiceError/BuildServiceError subclass -
    rate limiting is a cross-cutting concern scoped to neither service
    family (see docs/hardening_plan.md §2).
    """

    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__(f"Rate limit exceeded, retry after {retry_after}s")


class RateLimiter:
    """Sliding-window-log limiter, independently configured per category.

    One bucket per (bucket_key, category) pair, e.g. (api_key, "build_create").
    All buckets share a single asyncio.Lock - the tiny per-check cost (a
    deque scan of a handful of timestamps) makes finer-grained locking
    unnecessary.
    """

    def __init__(self, limits: dict[str, tuple[int, float]]) -> None:
        """limits: category -> (max_hits, window_seconds).

        Configure the three Phase 5 tiers independently, e.g.:
            RateLimiter({
                "session_create": (5, 60),
                "intake_turn": (20, 60),
                "build_create": (3, 3600),
            })
        """
        self._limits = dict(limits)
        self._hits: dict[tuple[str, str], deque[float]] = {}
        self._lock = asyncio.Lock()

    async def allow(self, bucket_key: str, category: str) -> bool:
        """Admit or reject one hit for (bucket_key, category).

        Evicts timestamps older than (now - window_seconds), then admits
        (appends now, returns True) iff the remaining count is under
        max_hits. A rejected call does NOT append - it must not count
        against the window it was rejected from.
        """
        max_hits, window_seconds = self._limits[category]
        key = (bucket_key, category)
        async with self._lock:
            now = time.monotonic()
            hits = self._hits.setdefault(key, deque())
            self._evict(hits, now, window_seconds)
            if len(hits) < max_hits:
                hits.append(now)
                return True
            return False

    async def retry_after(self, bucket_key: str, category: str) -> int:
        """Seconds until the oldest in-window hit for (bucket_key, category)
        ages out, rounded up, minimum 1. Returns 0 if there is currently
        nothing in the window to wait for (bucket never hit, or every prior
        hit has already aged out) - i.e. the caller could be admitted now.
        """
        _, window_seconds = self._limits[category]
        key = (bucket_key, category)
        async with self._lock:
            now = time.monotonic()
            hits = self._hits.get(key)
            if not hits:
                return 0
            self._evict(hits, now, window_seconds)
            if not hits:
                return 0
            remaining = window_seconds - (now - hits[0])
            return max(1, math.ceil(remaining))

    @staticmethod
    def _evict(hits: deque[float], now: float, window_seconds: float) -> None:
        cutoff = now - window_seconds
        while hits and hits[0] < cutoff:
            hits.popleft()
