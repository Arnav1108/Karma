"""Shared software minimum-spec lookup — Postgres-cached, LLM-backed.

Single source of truth for "what hardware floor does this software need",
replacing two independent stub tables that used to drift apart in spirit:
resolver.py's per-title `_BASE_FLOOR_STUB` and node2_allocation.py's raw-string
`_SOFTWARE_SPECS`. Both callers now get the same `BaseFloor` for the same title.

Lookup order:
    1. Postgres cache (`software_specs_cache` table, keyed by lowercased name).
    2. LLM estimate (temperature=0 — determinism reasoning mirrors the Node 3
       fitness-threshold call; the model estimates from its own knowledge, no
       web-search tool exists in this stack). Cached on success.
    3. `resolver._CATEGORY_FALLBACK_STUB[category]` on any failure (cache
       unreachable, LLM error, malformed response) — never cached, so a
       transient failure is retried on the next call rather than pinned.

Public surface:
    get_software_requirements(name, category) -> BaseFloor
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel

from .db.postgres import PostgresClient
from .feasibility.resolver import BaseFloor, CpuTier, GpuTier, _CATEGORY_FALLBACK_STUB
from .llm.client import call_structured

logger = logging.getLogger(__name__)


class _SoftwareSpecLLMResponse(BaseModel):
    """LLM output shape — tier NAMES (not raw ints), so the model reasons over
    a vocabulary it understands rather than guessing GpuTier/CpuTier ordinals."""
    gpu_tier: Literal["igpu", "entry", "mid", "high", "enthusiast"]
    cpu_tier: Literal["entry", "mid", "high", "hedt"]
    vram_gb: int
    ram_gb: int
    storage_gb: int


def _query_llm(name: str, category: str) -> BaseFloor:
    prompt = (
        f'Estimate the MINIMUM hardware requirements to run "{name}" '
        f"(category: {category}) at a playable/usable baseline, from your own "
        "knowledge of this software's published system requirements.\n\n"
        "gpu_tier: one of igpu, entry, mid, high, enthusiast\n"
        "cpu_tier: one of entry, mid, high, hedt\n"
        "vram_gb, ram_gb, storage_gb: integers in GB\n"
    )
    result = call_structured(prompt, _SoftwareSpecLLMResponse, temperature=0)
    return BaseFloor(
        gpu_tier=GpuTier[result.gpu_tier],
        cpu_tier=CpuTier[result.cpu_tier],
        vram_gb=result.vram_gb,
        ram_gb=result.ram_gb,
        storage_gb=result.storage_gb,
    )


def get_software_requirements(name: str, category: str) -> BaseFloor:
    """Minimum hardware floor for `name` (a software/game title), Postgres-cached.

    Never raises — any failure along the way (cache unreachable, LLM error,
    malformed response) falls back to the category stub.
    """
    key = name.strip().lower()

    try:
        cached = PostgresClient().get_software_spec_cache(key)
        if cached is not None:
            return BaseFloor(
                gpu_tier=GpuTier(cached["gpu_tier"]),
                cpu_tier=CpuTier(cached["cpu_tier"]),
                vram_gb=cached["vram_gb"],
                ram_gb=cached["ram_gb"],
                storage_gb=cached["storage_gb"],
            )
    except Exception:
        logger.warning("[software_specs] cache read failed for %r", name, exc_info=True)

    try:
        floor = _query_llm(name, category)
    except Exception:
        logger.warning(
            "[software_specs] LLM lookup failed for %r; falling back to category stub %r",
            name, category, exc_info=True,
        )
        return _CATEGORY_FALLBACK_STUB[category]

    try:
        PostgresClient().set_software_spec_cache(
            key, category,
            gpu_tier=int(floor.gpu_tier), cpu_tier=int(floor.cpu_tier),
            vram_gb=floor.vram_gb, ram_gb=floor.ram_gb, storage_gb=floor.storage_gb,
            source="llm",
        )
    except Exception:
        logger.warning("[software_specs] cache write failed for %r", name, exc_info=True)

    return floor
