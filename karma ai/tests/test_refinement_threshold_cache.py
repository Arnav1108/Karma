"""ThresholdCache HIT path + locked_parts persistence through the REAL dispatch.

test_graph_node_select.py already proves the cache-hit round-trip for the graph
node (node_select). It does NOT touch the *refinement* re-entry: the second place
a ThresholdCache is read is node3_refinement._select_build_with_pins (the
`cache.thresholds is not None and cache.key == current_key` branch at line ~400,
logging "reusing cached fitness thresholds"). Per the prior audit that branch is
only ever hit once, when refinement re-enters selection — and nothing asserted
it. These tests drive the REAL dispatch_refinement / _select_build_with_pins
across two rounds and pin down:

  1. round 2 with an unchanged brief is a genuine cache HIT (derive not re-called),
  2. a changed threshold field (target_resolution) is a genuine MISS (re-derived)
     — the negative control that proves the HIT above isn't vacuous,
  3. the loop-owned locked_parts dict accumulates pins across BOTH rounds while
     riding the same cache.

Isolation mirrors test_graph_node_select.py: derive_fitness_thresholds is
wrapped with a call counter, select_part returns a canned SlotOutcome, and
Neo4jClient.ping is forced False — so no live OpenAI/Postgres/Neo4j call happens
and this runs in the default `pytest tests/` suite. estimate_feasibility is never
reached because reject/pin ops set `changed=True` and go straight to the re-solve
branch (it's only called for additive brief edits).
"""
from __future__ import annotations

import agents.db.neo4j as neo4j_mod
import agents.nodes.node3_refinement as refine
import agents.nodes.node3_selector as selector
from agents.nodes.node3_refinement import (
    RefinementOps,
    _select_build_with_pins,
    dispatch_refinement,
)
from agents.nodes.node3_selector import SlotOutcome, ThresholdCache
from agents.schemas.build_card import BuildCard, BuildCardPart
from agents.schemas.price_bands import PriceBand, PriceBands
from agents.schemas.slots import ComponentSlot

_BANDS_INR: dict[str, tuple[int, int, int]] = {
    "gpu": (18000, 22000, 27000),
    "cpu": (10000, 13000, 16000),
    "ram": (3500, 4500, 6000),
    "storage": (3000, 4000, 5500),
    "motherboard": (5500, 7000, 9000),
    "psu": (3500, 4500, 6000),
    "case": (3000, 4000, 5500),
    "cooler": (1500, 2500, 3500),
    "fans": (800, 1200, 1800),
}


def _bands() -> PriceBands:
    return PriceBands(
        root={ComponentSlot(s): PriceBand(low=lo, mid=mid, high=hi)
              for s, (lo, mid, hi) in _BANDS_INR.items()}
    )


def _part(slot: ComponentSlot, pid: str, price: int) -> BuildCardPart:
    return BuildCardPart(slot=slot, product_id=pid, name=f"{slot.value} {pid}",
                         price_inr=price, justification="test")


def _fake_select_part(slot, band, brief, locked_parts, fitness_thresholds,
                      neo4j_available, req, remaining_budget=None,
                      ddr4_bias=False, min_psu_wattage=None):
    """Canned in-band pick so _select_build_with_pins never needs a live catalog."""
    return SlotOutcome(
        part=_part(slot, f"FAKE-{slot.value}", band.mid),
        status="ok",
    )


def _counting_derive(monkeypatch):
    """Wrap the real derive with a counter (never actually calls OpenAI — we
    return a fixed dict), returning the {"n": int} counter."""
    count = {"n": 0}

    def derive(brief):
        count["n"] += 1
        return {ComponentSlot.gpu: 0.5, ComponentSlot.cpu: 0.5}

    monkeypatch.setattr(selector, "derive_fitness_thresholds", derive)
    monkeypatch.setattr(refine, "select_part", _fake_select_part)
    monkeypatch.setattr(neo4j_mod.Neo4jClient, "ping", lambda self: False)
    return count


# ── 1 + 2: cache HIT on unchanged brief, MISS on a changed threshold field ─────

def test_resolve_hits_cache_on_unchanged_brief_and_misses_on_change(monkeypatch, budget_gamer_brief):
    count = _counting_derive(monkeypatch)
    brief = budget_gamer_brief.model_copy(deep=True)
    bands = _bands()
    cache = ThresholdCache()  # cold

    # Call 1 — cold cache, must derive.
    _select_build_with_pins(brief, bands, pinned_parts={}, cache=cache)
    assert count["n"] == 1, "cold cache should derive exactly once"

    # Call 2 — same brief, same cache: the refinement cache-HIT branch must fire.
    _select_build_with_pins(brief, bands, pinned_parts={}, cache=cache)
    assert count["n"] == 1, (
        "CACHE MISS on the refinement re-entry — _select_build_with_pins "
        "re-derived thresholds for an unchanged brief"
    )

    # Call 3 — negative control: change a field _threshold_key reads
    # (performance.target_resolution) so the cache key no longer matches; the
    # same cache object must now re-derive. Proves calls 1/2 exercised the real
    # cache mechanism, not an unrelated skip.
    changed = refine.patch_brief_field(
        brief, "performance",
        {"target_resolution": "4K", "target_framerate": 144, "source": "user_stated"},
    )
    _select_build_with_pins(changed, bands, pinned_parts={}, cache=cache)
    assert count["n"] == 2, (
        f"changed target_resolution should re-derive (n=2), got n={count['n']}"
    )


# ── 3: locked_parts persists across two real dispatch rounds on one cache ──────

def test_locked_parts_persist_and_cache_reused_across_two_dispatch_rounds(monkeypatch, budget_gamer_brief):
    count = _counting_derive(monkeypatch)
    brief = budget_gamer_brief.model_copy(deep=True)
    bands = _bands()
    cache = ThresholdCache()
    locked: dict[str, str] = {}

    start_card = BuildCard(
        parts=[_part(ComponentSlot.gpu, "GPU-OLD", 22000),
               _part(ComponentSlot.cpu, "CPU-OLD", 13000)],
        total_price_inr=35000, summary="start",
    )

    # Round 1 — pin GPU. Sets changed=True → re-solve → cold derive (MISS).
    r1 = dispatch_refinement(RefinementOps(pin=ComponentSlot.gpu),
                             brief, bands, start_card, locked, cache)
    assert locked == {"gpu": "GPU-OLD"}, "round 1 pin not recorded"
    assert count["n"] == 1, "round 1 should derive once (cold cache)"
    # The pinned GPU survives the re-solve.
    r1_gpu = next(p for p in r1.build_card.parts if p.slot == ComponentSlot.gpu)
    assert r1_gpu.product_id == "GPU-OLD"

    # Round 2 — pin CPU, carrying the SAME locked dict and SAME cache. Brief's
    # threshold fields are untouched by a pin, so this must be a cache HIT.
    r2 = dispatch_refinement(RefinementOps(pin=ComponentSlot.cpu),
                             r1.brief, r1.price_bands, r1.build_card, locked, cache)
    assert locked == {"gpu": "GPU-OLD", "cpu": "CPU-OLD"}, (
        f"round 2 pin did not accumulate onto round 1's locked_parts: {locked}"
    )
    assert count["n"] == 1, (
        f"round 2 re-solve should hit the cache (n stays 1), got n={count['n']}"
    )
    # Both pinned parts survive the round-2 re-solve.
    r2_ids = {p.slot: p.product_id for p in r2.build_card.parts}
    assert r2_ids[ComponentSlot.gpu] == "GPU-OLD"
    assert r2_ids[ComponentSlot.cpu] == "CPU-OLD"
