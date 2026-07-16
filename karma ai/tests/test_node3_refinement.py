"""Unit coverage for the Node 3 refinement loop (pure, non-interactive layer).

Covers exactly what task §Testing asks for:
  1. Field routing table dispatch — additive vs structural, plus the
     "unknown field defaults to additive with a warning" case.
  2. pin / reject / locked_parts round-trip through dispatch_refinement.
  3. diff_and_bias incumbent-bias: keep the old part when it's still valid,
     keep the new pick when the old part is out of band / rejected.

All tests are OFFLINE: every live Postgres/Neo4j/LLM call reached by
dispatch_refinement is monkeypatched, and diff_and_bias is driven with
neo4j_available passed explicitly so it never pings. No db_available skip is
needed here because nothing hits a live service; the DB-dependent tests live in
test_node3_selector.py / test_pipeline_integration.py and keep their skips.
"""

from __future__ import annotations

import logging

import agents.nodes.node3_refinement as refine
from agents.nodes.node3_refinement import (
    RefinementOps,
    RefinementResult,
    apply_reject,
    diff_and_bias,
    dispatch_refinement,
    patch_brief_field,
    rescale_budget,
    route_field_edit,
)
from agents.schemas.build_card import BuildCard, BuildCardPart
from agents.schemas.price_bands import PriceBand, PriceBands
from agents.schemas.slots import ComponentSlot


# ── Builders ──────────────────────────────────────────────────────────────────

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


_STUB_CARD = _card([_part(ComponentSlot.gpu, "RESOLVED-GPU", 24000)])


# ── 1. Field routing table ────────────────────────────────────────────────────

def test_route_additive_fields():
    for f in ("software", "performance", "extras", "physical", "longevity"):
        assert route_field_edit(f) == "additive", f

def test_route_structural_fields():
    for f in ("primary_use_case", "budget.scope", "existing.reuse_parts"):
        assert route_field_edit(f) == "structural", f

def test_route_unknown_field_defaults_additive_with_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="agents.nodes.node3_refinement"):
        assert route_field_edit("made_up_field") == "additive"
    assert any("made_up_field" in r.message for r in caplog.records), (
        "expected a warning naming the unknown field"
    )


# ── 1b. Routing drives dispatch (additive vs structural) ──────────────────────

