"""Unit coverage for the v2 intent-based refinement contract
(parse_refinement_request_v2 / RefinementPlan), gated behind
KARMA_REFINEMENT_MODE=intent.

Offline: call_structured is monkeypatched so no live LLM call is made —
mirrors the pattern used for the v1 parse_refinement_request coverage in
test_node3_refinement.py (e.g. test_dispatch_additive_software_edit_
preserves_list_end_to_end).
"""

from __future__ import annotations

import logging

import agents.nodes.node3_refinement as refine
from agents.feasibility.catalog_floor import rejected_product_ids
from agents.schemas.build_card import BuildCard, BuildCardPart
from agents.schemas.price_bands import PriceBand, PriceBands
from agents.schemas.slots import ComponentSlot


def _part(slot: ComponentSlot, pid: str, price: int,
          brand: str | None = None) -> BuildCardPart:
    return BuildCardPart(
        slot=slot, product_id=pid, name=f"{slot.value} {pid}",
        price_inr=price, justification="test", brand=brand,
    )


def _card(parts: list[BuildCardPart]) -> BuildCard:
    return BuildCard(
        parts=parts, total_price_inr=sum(p.price_inr for p in parts), summary="test",
    )


def _bands(**slot_price_ranges: tuple[int, int, int]) -> PriceBands:
    root = {
        ComponentSlot(s): PriceBand(low=lo, mid=mid, high=hi)
        for s, (lo, mid, hi) in slot_price_ranges.items()
    }
    return PriceBands(root=root)


class _ComfortableVerdict:
    verdict = "comfortable"
    reason = "ok"


def test_v2_set_preference_rejects_incumbent_and_applies_edit(monkeypatch, budget_gamer_brief):
    """A single turn carrying TWO intents — a durable cpu brand preference away
    from the incumbent's real brand, plus an additive 'physical' edit — must
    apply both: the mismatched incumbent gets rejected via the real _brand_
    mismatch/apply_reject path (not a mock echo), its slot is unpinned, the
    preference is persisted onto ecosystem_prefs, and the physical field is
    patched onto the brief.

    Exercises dispatch_refinement_v2 directly rather than parse_refinement_
    request_v2 — the latter is a thin call_structured wrapper, so mocking its
    LLM call would only prove the mock echoes back what it was told to return.
    """
    brief = budget_gamer_brief.model_copy(deep=True)
    card = _card([_part(ComponentSlot.cpu, "CPU-AMD-1", 16500, brand="AMD")])
    locked_parts = {"cpu": "CPU-AMD-1"}

    monkeypatch.setattr(refine, "estimate_feasibility", lambda b: _ComfortableVerdict())
    monkeypatch.setattr(refine, "_select_build_with_pins", lambda *a, **k: card)
    monkeypatch.setattr(refine, "diff_and_bias", lambda old, new, *a, **k: old)

    plan = refine.RefinementPlan(intents=[
        refine.SetPreferenceIntent(slot=ComponentSlot.cpu, attribute="brand", value="intel"),
        refine.EditFieldIntent(field="physical", value={"noise_tolerance": "silent_priority"}),
    ])

    result = refine.dispatch_refinement_v2(
        plan, brief, _bands(cpu=(12000, 16500, 20000)), card, locked_parts,
    )

    assert result.brief.existing.ecosystem_prefs.cpu_brand_pref == "intel"
    assert "CPU-AMD-1" in rejected_product_ids(result.brief)
    assert "cpu" not in locked_parts
    assert result.brief.physical.noise_tolerance == "silent_priority"


