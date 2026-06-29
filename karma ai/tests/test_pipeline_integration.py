"""Phase 1 integration tests.

Exercises estimate_feasibility and allocate_budget against all three fixtures
and reports pass/fail per checkpoint.

Allocation-sum assertions are deterministic: _compute_bands() guarantees
  sum(mids) == target  and  sum(highs) == ceiling
regardless of the LLM's proportional weights, so these tests always pass when
the API is reachable.

Feasibility tests depend on a live Postgres price anchor; they skip cleanly
when POSTGRES_URL is unreachable (controlled by the db_available fixture in
conftest.py).
"""

from __future__ import annotations

import pytest

from agents.feasibility.estimate import estimate_feasibility
from agents.nodes.node2_allocation import allocate_budget
from agents.schemas.slots import ComponentSlot


# ── Budget Gamer ──────────────────────────────────────────────────────────────
# Fixture: ₹60k–₹65k comfortable, ₹70k ceiling; pc_only scope; OEM Windows (₹1500)
# No reused parts → all 9 slots allocated.
# Deterministic pool: target = 65000 - 1500 = 63500; ceiling = 70000 - 1500 = 68500

class TestBudgetGamer:
    """Competitive FPS gaming build — ₹60k–₹70k, no reuse."""

    @pytest.fixture(scope="class")
    def bands(self, budget_gamer_brief):
        return allocate_budget(budget_gamer_brief)

    def test_feasibility_verdict(self, budget_gamer_brief, db_available):
        if not db_available:
            pytest.skip("Postgres unavailable — skipping DB-dependent feasibility test")
        verdict = estimate_feasibility(budget_gamer_brief)
        assert verdict.verdict in ("comfortable", "tight"), (
            f"Expected comfortable or tight, got {verdict.verdict!r}: {verdict.reason}"
        )

    def test_allocation_sums(self, bands):
        assert abs(bands.total_mid() - 63500) < 1000, (
            f"total_mid={bands.total_mid()}, expected ≈63500"
        )
        assert abs(bands.total_high() - 68500) < 1000, (
            f"total_high={bands.total_high()}, expected ≈68500"
        )
        assert bands.total_low() <= bands.total_mid() <= bands.total_high()

    def test_allocation_slots(self, bands):
        for slot in ComponentSlot:
            assert slot in bands.root, f"Missing slot: {slot.value}"

    def test_gpu_dominates(self, bands):
        gpu_mid = bands.root[ComponentSlot.gpu].mid
        assert gpu_mid >= 20000, (
            f"GPU mid={gpu_mid}, expected ≥20000 for gaming profile"
        )


# ── ML Workstation ────────────────────────────────────────────────────────────
# Fixture: ₹180k–₹220k comfortable, ₹240k ceiling; pc_only; Linux (₹0 OS cost)
# Samsung 990 Pro reused → storage slot excluded.
# Deterministic pool: target = 220000 (no fixed deductions)

class TestMlWorkstation:
    """ML training workstation — ₹180k–₹240k, Samsung 990 Pro reused."""

    @pytest.fixture(scope="class")
    def bands(self, ml_workstation_brief):
        return allocate_budget(ml_workstation_brief)

    def test_storage_excluded(self, bands):
        assert ComponentSlot.storage not in bands.root, (
            "storage slot should be absent (Samsung 990 Pro is reused)"
        )

    def test_allocation_sums(self, bands):
        assert abs(bands.total_mid() - 220000) < 5000, (
            f"total_mid={bands.total_mid()}, expected ≈220000"
        )
        assert bands.total_low() <= bands.total_mid() <= bands.total_high()

    def test_gpu_dominates(self, bands):
        gpu_mid = bands.root[ComponentSlot.gpu].mid
        assert gpu_mid >= 80000, (
            f"GPU mid={gpu_mid}, expected ≥80000 for ML workstation profile"
        )


# ── Video Editor ──────────────────────────────────────────────────────────────
# Fixture: ₹140k–₹160k comfortable, ₹175k ceiling; pc_plus_monitor scope
# Fixed deductions: OEM Windows ₹1500 + 2560×1440 monitor ₹30000 = ₹31500
# Deterministic pool: target = 160000 - 31500 = 128500
# No reused parts → all 9 slots allocated.

class TestVideoEditor:
    """Video editing workstation — ₹140k–₹175k, monitor included in scope."""

    @pytest.fixture(scope="class")
    def bands(self, video_editor_brief):
        return allocate_budget(video_editor_brief)

    def test_fixed_costs(self, bands):
        # 160000 (comfortable_max) - 1500 (OEM Windows) - 30000 (1440p monitor) = 128500
        assert abs(bands.total_mid() - 128500) < 500, (
            f"total_mid={bands.total_mid()}, expected 128500 "
            "(160000 - 1500 OS - 30000 monitor)"
        )

    def test_all_slots_present(self, bands):
        for slot in ComponentSlot:
            assert slot in bands.root, f"Missing slot: {slot.value}"

    def test_feasibility_not_comfortable(self, video_editor_brief, db_available):
        if not db_available:
            pytest.skip("Postgres unavailable — skipping DB-dependent feasibility test")
        verdict = estimate_feasibility(video_editor_brief)
        assert verdict.verdict != "comfortable", (
            f"Expected tight or impossible for demanding video editor build "
            f"(min 16GB VRAM on ₹128.5k core budget), got {verdict.verdict!r}"
        )
