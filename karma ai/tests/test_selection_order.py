"""Regression coverage for the CPU→RAM DDR cross-slot stranding bug.

Bug: SELECTION_ORDER used to lock RAM before Motherboard (GPU → CPU → RAM →
Storage → Motherboard → ...). On a build anchored to an AM5 CPU (e.g. cpu-009,
Ryzen 5 7600X) where the cheapest/first-shortlisted in-stock RAM kit is a DDR4
kit (e.g. ram-002), RAM would lock to DDR4 before any board was chosen. Every
in-stock AM5 motherboard in the catalog is DDR5-only, so the Motherboard slot's
compatibility funnel would then find zero candidates compatible with the
already-locked DDR4 RAM and dead-end as "no_compatible" — an 8/9-slot build
with the board slot stranded, even though a DDR5-kit + AM5-board combination
was fully buildable in-budget.

Fix: SELECTION_ORDER now locks Motherboard immediately after GPU + CPU (before
RAM), so RAM's compatibility filter (Step 2 of the funnel) resolves against an
already-locked board's DDR generation instead of the board having to adapt to
whatever RAM generation was picked first.

This test is hermetic — Postgres, Neo4j, resolve_requirements, and the LLM are
all mocked/stubbed — using the EXACT real catalog rows the bug was found with
(cpu-009 AM5, ram-002 DDR4-3600 16GB vs ram-006 DDR5-5200 16GB, mb-008 AM5/DDR5
board) on a comfortable ~₹85k-100k gaming brief, so it reproduces the documented
scenario rather than a synthetic stand-in.

Non-vacuous (verified manually, not committed): reverting SELECTION_ORDER to
the old GPU→CPU→RAM→Storage→Motherboard→... order reproduces the exact
documented dead-end — 8/9 slots filled, Motherboard stranded with a
"no_compatible" warning, because RAM locks to the DDR4 kit before any board is
chosen.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

from agents.feasibility.resolver import CpuTier, GpuTier, ResolvedRequirements
from agents.nodes import node3_selector as ns
from agents.schemas.feasibility import FeasibilityVerdict
from agents.schemas.price_bands import PriceBand, PriceBands
from agents.schemas.slots import ComponentSlot

# ── Real catalog rows (data/catalog/seed.sql) the bug was found with ─────────

_GPU = {
    "product_id": "gpu-001", "name": "RTX 4060 Ventus 2X 8G OC", "brand": "MSI",
    "price_inr": 27500,
    "specs": {"vram_gb": 8, "tdp_watts": 115, "length_mm": 200, "slot_width": 2.0, "pcie_gen": 4},
}
_CPU = {
    "product_id": "cpu-009", "name": "Ryzen 5 7600X", "brand": "AMD",
    "price_inr": 19500,
    "specs": {"socket": "AM5", "tdp_watts": 105, "cores": 6, "threads": 12,
              "base_ghz": 4.7, "boost_ghz": 5.3, "has_igpu": True},
}
_MB = {
    "product_id": "mb-008", "name": "B650M DS3H", "brand": "Gigabyte",
    "price_inr": 13500,
    "specs": {"socket": "AM5", "chipset": "B650", "form_factor": "mATX",
              "max_ram_gb": 192, "ram_slots": 4, "pcie_slots": 1, "ddr_type": 5},
}
_RAM_DDR4 = {
    "product_id": "ram-002", "name": "FURY Beast DDR4-3600 16GB Kit", "brand": "Kingston",
    "price_inr": 4200,
    "specs": {"capacity_gb": 16, "speed_mhz": 3600, "ddr_gen": 4, "kit_count": 2},
}
_RAM_DDR5 = {
    "product_id": "ram-006", "name": "FURY Beast DDR5-5200 16GB Kit", "brand": "Kingston",
    "price_inr": 6000,
    "specs": {"capacity_gb": 16, "speed_mhz": 5200, "ddr_gen": 5, "kit_count": 2},
}
_STORAGE = {
    "product_id": "storage-001", "name": "NV2 1TB M.2 NVMe", "brand": "Kingston",
    "price_inr": 4500,
    "specs": {"capacity_gb": 1000, "interface": "M.2 NVMe Gen4", "read_mbps": 3500, "write_mbps": 2100},
}
_PSU = {
    "product_id": "psu-001", "name": "NE 550W 80+ Bronze", "brand": "Antec",
    "price_inr": 4000,
    "specs": {"wattage": 550, "efficiency_rating": "80+ Bronze", "modular": "non"},
}
_CASE = {
    "product_id": "case-001", "name": "CC360 ARGB mATX", "brand": "DeepCool",
    "price_inr": 4500,
    "specs": {"form_factor_support": ["mATX", "ITX"], "max_gpu_length_mm": 320, "max_cooler_height_mm": 165},
}
_COOLER = {
    "product_id": "cooler-001", "name": "AK400 Air CPU Cooler", "brand": "DeepCool",
    "price_inr": 2500,
    "specs": {"type": "air", "tdp_support_watts": 220, "height_mm": 155,
              "socket_compat": ["LGA1700", "LGA1851", "AM4", "AM5"]},
}
_FANS = {
    "product_id": "fans-001", "name": "P12 PWM PST 120mm", "brand": "Arctic",
    "price_inr": 700,
    "specs": {"size_mm": 120, "static_pressure": 2.20, "airflow_cfm": 48.8},
}

_CANDIDATES: dict[ComponentSlot, list[dict]] = {
    ComponentSlot.gpu: [_GPU],
    ComponentSlot.cpu: [_CPU],
    ComponentSlot.motherboard: [_MB],
    # DDR4 kit listed first -- it's the cheaper, catalog-price-ordered candidate
    # a real fetch would shortlist first, same as the documented bug.
    ComponentSlot.ram: [_RAM_DDR4, _RAM_DDR5],
    ComponentSlot.storage: [_STORAGE],
    ComponentSlot.psu: [_PSU],
    ComponentSlot.case: [_CASE],
    ComponentSlot.cooler: [_COOLER],
    ComponentSlot.fans: [_FANS],
}

_PRODUCTS_BY_ID = {p["product_id"]: p for group in _CANDIDATES.values() for p in group}

_FIXED_REQ = ResolvedRequirements(
    gpu_tier=GpuTier.mid, cpu_tier=CpuTier.mid,
    vram_gb=6, ram_gb=16, storage_gb=80,
)


def _pair_compatible(slot_a: ComponentSlot, id_a: str, slot_b: ComponentSlot, id_b: str) -> bool:
    """Mirror of the three hard compatibility families Neo4j enforces
    (catalog_floor.min_viable_build is the reference implementation)."""
    a, b = _PRODUCTS_BY_ID[id_a]["specs"], _PRODUCTS_BY_ID[id_b]["specs"]
    pair = frozenset((slot_a, slot_b))
    if pair == frozenset((ComponentSlot.cpu, ComponentSlot.motherboard)):
        return a.get("socket") == b.get("socket")
    if pair == frozenset((ComponentSlot.ram, ComponentSlot.motherboard)):
        ram, mb = (a, b) if slot_a == ComponentSlot.ram else (b, a)
        return ram.get("ddr_gen") == mb.get("ddr_type")
    if pair == frozenset((ComponentSlot.case, ComponentSlot.motherboard)):
        case, mb = (a, b) if slot_a == ComponentSlot.case else (b, a)
        return mb.get("form_factor") in case.get("form_factor_support", [])
    if pair == frozenset((ComponentSlot.cooler, ComponentSlot.cpu)):
        cooler, cpu = (a, b) if slot_a == ComponentSlot.cooler else (b, a)
        return cpu.get("socket") in cooler.get("socket_compat", [])
    return True  # no compatibility rule between these two slots


class _FakePostgres:
    def get_parts_in_band(self, slot, low, high, in_stock=True):
        return [dict(p) for p in _CANDIDATES.get(slot, [])]


class _FakeNeo4j:
    def ping(self):
        return True

    def compatibility_check(self, candidate_ids, locked_parts, slot):
        return [
            cid for cid in candidate_ids
            if all(
                _pair_compatible(slot, cid, lslot, lpid)
                for lslot, lpid in locked_parts.items()
            )
        ]

    def fitness_filter(self, ids, primary_use_case, threshold):
        # No real fitness signal wired for this test -- fail-open, unranked.
        return SimpleNamespace(ordered_ids=[], is_real_ranking=False)


def _fake_call_structured(prompt, response_model, *args, **kwargs):
    """Deterministically picks the FIRST candidate the funnel shortlisted --
    the same 'value-optimize to the top of the list' behaviour a real LLM pick
    exhibits when the first entry is also the cheapest, as ram-002 is here."""
    assert response_model is ns.SelectedPart, f"unexpected structured call for {response_model}"
    match = re.search(r"product_id=(\S+)\s*\|", prompt)
    assert match, f"could not find a product_id in shortlist prompt:\n{prompt}"
    return ns.SelectedPart(product_id=match.group(1), justification="test-pick-first")


def _install_fakes(monkeypatch):
    monkeypatch.setattr(ns, "PostgresClient", lambda: _FakePostgres())
    monkeypatch.setattr(ns, "Neo4jClient", lambda: _FakeNeo4j())
    monkeypatch.setattr(ns, "resolve_requirements", lambda brief: _FIXED_REQ)
    monkeypatch.setattr(ns, "derive_fitness_thresholds", lambda brief: {s: 0.5 for s in ComponentSlot})
    monkeypatch.setattr(ns, "call_structured", _fake_call_structured)


def test_motherboard_before_ram_prevents_ddr_stranding(monkeypatch, budget_gamer_brief):
    """cpu-009 (AM5) + the DDR4/DDR5 ram-002/ram-006 pair must build 9/9, with
    RAM resolved to the DDR5 kit that matches the (now already-locked) AM5/DDR5
    board -- not stranded on the cheaper DDR4 kit picked before any board exists.
    """
    _install_fakes(monkeypatch)

    brief = budget_gamer_brief.model_copy(deep=True)
    brief.budget.comfortable_min = 85000
    brief.budget.comfortable_max = 95000
    brief.budget.ceiling = 100000

    verdict = FeasibilityVerdict(verdict="comfortable", basis="deterministic", reason="test")
    bands = PriceBands(root={s: PriceBand(low=0, mid=10000, high=100000) for s in ComponentSlot})

    build = ns.select_build(brief, bands, feasibility_verdict=verdict)

    by_slot = {p.slot: p for p in build.parts}
    assert len(build.parts) == 9, (
        f"expected all 9 slots filled, got {len(build.parts)}/9 -- warnings: {build.warnings}"
    )
    assert ComponentSlot.motherboard in by_slot, (
        "Motherboard slot was stranded -- the exact documented CPU->RAM DDR cross-slot bug"
    )
    assert by_slot[ComponentSlot.motherboard].product_id == "mb-008"
    assert by_slot[ComponentSlot.cpu].product_id == "cpu-009"
    assert by_slot[ComponentSlot.ram].product_id == "ram-006", (
        "RAM must resolve to the DDR5 kit compatible with the locked AM5/DDR5 "
        f"board, not the cheaper DDR4 ram-002; got {by_slot[ComponentSlot.ram].product_id}"
    )
    assert not build.warnings, f"expected a clean build, got warnings: {build.warnings}"