def test_v2_precedence_structural_short_circuits_everything_else(monkeypatch, budget_gamer_brief):
    """A single plan carrying a structural intent alongside budget/pin/reject
    intents must honor dispatch_refinement_v2's own documented precedence —
    "structural (skip rest)". When ANY structural intent is present, dispatch
    applies ONLY that intent, restarts via run_from_brief, and returns
    immediately; the budget, pin, and reject intents never execute.

    The plan lists them in the order BudgetIntent -> PinIntent -> StructuralIntent
    -> RejectIntent precisely to prove precedence is by KIND, not by position:
    the structural intent wins even though three other intents precede or follow
    it in the list.

    run_from_brief is monkeypatched to a recognizable sentinel state (the DB/LLM
    calls beneath it are NOT touched — allocate_budget, the only other live call
    on the structural path, is stubbed too — it now feeds only the price_bands
    fallback used if run_from_brief's state lacks "price_bands", not run_from_brief
    itself) so we can assert dispatch returned exactly that state, unmodified by
    the other three intents:
      * the budget ceiling is still the brief's original 70_000 — never rescaled
        to NEW_CEILING — proving BudgetIntent was skipped,
      * locked_parts is untouched — proving PinIntent was skipped,
      * the incumbent GPU was never rejected — proving RejectIntent was skipped.
    Meanwhile primary_use_case IS patched, proving the structural intent applied.
    """
    brief = budget_gamer_brief.model_copy(deep=True)
    assert brief.budget.ceiling == 70_000           # guard the premise
    assert brief.purpose.primary_use_case == "gaming"

    card = _card([_part(ComponentSlot.gpu, "GPU-INCUMBENT-1", 30000, brand="NVIDIA")])
    locked_parts: dict[str, str] = {}

    NEW_CEILING = 999_999    # BudgetIntent target — must NOT surface in the result

    sentinel_card = _card([_part(ComponentSlot.gpu, "GPU-FROM-RESTART", 42424, brand="AMD")])
    sentinel_bands = _bands(gpu=(111, 222, 333))
    alloc_bands = _bands(gpu=(1, 2, 3))

    # allocate_budget is called inside the structural branch before run_from_brief;
    # stub it so no live Node 2 LLM call fires (per task: don't touch DB/LLM under it).
    monkeypatch.setattr(refine, "allocate_budget", lambda b: alloc_bands)

    captured: dict = {}

    def fake_run_from_brief(b):
        captured["brief"] = b
        return {
            "build_card": sentinel_card,
            "current_brief": b,
            "price_bands": sentinel_bands,
        }

    # dispatch imports run_from_brief lazily from ..graph_runner, so patch it there.
    import agents.graph_runner as graph_runner
    monkeypatch.setattr(graph_runner, "run_from_brief", fake_run_from_brief)

    plan = refine.RefinementPlan(intents=[
        refine.BudgetIntent(new_ceiling_inr=NEW_CEILING),
        refine.PinIntent(slot=ComponentSlot.gpu),
        refine.StructuralIntent(field="primary_use_case", value="content_creation"),
        refine.RejectIntent(slot=ComponentSlot.gpu, product_id="GPU-INCUMBENT-1"),
    ])

    result = refine.dispatch_refinement_v2(
        plan, brief, _bands(gpu=(25000, 30000, 35000)), card, locked_parts,
    )

    # dispatch returned run_from_brief's state verbatim (structural short-circuit)
    assert result.build_card is sentinel_card
    assert result.price_bands is sentinel_bands
    assert result.message == "Restarted after structural change to primary_use_case."

    # the structural intent WAS applied to the brief handed to run_from_brief
    assert captured["brief"].purpose.primary_use_case == "content_creation"

    # BudgetIntent skipped: ceiling is the original, never rescaled to NEW_CEILING
    assert result.brief.budget.ceiling == 70_000
    assert result.brief.budget.ceiling != NEW_CEILING

    # PinIntent skipped: locked_parts untouched
    assert locked_parts == {}
    assert "gpu" not in locked_parts

    # RejectIntent skipped: the incumbent GPU was never rejected on the returned brief
    assert not rejected_product_ids(result.brief)
    assert "GPU-INCUMBENT-1" not in rejected_product_ids(result.brief)


