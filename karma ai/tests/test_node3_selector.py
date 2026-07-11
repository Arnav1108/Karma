"""Regression coverage for node3_selector.py's rejected_parts exclusion.

brief.hard_constraints.rejected_parts was populated correctly by
node3_refinement.py's swap action but never read anywhere in the selection
funnel, so a rejected product_id could be re-selected for the same slot on a
later pass. This test exercises _fetch_floor (the single choke point every
catalog fetch in the funnel routes through) directly against the live
catalog -- same live-Postgres pattern the rest of the suite relies on --
and skips cleanly when Postgres is unreachable.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agents.db.postgres import PostgresClient
from agents.feasibility.resolver import resolve_requirements
from agents.nodes.node3_selector import (
    _FULL_CATALOG_HIGH,
    _brand_ranked_candidates,
    _fetch_floor,
)
from agents.schemas.brief import RejectedPart
from agents.schemas.slots import ComponentSlot


def test_fetch_floor_excludes_rejected_product_id(budget_gamer_brief, db_available):
    if not db_available:
        pytest.skip("Postgres unavailable — skipping DB-dependent rejected_parts test")

    # budget_gamer_brief is a session-scoped fixture shared across the whole
    # test session -- deep-copy before mutating hard_constraints so this test
    # doesn't leak a rejected part into other tests.
    brief = budget_gamer_brief.model_copy(deep=True)
    req = resolve_requirements(brief)
    pg = PostgresClient()

    before = _fetch_floor(pg, ComponentSlot.gpu, 0, _FULL_CATALOG_HIGH, req, brief)
    assert before, "expected at least one in-stock GPU candidate to test against"
    target_id = before[0]["product_id"]

    brief.hard_constraints.rejected_parts.append(
        RejectedPart(
            product_id=target_id,
            reason="test rejection",
            rejected_at=datetime.now(timezone.utc),
        )
    )

    after = _fetch_floor(pg, ComponentSlot.gpu, 0, _FULL_CATALOG_HIGH, req, brief)

    assert not any(c["product_id"] == target_id for c in after), (
        f"rejected product_id {target_id!r} still present in GPU candidates"
    )
    assert len(after) == len(before) - 1, (
        f"expected exactly one fewer GPU candidate ({len(before) - 1}), got {len(after)}"
    )


def test_fetch_floor_rejection_is_slot_scoped(budget_gamer_brief, db_available):
    """Rejecting a GPU product_id must not affect an unrelated slot (RAM).

    RejectedPart carries no slot field, but product_id is the catalog's
    PRIMARY KEY and 1:1 with a single category/slot, so the flat exclusion
    set in _reject_filter is inherently slot-scoped without needing one.
    """
    if not db_available:
        pytest.skip("Postgres unavailable — skipping DB-dependent rejected_parts test")

    brief = budget_gamer_brief.model_copy(deep=True)
    req = resolve_requirements(brief)
    pg = PostgresClient()

    gpu_candidates = _fetch_floor(pg, ComponentSlot.gpu, 0, _FULL_CATALOG_HIGH, req, brief)
    assert gpu_candidates, "expected at least one in-stock GPU candidate to test against"
    target_id = gpu_candidates[0]["product_id"]

    ram_before = _fetch_floor(pg, ComponentSlot.ram, 0, _FULL_CATALOG_HIGH, req, brief)

    brief.hard_constraints.rejected_parts.append(
        RejectedPart(
            product_id=target_id,
            reason="test rejection",
            rejected_at=datetime.now(timezone.utc),
        )
    )

    ram_after = _fetch_floor(pg, ComponentSlot.ram, 0, _FULL_CATALOG_HIGH, req, brief)

    assert not any(c["product_id"] == target_id for c in ram_before), (
        "unexpected: the GPU product_id collided with a RAM candidate before "
        "rejection — product_id is not globally unique per slot as assumed"
    )
    assert len(ram_after) == len(ram_before), (
        "rejecting a GPU product_id changed the RAM candidate count -- "
        "exclusion is not correctly slot-scoped"
    )


# ── _brand_ranked_candidates ────────────────────────────────────────────────
#
# Pure unit tests -- plain dicts + a brief, no Postgres/Neo4j required.

_CPU_CANDIDATES = [
    {"product_id": "C1", "brand": "Intel"},
    {"product_id": "C2", "brand": "AMD"},
    {"product_id": "C3", "brand": "Intel"},
    {"product_id": "C4", "brand": "AMD"},
]


def test_brand_ranked_cpu_preference_reorders_matches_first(budget_gamer_brief):
    brief = budget_gamer_brief.model_copy(deep=True)
    brief.existing.ecosystem_prefs.cpu_brand_pref = "AMD"

    result, was_applied = _brand_ranked_candidates(
        ComponentSlot.cpu, _CPU_CANDIDATES, brief
    )

    assert was_applied is True
    result_ids = [c["product_id"] for c in result]
    assert result_ids == ["C2", "C4", "C1", "C3"], (
        "expected AMD matches (C2, C4) first, Intel (C1, C3) after, with "
        "original relative order preserved within each brand group -- got "
        f"{result_ids}"
    )


def test_brand_ranked_no_preference_set_is_noop(budget_gamer_brief):
    brief = budget_gamer_brief.model_copy(deep=True)
    brief.existing.ecosystem_prefs.cpu_brand_pref = None

    result, was_applied = _brand_ranked_candidates(
        ComponentSlot.cpu, _CPU_CANDIDATES, brief
    )

    assert was_applied is False
    assert result == _CPU_CANDIDATES


def test_brand_ranked_preference_set_but_no_matching_candidate_fails_open(
    budget_gamer_brief,
):
    brief = budget_gamer_brief.model_copy(deep=True)
    brief.existing.ecosystem_prefs.cpu_brand_pref = "AMD"

    all_intel = [
        {"product_id": "C1", "brand": "Intel"},
        {"product_id": "C3", "brand": "Intel"},
    ]

    result, was_applied = _brand_ranked_candidates(ComponentSlot.cpu, all_intel, brief)

    assert was_applied is False
    assert result == all_intel


def test_brand_ranked_gpu_uses_shared_vendor_inference(budget_gamer_brief):
    brief = budget_gamer_brief.model_copy(deep=True)
    brief.existing.ecosystem_prefs.gpu_brand_pref = "AMD"

    gpu_candidates = [
        {"product_id": "G1", "name": "ASUS ROG RTX 4070"},
        {"product_id": "G2", "name": "Sapphire Pulse RX 7800 XT"},
    ]

    result, was_applied = _brand_ranked_candidates(
        ComponentSlot.gpu, gpu_candidates, brief
    )

    assert was_applied is True
    assert result[0]["product_id"] == "G2"


def test_brand_ranked_non_gpu_cpu_slot_is_always_noop(budget_gamer_brief):
    brief = budget_gamer_brief.model_copy(deep=True)
    brief.existing.ecosystem_prefs.cpu_brand_pref = "AMD"
    brief.existing.ecosystem_prefs.gpu_brand_pref = "AMD"

    ram_candidates = [
        {"product_id": "R1", "brand": "Corsair"},
        {"product_id": "R2", "brand": "AMD"},
    ]

    result, was_applied = _brand_ranked_candidates(
        ComponentSlot.ram, ram_candidates, brief
    )

    assert was_applied is False
    assert result == ram_candidates
