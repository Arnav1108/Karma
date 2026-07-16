"""Regression coverage: derive_fitness_thresholds must only compute a
threshold for gpu/cpu -- the only two slots with any GOOD_FOR fitness edges
in the graph (confirmed via live query: 120 gpu / 124 cpu edges, zero for
ram/storage/motherboard/psu/case/cooler/fans -- see docs/context.md open
items 4-5). The other seven slots always fail open in fitness_filter
regardless of the threshold passed in, so deriving one for them was a wasted
gpt-4o call.

These tests are hermetic -- the LLM (call_structured), Postgres, and Neo4j
are all mocked -- so they run without live services.
"""

from __future__ import annotations

from types import SimpleNamespace

from agents.feasibility.resolver import resolve_requirements
from agents.nodes import node3_selector as ns
from agents.schemas.price_bands import PriceBand
from agents.schemas.slots import ComponentSlot


def test_derive_fitness_thresholds_only_gpu_cpu(monkeypatch, budget_gamer_brief):
    """The returned dict must contain exactly {gpu, cpu} -- no entry for any
    of the other seven slots -- and the LLM call must not be asked for them."""
    monkeypatch.setattr(
        ns, "call_structured",
        lambda prompt, response_model, **k: ns.FitnessThresholds(gpu=0.8, cpu=0.6),
    )
    thresholds = ns.derive_fitness_thresholds(budget_gamer_brief)

    assert set(thresholds.keys()) == {ComponentSlot.gpu, ComponentSlot.cpu}, (
        f"expected thresholds only for gpu/cpu, got {sorted(s.value for s in thresholds)}"
    )
    assert thresholds[ComponentSlot.gpu] == 0.8
    assert thresholds[ComponentSlot.cpu] == 0.6


_CASE_A = {
    "product_id": "case-A", "name": "Case A", "brand": "Brand",
    "price_inr": 4000, "specs": {"form_factor_support": ["ATX"]},
}
_CASE_B = {
    "product_id": "case-B", "name": "Case B", "brand": "Brand",
    "price_inr": 5000, "specs": {"form_factor_support": ["ATX"]},
}


class _FakePostgres:
    def get_parts_in_band(self, slot, low, high, in_stock=True):
        return [dict(_CASE_A), dict(_CASE_B)]


class _FakeNeo4jNoCoverage:
    """Mirrors the real fitness_filter's fail-open behaviour for a category
    with zero GOOD_FOR edges (e.g. case): always returns catalog order,
    unranked, regardless of the threshold passed in. Counts calls so the
    test can prove the wasted call is skipped when no threshold is present.
    """
    def __init__(self):
        self.fitness_filter_calls = 0

    def compatibility_check(self, candidate_ids, locked_parts, slot):
        return list(candidate_ids)

    def fitness_filter(self, ids, primary_use_case, threshold):
        self.fitness_filter_calls += 1
        return SimpleNamespace(ordered_ids=list(ids), is_real_ranking=False)


def _run_select_part(monkeypatch, budget_gamer_brief, fitness_thresholds, fake_neo4j):
    monkeypatch.setattr(ns, "PostgresClient", lambda: _FakePostgres())
    monkeypatch.setattr(ns, "Neo4jClient", lambda: fake_neo4j)
    monkeypatch.setattr(
        ns, "call_structured",
        lambda prompt, response_model, *a, **k: ns.SelectedPart(
            product_id="case-A", justification="test-pick"
        ),
    )
    req = resolve_requirements(budget_gamer_brief)
    return ns.select_part(
        slot=ComponentSlot.case,
        band=PriceBand(low=0, mid=4500, high=10000),
        brief=budget_gamer_brief,
        locked_parts={},
        fitness_thresholds=fitness_thresholds,
        neo4j_available=True,
        req=req,
        remaining_budget=100000,
    )


def test_case_slot_selection_identical_with_or_without_threshold(monkeypatch, budget_gamer_brief):
    """case has zero GOOD_FOR coverage: whether fitness_thresholds carries a
    case entry (old, pre-fix behaviour) or omits it (new, restricted
    behaviour), select_part must produce the identical pick -- but the
    omitted case must skip the neo4j fitness_filter call entirely.
    """
    neo4j_with_key = _FakeNeo4jNoCoverage()
    outcome_with_key = _run_select_part(
        monkeypatch, budget_gamer_brief, {ComponentSlot.case: 0.5}, neo4j_with_key
    )

    neo4j_without_key = _FakeNeo4jNoCoverage()
    outcome_without_key = _run_select_part(
        monkeypatch, budget_gamer_brief, {}, neo4j_without_key
    )

    assert outcome_with_key.part is not None
    assert outcome_without_key.part is not None
    assert outcome_with_key.status == outcome_without_key.status == "ok"
    assert outcome_with_key.part.product_id == outcome_without_key.part.product_id == "case-A", (
        "the case slot pick must be identical whether or not a (never-usable) "
        "threshold entry is present"
    )

    assert neo4j_with_key.fitness_filter_calls == 1, (
        "sanity check: the old behaviour (threshold present) still calls "
        "fitness_filter once for case"
    )
    assert neo4j_without_key.fitness_filter_calls == 0, (
        "expected the restricted threshold dict to skip the wasted "
        "fitness_filter call for a slot with no fitness coverage"
    )
