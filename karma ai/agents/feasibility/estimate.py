"""Step 3 of the Feasibility Check (DESIGN.md 2.2) - the cost-vs-budget verdict.

Turns the deterministic resolved requirements (steps 1 & 2, resolver.py) into a
three-state `FeasibilityVerdict`.

Primary path (catalog reachable): the verdict is DETERMINISTIC. catalog_floor.py
computes the cheapest complete compatible in-stock build meeting the resolved
floors (the same primitive Node 2 uses to repair its price bands), and the
verdict falls out of comparing that floor to the core budget pools from
agents/costs.py (the same pools Node 2 allocates). The LLM only writes the
prose (reason / binding constraint / suggested adjustments) — it cannot flip
the verdict. Calibrated empirically by scripts/calibration_sweep.py.

Fallback path (catalog unreachable): the legacy single-anchor LLM estimate.
Sweep evidence (2026-07-02): the LLM-guessed verdict flipped between identical
consecutive runs (ml_workstation: tight → impossible) and missed on both sides
(said tight for a build ₹2.5k past its ceiling, and tight for one with 18%
headroom). It remains only as an honest degraded mode.

Public surface:
    estimate_feasibility(brief: UserBuildBrief) -> FeasibilityVerdict
"""

from __future__ import annotations

import logging

from .. import costs as _costs
from ..db.postgres import PostgresClient
from ..llm.client import call_structured
from ..schemas.brief import UserBuildBrief
from ..schemas.feasibility import FeasibilityVerdict
from ..schemas.slots import ComponentSlot
from .catalog_floor import CatalogFloor, compute_catalog_floor
from .resolver import (
    CpuTier,
    GpuTier,
    ResolvedRequirements,
    ScopeAdjustments,
    aggregate_scope,
    resolve_requirements,
)

logger = logging.getLogger(__name__)

# Verdict threshold, calibrated by scripts/calibration_sweep.py (2026-07-02):
# a build is "tight" when the cheapest viable build consumes more than this
# share of the core TARGET pool. Empirical anchors from the sweep:
#   budget_gamer     min/target = 1.04  → tight        (buildable at ceiling only)
#   edge_intel_gamer min/target = 0.82  → comfortable  (18% headroom at target)
# Any value in (0.82, 1.04) separates today's profiles; 0.85 keeps "comfortable"
# meaning real upgrade headroom, not just barely-fits. Re-run the sweep before
# changing this.
_TIGHT_RATIO = 0.85

_SYSTEM = (
    "You are the Feasibility Check, a lightweight pre-build gate for a PC-building "
    "assistant in India (prices in INR). Your ONLY job is to answer, roughly: can the "
    "user's resolved hardware floor be built within their budget? You do NOT validate a "
    "complete build, do NOT search inventory, and do NOT pick specific parts - that is a "
    "later stage. You are honest that this is an estimate.\n\n"
    "You are given the resolved component floor, the full budget picture, the aggregated "
    "non-component (scope) total, and exactly ONE live catalog price anchor: the current "
    "minimum in-stock price of the single binding component. Treat that anchor as your only "
    "live data point; reason about every other part's cost from general knowledge of "
    "current Indian PC-parts pricing.\n\n"
    "Return a verdict:\n"
    "  - 'comfortable': the budget has meaningful headroom above the estimated floor.\n"
    "  - 'tight': buildable, but little flexibility; expect compromises.\n"
    "  - 'impossible': the estimated floor materially exceeds the ceiling.\n\n"
    "Set 'binding_constraint' to the component or requirement that drives the cost (usually "
    "the GPU/VRAM, sometimes the CPU). For 'tight' or 'impossible', give concrete "
    "'suggested_adjustments' (e.g. raise budget, lower target resolution, relax form-factor "
    "or brand constraints). Keep 'reason' brief and concrete."
)

_SYSTEM_PROSE = (
    "You are the Feasibility Check narrator for a PC-building assistant in India "
    "(prices in INR). The feasibility verdict has ALREADY been computed "
    "deterministically from live catalog stock - you must NOT change it. Your job "
    "is to explain it: write a brief, concrete 'reason', name the "
    "'binding_constraint' (the component or requirement that drives the cost), "
    "and for 'tight' or 'impossible' verdicts give concrete 'suggested_adjustments' "
    "(e.g. raise budget, relax a brand preference, lower target resolution). "
    "Return the given verdict verbatim in the 'verdict' field."
)


# ── Deterministic path ────────────────────────────────────────────────────────

