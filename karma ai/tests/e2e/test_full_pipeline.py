"""First true end-to-end test: intake -> locked build -> refinement, in one flow.

Every existing test (tests/test_pipeline_integration.py and friends) starts from
a pre-built fixture Brief; nothing previously verified the stages actually wire
together. This suite runs the real pipeline once — real extraction LLM calls
through drive_intake(), the real LangGraph via run_from_brief(), and two real
refinement turns (parse_refinement_request + dispatch_refinement) — and shares
that single run across several focused assertion tests.

Marked @pytest.mark.e2e (see ../../pytest.ini): excluded from the default
`pytest tests/` run, opt-in via `pytest -m e2e`. Real OpenAI + Postgres (+
optionally Neo4j) calls; ~15-20 LLM calls per run, a couple of minutes wall
clock. Skips cleanly (not fails) when OPENAI_API_KEY or Postgres is unavailable
so it never turns red as a false negative in an environment that simply lacks
credentials.

Design note on flakiness (see the design proposal this implements): assertions
here are restricted to what's true by construction regardless of which
specific parts an LLM picks — enum membership, arithmetic invariants that
_compute_bands()/_repair_bands_to_catalog() guarantee, live catalog membership,
and the refinement isolation guarantee (diff_and_bias keeps every non-targeted
slot's product_id unchanged). Nothing asserts on prose, weight ratios, or which
part won a value judgment.

Known open issue (found 2026-07-06, tracked separately, not fixed here): with
Neo4j live, TestSelection.test_all_nine_slots_filled and
TestRefinementReject.test_card_invariants_still_hold can fail because
node3_selector locks RAM before Motherboard without checking the already-locked
CPU's socket-implied DDR generation (e.g. an AM5/DDR5-only CPU paired with a
DDR4 RAM kit strands the Motherboard slot at 8/9 — Neo4j only models DDR
compatibility as motherboard<->RAM, not CPU<->RAM). A red run on these two
tests citing "No compatible motherboard exists..." is this known selector gap,
not a new regression, until it's fixed.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field, fields as dataclass_fields, is_dataclass
from enum import Enum
from pathlib import Path

import pytest
from pydantic import BaseModel

from agents.costs import core_pools
from agents.db.neo4j import Neo4jClient
from agents.db.postgres import PostgresClient
from agents.graph_runner import run_from_brief
from agents.nodes.node1_intake import QUESTION_SEQUENCE, blank_brief, drive_intake
from agents.nodes.node3_refinement import (
    RefinementOps,
    RefinementResult,
    dispatch_refinement,
    parse_refinement_request,
)
from agents.nodes.node3_selector import ThresholdCache
from agents.schemas.brief import UserBuildBrief
from agents.schemas.build_card import BuildCard
from agents.schemas.feasibility import FeasibilityVerdict
from agents.schemas.price_bands import PriceBands
from agents.schemas.slots import ComponentSlot
from agents.schemas.source_flag import SourceFlag

from .intake_script import CANNED_ANSWERS, make_answer_fn

pytestmark = pytest.mark.e2e

_VALID_CHANGED_SLOT_REASONS = {"changed", "added", "rejected", "out_of_band", "incompatible"}


# ---------------------------------------------------------------------------
# Artifact recorder (Q4 of the design proposal) — one record per pipeline run,
# progressively filled in as each stage completes, dumped to JSON pass or fail
# so a failure (or a "verdict looks different than last time" investigation)
# never requires re-running the expensive suite with print statements bolted on.
# ---------------------------------------------------------------------------

@dataclass
class PipelineArtifacts:
    stage_reached: str = "not started"
    error: str | None = None

    conversation_history: list[dict] = field(default_factory=list)
    brief_after_intake: UserBuildBrief | None = None

    verdict: FeasibilityVerdict | None = None
    price_bands: PriceBands | None = None
    build_card: BuildCard | None = None
    fitness_thresholds: dict | None = None
    fitness_thresholds_key: dict | None = None

    old_gpu_product_id: str | None = None
    reject_ops: RefinementOps | None = None
    reject_result: RefinementResult | None = None

    pre_accept_card: BuildCard | None = None
    accept_ops: RefinementOps | None = None
    accept_result: RefinementResult | None = None

    catalog_products: list[dict] = field(default_factory=list)
    neo4j_available: bool = False

    dump_path: str | None = None


def _to_jsonable(obj):
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _to_jsonable(getattr(obj, f.name)) for f in dataclass_fields(obj)}
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    return str(obj)


def _write_dump(path: Path, artifacts: PipelineArtifacts) -> None:
    path.write_text(json.dumps(_to_jsonable(artifacts), indent=2, default=str), encoding="utf-8")


def _format_summary(a: PipelineArtifacts) -> str:
    lines = [f"stage_reached: {a.stage_reached}"]
    if a.error:
        lines.append(f"error: {a.error}")
    if a.brief_after_intake is not None:
        b = a.brief_after_intake
        lines.append(
            f"brief: status={b.status} "
            f"budget={b.budget.comfortable_min}-{b.budget.comfortable_max}/{b.budget.ceiling} "
            f"use_case={b.purpose.primary_use_case}/{b.purpose.sub_case} "
            f"software={[s.name for s in b.software]}"
        )
    if a.conversation_history:
        user_turns = [m["content"] for m in a.conversation_history if m.get("role") == "user"]
        lines.append(f"conversation user turns ({len(user_turns)}): {user_turns}")
    if a.verdict is not None:
        lines.append(
            f"verdict: {a.verdict.verdict} (basis={a.verdict.basis}) reason={a.verdict.reason}"
        )
    if a.price_bands is not None:
        lines.append(
            f"bands: total_low={a.price_bands.total_low()} "
            f"total_mid={a.price_bands.total_mid()} total_high={a.price_bands.total_high()}"
        )
    if a.build_card is not None:
        lines.append(
            f"build_card: {len(a.build_card.parts)} parts, "
            f"total=INR {a.build_card.total_price_inr}, warnings={a.build_card.warnings}"
        )
        for p in a.build_card.parts:
            lines.append(f"    {p.slot.value}: {p.product_id}  {p.name}  INR {p.price_inr}")
    if a.reject_result is not None:
        lines.append(f"reject_ops: {a.reject_ops.model_dump() if a.reject_ops else None}")
        lines.append(f"reject changed_slots: {a.reject_result.build_card.changed_slots}")
    if a.accept_result is not None:
        lines.append(f"accept_ops: {a.accept_ops.model_dump() if a.accept_ops else None}")
        lines.append(
            f"accepted: {a.accept_result.accepted} product_ids={a.accept_result.product_ids}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The one expensive pipeline run, shared by every test below.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def pipeline_artifacts(tmp_path_factory, db_available):
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY not set — skipping E2E pipeline run")
    if not db_available:
        pytest.skip("Postgres unavailable — skipping E2E pipeline run")

    dump_path = tmp_path_factory.mktemp("e2e") / "pipeline_artifacts.json"
    a = PipelineArtifacts(dump_path=str(dump_path))

    def _fail(stage: str, exc: Exception):
        a.stage_reached = stage
        a.error = f"{type(exc).__name__}: {exc}"
        _write_dump(dump_path, a)
        pytest.fail(
            f"FAILED AT STAGE: {stage}\n{type(exc).__name__}: {exc}\n\n"
            f"Artifacts captured so far (full dump: {dump_path}):\n{_format_summary(a)}"
        )

    # ── Stage: intake — real extraction LLM, no phrasing calls (Q1) ──────────
    try:
        by_id = {q.id: q for q in QUESTION_SEQUENCE}
        brief = blank_brief(
            brief_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            chat_id=uuid.uuid4(),
        )
        brief, history = drive_intake(
            brief,
            make_answer_fn(),
            phrase_fn=lambda qid: by_id[qid].raw_text,
        )
        a.brief_after_intake = brief
        a.conversation_history = history
        a.stage_reached = "intake"
    except Exception as exc:  # noqa: BLE001 — captured for the diagnostic dump
        _fail("intake", exc)

    # ── Stage: feasibility -> allocation -> selection via the real graph ─────
    try:
        state = run_from_brief(brief)
        if state.get("error_message"):
            raise RuntimeError(state["error_message"])
        a.verdict = state.get("feasibility_verdict")
        a.price_bands = state.get("price_bands")
        a.build_card = state.get("build_card")
        a.fitness_thresholds = state.get("fitness_thresholds")
        a.fitness_thresholds_key = state.get("fitness_thresholds_key")
        if a.build_card is None:
            raise RuntimeError("graph reached END with no build_card in state")
        a.stage_reached = "selection"
    except Exception as exc:  # noqa: BLE001
        _fail("feasibility/allocation/selection", exc)

    # ── Stage: refinement — reject turn ──────────────────────────────────────
    try:
        cache = ThresholdCache(
            thresholds=(
                {ComponentSlot(s): v for s, v in a.fitness_thresholds.items()}
                if a.fitness_thresholds else None
            ),
            key=a.fitness_thresholds_key,
        )
        locked_parts: dict[str, str] = {}
        old_gpu = next(p for p in a.build_card.parts if p.slot == ComponentSlot.gpu)
        a.old_gpu_product_id = old_gpu.product_id

        reject_ops = parse_refinement_request(
            "I don't like the graphics card you picked — reject it and give me a "
            "different GPU.",
            a.brief_after_intake,
            a.build_card,
        )
        reject_result = dispatch_refinement(
            reject_ops, a.brief_after_intake, a.price_bands, a.build_card, locked_parts, cache
        )
        a.reject_ops = reject_ops
        a.reject_result = reject_result
        a.stage_reached = "refinement-reject"
    except Exception as exc:  # noqa: BLE001
        _fail("refinement-reject", exc)

    # ── Stage: refinement — accept turn ──────────────────────────────────────
    try:
        a.pre_accept_card = reject_result.build_card
        accept_ops = parse_refinement_request(
            "This looks great, please finalize it.",
            reject_result.brief,
            reject_result.build_card,
        )
        accept_result = dispatch_refinement(
            accept_ops,
            reject_result.brief,
            reject_result.price_bands,
            reject_result.build_card,
            locked_parts,
            cache,
        )
        a.accept_ops = accept_ops
        a.accept_result = accept_result
        a.stage_reached = "refinement-accept"
    except Exception as exc:  # noqa: BLE001
        _fail("refinement-accept", exc)

    # ── Catalog cross-check data for the selection assertions ────────────────
    try:
        a.catalog_products = PostgresClient().get_all_products()
        a.neo4j_available = Neo4jClient().ping()
        a.stage_reached = "done"
    except Exception as exc:  # noqa: BLE001
        _fail("catalog-crosscheck-setup", exc)

    _write_dump(dump_path, a)
    print(f"\n[E2E] pipeline artifact dump written to: {dump_path}")

    return a


# ---------------------------------------------------------------------------
# Assertions — intake / brief gate
# ---------------------------------------------------------------------------

class TestBriefGate:
    """Deterministic checks on the brief the real extraction LLM produced."""

    def test_locked_and_floor_met(self, pipeline_artifacts):
        brief = pipeline_artifacts.brief_after_intake
        assert brief.status == "locked"
        assert brief.budget.comfortable_max > 0
        assert brief.purpose.primary_use_case

    def test_budget_extracted_exactly(self, pipeline_artifacts):
        brief = pipeline_artifacts.brief_after_intake
        assert brief.budget.comfortable_min == 85000
        assert brief.budget.comfortable_max == 95000
        assert brief.budget.ceiling == 100000

    def test_use_case_and_software_extracted(self, pipeline_artifacts):
        brief = pipeline_artifacts.brief_after_intake
        assert brief.purpose.primary_use_case == "gaming"
        assert brief.purpose.sub_case, "sub_case should be non-empty for a gaming brief"
        assert len(brief.software) >= 2

    def test_performance_source_user_stated(self, pipeline_artifacts):
        brief = pipeline_artifacts.brief_after_intake
        assert brief.performance.source == SourceFlag.user_stated

    def test_opportunistic_fill_shortened_the_script(self, pipeline_artifacts):
        """Several canned answers volunteer info beyond their own question
        (e.g. the budget answer's monitor/peripherals aside, the
        primary_use_case answer's frame-rate aside); drive_intake's
        opportunistic-fill (newly_filled_sections) should skip re-asking at
        least one field outright via one of these, rather than asking every
        one of the 7 canned questions individually before "done". Which
        specific field gets skipped is not asserted here — see
        test_performance_source_user_stated for the one field-level check on
        an opportunistically-filled value.
        """
        user_turns = [
            m["content"] for m in pipeline_artifacts.conversation_history
            if m.get("role") == "user"
        ]
        # 7 canned answers (budget..storage) + "done" = 8 if nothing was skipped.
        assert 6 <= len(user_turns) <= len(CANNED_ANSWERS) + 1
        assert len(user_turns) < len(CANNED_ANSWERS) + 1, (
            "expected opportunistic-fill to skip at least one of monitor/"
            "peripherals via the aside in the budget answer, but every "
            "canned question was asked individually"
        )


# ---------------------------------------------------------------------------
# Assertions — feasibility
# ---------------------------------------------------------------------------

class TestFeasibility:
    def test_verdict_is_buildable(self, pipeline_artifacts):
        verdict = pipeline_artifacts.verdict
        assert verdict is not None
        assert verdict.verdict in ("comfortable", "tight", "impossible")
        assert verdict.verdict != "impossible", (
            f"expected a buildable verdict for a comfortable INR 85k-100k gaming "
            f"brief, got impossible: {verdict.reason}"
        )

    def test_verdict_used_the_deterministic_path(self, pipeline_artifacts):
        verdict = pipeline_artifacts.verdict
        assert verdict.basis == "deterministic", (
            f"db_available confirmed Postgres was reachable, so the verdict should "
            f"come from the live catalog floor, not the degraded LLM-estimate "
            f"fallback — got basis={verdict.basis!r}: {verdict.reason}"
        )


# ---------------------------------------------------------------------------
# Assertions — allocation
# ---------------------------------------------------------------------------

class TestAllocation:
    def test_all_slots_present_and_ordered(self, pipeline_artifacts):
        bands = pipeline_artifacts.price_bands
        for slot in ComponentSlot:
            assert slot in bands.root, f"missing slot: {slot.value}"
        for slot, band in bands.root.items():
            assert band.low <= band.mid <= band.high, f"{slot.value}: band out of order"

    def test_sums_match_core_pools(self, pipeline_artifacts):
        bands = pipeline_artifacts.price_bands
        brief = pipeline_artifacts.brief_after_intake
        _floor, target, ceiling = core_pools(brief)

        assert abs(bands.total_mid() - target) <= 500, (
            f"total_mid={bands.total_mid()}, expected ~{target} (core target)"
        )
        assert abs(bands.total_high() - ceiling) <= 500, (
            f"total_high={bands.total_high()}, expected ~{ceiling} (core ceiling)"
        )
        assert bands.total_low() <= bands.total_mid() <= bands.total_high()


# ---------------------------------------------------------------------------
# Assertions — selection (BuildCard)
# ---------------------------------------------------------------------------

class TestSelection:
    def test_all_nine_slots_filled(self, pipeline_artifacts):
        card = pipeline_artifacts.build_card
        assert len(card.parts) == 9, (
            f"expected all 9 slots filled for a comfortable-verdict brief, got "
            f"{len(card.parts)}; warnings={card.warnings}"
        )
        slots_seen = [p.slot for p in card.parts]
        assert len(slots_seen) == len(set(slots_seen)), "duplicate slot in build card"

    def test_parts_are_real_catalog_stock(self, pipeline_artifacts):
        card = pipeline_artifacts.build_card
        catalog_by_id = {p["product_id"]: p for p in pipeline_artifacts.catalog_products}

        for part in card.parts:
            catalog_part = catalog_by_id.get(part.product_id)
            assert catalog_part is not None, (
                f"{part.product_id} ({part.slot.value}) not found in the live catalog"
            )
            assert catalog_part["category"] == part.slot.value
            assert int(catalog_part["price_inr"]) == part.price_inr

    def test_total_price_invariants(self, pipeline_artifacts):
        card = pipeline_artifacts.build_card
        brief = pipeline_artifacts.brief_after_intake
        assert card.total_price_inr == sum(p.price_inr for p in card.parts)
        assert card.total_price_inr <= brief.budget.ceiling

    def test_locked_parts_are_mutually_compatible(self, pipeline_artifacts):
        if not pipeline_artifacts.neo4j_available:
            pytest.skip("Neo4j unavailable — skipping compatibility cross-check")
        card = pipeline_artifacts.build_card
        neo4j = Neo4jClient()
        locked = {p.slot: p.product_id for p in card.parts}
        for part in card.parts:
            others = {s: pid for s, pid in locked.items() if s != part.slot}
            ok = neo4j.compatibility_check([part.product_id], others, part.slot)
            assert part.product_id in ok, (
                f"{part.slot.value}={part.product_id} flagged incompatible with "
                f"already-locked parts {others}"
            )


# ---------------------------------------------------------------------------
# Assertions — refinement: reject turn
# ---------------------------------------------------------------------------

class TestRefinementReject:
    def test_reject_did_not_accept(self, pipeline_artifacts):
        assert pipeline_artifacts.reject_result.accepted is False

    def test_gpu_changed_and_old_gpu_rejected(self, pipeline_artifacts):
        result = pipeline_artifacts.reject_result
        new_card = result.build_card
        old_gpu_id = pipeline_artifacts.old_gpu_product_id

        new_gpu = next(p for p in new_card.parts if p.slot == ComponentSlot.gpu)
        assert new_gpu.product_id != old_gpu_id, "GPU is unchanged after a reject request"

        rejected_ids = {r.product_id for r in result.brief.hard_constraints.rejected_parts}
        assert old_gpu_id in rejected_ids

    def test_changed_slots_are_well_formed(self, pipeline_artifacts):
        new_card = pipeline_artifacts.reject_result.build_card
        changed_slot_names = {c["slot"] for c in new_card.changed_slots}
        assert "gpu" in changed_slot_names
        for c in new_card.changed_slots:
            assert c["reason"] in _VALID_CHANGED_SLOT_REASONS, f"unexpected reason: {c!r}"

    def test_non_targeted_slots_are_unchanged(self, pipeline_artifacts):
        """The isolation guarantee: a reject on GPU must not silently move any
        other slot's product_id (diff_and_bias's incumbent bias)."""
        old_card = pipeline_artifacts.build_card
        new_card = pipeline_artifacts.reject_result.build_card
        changed_slot_names = {c["slot"] for c in new_card.changed_slots}

        old_by_slot = {p.slot: p.product_id for p in old_card.parts}
        new_by_slot = {p.slot: p.product_id for p in new_card.parts}
        for slot, product_id in new_by_slot.items():
            if slot.value not in changed_slot_names:
                assert old_by_slot.get(slot) == product_id, (
                    f"{slot.value} changed ({old_by_slot.get(slot)} -> {product_id}) "
                    f"without appearing in changed_slots"
                )

    def test_card_invariants_still_hold(self, pipeline_artifacts):
        new_card = pipeline_artifacts.reject_result.build_card
        assert len(new_card.parts) == 9
        assert new_card.total_price_inr == sum(p.price_inr for p in new_card.parts)


# ---------------------------------------------------------------------------
# Assertions — refinement: accept turn
# ---------------------------------------------------------------------------

class TestRefinementAccept:
    def test_accepted_with_matching_product_ids(self, pipeline_artifacts):
        result = pipeline_artifacts.accept_result
        pre_accept_card = pipeline_artifacts.pre_accept_card
        assert result.accepted is True
        assert result.product_ids == [p.product_id for p in pre_accept_card.parts]

    def test_accept_did_not_resolve(self, pipeline_artifacts):
        """accept is a pure finalize — dispatch_refinement's accept branch
        returns the same build_card it was given, never a fresh re-solve."""
        result = pipeline_artifacts.accept_result
        assert result.build_card is pipeline_artifacts.pre_accept_card