def test_v2_llm_misrouted_structural_field_follows_route_field_edit_not_llm_tag(
    monkeypatch, caplog, budget_gamer_brief,
):
    """route_field_edit is the SOLE routing authority (DESIGN §field-routing:
    "The table decides routing even if the LLM puts a structural field name in
    brief_edit"). Here the LLM mis-tags a structural field as an additive
    EditFieldIntent (kind="edit_field") — but its `field`, "primary_use_case",
    is in STRUCTURAL_FIELDS, so dispatch must take the structural restart path
    anyway and log a warning about the mismatch.

    Both branches are stubbed with DISTINCT sentinels so the assertion proves
    which one ran, offline:
      * structural branch -> run_from_brief returns `structural_card`
      * additive branch   -> _select_build_with_pins returns `additive_card`
        (estimate_feasibility / diff_and_bias stubbed too, so if routing ever
        regresses to trusting the LLM tag, the test fails on a clean assertion
        instead of a live LLM/DB call)
    """
    brief = budget_gamer_brief.model_copy(deep=True)
    card = _card([_part(ComponentSlot.gpu, "GPU-INCUMBENT-1", 30000, brand="NVIDIA")])
    locked_parts: dict[str, str] = {}

    structural_card = _card([_part(ComponentSlot.gpu, "GPU-FROM-RESTART", 42424, brand="AMD")])
    additive_card = _card([_part(ComponentSlot.gpu, "GPU-FROM-ADDITIVE", 13131, brand="AMD")])
    sentinel_bands = _bands(gpu=(111, 222, 333))
    alloc_bands = _bands(gpu=(1, 2, 3))

    # Structural-branch stubs (same shape as the precedence test).
    monkeypatch.setattr(refine, "allocate_budget", lambda b: alloc_bands)

    captured: dict = {}

    def fake_run_from_brief(b):
        captured["brief"] = b
        return {
            "build_card": structural_card,
            "current_brief": b,
            "price_bands": sentinel_bands,
        }

    # dispatch imports run_from_brief lazily from ..graph_runner, so patch it there.
    import agents.graph_runner as graph_runner
    monkeypatch.setattr(graph_runner, "run_from_brief", fake_run_from_brief)

    # Additive-branch stubs — must NOT be reached; distinct sentinel proves it.
    monkeypatch.setattr(refine, "estimate_feasibility", lambda b: _ComfortableVerdict())
    monkeypatch.setattr(refine, "_select_build_with_pins", lambda *a, **k: additive_card)
    monkeypatch.setattr(refine, "diff_and_bias", lambda old, new, *a, **k: new)

    # The LLM's tag says additive (kind="edit_field") but the field is structural.
    plan = refine.RefinementPlan(intents=[
        refine.EditFieldIntent(field="primary_use_case", value="content_creation"),
    ])

    with caplog.at_level(logging.WARNING, logger=refine.__name__):
        result = refine.dispatch_refinement_v2(
            plan, brief, _bands(gpu=(25000, 30000, 35000)), card, locked_parts,
        )

    # 1. The structural branch executed DESPITE the edit_field tag: dispatch
    #    returned run_from_brief's sentinel state, not the additive re-solve.
    assert result.build_card is structural_card
    assert result.build_card is not additive_card
    assert result.price_bands is sentinel_bands
    assert result.message == "Restarted after structural change to primary_use_case."
    assert captured["brief"].purpose.primary_use_case == "content_creation"

    # 2. The mismatch warning fired, with the real message from dispatch's
    #    routing loop (field, both verdicts, and the "following" clause).
    mismatch_warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "following route_field_edit" in r.getMessage()
    ]
    assert len(mismatch_warnings) == 1
    msg = mismatch_warnings[0].getMessage()
    assert "[Refine v2] field 'primary_use_case'" in msg
    assert "route_field_edit says 'structural'" in msg
    assert "the LLM tagged this intent kind='edit_field'" in msg


def test_v2_empty_plan_no_actionable_change_fallback(monkeypatch, budget_gamer_brief):
    """An empty RefinementPlan (intents=[]) — the LLM parsed the user's turn into
    nothing actionable — must fall through every dispatch section to the final
    fallback: a clean "No actionable change" message with state untouched, and
    NO re-solve of any kind.

    Both re-solve entry points — run_from_brief (structural restart) and
    _select_build_with_pins (the `if changed:` incumbent-biased re-solve) — are
    stubbed to raise AssertionError if called, so "no re-solve fired" is a hard
    failure rather than something inferred from the result.
    """
    brief = budget_gamer_brief.model_copy(deep=True)
    card = _card([_part(ComponentSlot.gpu, "GPU-INCUMBENT-1", 30000, brand="NVIDIA")])
    locked_parts = {"gpu": "GPU-INCUMBENT-1"}
    bands = _bands(gpu=(25000, 30000, 35000))

    def _boom(name):
        def _raise(*a, **k):
            raise AssertionError(f"{name} should not be called for an empty plan")
        return _raise

    import agents.graph_runner as graph_runner
    monkeypatch.setattr(graph_runner, "run_from_brief", _boom("run_from_brief"))
    monkeypatch.setattr(refine, "_select_build_with_pins", _boom("_select_build_with_pins"))

    plan = refine.RefinementPlan(intents=[])

    result = refine.dispatch_refinement_v2(plan, brief, bands, card, locked_parts)

    # State truly untouched: the very same objects come back.
    assert result.build_card is card
    assert result.price_bands is bands
    assert locked_parts == {"gpu": "GPU-INCUMBENT-1"}
    assert not result.accepted

    # The real fallback string from dispatch's final branch, verbatim.
    assert result.message == (
        "No actionable change detected — try 'pin <slot>', 'reject <slot>', "
        "a new budget, or 'accept'."
    )
