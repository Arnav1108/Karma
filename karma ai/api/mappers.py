"""Mappers from core domain objects to API DTOs.

No FastAPI imports here — unit-testable without spinning up the app. See
karma ai/docs/intake_routes_plan.md section 4 for the full contract this file
implements.
"""

from __future__ import annotations

from agents.nodes.node1_intake import QUESTION_SEQUENCE, IntakeQuestion, IntakeSessionState, floor_met
from agents.schemas.brief import UserBuildBrief
from agents.schemas.build_card import BuildCard, BuildCardPart
from agents.schemas.feasibility import FeasibilityVerdict

from api.dtos import (
    BriefSummaryDTO,
    BuildCardDTO,
    BuildPartDTO,
    BuildStatusResponse,
    ErrorBody,
    PeripheralDTO,
    ProgressDTO,
    QuestionDTO,
    ReusePartDTO,
    SecondaryUseCaseDTO,
    SoftwareEntryDTO,
    SpecificPartRequestDTO,
    VerdictDTO,
)
from api.services.job_registry import JobRecord

# error_code -> retryable, for the "failed" build status. Infra flaps
# (timeout, upstream LLM, a degraded dependency) are worth retrying; an
# unclassified internal error is not.
_RETRYABLE_ERROR_CODES = {
    "BUILD_TIMEOUT": True,
    "LLM_UPSTREAM_ERROR": True,
    "DEGRADED_DEPENDENCY": True,
    "DATABASE_UNAVAILABLE": True,
    "INTERNAL_ERROR": False,
}

BUILD_POLL_AFTER_MS = 2000


def map_question(q: IntakeQuestion) -> QuestionDTO:
    return QuestionDTO(question_id=q.question_id, text=q.text, kind=q.kind)


def map_progress(state: IntakeSessionState, brief: UserBuildBrief) -> ProgressDTO:
    return ProgressDTO(
        answered=len(set(state.asked_so_far)),
        total=len(QUESTION_SEQUENCE),
        floor_met=floor_met(brief),
    )


def _map_monitor_specs(brief: UserBuildBrief) -> str | None:
    if brief.monitor.owned == "yes" and brief.monitor.owned_specs:
        s = brief.monitor.owned_specs
        return f"{s.resolution} @ {s.refresh_hz}Hz" + (" HDR" if s.hdr else "")
    if brief.monitor.target_specs:
        t = brief.monitor.target_specs
        return f"{t.resolution} @ {t.refresh_hz}Hz" + (" HDR" if t.hdr else "")
    return None


def map_brief_summary(brief: UserBuildBrief, asked_so_far: list[str]) -> BriefSummaryDTO:
    asked_set = set(asked_so_far)
    answered_fields = [q.id for q in QUESTION_SEQUENCE if q.id in asked_set]

    budget = {
        "comfortable_min": brief.budget.comfortable_min,
        "comfortable_max": brief.budget.comfortable_max,
        "ceiling": brief.budget.ceiling,
        "scope": brief.budget.scope,
        "currency": brief.budget.currency,
        "notes": brief.budget.notes,
    }

    purpose = {
        "primary_use_case": brief.purpose.primary_use_case,
        "sub_case": brief.purpose.sub_case,
        "secondary_use_cases": [
            SecondaryUseCaseDTO(use_case=s.use_case, weight=s.weight)
            for s in brief.purpose.secondary_use_cases
        ],
    }

    software = [
        SoftwareEntryDTO(
            name=s.name, category=s.category, frequency=s.frequency, intensity=s.intensity
        )
        for s in brief.software
    ]

    performance = {
        "target_resolution": brief.performance.target_resolution,
        "target_framerate": brief.performance.target_framerate,
        "hdr_wanted": brief.performance.hdr_wanted,
        "source": brief.performance.source,
    }

    monitor = {
        "owned": brief.monitor.owned,
        "specs": _map_monitor_specs(brief),
        "count": brief.monitor.count,
        "source": brief.monitor.source,
    }

    peripherals = [
        PeripheralDTO(type=p.type, requirements=p.requirements, priority=p.priority)
        for p in brief.peripherals
    ]

    storage = {
        "capacity_gb": brief.storage.capacity_gb,
        "speed_tier": brief.storage.speed_tier,
        "data_profile": brief.storage.data_profile,
        "source": brief.storage.source,
    }

    operating_system = {
        "os": brief.operating_system.os,
        "license": brief.operating_system.license,
        "source": brief.operating_system.source,
    }

    reuse_parts = [
        ReusePartDTO(slot=r.slot, identifier=r.identifier, action=r.action)
        for r in brief.existing.reuse_parts
    ]
    brand_prefs = {
        "cpu": brief.existing.ecosystem_prefs.cpu_brand_pref,
        "gpu": brief.existing.ecosystem_prefs.gpu_brand_pref,
    }

    physical = {
        "form_factor_pref": brief.physical.form_factor_pref,
        "noise_tolerance": brief.physical.noise_tolerance,
        "placement": brief.physical.placement,
        "portability_need": brief.physical.portability_need,
    }

    longevity = {
        "reliability_priority": brief.longevity.reliability_priority,
        "upgrade_path": brief.longevity.upgrade_path,
        "timeline": brief.longevity.timeline,
    }

    extras = {
        "rgb_pref": brief.extras.rgb_pref,
        "visual_style": brief.extras.visual_style,
        "connectivity_needs": brief.extras.connectivity_needs,
        "specific_part_requests": [
            SpecificPartRequestDTO(slot=r.slot, requested=r.requested)
            for r in brief.extras.specific_part_requests
        ],
    }

    hard_constraints = {
        "must_have": [c.value for c in brief.hard_constraints.must_have],
        "must_not": [c.value for c in brief.hard_constraints.must_not],
    }

    return BriefSummaryDTO(
        answered_fields=answered_fields,
        completeness=brief.completeness.model_dump(),
        budget=budget,
        purpose=purpose,
        software=software,
        performance=performance,
        monitor=monitor,
        peripherals=peripherals,
        storage=storage,
        operating_system=operating_system,
        reuse_parts=reuse_parts,
        brand_prefs=brand_prefs,
        physical=physical,
        longevity=longevity,
        extras=extras,
        hard_constraints=hard_constraints,
    )


