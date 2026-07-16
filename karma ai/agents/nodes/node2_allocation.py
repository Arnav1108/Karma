"""Node 2 — Budget Allocation Agent.

Architecture: LLM produces proportional weights (skew); Python computes exact INR
price bands using largest-remainder distribution on 500-INR tokens. Sum constraints
(lows==floor, mids==target, highs==ceiling) are guaranteed by construction — the LLM
never touches INR arithmetic.

Public surface:
    allocate_budget(brief: UserBuildBrief) -> PriceBands
"""

from __future__ import annotations

import logging
import math
from pydantic import RootModel

from .. import costs as _costs
from .. import software_specs
from ..feasibility.catalog_floor import compute_catalog_floor
from ..feasibility.resolver import resolve_requirements
from ..llm.client import call_structured
from ..schemas.brief import UserBuildBrief
from ..schemas.price_bands import PriceBand, PriceBands
from ..schemas.slots import ComponentSlot

logger = logging.getLogger(__name__)


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

# ── Fixed costs ───────────────────────────────────────────────────────────────
# Tables live in agents/costs.py, SHARED with the Feasibility Check so the core
# pool Node 2 allocates and the pool the verdict is judged against are the same.

# ── Software minimum specs ────────────────────────────────────────────────────
# Sourced via agents/software_specs.py — the same Postgres-cached, LLM-backed
# lookup the Feasibility Check's resolver.py uses, so the two never disagree.

def _build_software_hints(brief: UserBuildBrief) -> str:
    """Format each brief.software entry's resolved floor into a prompt hint line."""
    lines: list[str] = []
    for entry in brief.software:
        floor = software_specs.get_software_requirements(entry.name, entry.category)
        lines.append(
            f"  {entry.name}: GPU tier={floor.gpu_tier.name} (~{floor.vram_gb}GB VRAM), "
            f"CPU tier={floor.cpu_tier.name}, RAM={floor.ram_gb}GB"
        )
    return "\n".join(lines) if lines else "  (no software specified)"


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


# ── Catalog-grounding band repair ─────────────────────────────────────────────

