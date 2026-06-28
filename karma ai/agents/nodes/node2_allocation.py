"""Node 2 — Budget Allocation Agent.

Architecture: LLM produces proportional weights (skew); Python computes exact INR
price bands using largest-remainder distribution on 500-INR tokens. Sum constraints
(lows==floor, mids==target, highs==ceiling) are guaranteed by construction — the LLM
never touches INR arithmetic.

Public surface:
    allocate_budget(brief: UserBuildBrief) -> PriceBands
"""

from __future__ import annotations

import math
from pydantic import RootModel

from ..llm.client import call_structured
from ..schemas.brief import UserBuildBrief
from ..schemas.price_bands import PriceBand, PriceBands
from ..schemas.slots import ComponentSlot


# ── Internal LLM response type ────────────────────────────────────────────────

class _AllocationSkew(RootModel[dict[ComponentSlot, float]]):
    """LLM-produced proportional weights. Need not sum to 1; Python normalizes."""


# ── Stub allocation profiles ──────────────────────────────────────────────────
# Weights (positive ints) per slot per use-case. Skew only — Python normalizes.
# "work_productivity_ml" is a derived sub-profile (not a primary_use_case value).
# STUB: Replace with data-backed weights once real build data is available.

_ALLOCATION_PROFILES: dict[str, dict[ComponentSlot, int]] = {
    "gaming": {
        ComponentSlot.gpu: 35,
        ComponentSlot.cpu: 20,
        ComponentSlot.ram: 8,
        ComponentSlot.storage: 7,
        ComponentSlot.motherboard: 10,
        ComponentSlot.psu: 7,
        ComponentSlot.case: 5,
        ComponentSlot.cooler: 5,
        ComponentSlot.fans: 3,
    },
    "content_creation": {
        ComponentSlot.gpu: 30,
        ComponentSlot.cpu: 20,
        ComponentSlot.ram: 10,
        ComponentSlot.storage: 15,
        ComponentSlot.motherboard: 10,
        ComponentSlot.psu: 7,
        ComponentSlot.case: 4,
        ComponentSlot.cooler: 3,
        ComponentSlot.fans: 1,
    },
    "work_productivity": {
        ComponentSlot.gpu: 10,
        ComponentSlot.cpu: 30,
        ComponentSlot.ram: 20,
        ComponentSlot.storage: 15,
        ComponentSlot.motherboard: 12,
        ComponentSlot.psu: 6,
        ComponentSlot.case: 4,
        ComponentSlot.cooler: 2,
        ComponentSlot.fans: 1,
    },
    "work_productivity_ml": {
        ComponentSlot.gpu: 40,
        ComponentSlot.cpu: 18,
        ComponentSlot.ram: 20,
        ComponentSlot.storage: 8,
        ComponentSlot.motherboard: 7,
        ComponentSlot.psu: 3,
        ComponentSlot.case: 2,
        ComponentSlot.cooler: 1,
        ComponentSlot.fans: 1,
    },
    "storage_homeserver": {
        ComponentSlot.gpu: 5,
        ComponentSlot.cpu: 15,
        ComponentSlot.ram: 15,
        ComponentSlot.storage: 40,
        ComponentSlot.motherboard: 10,
        ComponentSlot.psu: 8,
        ComponentSlot.case: 5,
        ComponentSlot.cooler: 1,
        ComponentSlot.fans: 1,
    },
    "general_use": {
        ComponentSlot.gpu: 20,
        ComponentSlot.cpu: 22,
        ComponentSlot.ram: 15,
        ComponentSlot.storage: 12,
        ComponentSlot.motherboard: 12,
        ComponentSlot.psu: 7,
        ComponentSlot.case: 5,
        ComponentSlot.cooler: 5,
        ComponentSlot.fans: 2,
    },
}

# ── Stub fixed-cost tables ────────────────────────────────────────────────────
# STUB: Replace with real market prices once catalog is seeded.

_OS_COST: dict[str, int] = {
    "oem": 1500,
    "retail": 13000,
    "byo": 0,
    "na": 0,
}

# Used when monitor is in scope (pc_plus_monitor / full_setup) and not owned.
# Keyed by resolution string from brief.monitor.target_specs.resolution.
# STUB: Replace with real market data.
_MONITOR_COST_BY_RESOLUTION: dict[str, int] = {
    "1080p": 10000,
    "1440p": 30000,
    "2560x1440": 30000,
    "4K": 55000,
    "3840x2160": 55000,
    "default": 20000,
}

