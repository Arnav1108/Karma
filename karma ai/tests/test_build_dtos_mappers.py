"""Unit tests for the build-route DTOs/mappers added to api/dtos.py and
api/mappers.py — see karma ai/docs/build_service_plan.md section 7.

Pure unit tests: no DB, no network, no LLM calls, no app/executor spin-up.
JobRecord instances are constructed directly; PipelineState is a plain dict
(TypedDict at runtime) so it's built as one.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from agents.schemas.build_card import BuildCard, BuildCardPart
from agents.schemas.feasibility import FeasibilityVerdict
from agents.schemas.slots import ComponentSlot

from api.dtos import BuildCardDTO, VerdictDTO
from api.mappers import map_build_card, map_build_part, map_build_status, map_verdict
from api.services.job_registry import JobRecord


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_record(**overrides) -> JobRecord:
    defaults = dict(
        build_id=str(uuid4()),
        session_id=str(uuid4()),
        status="queued",
        created_at=_now(),
        started_at=None,
        finished_at=None,
        state=None,
        error_code=None,
        error_message=None,
        warnings=[],
    )
    defaults.update(overrides)
    return JobRecord(**defaults)


def _make_verdict(**overrides) -> FeasibilityVerdict:
    defaults = dict(
        verdict="comfortable",
        basis="deterministic",
        reason="Budget comfortably covers the core pool.",
        binding_constraint=None,
        suggested_adjustments=[],
    )
    defaults.update(overrides)
    return FeasibilityVerdict(**defaults)


def _make_part(**overrides) -> BuildCardPart:
    defaults = dict(
        slot=ComponentSlot.gpu,
        product_id="gpu-001",
        name="RTX 4070 Super",
        price_inr=55000,
        justification="Best perf/rupee in band.",
        brand="Asus",
    )
    defaults.update(overrides)
    return BuildCardPart(**defaults)


def _make_build_card(**overrides) -> BuildCard:
    defaults = dict(
        parts=[_make_part()],
        total_price_inr=55000,
        summary="A solid 1440p gaming build.",
        warnings=[],
        changed_slots=[{"slot": "gpu", "old_product_id": None, "new_product_id": "gpu-001", "reason": "initial pick"}],
    )
    defaults.update(overrides)
    return BuildCard(**defaults)


# ---------------------------------------------------------------------------
# map_verdict
# ---------------------------------------------------------------------------

def test_map_verdict_correct_fields_basis_absent():
    verdict = _make_verdict(
        verdict="tight",
        basis="llm_fallback",
        reason="GPU floor eats most of the ceiling.",
        binding_constraint="gpu",
        suggested_adjustments=["Raise ceiling by 10000", "Drop target resolution to 1080p"],
    )
    dto = map_verdict(verdict)

    assert isinstance(dto, VerdictDTO)
    assert dto.verdict == "tight"
    assert dto.reason == "GPU floor eats most of the ceiling."
    assert dto.binding_constraint == "gpu"
    assert dto.suggested_adjustments == ["Raise ceiling by 10000", "Drop target resolution to 1080p"]
    assert "basis" not in dto.model_dump()


# ---------------------------------------------------------------------------
# map_build_part
# ---------------------------------------------------------------------------

def test_map_build_part_slot_enum_becomes_string():
    part = _make_part(slot=ComponentSlot.motherboard)
    dto = map_build_part(part)

    assert dto.slot == "motherboard"
    assert isinstance(dto.slot, str)
    assert dto.product_id == part.product_id
    assert dto.name == part.name
    assert dto.brand == part.brand
    assert dto.price_inr == part.price_inr
    assert dto.justification == part.justification


def test_map_build_part_brand_none_passes_through():
    part = _make_part(brand=None)
    dto = map_build_part(part)
    assert dto.brand is None


# ---------------------------------------------------------------------------
# map_build_card
# ---------------------------------------------------------------------------

def test_map_build_card_parts_mapped_and_changed_slots_absent():
    card = _make_build_card(
        parts=[_make_part(slot=ComponentSlot.gpu), _make_part(slot=ComponentSlot.cpu, product_id="cpu-001")],
        total_price_inr=110000,
        summary="Two-part card.",
        warnings=["No compatible cooler within budget."],
    )
    dto = map_build_card(card)

    assert isinstance(dto, BuildCardDTO)
    assert len(dto.parts) == 2
    assert dto.parts[0].slot == "gpu"
    assert dto.parts[1].slot == "cpu"
    assert dto.total_price_inr == 110000
    assert dto.summary == "Two-part card."
    assert dto.warnings == ["No compatible cooler within budget."]
    assert not hasattr(dto, "changed_slots")
    assert "changed_slots" not in dto.model_dump()


# ---------------------------------------------------------------------------
# map_build_status — queued / running
# ---------------------------------------------------------------------------

def test_map_build_status_queued():
    record = _make_record(status="queued")
    resp = map_build_status(record)

    assert resp.build_id == record.build_id
    assert resp.status == "queued"
    assert resp.poll_after_ms == 2000
    assert resp.verdict is None
    assert resp.build is None
    assert resp.error is None
    assert resp.reason is None


def test_map_build_status_running():
    record = _make_record(status="running", started_at=_now())
    resp = map_build_status(record)

    assert resp.status == "running"
    assert resp.poll_after_ms == 2000
    assert resp.verdict is None
    assert resp.build is None
    assert resp.error is None
    assert resp.reason is None


# ---------------------------------------------------------------------------
# map_build_status — succeeded
# ---------------------------------------------------------------------------

def test_map_build_status_succeeded_uses_record_warnings_not_build_card_warnings():
    """The Neo4j-degraded notice BuildService synthesizes lives only in
    record.warnings (the merged list _classify returns) — build_card.warnings
    on the state object never gets that notice appended. Confirms the client
    actually receives it."""
    neo4j_notice = (
        "Compatibility graph was unavailable; parts were selected on catalog data "
        "only -- cross-compatibility and fitness checks were skipped."
    )
    card = _make_build_card(warnings=["No compatible cooler within budget."])
    verdict = _make_verdict(verdict="comfortable")
    record = _make_record(
        status="succeeded",
        state={"build_card": card, "feasibility_verdict": verdict},
        warnings=["No compatible cooler within budget.", neo4j_notice],
    )

    resp = map_build_status(record)

    assert resp.status == "succeeded"
    assert resp.build is not None
    assert resp.build.warnings == ["No compatible cooler within budget.", neo4j_notice]
    assert resp.verdict is not None
    assert resp.verdict.verdict == "comfortable"
    assert resp.poll_after_ms is None
    assert resp.error is None
    assert resp.reason is None


def test_map_build_status_succeeded_verdict_absent_handled_gracefully():
    card = _make_build_card()
    record = _make_record(
        status="succeeded",
        state={"build_card": card},
        warnings=[],
    )

    resp = map_build_status(record)

    assert resp.status == "succeeded"
    assert resp.build is not None
    assert resp.verdict is None


# ---------------------------------------------------------------------------
# map_build_status — infeasible
# ---------------------------------------------------------------------------

def test_map_build_status_infeasible():
    verdict = _make_verdict(
        verdict="impossible",
        reason="Even the cheapest compatible build exceeds the ceiling.",
        binding_constraint="cpu",
        suggested_adjustments=["Raise ceiling"],
    )
    record = _make_record(
        status="infeasible",
        state={"feasibility_verdict": verdict},
    )

    resp = map_build_status(record)

    assert resp.status == "infeasible"
    assert resp.verdict is not None
    assert resp.verdict.verdict == "impossible"
    assert resp.verdict.reason == "Even the cheapest compatible build exceeds the ceiling."
    assert resp.verdict.binding_constraint == "cpu"
    assert resp.build is None
    assert resp.error is None
    assert resp.reason is None


# ---------------------------------------------------------------------------
# map_build_status — cannot_proceed
# ---------------------------------------------------------------------------

def test_map_build_status_cannot_proceed():
    record = _make_record(
        status="cannot_proceed",
        error_message="Cannot proceed: required information was never provided.",
    )

    resp = map_build_status(record)

    assert resp.status == "cannot_proceed"
    assert resp.reason == "Cannot proceed: required information was never provided."
    assert resp.build is None
    assert resp.verdict is None
    assert resp.error is None


# ---------------------------------------------------------------------------
# map_build_status — failed, retryable per error_code
# ---------------------------------------------------------------------------

def test_map_build_status_failed_build_timeout_is_retryable():
    record = _make_record(
        status="failed",
        error_code="BUILD_TIMEOUT",
        error_message="The build did not complete within the allotted time.",
    )
    resp = map_build_status(record)

    assert resp.status == "failed"
    assert resp.error is not None
    assert resp.error.code == "BUILD_TIMEOUT"
    assert resp.error.retryable is True
    assert resp.build is None
    assert resp.verdict is None
    assert resp.reason is None


def test_map_build_status_failed_internal_error_is_not_retryable():
    record = _make_record(
        status="failed",
        error_code="INTERNAL_ERROR",
        error_message="An internal error occurred: RuntimeError.",
    )
    resp = map_build_status(record)

    assert resp.status == "failed"
    assert resp.error.code == "INTERNAL_ERROR"
    assert resp.error.retryable is False


def test_map_build_status_failed_llm_upstream_error_is_retryable():
    record = _make_record(
        status="failed",
        error_code="LLM_UPSTREAM_ERROR",
        error_message="The upstream language model call failed: OpenAIError.",
    )
    resp = map_build_status(record)
    assert resp.error.retryable is True


def test_map_build_status_failed_degraded_dependency_is_retryable():
    record = _make_record(
        status="failed",
        error_code="DEGRADED_DEPENDENCY",
        error_message="Postgres was unreachable mid-build.",
    )
    resp = map_build_status(record)
    assert resp.error.retryable is True
