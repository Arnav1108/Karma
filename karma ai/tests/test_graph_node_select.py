"""Regression coverage for graph.py::node_select's ThresholdCache round-trip.

node_select used to construct a fresh, empty ThresholdCache() on every call and
never read back state's fitness_thresholds / fitness_thresholds_key, so the
cache-hit check in select_build (_threshold_key(brief) == cache.key) could
never fire. These tests monkeypatch derive_fitness_thresholds with a
call-counting wrapper (no live OpenAI call needed) and stub out select_part /
Neo4jClient.ping (no live Postgres/Neo4j needed) so they isolate exactly the
cache round-trip logic added in graph.py.
"""

from __future__ import annotations

import agents.graph as graph
import agents.nodes.node3_selector as node3_selector
from agents.db.neo4j import Neo4jClient
from agents.schemas.build_card import BuildCardPart
from agents.schemas.price_bands import PriceBand, PriceBands
from agents.schemas.slots import ComponentSlot

_STUB_BANDS_INR: dict[str, dict[str, int]] = {
    "gpu":         {"low": 18000, "mid": 22000, "high": 27000},
    "cpu":         {"low": 10000, "mid": 13000, "high": 16000},
    "ram":         {"low":  3500, "mid":  4500, "high":  6000},
    "storage":     {"low":  3000, "mid":  4000, "high":  5500},
    "motherboard": {"low":  5500, "mid":  7000, "high":  9000},
    "psu":         {"low":  3500, "mid":  4500, "high":  6000},
    "case":        {"low":  3000, "mid":  4000, "high":  5500},
    "cooler":      {"low":  1500, "mid":  2500, "high":  3500},
    "fans":        {"low":    800, "mid":  1200, "high":  1800},
}


def _stub_bands() -> PriceBands:
    return PriceBands(
        root={ComponentSlot(s): PriceBand(**v) for s, v in _STUB_BANDS_INR.items()}
    )


def _fake_select_part(
    slot,
    band,
    brief,
    locked_parts,
    fitness_thresholds,
    neo4j_available,
    req,
    remaining_budget=None,
    ddr4_bias=False,
    min_psu_wattage=None,
):
    """Canned SlotOutcome so select_build never needs a live catalog/LLM call."""
    return node3_selector.SlotOutcome(
        part=BuildCardPart(
            slot=slot,
            product_id=f"FAKE-{slot.value}",
            name=f"Fake {slot.value}",
            price_inr=1000,
            justification="stub",
        ),
        status="ok",
    )


def test_node_select_reuses_cached_fitness_thresholds(monkeypatch, budget_gamer_brief):
    """Cache-hit path: threading the first call's fitness_thresholds /
    fitness_thresholds_key into a second call's state must avoid re-deriving,
    and a cold third call (fields omitted) must re-derive -- proving the hit
    above is a genuine cache hit, not a vacuous pass."""
    call_count = {"n": 0}
    real_derive = node3_selector.derive_fitness_thresholds

    def counting_derive(brief):
        call_count["n"] += 1
        return real_derive(brief)

    monkeypatch.setattr(node3_selector, "derive_fitness_thresholds", counting_derive)
    monkeypatch.setattr(node3_selector, "select_part", _fake_select_part)
    monkeypatch.setattr(Neo4jClient, "ping", lambda self: False)

    bands = _stub_bands()

    # Call 1 -- cold state, must derive.
    state1 = {
        "current_brief": budget_gamer_brief,
        "price_bands": bands,
        "feasibility_verdict": None,
    }
    result1 = graph.node_select(state1)
    assert call_count["n"] == 1, "expected exactly 1 derive call on the first (cold) invocation"

    # Call 2 -- same brief, with call 1's cache fields threaded through a fresh
    # state dict (mirroring a checkpointer round-trip). Must reuse the cache.
    state2 = {
        "current_brief": budget_gamer_brief,
        "price_bands": bands,
        "feasibility_verdict": None,
        "fitness_thresholds": result1["fitness_thresholds"],
        "fitness_thresholds_key": result1["fitness_thresholds_key"],
    }
    result2 = graph.node_select(state2)
    assert call_count["n"] == 1, (
        f"CACHE MISS on second call -- derive_fitness_thresholds was called again "
        f"(call_count={call_count['n']}, expected 1)"
    )
    assert result2["fitness_thresholds"] == result1["fitness_thresholds"]

    # Call 3 -- negative control: a cold state (no cache fields threaded in, as
    # node_select always did pre-fix) must re-derive. Confirms calls 1/2 above
    # exercised the real cache mechanism rather than some unrelated skip.
    state3_cold = {
        "current_brief": budget_gamer_brief,
        "price_bands": bands,
        "feasibility_verdict": None,
    }
    graph.node_select(state3_cold)
    assert call_count["n"] == 2, (
        f"expected the cold-state call to re-derive (call_count=2), got {call_count['n']}"
    )