# ── Software minimum spec stubs ───────────────────────────────────────────────
# STUB: At runtime, these should be fetched via web-search from authoritative
# sources (Steam, Epic Games, vendor pages). Hardcoded for Phase 1.
_SOFTWARE_SPECS: dict[str, str] = {
    "Valorant": "GPU: GTX 1050 Ti 4GB, CPU: i3-4150, RAM: 4GB",
    "CS2": "GPU: GTX 970, CPU: i5-7600K, RAM: 8GB",
    "GTA V": "GPU: GTX 660 2GB, CPU: i5-3470, RAM: 8GB",
    "DaVinci Resolve": "GPU: 2GB VRAM min (8GB+ recommended), CPU: 8 cores, RAM: 16GB",
    "Adobe Premiere Pro": "GPU: 4GB VRAM (CUDA/Metal), CPU: 8 cores, RAM: 16GB",
    "Blender": "GPU: 4GB VRAM, CPU: 8 cores, RAM: 16GB",
    "PyTorch with CUDA": "GPU: CUDA-capable NVIDIA (24GB VRAM+ for LLM training), RAM: 32GB+",
    "Stable Diffusion": "GPU: NVIDIA 8GB+ VRAM recommended, RAM: 16GB+",
    "VS Code": "CPU: dual-core, RAM: 4GB",
}


# ── Deterministic distribution ────────────────────────────────────────────────

def _distribute(total: int, weights: dict[ComponentSlot, float]) -> dict[ComponentSlot, int]:
    """Distribute `total` INR across slots by weights, all values in multiples of 500.

    Uses largest-remainder method on 500-INR tokens. Guarantees
    sum(result.values()) == total exactly (total must be a multiple of 500).
    """
    tw = sum(weights.values()) or 1.0
    n_units = total // 500
    raw = {s: n_units * w / tw for s, w in weights.items()}
    floored = {s: int(math.floor(raw[s])) for s in weights}
    remainder = n_units - sum(floored.values())
    fracs = sorted(weights, key=lambda s: raw[s] - floored[s], reverse=True)
    for i in range(max(0, remainder)):
        floored[fracs[i % len(fracs)]] += 1
    return {s: v * 500 for s, v in floored.items()}


def _compute_bands(
    skew: dict[ComponentSlot, float],
    floor: int,
    target: int,
    ceiling: int,
) -> PriceBands:
    """Convert proportional weights into PriceBands with exact sum guarantees.

    Distributes increments (not absolute values) so that:
    - low[s] = share of floor
    - mid[s] = low[s] + share of (target - floor)
    - high[s] = mid[s] + share of (ceiling - target)

    This ensures low <= mid <= high per slot (increments are non-negative) and
    sum(lows) == floor, sum(mids) == target, sum(highs) == ceiling exactly.
    All output values are multiples of 500.
    """
    lows = _distribute(floor, skew)
    mid_inc = _distribute(target - floor, skew)
    high_inc = _distribute(ceiling - target, skew)

    bands: dict[ComponentSlot, PriceBand] = {}
    for s in skew:
        lo = lows[s]
        mi = lo + mid_inc[s]
        hi = mi + high_inc[s]
        bands[s] = PriceBand(low=lo, mid=mi, high=hi)

    return PriceBands(root=bands)


# ── Deterministic pre-steps ───────────────────────────────────────────────────

def _build_shopping_list(brief: UserBuildBrief) -> list[ComponentSlot]:
    """Return component slots that need purchasing (excludes reused parts).

    brief.existing.reuse_parts[*].slot is already ComponentSlot (Pydantic-validated),
    so no string conversion is needed.
    """
    reused = {p.slot for p in brief.existing.reuse_parts if p.action == "keep"}
    return [s for s in ComponentSlot if s not in reused]


def _estimate_monitor_cost(brief: UserBuildBrief) -> int:
    """Return estimated monitor cost to subtract from core pool, or 0.

    Only applies when the budget scope includes a monitor AND the user does not
    already own one. Uses target_specs.resolution for a rough stub estimate.
    """
    scope = brief.budget.scope
    if scope not in ("pc_plus_monitor", "full_setup"):
        return 0
    if brief.monitor.owned == "yes":
        return 0

    resolution = "default"
    if brief.monitor.target_specs and brief.monitor.target_specs.resolution:
        resolution = brief.monitor.target_specs.resolution.lower()

    for key in _MONITOR_COST_BY_RESOLUTION:
        if key.lower() in resolution or resolution in key.lower():
            return _MONITOR_COST_BY_RESOLUTION[key]
    return _MONITOR_COST_BY_RESOLUTION["default"]