def _deterministic_verdict(
    floor: CatalogFloor, core_target: int, core_ceiling: int
) -> tuple[str, str]:
    """(verdict, basis) from the catalog floor vs the shared core budget pools.

    Brand preferences (ecosystem_prefs) are PREFERENCES, not hard constraints
    (those live in hard_constraints.must_have): a build that only fits the
    ceiling after relaxing them is 'tight', not 'impossible'.
    """
    soft, hard = floor.soft_total, floor.hard_total
    if soft is None or soft > core_ceiling:
        return "impossible", (
            f"cheapest compatible build meeting the floors costs "
            f"INR {soft:,} — above the core ceiling INR {core_ceiling:,}"
            if soft is not None else
            "no complete compatible in-stock build satisfies the resolved floors"
        )
    if hard is None or hard > core_ceiling:
        return "tight", (
            f"honouring brand preferences the cheapest build costs "
            f"{'INR {:,}'.format(hard) if hard is not None else 'more than stock allows'} "
            f"(over the core ceiling INR {core_ceiling:,}); relaxing them it costs "
            f"INR {soft:,}, which fits"
        )
    if hard > _TIGHT_RATIO * core_target:
        return "tight", (
            f"cheapest viable build INR {hard:,} consumes "
            f"{hard / core_target:.0%} of the core target INR {core_target:,}"
        )
    return "comfortable", (
        f"cheapest viable build INR {hard:,} leaves "
        f"{1 - hard / core_target:.0%} headroom under the core target INR {core_target:,}"
    )


def _floor_parts_line(floor: CatalogFloor) -> str:
    parts = floor.hard_parts or floor.soft_parts
    if not parts:
        return "(none)"
    return ", ".join(
        f"{s.value}={p['name']} INR {p['price_inr']:,}" for s, p in parts.items()
    )


def _prose_prompt(
    brief: UserBuildBrief,
    req: ResolvedRequirements,
    floor: CatalogFloor,
    verdict: str,
    basis: str,
    core_target: int,
    core_ceiling: int,
) -> str:
    b = brief.budget
    hard_line = f"INR {floor.hard_total:,}" if floor.hard_total is not None else "not buildable"
    soft_line = f"INR {floor.soft_total:,}" if floor.soft_total is not None else "not buildable"
    return (
        f"COMPUTED VERDICT (deterministic, from live catalog stock): {verdict}\n"
        f"  Basis: {basis}\n\n"
        "LIVE CATALOG FLOOR (cheapest complete compatible in-stock build):\n"
        f"  - Honouring brand preferences: {hard_line}\n"
        f"  - Preferences relaxed:         {soft_line}\n"
        f"  - Floor build parts: {_floor_parts_line(floor)}\n\n"
        "RESOLVED HARDWARE FLOOR:\n"
        f"  - GPU tier {req.gpu_tier.name}, CPU tier {req.cpu_tier.name}, "
        f"VRAM {req.vram_gb} GB, RAM {req.ram_gb} GB, storage {req.storage_gb} GB\n"
        f"  - Brand constraints: {', '.join(req.brand_constraints) or '(none)'}\n"
        f"  - Reused slots: {', '.join(s.value for s in req.reused_slots) or '(none)'}\n\n"
        "BUDGET PICTURE:\n"
        f"  - Comfortable range: {b.comfortable_min:,} - {b.comfortable_max:,}; "
        f"hard ceiling: {b.ceiling:,} ({b.scope})\n"
        f"  - Core component pools after fixed costs: target INR {core_target:,}, "
        f"ceiling INR {core_ceiling:,}\n\n"
        f"Write the reason, binding_constraint and suggested_adjustments for this "
        f"'{verdict}' verdict."
    )


# ── Legacy single-anchor fallback (catalog unreachable) ───────────────────────

def _binding_slot(req: ResolvedRequirements) -> ComponentSlot:
    """Pick the single cost-driving component.

    Default to the GPU (the cost driver for essentially all graphics/ML workloads).
    Fall back to the CPU only when GPU demand is minimal but CPU demand is high -
    i.e. a heavy-compute, non-graphics workload.
    """
    if req.gpu_tier <= GpuTier.entry and req.cpu_tier >= CpuTier.high:
        return ComponentSlot.cpu
    return ComponentSlot.gpu


def _fetch_anchor(binding_slot: ComponentSlot) -> tuple[int, str | None]:
    """Pull the one live price anchor, degrading honestly if Postgres is unreachable.

    Returns (price_inr, error_note). On any DB failure we return (0, reason) rather than
    aborting the whole feasibility estimate: the LLM can still reason from general pricing,
    and the prompt makes clear the anchor was unavailable (never that the part is free).
    """
    try:
        return PostgresClient().get_min_catalog_price(binding_slot), None
    except Exception as exc:  # noqa: BLE001 - best-effort live anchor; never abort the gate
        return 0, f"{type(exc).__name__}: {exc}"