def test_dispatch_structural_edit_restarts_and_skips_other_ops(monkeypatch, budget_gamer_brief):
    """A structural field edit restarts via run_from_brief and skips pin/accept."""
    brief = budget_gamer_brief.model_copy(deep=True)
    calls = {"restart": 0, "resolve": 0}

    restarted_card = _card([_part(ComponentSlot.gpu, "RESTARTED-GPU", 30000)])

    def fake_run_from_brief(b):
        calls["restart"] += 1
        return {"build_card": restarted_card, "current_brief": b}

    monkeypatch.setattr("agents.graph_runner.run_from_brief", fake_run_from_brief)
    monkeypatch.setattr(refine, "allocate_budget", lambda b: _bands(gpu=(20000, 25000, 30000)))
    monkeypatch.setattr(refine, "_select_build_with_pins",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not re-solve")))

    ops = RefinementOps(
        restart_trigger={"field": "primary_use_case", "value": "content_creation"},
        pin=ComponentSlot.gpu,   # must be skipped
        accept=True,             # must be skipped
    )
    locked: dict[str, str] = {}
    result = dispatch_refinement(ops, brief, _bands(gpu=(20000, 25000, 30000)),
                                 _STUB_CARD, locked)

    assert calls["restart"] == 1
    assert result.build_card is restarted_card
    assert not result.accepted, "accept must be skipped when a structural edit fires"
    assert locked == {}, "pin must be skipped when a structural edit fires"


def test_dispatch_misrouted_structural_in_brief_edit_still_restarts(monkeypatch, budget_gamer_brief):
    """primary_use_case arriving in brief_edit is re-routed to structural by the table."""
    brief = budget_gamer_brief.model_copy(deep=True)
    hits = {"restart": 0}
    monkeypatch.setattr("agents.graph_runner.run_from_brief",
                        lambda b: (hits.__setitem__("restart", hits["restart"] + 1)
                                  or {"build_card": _STUB_CARD}))
    monkeypatch.setattr(refine, "allocate_budget", lambda b: _bands(gpu=(20000, 25000, 30000)))

    ops = RefinementOps(brief_edit={"field": "primary_use_case", "value": "work_productivity"})
    dispatch_refinement(ops, brief, _bands(gpu=(20000, 25000, 30000)), _STUB_CARD, {})
    assert hits["restart"] == 1


def test_dispatch_additive_edit_rechecks_feasibility_then_resolves(monkeypatch, budget_gamer_brief):
    """An additive edit runs feasibility (not a restart) and re-solves."""
    brief = budget_gamer_brief.model_copy(deep=True)
    seen = {"feasibility": 0, "resolve": 0}

    class _V:  # minimal verdict stand-in
        verdict = "comfortable"

    monkeypatch.setattr(refine, "estimate_feasibility",
                        lambda b: seen.__setitem__("feasibility", 1) or _V())
    monkeypatch.setattr(refine, "_select_build_with_pins",
                        lambda *a, **k: seen.__setitem__("resolve", 1) or _STUB_CARD)
    monkeypatch.setattr(refine, "diff_and_bias", lambda old, new, *a, **k: new)
    monkeypatch.setattr("agents.graph_runner.run_from_brief",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not restart")))

    ops = RefinementOps(brief_edit={"field": "longevity",
                                    "value": {"upgrade_path": "future_proof"}})
    result = dispatch_refinement(ops, brief, _bands(gpu=(20000, 25000, 30000)), _STUB_CARD, {})

    assert seen["feasibility"] == 1
    assert seen["resolve"] == 1
    assert result.build_card is _STUB_CARD
    assert not result.accepted


def test_dispatch_additive_impossible_does_not_resolve(monkeypatch, budget_gamer_brief):
    brief = budget_gamer_brief.model_copy(deep=True)

    class _V:
        verdict = "impossible"
        reason = "floor exceeds ceiling"

    monkeypatch.setattr(refine, "estimate_feasibility", lambda b: _V())
    monkeypatch.setattr(refine, "_select_build_with_pins",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not re-solve")))

    ops = RefinementOps(brief_edit={"field": "performance",
                                    "value": {"target_resolution": "4K",
                                              "target_framerate": 144,
                                              "source": "user_stated"}})
    result = dispatch_refinement(ops, brief, _bands(gpu=(20000, 25000, 30000)), _STUB_CARD, {})
    assert result.build_card is _STUB_CARD           # unchanged
    assert result.message and "impossible" in result.message.lower()


# ── 1c. Exception leak guard (both patch-failure except blocks) ────────────────

def test_additive_patch_failure_does_not_leak_exception_to_user(monkeypatch, budget_gamer_brief):
    """A raising patch_brief_field must not put raw exception text in front of the user."""
    brief = budget_gamer_brief.model_copy(deep=True)
    card = _card([_part(ComponentSlot.gpu, "GPU-1", 25000)])

    def _raise_sentinel(*a, **k):
        raise ValueError("SENTINEL_PYDANTIC_TRACE")

    monkeypatch.setattr(refine, "patch_brief_field", _raise_sentinel)

    ops = RefinementOps(brief_edit={"field": "extras", "value": {"rgb_pref": "want_rgb"}})
    result = dispatch_refinement(ops, brief, _bands(gpu=(20000, 25000, 30000)), card, {})

    assert result.message is not None
    assert "SENTINEL_PYDANTIC_TRACE" not in result.message, (
        "raw exception text must not reach the user-facing message"
    )
    assert "extras" in result.message, "message should still name the field that failed"
    assert result.build_card is card
    assert result.accepted is False


def test_structural_patch_failure_does_not_leak_exception_to_user(monkeypatch, budget_gamer_brief):
    """A raising patch_brief_field must not leak exception text, and must return
    before ever reaching run_from_brief — a failed patch must not restart."""
    brief = budget_gamer_brief.model_copy(deep=True)
    card = _card([_part(ComponentSlot.gpu, "GPU-1", 25000)])

    def _raise_sentinel(*a, **k):
        raise ValueError("SENTINEL_PYDANTIC_TRACE")

    monkeypatch.setattr(refine, "patch_brief_field", _raise_sentinel)
    monkeypatch.setattr(
        "agents.graph_runner.run_from_brief",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not restart on a failed patch")),
    )

    ops = RefinementOps(restart_trigger={"field": "primary_use_case", "value": "not_a_real_use_case"})
    result = dispatch_refinement(ops, brief, _bands(gpu=(20000, 25000, 30000)), card, {})

    assert result.message is not None
    assert "SENTINEL_PYDANTIC_TRACE" not in result.message, (
        "raw exception text must not reach the user-facing message"
    )
    assert "primary_use_case" in result.message, "message should still name the field that failed"
    assert result.build_card is card
    assert result.accepted is False


# ── 2. pin / reject / locked_parts round-trip ─────────────────────────────────

def _patch_resolve(monkeypatch):
    """Stub the re-solve + diff so pin/reject tests stay offline and deterministic."""
    monkeypatch.setattr(refine, "_select_build_with_pins", lambda *a, **k: _STUB_CARD)
    monkeypatch.setattr(refine, "diff_and_bias", lambda old, new, *a, **k: new)


def test_dispatch_pin_records_locked_part(monkeypatch, budget_gamer_brief):
    _patch_resolve(monkeypatch)
    brief = budget_gamer_brief.model_copy(deep=True)
    card = _card([_part(ComponentSlot.gpu, "GPU-1", 25000),
                  _part(ComponentSlot.cpu, "CPU-1", 15000)])
    locked: dict[str, str] = {}

    dispatch_refinement(RefinementOps(pin=ComponentSlot.gpu), brief,
                        _bands(gpu=(20000, 25000, 30000)), card, locked)
    assert locked == {"gpu": "GPU-1"}


def test_dispatch_reject_appends_and_unpins(monkeypatch, budget_gamer_brief):
    _patch_resolve(monkeypatch)
    brief = budget_gamer_brief.model_copy(deep=True)
    card = _card([_part(ComponentSlot.gpu, "GPU-1", 25000)])
    locked: dict[str, str] = {"gpu": "GPU-1"}   # gpu was pinned in a prior round

    dispatch_refinement(
        RefinementOps(reject={"slot": "gpu", "product_id": "GPU-1", "reason": "too pricey"}),
        brief, _bands(gpu=(20000, 25000, 30000)), card, locked,
    )

    rejected = {r.product_id for r in brief.hard_constraints.rejected_parts}
    assert "GPU-1" in rejected
    assert "gpu" not in locked, "rejecting a pinned slot must unpin it"


def test_dispatch_reject_resolves_product_id_from_card(monkeypatch, budget_gamer_brief):
    """reject with only a slot (no product_id) resolves the id from the current card."""
    _patch_resolve(monkeypatch)
    brief = budget_gamer_brief.model_copy(deep=True)
    card = _card([_part(ComponentSlot.psu, "PSU-9", 4500)])

    dispatch_refinement(RefinementOps(reject={"slot": "psu", "reason": "loud"}),
                        brief, _bands(psu=(3500, 4500, 6000)), card, {})
    assert "PSU-9" in {r.product_id for r in brief.hard_constraints.rejected_parts}


def test_pin_then_reject_round_trip(monkeypatch, budget_gamer_brief):
    """Pin a slot one round, reject it the next — it must end up unpinned + rejected."""
    _patch_resolve(monkeypatch)
    brief = budget_gamer_brief.model_copy(deep=True)
    card = _card([_part(ComponentSlot.gpu, "GPU-1", 25000)])
    locked: dict[str, str] = {}
    bands = _bands(gpu=(20000, 25000, 30000))

    dispatch_refinement(RefinementOps(pin=ComponentSlot.gpu), brief, bands, card, locked)
    assert locked == {"gpu": "GPU-1"}

    dispatch_refinement(RefinementOps(reject={"slot": "gpu", "product_id": "GPU-1"}),
                        brief, bands, card, locked)
    assert "gpu" not in locked
    assert "GPU-1" in {r.product_id for r in brief.hard_constraints.rejected_parts}


def test_dispatch_accept_returns_product_ids(budget_gamer_brief):
    brief = budget_gamer_brief.model_copy(deep=True)
    card = _card([_part(ComponentSlot.gpu, "GPU-1", 25000),
                  _part(ComponentSlot.cpu, "CPU-1", 15000)])
    result = dispatch_refinement(RefinementOps(accept=True), brief,
                                 _bands(gpu=(20000, 25000, 30000)), card, {})
    assert result.accepted
    assert result.product_ids == ["GPU-1", "CPU-1"]


# ── 3. diff_and_bias incumbent bias ───────────────────────────────────────────

def test_diff_keeps_valid_incumbent(budget_gamer_brief):
    """Old part still in band + not rejected → keep it; no changed_slots entry."""
    brief = budget_gamer_brief.model_copy(deep=True)
    old = _card([_part(ComponentSlot.gpu, "GPU-OLD", 25000)])
    new = _card([_part(ComponentSlot.gpu, "GPU-NEW", 26000)])
    bands = _bands(gpu=(20000, 25000, 30000))

    out = diff_and_bias(old, new, locked_parts={}, brief=brief,
                        price_bands=bands, neo4j_available=False)

    assert out.parts[0].product_id == "GPU-OLD", "valid incumbent must be retained"
    assert out.changed_slots == [], "a retained incumbent reads as unchanged"


def test_diff_replaces_out_of_band_incumbent(budget_gamer_brief):
    """Old part now above the (widened) band → keep the new pick, record the change."""
    brief = budget_gamer_brief.model_copy(deep=True)
    old = _card([_part(ComponentSlot.gpu, "GPU-OLD", 50000)])   # far above band
    new = _card([_part(ComponentSlot.gpu, "GPU-NEW", 26000)])
    bands = _bands(gpu=(20000, 25000, 30000))                   # widened high = 36000

    out = diff_and_bias(old, new, locked_parts={}, brief=brief,
                        price_bands=bands, neo4j_available=False)

    assert out.parts[0].product_id == "GPU-NEW"
    assert len(out.changed_slots) == 1
    entry = out.changed_slots[0]
    assert entry["slot"] == "gpu"
    assert entry["old_product_id"] == "GPU-OLD"
    assert entry["new_product_id"] == "GPU-NEW"
    assert entry["reason"] == "out_of_band"


def test_diff_replaces_rejected_incumbent(budget_gamer_brief):
    """Old part rejected → keep the new pick with reason 'rejected'."""
    brief = budget_gamer_brief.model_copy(deep=True)
    apply_reject(brief, "GPU-OLD", "user rejected")
    old = _card([_part(ComponentSlot.gpu, "GPU-OLD", 25000)])   # in band, but rejected
    new = _card([_part(ComponentSlot.gpu, "GPU-NEW", 26000)])
    bands = _bands(gpu=(20000, 25000, 30000))

    out = diff_and_bias(old, new, locked_parts={}, brief=brief,
                        price_bands=bands, neo4j_available=False)

    assert out.parts[0].product_id == "GPU-NEW"
    assert out.changed_slots[0]["reason"] == "rejected"


def test_diff_pinned_slot_is_never_biased(budget_gamer_brief):
    """A user-pinned slot always takes the new (pinned) part — bias never applies."""
    brief = budget_gamer_brief.model_copy(deep=True)
    old = _card([_part(ComponentSlot.gpu, "GPU-OLD", 25000)])
    new = _card([_part(ComponentSlot.gpu, "GPU-PINNED", 27000)])
    bands = _bands(gpu=(20000, 25000, 30000))

    out = diff_and_bias(old, new, locked_parts={"gpu": "GPU-PINNED"}, brief=brief,
                        price_bands=bands, neo4j_available=False)
    assert out.parts[0].product_id == "GPU-PINNED"


# ── Pure brief-patch helpers ──────────────────────────────────────────────────

def test_patch_structural_field_nested(budget_gamer_brief):
    brief = budget_gamer_brief.model_copy(deep=True)
    patched = patch_brief_field(brief, "primary_use_case", "content_creation")
    assert patched.purpose.primary_use_case == "content_creation"
    assert brief.purpose.primary_use_case == "gaming", "original brief must be untouched"

def test_patch_budget_scope(budget_gamer_brief):
    brief = budget_gamer_brief.model_copy(deep=True)
    patched = patch_brief_field(brief, "budget.scope", "pc_plus_monitor")
    assert patched.budget.scope == "pc_plus_monitor"

def test_rescale_budget_proportional(budget_gamer_brief):
    brief = budget_gamer_brief.model_copy(deep=True)
    old_ceiling = brief.budget.ceiling
    old_max = brief.budget.comfortable_max
    new = rescale_budget(brief, old_ceiling * 2)
    assert new.budget.ceiling == old_ceiling * 2
    assert new.budget.comfortable_max == old_max * 2


# ── software list merge (not full-replacement) ────────────────────────────────
# Gap closed before merge: brief_edit's patch_brief_field used to do a blind
# `ref[path[-1]] = value` for every field, including list-valued ones. Since
# parse_refinement_request only sees ONE turn's message, an LLM asked to edit
# "software" for "also add Blender" has no reliable view of the brief's other
# entries — a naive full-replacement patch would silently drop them. These
# tests pin down that budget_gamer_brief's existing 3-entry software list
# (Valorant, CS2, GTA V — see data/fixtures/budget_gamer.json) survives an
# additive edit that only mentions a new item.

def test_patch_software_add_preserves_existing_entries(budget_gamer_brief):
    """'also add Blender' → value=[Blender] must NOT drop Valorant/CS2/GTA V."""
    brief = budget_gamer_brief.model_copy(deep=True)
    existing_names = {s.name for s in brief.software}
    assert existing_names == {"Valorant", "CS2", "GTA V"}, (
        "fixture assumption changed — update this test's expectations"
    )

    patched = patch_brief_field(
        brief, "software",
        [{"name": "Blender", "category": "3d", "frequency": "secondary", "intensity": "moderate"}],
    )

    names = {s.name for s in patched.software}
    assert names == existing_names | {"Blender"}, (
        f"expected existing entries preserved plus Blender, got {names}"
    )
    assert len(patched.software) == 4
    # Original untouched (patch_brief_field must not mutate in place).
    assert len(brief.software) == 3


def test_patch_software_add_single_dict_value_also_preserved(budget_gamer_brief):
    """A single-object value (not wrapped in a list) must merge the same way."""
    brief = budget_gamer_brief.model_copy(deep=True)
    patched = patch_brief_field(
        brief, "software",
        {"name": "VS Code", "category": "dev", "frequency": "occasional", "intensity": "casual"},
    )
    names = {s.name for s in patched.software}
    assert names == {"Valorant", "CS2", "GTA V", "VS Code"}


def test_patch_software_update_existing_by_name_no_duplicate(budget_gamer_brief):
    """Re-mentioning an existing title (e.g. changed intensity) updates in place."""
    brief = budget_gamer_brief.model_copy(deep=True)
    patched = patch_brief_field(
        brief, "software",
        [{"name": "CS2", "category": "game", "frequency": "primary", "intensity": "heavy"}],
    )
    assert len(patched.software) == 3, "matching name must update, not duplicate"
    cs2 = next(s for s in patched.software if s.name == "CS2")
    assert cs2.intensity == "heavy"
    # Untouched entries keep their original values.
    valorant = next(s for s in patched.software if s.name == "Valorant")
    assert valorant.intensity == "casual"


def test_dispatch_additive_software_edit_preserves_list_end_to_end(monkeypatch, budget_gamer_brief):
    """Full path: parse_refinement_request -> dispatch_refinement must not lose entries.

    Mocks call_structured to return exactly what a real LLM is instructed to
    return per the updated prompt (only the new entry), proving the merge
    happens regardless of which layer (prompt compliance vs. patch logic) is
    doing the work — dispatch_refinement is what actually reaches the brief.
    """
    brief = budget_gamer_brief.model_copy(deep=True)

    def fake_call_structured(prompt, response_model, **kwargs):
        return RefinementOps(
            brief_edit={
                "field": "software",
                "value": [{"name": "Blender", "category": "3d",
                          "frequency": "secondary", "intensity": "moderate"}],
            }
        )

    monkeypatch.setattr(refine, "call_structured", fake_call_structured)

    class _V:
        verdict = "comfortable"

    monkeypatch.setattr(refine, "estimate_feasibility", lambda b: _V())
    monkeypatch.setattr(refine, "_select_build_with_pins", lambda *a, **k: _STUB_CARD)
    monkeypatch.setattr(refine, "diff_and_bias", lambda old, new, *a, **k: new)

    ops = refine.parse_refinement_request(
        "also add Blender for some 3D work", brief, _STUB_CARD
    )
    result = dispatch_refinement(ops, brief, _bands(gpu=(20000, 25000, 30000)), _STUB_CARD, {})

    names = {s.name for s in result.brief.software}
    assert names == {"Valorant", "CS2", "GTA V", "Blender"}, (
        f"existing software entries were dropped by the additive edit: {names}"
    )


# ── extras merge (not full-replacement) ───────────────────────────────────────
# Same gap class as software, but nested one level deeper: `extras` is an
# object (rgb_pref, visual_style, connectivity_needs, specific_part_requests),
# and patch_brief_field used to do a blind `ref["extras"] = value` for it too.
# An LLM asked to edit "extras" for "also want thunderbolt" only sees that
# turn's message — a naive full-replace patch would silently wipe rgb_pref /
# visual_style back to their schema defaults AND drop every connectivity_needs
# entry the value dict didn't re-list. budget_gamer_brief's fixture extras are
# rgb_pref="minimal", visual_style="no_preference", connectivity_needs=["wifi"]
# (see data/fixtures/budget_gamer.json) — these tests pin down that an edit
# mentioning only one sub-field preserves the rest.

def test_patch_extras_add_connectivity_need_preserves_existing(budget_gamer_brief):
    """'also want thunderbolt' → value={connectivity_needs:[thunderbolt]} must NOT
    drop 'wifi' or reset rgb_pref/visual_style to defaults."""
    brief = budget_gamer_brief.model_copy(deep=True)
    assert brief.extras.connectivity_needs == ["wifi"], (
        "fixture assumption changed — update this test's expectations"
    )
    assert brief.extras.rgb_pref == "minimal"

    patched = patch_brief_field(
        brief, "extras", {"connectivity_needs": ["thunderbolt"]},
    )

    assert patched.extras.connectivity_needs == ["wifi", "thunderbolt"], (
        f"expected existing 'wifi' preserved plus 'thunderbolt', "
        f"got {patched.extras.connectivity_needs}"
    )
    assert patched.extras.rgb_pref == "minimal", (
        "rgb_pref must be preserved, not reset to default, by a "
        "connectivity_needs-only edit"
    )
    assert patched.extras.visual_style == "no_preference"
    # Original untouched (patch_brief_field must not mutate in place).
    assert brief.extras.connectivity_needs == ["wifi"]


def test_patch_extras_connectivity_needs_dedupes(budget_gamer_brief):
    """Re-mentioning an existing connectivity need must not duplicate it."""
    brief = budget_gamer_brief.model_copy(deep=True)
    patched = patch_brief_field(brief, "extras", {"connectivity_needs": ["wifi"]})
    assert patched.extras.connectivity_needs == ["wifi"], "must not duplicate 'wifi'"


def test_patch_extras_update_rgb_pref_preserves_connectivity_needs(budget_gamer_brief):
    """Changing rgb_pref alone must not touch connectivity_needs or visual_style."""
    brief = budget_gamer_brief.model_copy(deep=True)
    patched = patch_brief_field(brief, "extras", {"rgb_pref": "want_rgb"})
    assert patched.extras.rgb_pref == "want_rgb"
    assert patched.extras.connectivity_needs == ["wifi"], (
        "connectivity_needs must be preserved by an rgb_pref-only edit"
    )
    assert patched.extras.visual_style == "no_preference"


def test_dispatch_additive_extras_edit_preserves_fields_end_to_end(monkeypatch, budget_gamer_brief):
    """Full path: parse_refinement_request -> dispatch_refinement must not lose
    existing extras sub-fields.

    Mocks call_structured to return exactly what a real LLM is instructed to
    return per the updated prompt (only the changed sub-field), proving the
    merge happens regardless of which layer (prompt compliance vs. patch
    logic) is doing the work — dispatch_refinement is what actually reaches
    the brief.
    """
    brief = budget_gamer_brief.model_copy(deep=True)

    def fake_call_structured(prompt, response_model, **kwargs):
        return RefinementOps(
            brief_edit={
                "field": "extras",
                "value": {"connectivity_needs": ["thunderbolt"]},
            }
        )

    monkeypatch.setattr(refine, "call_structured", fake_call_structured)

    class _V:
        verdict = "comfortable"

    monkeypatch.setattr(refine, "estimate_feasibility", lambda b: _V())
    monkeypatch.setattr(refine, "_select_build_with_pins", lambda *a, **k: _STUB_CARD)
    monkeypatch.setattr(refine, "diff_and_bias", lambda old, new, *a, **k: new)

    ops = refine.parse_refinement_request(
        "also want thunderbolt", brief, _STUB_CARD
    )
    result = dispatch_refinement(ops, brief, _bands(gpu=(20000, 25000, 30000)), _STUB_CARD, {})

    assert result.brief.extras.connectivity_needs == ["wifi", "thunderbolt"], (
        f"existing connectivity_needs was dropped by the additive edit: "
        f"{result.brief.extras.connectivity_needs}"
    )
    assert result.brief.extras.rgb_pref == "minimal", (
        "rgb_pref must survive an extras edit that didn't mention it"
    )


# ── v2: set_preference brand-mismatch reject ──────────────────────────────────

class _ComfortableVerdict:
    verdict = "comfortable"
    reason = "ok"


def _stub_v2_resolve(monkeypatch):
    """Stub the re-solve so dispatch_refinement_v2 needs no live catalog/LLM.

    Feasibility returns comfortable; the pinned re-solve and incumbent-bias both
    return the incoming card unchanged. The test only exercises the reject +
    brief-patch path, not the re-solve itself.
    """
    monkeypatch.setattr(refine, "estimate_feasibility", lambda b: _ComfortableVerdict())
    monkeypatch.setattr(refine, "_select_build_with_pins", lambda *a, **k: _STUB_CARD)
    monkeypatch.setattr(refine, "diff_and_bias", lambda old, new, *a, **k: old)


def test_set_preference_cpu_brand_rejects_mismatched_incumbent_via_real_brand_field(
    monkeypatch, budget_gamer_brief
):
    """An 'amd' cpu brand preference rejects an incumbent whose real brand is Intel.

    Proves _brand_mismatch reads BuildCardPart.brand (the real vendor column,
    "Intel") for non-GPU slots — not name-sniffing — and that the mismatch
    triggers apply_reject while the preference is durably persisted onto
    ecosystem_prefs.cpu_brand_pref.
    """
    brief = budget_gamer_brief.model_copy(deep=True)
    card = _card([_part(ComponentSlot.cpu, "CPU-INTEL-1", 16500, brand="Intel")])
    _stub_v2_resolve(monkeypatch)

    plan = refine.RefinementPlan(intents=[
        refine.SetPreferenceIntent(slot=ComponentSlot.cpu, attribute="brand", value="amd"),
    ])
    locked: dict[str, str] = {}
    result = refine.dispatch_refinement_v2(
        plan, brief, _bands(cpu=(12000, 16500, 20000)), card, locked,
    )

    rejected_ids = {r.product_id for r in result.brief.hard_constraints.rejected_parts}
    assert "CPU-INTEL-1" in rejected_ids, (
        "Intel incumbent must be rejected when the cpu brand preference is amd"
    )
    assert result.brief.existing.ecosystem_prefs.cpu_brand_pref == "amd", (
        "the cpu brand preference must be persisted onto ecosystem_prefs"
    )


def test_set_preference_cpu_brand_matching_incumbent_not_rejected(
    monkeypatch, budget_gamer_brief
):
    """Non-vacuousness: an 'amd' preference does NOT reject an already-AMD incumbent.

    Same flow as the mismatch test but the incumbent's real brand is AMD, so
    _brand_mismatch must return False and apply_reject must NOT fire. This proves
    the discriminator actually distinguishes match from mismatch rather than
    always rejecting.
    """
    brief = budget_gamer_brief.model_copy(deep=True)
    card = _card([_part(ComponentSlot.cpu, "CPU-AMD-1", 19500, brand="AMD")])
    _stub_v2_resolve(monkeypatch)

    plan = refine.RefinementPlan(intents=[
        refine.SetPreferenceIntent(slot=ComponentSlot.cpu, attribute="brand", value="amd"),
    ])
    locked: dict[str, str] = {}
    result = refine.dispatch_refinement_v2(
        plan, brief, _bands(cpu=(12000, 16500, 20000)), card, locked,
    )

    rejected_ids = {r.product_id for r in result.brief.hard_constraints.rejected_parts}
    assert "CPU-AMD-1" not in rejected_ids, (
        "an already-AMD incumbent must NOT be rejected by an amd preference"
    )
    assert result.brief.existing.ecosystem_prefs.cpu_brand_pref == "amd", (
        "the preference is still persisted even when no reject fires"
    )
