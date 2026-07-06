"""Regression tests for PSU wattage enforcement at part-selection time.

Bug: catalog_floor's feasibility floor assumes a PSU with
cpu_tdp + gpu_tdp + PSU_HEADROOM_W of headroom exists inside budget
(min_viable_build), but Node 3's PSU slot selection never checked the picked
PSU's wattage against the locked GPU+CPU. A build could pass feasibility on that
wattage assumption and then ship an underpowered PSU.

Fix: required_psu_wattage(locked_specs) is applied as a HARD candidate filter in
the PSU slot's funnel (via _fetch_floor), mirroring the requirement-floor filter.

These tests are hermetic — Postgres, Neo4j, and the LLM are all mocked — so they
run without live services. The first is the non-vacuous regression: reverting the
_fetch_floor wattage filter makes it fail (the underpowered PSU survives and the
mocked "value-optimizing" LLM pick ships it).
"""

from __future__ import annotations

from agents.feasibility.catalog_floor import required_psu_wattage
from agents.feasibility.resolver import resolve_requirements
from agents.nodes import node3_selector as ns
from agents.schemas.build_card import BuildCardPart
from agents.schemas.price_bands import PriceBand, PriceBands
from agents.schemas.slots import ComponentSlot

# GPU 450 W + CPU 170 W + 150 W headroom → a compliant PSU must be ≥ 770 W.
_GPU_TDP = 450
_CPU_TDP = 170
_EXPECTED_MIN_W = _GPU_TDP + _CPU_TDP + 150  # 770

_PSU_LOW = {
    "product_id": "PSU_LOW", "name": "550W Bronze", "brand": "Generic",
    "category": "psu", "in_stock": True, "price_inr": 3000,
    "specs": {"wattage": 550, "efficiency_rating": "80+ Bronze", "modular": "non"},
}
_PSU_OK = {
    "product_id": "PSU_OK", "name": "850W Gold", "brand": "Corsair",
    "category": "psu", "in_stock": True, "price_inr": 6000,
    "specs": {"wattage": 850, "efficiency_rating": "80+ Gold", "modular": "full"},
}


def test_required_psu_wattage_sums_tdp_plus_headroom():
    locked = {
        ComponentSlot.gpu: {"tdp_watts": _GPU_TDP},
        ComponentSlot.cpu: {"tdp_watts": _CPU_TDP},
    }
    assert required_psu_wattage(locked) == _EXPECTED_MIN_W
    # No locked parts yet → just the headroom margin.
    assert required_psu_wattage({}) == 150


def test_psu_wattage_floor_excludes_underpowered(monkeypatch, budget_gamer_brief):
    """A 550 W PSU behind a 620 W GPU+CPU load must be excluded; the 850 W wins.

    Non-vacuous: the mocked LLM deliberately "value-optimizes" to the cheapest PSU
    (PSU_LOW). With the wattage floor, PSU_LOW never reaches the shortlist, so the
    pick falls through to PSU_OK. Revert the _fetch_floor wattage filter and
    PSU_LOW survives → the LLM ships it → this assertion fails.
    """
    class FakePG:
        def get_parts_in_band(self, slot, low, high, in_stock=True):
            if slot == ComponentSlot.psu:
                # Underpowered-but-cheapest first, mimicking catalog price order.
                return [dict(_PSU_LOW), dict(_PSU_OK)]
            return []

    monkeypatch.setattr(ns, "PostgresClient", lambda: FakePG())
    monkeypatch.setattr(
        ns, "call_structured",
        lambda prompt, model, *a, **k: ns.SelectedPart(
            product_id="PSU_LOW", justification="cheapest available"
        ),
    )

    locked_specs = {
        ComponentSlot.gpu: {"tdp_watts": _GPU_TDP},
        ComponentSlot.cpu: {"tdp_watts": _CPU_TDP},
    }
    min_w = required_psu_wattage(locked_specs)
    assert min_w == _EXPECTED_MIN_W

    req = resolve_requirements(budget_gamer_brief)
    outcome = ns.select_part(
        slot=ComponentSlot.psu,
        band=PriceBand(low=0, mid=6000, high=100000),
        brief=budget_gamer_brief,
        locked_parts={ComponentSlot.gpu: "GPU_X", ComponentSlot.cpu: "CPU_Y"},
        fitness_thresholds={s: 0.5 for s in ComponentSlot},
        neo4j_available=False,
        req=req,
        remaining_budget=100000,
        min_psu_wattage=min_w,
    )

    assert outcome.part is not None, "expected an adequate PSU, got a dead-end"
    assert outcome.part.product_id == "PSU_OK", (
        f"PSU wattage floor (≥{min_w} W) must exclude the 550 W unit and pick the "
        f"850 W unit; got {outcome.part.product_id}"
    )