def _ceil500(x: int) -> int:
    return -(-x // 500) * 500


def _repair_bands_to_catalog(
    bands: PriceBands,
    floor_prices: dict[ComponentSlot, int],
) -> PriceBands | None:
    """Adjust bands so every slot's [low, high] contains its min-viable-build part.

    floor_prices is the per-slot price of the cheapest COMPLETE compatible build
    that fits the core ceiling (from catalog_floor). Percent-based bands routinely
    miss the catalog's price cliffs (cheapest in-stock GPU ₹27.5k, the only ≥48GB
    RAM kit ₹22k, cheapest DDR5 LGA1700 board ₹15k — measured by
    scripts/calibration_sweep.py); this pass pins them back to real stock.

    Invariants preserved: sum(mid) == target, sum(high) == ceiling,
    low <= mid <= high. Deliberately relaxed: sum(low) may drop below the floor
    budget — band.low is a catalog query bound, and keeping it high excludes
    cheaper viable stock (e.g. DDR4 kits below an ≥₹80k gaming build's RAM low).

    Returns None when the highs cannot cover the floor prices within the ceiling
    (the verdict for such a brief is impossible; bands are left as computed).
    """
    high = {s: b.high for s, b in bands.root.items()}
    mid = {s: b.mid for s, b in bands.root.items()}
    low = {s: b.low for s, b in bands.root.items()}
    slots = [s for s in high if s in floor_prices]
    f500 = {s: _ceil500(floor_prices[s]) for s in slots}

    # 1 — raise deficient highs to the floor price, funded from surplus highs.
    deficit = {s: f500[s] - high[s] for s in slots if f500[s] > high[s]}
    if deficit:
        need = sum(deficit.values())
        avail = {s: high[s] - f500[s] for s in slots if high[s] > f500[s]}
        if sum(avail.values()) < need:
            return None
        for s in deficit:
            high[s] = f500[s]
        remaining = need
        while remaining > 0:
            donor = max(avail, key=lambda s: avail[s])
            take = min(500, remaining, avail[donor])
            high[donor] -= take
            avail[donor] -= take
            remaining -= take

    # 2 — clamp mids to the new highs; redistribute the clamped excess so
    #     sum(mid) == target still holds exactly.
    excess = 0
    for s in high:
        if mid[s] > high[s]:
            excess += mid[s] - high[s]
            mid[s] = high[s]
    while excess > 0:
        headroom = {s: high[s] - mid[s] for s in high if high[s] > mid[s]}
        if not headroom:  # sum(mid) <= sum(high) by construction; defensive only
            break
        s = max(headroom, key=lambda k: headroom[k])
        add = min(500, excess, headroom[s])
        mid[s] += add
        excess -= add

    # 3 — lows must not exclude the floor part (or invert the band ordering).
    for s in slots:
        low[s] = min(low[s], floor_prices[s])
    for s in high:
        low[s] = min(low[s], mid[s])

    return PriceBands(root={
        s: PriceBand(low=low[s], mid=mid[s], high=high[s]) for s in high
    })


# ── Deterministic pre-steps ───────────────────────────────────────────────────

def _build_shopping_list(brief: UserBuildBrief) -> list[ComponentSlot]:
    """Return component slots that need purchasing (excludes reused parts).

    brief.existing.reuse_parts[*].slot is already ComponentSlot (Pydantic-validated),
    so no string conversion is needed.
    """
    reused = {p.slot for p in brief.existing.reuse_parts if p.action == "keep"}
    return [s for s in ComponentSlot if s not in reused]


def _compute_fixed_costs(brief: UserBuildBrief) -> int:
    """Subtract OS license and monitor from total budget to get core component pool.

    Delegates to agents/costs.py — the same tables the Feasibility Check uses.
    Peripheral must-haves are NOT subtracted here — pricing them requires the
    catalog (Node 3's responsibility).
    """
    return _costs.core_fixed_costs(brief)


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

    Post-steps (deterministic):
      4. _compute_bands() → PriceBands with exact sum constraints.
      5. _repair_bands_to_catalog() → pin every band to real catalog stock via
         the min-viable-build floor (shared with the Feasibility Check). Skipped
         gracefully when the catalog is unreachable or nothing fits the ceiling.
    """
    # Step 1 — shopping list
    shopping_list = _build_shopping_list(brief)

    # Step 2 — core pool
    fixed = _compute_fixed_costs(brief)
    floor, target, ceiling = _costs.core_pools(brief)

    # Step 3 — LLM: produce proportional weights
    default_profile = _get_profile(brief)
    # Filter profile to shopping list (reused slots don't need allocation)
    profile_for_prompt = {s: default_profile.get(s, 5) for s in shopping_list}
    total_profile_weight = sum(profile_for_prompt.values()) or 1
    profile_pct = {
        s: round(w / total_profile_weight * 100, 1)
        for s, w in profile_for_prompt.items()
    }

    # Software hints — resolved floors from the shared software_specs lookup.
    sw_section = _build_software_hints(brief)

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

Software minimum specs:
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
    bands = _compute_bands(skew, floor, target, ceiling)

    # Step 5 — pin bands to real catalog stock (shared floor with feasibility)
    catalog_floor = compute_catalog_floor(brief)
    if catalog_floor is None:
        logger.warning(
            "[Node2] catalog unreachable — bands NOT repaired against real stock"
        )
        return bands

    viable = catalog_floor.best_within(ceiling)
    if viable is None:
        logger.warning(
            "[Node2] no complete compatible build fits the core ceiling ₹%d — "
            "bands left as allocated (feasibility verdict should be 'impossible')",
            ceiling,
        )
        return bands

    floor_prices = {s: p["price_inr"] for s, p in viable.items() if s in shopping_list}
    repaired = _repair_bands_to_catalog(bands, floor_prices)
    if repaired is None:
        logger.warning(
            "[Node2] band repair infeasible within ceiling ₹%d — bands left as allocated",
            ceiling,
        )
        return bands

    changed = [
        s.value for s in bands.root
        if bands.root[s] != repaired.root[s]
    ]
    if changed:
        logger.info("[Node2] bands repaired to catalog floor: %s", ", ".join(changed))
    return repaired
