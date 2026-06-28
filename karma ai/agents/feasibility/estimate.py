"""Step 3 of the Feasibility Check (DESIGN.md 2.2) - the LLM cost estimate.

Turns the deterministic resolved requirements (steps 1 & 2, resolver.py) into a
three-state `FeasibilityVerdict`. This is the ONLY network-touching piece of the
Feasibility Check: it pulls exactly ONE live price anchor from Postgres for the
binding component, then asks the LLM to reason about the rest from general Indian
PC-parts pricing. It does NOT search inventory or pick parts - that is Node Three.

Public surface:
    estimate_feasibility(brief: UserBuildBrief) -> FeasibilityVerdict
"""

from __future__ import annotations

from ..db.postgres import PostgresClient
from ..llm.client import call_structured
from ..schemas.brief import UserBuildBrief
from ..schemas.feasibility import FeasibilityVerdict
from ..schemas.slots import ComponentSlot
from .resolver import (
    CpuTier,
    GpuTier,
    ResolvedRequirements,
    ScopeAdjustments,
    aggregate_scope,
    resolve_requirements,
)

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


def estimate_feasibility(brief: UserBuildBrief) -> FeasibilityVerdict:
    """Run the full Feasibility Check and return a validated three-state verdict.

    1. Resolve the deterministic floor + scope total (steps 1 & 2).
    2. Pick the binding component and pull ONE live Postgres price anchor for it.
    3. Ask the LLM to reason about the rest and return a `FeasibilityVerdict`.
    """
    req = resolve_requirements(brief)
    scope = aggregate_scope(brief)

    binding_slot = _binding_slot(req)
    anchor_inr, anchor_error = _fetch_anchor(binding_slot)

    prompt = _build_prompt(brief, req, scope, binding_slot, anchor_inr, anchor_error)
    return call_structured(prompt, FeasibilityVerdict, system=_SYSTEM)