def test_only_underpowered_in_stock_is_a_dead_end(monkeypatch, budget_gamer_brief):
    """If every in-stock PSU is underpowered, the slot dead-ends (no silent pick)."""
    class FakePGLowOnly:
        def get_parts_in_band(self, slot, low, high, in_stock=True):
            if slot == ComponentSlot.psu:
                return [dict(_PSU_LOW)]
            return []

    monkeypatch.setattr(ns, "PostgresClient", lambda: FakePGLowOnly())
    monkeypatch.setattr(
        ns, "call_structured",
        lambda prompt, model, *a, **k: ns.SelectedPart(product_id="PSU_LOW", justification="x"),
    )

    req = resolve_requirements(budget_gamer_brief)
    outcome = ns.select_part(
        slot=ComponentSlot.psu,
        band=PriceBand(low=0, mid=3000, high=100000),
        brief=budget_gamer_brief,
        locked_parts={ComponentSlot.gpu: "GPU_X", ComponentSlot.cpu: "CPU_Y"},
        fitness_thresholds={s: 0.5 for s in ComponentSlot},
        neo4j_available=False,
        req=req,
        remaining_budget=100000,
        min_psu_wattage=_EXPECTED_MIN_W,
    )

    assert outcome.part is None
    assert outcome.status == "no_floor"
    assert f"≥{_EXPECTED_MIN_W} W" in (outcome.message or "")


def test_select_build_passes_psu_wattage_from_locked_tdp(monkeypatch, budget_gamer_brief):
    """select_build must compute the PSU floor from the ALREADY-locked GPU+CPU TDP.

    Proves the wiring end-to-end: select_part is stubbed to record the
    min_psu_wattage it receives for the PSU slot and to hand back per-slot specs
    so select_build can accumulate locked TDP. GPU 450 + CPU 170 + 150 = 770.
    """
    class FakeNeo4j:
        def ping(self):
            return False

    monkeypatch.setattr(ns, "Neo4jClient", lambda: FakeNeo4j())
    monkeypatch.setattr(
        ns, "derive_fitness_thresholds", lambda brief: {s: 0.5 for s in ComponentSlot}
    )

    tdp_by_slot = {ComponentSlot.gpu: _GPU_TDP, ComponentSlot.cpu: _CPU_TDP}
    captured: dict[str, int | None] = {}

    def fake_select_part(**kwargs):
        slot = kwargs["slot"]
        if slot == ComponentSlot.psu:
            captured["min_psu_wattage"] = kwargs.get("min_psu_wattage")
        specs = {"tdp_watts": tdp_by_slot[slot]} if slot in tdp_by_slot else {}
        return ns.SlotOutcome(
            part=BuildCardPart(
                slot=slot, product_id=f"{slot.value}_X", name=slot.value,
                price_inr=1000, justification="x",
            ),
            status="ok",
            specs=specs,
        )

    monkeypatch.setattr(ns, "select_part", fake_select_part)

    bands = PriceBands({s: PriceBand(low=0, mid=1000, high=100000) for s in ComponentSlot})
    ns.select_build(budget_gamer_brief, bands)

    assert captured.get("min_psu_wattage") == _EXPECTED_MIN_W, (
        "select_build must pass locked GPU+CPU TDP + headroom (450+170+150=770) "
        f"as the PSU wattage floor; got {captured.get('min_psu_wattage')}"
    )