# ---------------------------------------------------------------------------
# Build route mappers — see karma ai/docs/build_service_plan.md sections 6-7.
# ---------------------------------------------------------------------------

def map_verdict(verdict: FeasibilityVerdict) -> VerdictDTO:
    """basis is internal diagnostics (deterministic/llm_fallback/stub) — dropped."""
    return VerdictDTO(
        verdict=verdict.verdict,
        reason=verdict.reason,
        binding_constraint=verdict.binding_constraint,
        suggested_adjustments=list(verdict.suggested_adjustments),
    )


def map_build_part(part: BuildCardPart) -> BuildPartDTO:
    return BuildPartDTO(
        slot=part.slot.value,
        product_id=part.product_id,
        name=part.name,
        brand=part.brand,
        price_inr=part.price_inr,
        justification=part.justification,
    )


def map_build_card(card: BuildCard) -> BuildCardDTO:
    """changed_slots is omitted (v1, refinement-only, always empty on a fresh build)."""
    return BuildCardDTO(
        parts=[map_build_part(p) for p in card.parts],
        total_price_inr=card.total_price_inr,
        summary=card.summary,
        warnings=list(card.warnings),
    )


def map_build_status(record: JobRecord) -> BuildStatusResponse:
    """Discriminate on record.status per plan section 7's exact table.

    The succeeded branch uses record.warnings, not build_card.warnings —
    BuildService._classify already returns the full, merged warnings list
    (per-slot dead-ends plus the synthesized Neo4j-degraded notice when
    applicable), so re-reading build_card.warnings here would silently drop
    that notice.
    """
    if record.status in ("queued", "running"):
        return BuildStatusResponse(
            build_id=record.build_id,
            status=record.status,
            poll_after_ms=BUILD_POLL_AFTER_MS,
        )

    if record.status == "succeeded":
        state = record.state or {}
        build_card = state.get("build_card")
        verdict = state.get("feasibility_verdict")
        card_dto = map_build_card(build_card).model_copy(update={"warnings": list(record.warnings)})
        return BuildStatusResponse(
            build_id=record.build_id,
            status="succeeded",
            build=card_dto,
            verdict=map_verdict(verdict) if verdict is not None else None,
        )

    if record.status == "infeasible":
        state = record.state or {}
        verdict = state.get("feasibility_verdict")
        return BuildStatusResponse(
            build_id=record.build_id,
            status="infeasible",
            verdict=map_verdict(verdict) if verdict is not None else None,
        )

    if record.status == "cannot_proceed":
        return BuildStatusResponse(
            build_id=record.build_id,
            status="cannot_proceed",
            reason=record.error_message,
        )

    # failed
    return BuildStatusResponse(
        build_id=record.build_id,
        status="failed",
        error=ErrorBody(
            code=record.error_code or "INTERNAL_ERROR",
            message=record.error_message or "An internal error occurred.",
            retryable=_RETRYABLE_ERROR_CODES.get(record.error_code, False),
        ),
    )