def _compute_fixed_costs(brief: UserBuildBrief) -> int:
    """Subtract OS license and monitor from total budget to get core component pool.

    Peripheral must-haves are NOT subtracted here — pricing them requires the
    catalog (Node 3's responsibility).
    """
    os_cost = _OS_COST.get(brief.operating_system.license, 0)
    monitor_cost = _estimate_monitor_cost(brief)
    return os_cost + monitor_cost


def _get_profile(brief: UserBuildBrief) -> dict[ComponentSlot, int]:
    """Select the allocation profile for this brief's use-case.

    Detects the ML sub-case under work_productivity and routes it to a
    GPU+RAM-heavy profile instead of the generic productivity weights.
    """
    use_case = brief.purpose.primary_use_case
    sub_case = brief.purpose.sub_case.lower()

    if use_case == "work_productivity" and "ml" in sub_case:
        return _ALLOCATION_PROFILES["work_productivity_ml"]

    return _ALLOCATION_PROFILES.get(use_case, _ALLOCATION_PROFILES["general_use"])


# ── Main entry point ──────────────────────────────────────────────────────────

def allocate_budget(brief: UserBuildBrief) -> PriceBands:
    """Produce price bands for all components that need purchasing.

    Pre-steps (deterministic):
      1. Build shopping list — exclude reused parts.
      2. Subtract fixed costs (OS, monitor) → core pool.

    LLM step:
      3. Ask for proportional weights only (no INR values).

    Post-step (deterministic):
      4. _compute_bands() → PriceBands with exact sum constraints.
    """
    # Step 1 — shopping list
    shopping_list = _build_shopping_list(brief)

    # Step 2 — core pool
    fixed = _compute_fixed_costs(brief)
    floor = max(0, brief.budget.comfortable_min - fixed)
    target = max(floor, brief.budget.comfortable_max - fixed)
    ceiling = max(target, brief.budget.ceiling - fixed)

    # Step 3 — LLM: produce proportional weights
    default_profile = _get_profile(brief)
    # Filter profile to shopping list (reused slots don't need allocation)
    profile_for_prompt = {s: default_profile.get(s, 5) for s in shopping_list}
    total_profile_weight = sum(profile_for_prompt.values()) or 1
    profile_pct = {
        s: round(w / total_profile_weight * 100, 1)
        for s, w in profile_for_prompt.items()
    }

    # Software hints (STUB — should be fetched via web-search at runtime)
    sw_lines: list[str] = []
    for entry in brief.software:
        spec = _SOFTWARE_SPECS.get(entry.name)
        if spec:
            sw_lines.append(f"  {entry.name}: {spec}")
    sw_section = "\n".join(sw_lines) if sw_lines else "  (no known specs for listed software)"

    slot_names = ", ".join(s.value for s in shopping_list)
    profile_lines = "\n".join(
        f"  {s.value}: {pct}%" for s, pct in profile_pct.items()
    )

    system = (
        "You are a PC component budget allocation expert for an Indian PC builder. "
        "Output a relative weight (positive float) for each component slot. "
        "Python will normalize these weights and compute all INR values — "
        "you must NOT output INR amounts. Slots not in the provided list must not appear."
    )

    prompt = f"""\
Assign relative allocation weights for a PC build across these slots: {slot_names}

Use-case: {brief.purpose.primary_use_case} / {brief.purpose.sub_case}
Budget range (after fixed costs of ₹{fixed:,}): ₹{floor:,} – ₹{ceiling:,} INR

Default allocation profile (use as baseline; adjust based on context below):
{profile_lines}

Brief context:
- Ecosystem preferences: CPU={brief.existing.ecosystem_prefs.cpu_brand_pref or "none"}, \
GPU={brief.existing.ecosystem_prefs.gpu_brand_pref or "none"}
- Hard constraints (must_have): {", ".join(c.type + "=" + c.value for c in brief.hard_constraints.must_have) or "none"}
- Performance target: {brief.performance.target_resolution or "N/A"} / {brief.performance.target_framerate or "N/A"} fps

Software minimum specs (STUB — authoritative sources not queried):
{sw_section}

Return a JSON object with exactly these keys: {slot_names}
Values must be positive floats representing relative weight. They need not sum to any value."""

    skew_result = call_structured(prompt, _AllocationSkew, system=system)

    # Filter skew to shopping list only (discard any extra keys the LLM added)
    skew = {s: v for s, v in skew_result.root.items() if s in shopping_list}

    # Ensure every shopping-list slot has a weight (fall back to profile default)
    for s in shopping_list:
        if s not in skew or skew[s] <= 0:
            skew[s] = float(default_profile.get(s, 1))

    # Step 4 — compute exact price bands deterministically
    return _compute_bands(skew, floor, target, ceiling)