def _build_prompt(
    brief: UserBuildBrief,
    req: ResolvedRequirements,
    scope: ScopeAdjustments,
    binding_slot: ComponentSlot,
    anchor_inr: int,
    anchor_error: str | None = None,
) -> str:
    """Assemble the §2.2 inputs into one prompt for the LLM."""
    b = brief.budget

    if anchor_inr > 0:
        anchor_line = (
            f"{binding_slot.value}: cheapest in-stock catalog price = "
            f"INR {anchor_inr:,} (LIVE anchor)"
        )
    elif anchor_error:
        anchor_line = (
            f"{binding_slot.value}: live price UNAVAILABLE (catalog lookup failed: "
            f"{anchor_error}) - estimate this component from general pricing knowledge too"
        )
    else:
        anchor_line = (
            f"{binding_slot.value}: no live price available (catalog returned none) - "
            f"estimate this component from general pricing knowledge too"
        )

    constraints = req.live_constraints or ["(none)"]
    brands = req.brand_constraints or ["(none)"]
    reused = [s.value for s in req.reused_slots] or ["(none)"]

    return (
        "RESOLVED HARDWARE FLOOR (peak demand across the whole workload):\n"
        f"  - GPU tier: {req.gpu_tier.name}\n"
        f"  - CPU tier: {req.cpu_tier.name}\n"
        f"  - VRAM: {req.vram_gb} GB\n"
        f"  - System RAM: {req.ram_gb} GB\n"
        f"  - Storage: {req.storage_gb} GB\n"
        f"  - Form factor: {req.form_factor or 'no preference'}\n"
        f"  - Brand constraints: {', '.join(brands)}\n"
        f"  - Reused slots (cost zeroed, constraints live): {', '.join(reused)}\n"
        f"  - Floor-shaping notes: {'; '.join(constraints)}\n\n"
        "BUDGET PICTURE:\n"
        f"  - Currency: {b.currency}\n"
        f"  - Comfortable range: {b.comfortable_min:,} - {b.comfortable_max:,}\n"
        f"  - Hard ceiling: {b.ceiling:,}\n"
        f"  - Scope: {b.scope}\n\n"
        "NON-COMPONENT (SCOPE) TOTAL:\n"
        f"  - Net add-ons minus reused-part savings: INR {scope.total_inr:,}\n\n"
        "LIVE PRICE ANCHOR (the one binding component):\n"
        f"  - {anchor_line}\n\n"
        "Estimate the total build cost against this budget and return the feasibility "
        "verdict."
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def estimate_feasibility(brief: UserBuildBrief) -> FeasibilityVerdict:
    """Run the full Feasibility Check and return a validated three-state verdict.

    1. Resolve the deterministic floor + scope total (steps 1 & 2).
    2. Compute the live catalog floor (cheapest complete compatible build).
       If available: verdict is deterministic; the LLM writes prose only.
    3. If the catalog is unreachable: legacy single-anchor LLM estimate.
    """
    req = resolve_requirements(brief)

    floor = compute_catalog_floor(brief, req)
    if floor is not None:
        _, core_target, core_ceiling = _costs.core_pools(brief)
        verdict, basis = _deterministic_verdict(floor, core_target, core_ceiling)
        logger.info("[Feasibility] deterministic verdict=%s (%s)", verdict, basis)
        prompt = _prose_prompt(brief, req, floor, verdict, basis, core_target, core_ceiling)
        result = call_structured(prompt, FeasibilityVerdict, system=_SYSTEM_PROSE)
        if result.verdict != verdict:
            logger.warning(
                "[Feasibility] LLM tried to change the verdict (%s → %s) — overridden",
                verdict, result.verdict,
            )
        # The verdict is code-owned on this path; the LLM only narrates.
        return result.model_copy(update={"verdict": verdict})

    # Degraded mode: catalog unreachable — single-anchor LLM estimate.
    logger.warning("[Feasibility] catalog floor unavailable — falling back to LLM estimate")
    scope = aggregate_scope(brief)
    binding_slot = _binding_slot(req)
    anchor_inr, anchor_error = _fetch_anchor(binding_slot)
    prompt = _build_prompt(brief, req, scope, binding_slot, anchor_inr, anchor_error)
    return call_structured(prompt, FeasibilityVerdict, system=_SYSTEM)
