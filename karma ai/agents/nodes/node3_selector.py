"""Node 3 — Part Finder & Recommender.

Selection sequence (locked, DESIGN.md §2.4):
  GPU → CPU → Motherboard → RAM → Storage → PSU → Case → Cooler → Fans

Per-slot three-step funnel:
  1. Catalog query  — Postgres get_parts_in_band (20% band-widening fallback)
  2. Graph filter   — Neo4j compatibility_check → fitness_filter (skipped if unavailable)
  3. LLM final pick — call_structured → SelectedPart

Public surface:
  SELECTION_ORDER                        list[ComponentSlot]
  FitnessThresholds                      Pydantic model (LLM output)
  SelectedPart                           Pydantic model (LLM output)
  derive_fitness_thresholds(brief)    -> dict[ComponentSlot, float]
  select_part(slot, band, brief, ...)  -> BuildCardPart | None
  select_build(brief, price_bands)    -> BuildCard
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import BaseModel

from ..db.neo4j import Neo4jClient
from ..db.postgres import PostgresClient
from ..feasibility.catalog_floor import (
    filter_rejected,
    required_psu_wattage,
    slot_requirement_filter,
)
from ..feasibility.resolver import ResolvedRequirements, resolve_requirements
from ..llm.client import THRESHOLD_MODEL, call_structured
from ..schemas.brief import UserBuildBrief
from ..schemas.build_card import BuildCard, BuildCardPart
from ..schemas.feasibility import FeasibilityVerdict
from ..schemas.price_bands import PriceBand, PriceBands
from ..schemas.slots import ComponentSlot

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

SELECTION_ORDER: list[ComponentSlot] = [
    ComponentSlot.gpu,
    ComponentSlot.cpu,
    ComponentSlot.motherboard,
    ComponentSlot.ram,
    ComponentSlot.storage,
    ComponentSlot.psu,
    ComponentSlot.case,
    ComponentSlot.cooler,
    ComponentSlot.fans,
]

_BAND_WIDEN_FACTOR = 0.20
_MAX_SHORTLIST = 7
# Upper bound for the full-catalog escalation query (compatibility never bypasses
# the price band silently — it escalates the band, then surfaces a dead-end).
_FULL_CATALOG_HIGH = 10**9


# ── Structured LLM output models ──────────────────────────────────────────────

class FitnessThresholds(BaseModel):
    """Per-slot fitness thresholds (0.0–1.0) produced by one upfront LLM call.

    Higher value = component is more critical for the use case → stricter
    Neo4j fitness_filter threshold applied during slot selection.
    """
    gpu: float
    cpu: float
    ram: float
    storage: float
    motherboard: float
    psu: float
    case: float
    cooler: float
    fans: float


class SelectedPart(BaseModel):
    """LLM's final pick from the shortlisted candidates for a single slot."""
    product_id: str
    justification: str


# ── Slot selection outcome ────────────────────────────────────────────────────

@dataclass
class SlotOutcome:
    """Result of attempting to fill one slot.

    status:
      "ok"            → part is a valid, floor-meeting, compatible, in-budget pick.
      "no_floor"      → no in-stock part meets the resolved requirement floor at
                        any price (real dead-end, message set).
      "no_stock"      → catalog has no in-stock candidate in the band (graph-off path).
      "no_compatible" → a floor-meeting part exists but none is compatible with
                        the locked parts (real dead-end, message set).
      "over_budget"   → a valid part exists but the cheapest one exceeds the
                        remaining budget-pool / ceiling (message set).

    specs carries the picked catalog row's specs JSONB (or None on a dead-end) so
    the caller can accumulate the locked build's TDP for the PSU wattage floor.
    """
    part: BuildCardPart | None = None
    status: str = "ok"
    message: str | None = None
    specs: dict | None = None


def _get_ddr_gen(candidate: dict) -> int | None:
    specs = candidate.get("specs")
    if isinstance(specs, dict):
        return specs.get("ddr_gen")
    return None


def _floor_filter(
    slot: ComponentSlot,
    parts: list[dict],
    req: ResolvedRequirements,
    brief: UserBuildBrief,
) -> list[dict]:
    """Drop parts that violate the slot's resolved requirement floor.

    Reuses catalog_floor.slot_requirement_filter — the SAME predicate that
    defines the ground-truth min-viable build the price bands are pinned to —
    so Node 3 can never pick a part the floor computation would have excluded.

    enforce_brand=False on purpose: ecosystem brand prefs are PREFERENCES, not
    floors. The feasibility gate defines a 'tight' verdict as buildable only
    after relaxing them, so hard-filtering brand here would dead-end builds the
    gate already declared feasible. Slots with no numeric/type floor
    (motherboard, psu, case, cooler, fans) pass through unchanged.
    """
    return slot_requirement_filter(slot, parts, req, brief, enforce_brand=False)


def _psu_wattage_filter(parts: list[dict], min_wattage: int) -> list[dict]:
    """Drop PSUs whose rated wattage can't power the locked build (+ headroom).

    The PSU analogue of the requirement floor: min_wattage is
    required_psu_wattage(locked_specs) — the same cpu_tdp+gpu_tdp+headroom bar the
    feasibility floor assumed a PSU could clear inside budget. Order-preserving.
    """
    return [p for p in parts if (p.get("specs") or {}).get("wattage", 0) >= min_wattage]


def _fetch_floor(
    pg: PostgresClient,
    slot: ComponentSlot,
    low: int,
    high: int,
    req: ResolvedRequirements,
    brief: UserBuildBrief,
    min_psu_wattage: int | None = None,
) -> list[dict]:
    """Catalog query with the requirement floor applied as a HARD filter.

    This is the query-layer enforcement point — the same layer where in-stock
    and price-band filtering already happen. A floor-violating part never leaves
    this function, so it can never reach the shortlist or the LLM pick. Every
    catalog fetch in the selection funnel (band, widened band, DDR4 pull, and
    both full-catalog escalations) routes through here, so the escalation ladder
    relaxes only the price band — never the floor. Rejected parts (from a prior
    refinement 'swap') are dropped at the same choke point, so a rejected
    product_id can never re-enter the shortlist for any slot on a later pass.

    For the PSU slot, min_psu_wattage adds a second hard floor — the wattage the
    locked GPU+CPU draw plus headroom — applied at this SAME choke point, so the
    price-band escalation ladder can never surface an underpowered PSU either.
    """
    parts = pg.get_parts_in_band(slot, low, high, in_stock=True)
    parts = filter_rejected(parts, brief)
    parts = _floor_filter(slot, parts, req, brief)
    if slot == ComponentSlot.psu and min_psu_wattage is not None:
        parts = _psu_wattage_filter(parts, min_psu_wattage)
    return parts


def _floor_desc(
    slot: ComponentSlot,
    req: ResolvedRequirements,
    brief: UserBuildBrief,
    min_psu_wattage: int | None = None,
) -> str:
    """Human-readable description of a slot's resolved floor (for dead-ends)."""
    if slot == ComponentSlot.gpu:
        return f"≥{req.vram_gb} GB VRAM"
    if slot == ComponentSlot.cpu:
        return f"CPU tier {req.cpu_tier.name}"
    if slot == ComponentSlot.ram:
        return f"≥{req.ram_gb} GB capacity"
    if slot == ComponentSlot.storage:
        return f"≥{req.storage_gb} GB, {brief.storage.speed_tier}"
    if slot == ComponentSlot.psu and min_psu_wattage is not None:
        return f"≥{min_psu_wattage} W for the locked build"
    return "no floor"


def _no_floor_message(
    slot: ComponentSlot,
    req: ResolvedRequirements,
    brief: UserBuildBrief,
    min_psu_wattage: int | None = None,
) -> str:
    return (
        f"No {slot.value} in stock meets the required floor "
        f"({_floor_desc(slot, req, brief, min_psu_wattage)}). Every in-catalog "
        f"{slot.value} falls below this minimum for your workload. Consider relaxing "
        f"the requirement or adjusting the target software / performance settings."
    )


def _ddr4_can_meet_ram_floor(brief: UserBuildBrief) -> bool:
    """True if an in-stock DDR4 kit satisfies the brief's resolved RAM floor.

    Gate for the tight-budget DDR4 bias: a verdict can be 'tight' for reasons
    unrelated to the memory platform (e.g. brand preferences on the GPU), and
    the catalog stocks no DDR4 kit above 32 GB — biasing a 48 GB+ build toward
    DDR4 would strand the RAM floor entirely (found by scripts/calibration_sweep.py).
    Fails open (True → old behaviour) when the catalog can't be read: with
    Postgres down, no candidates exist to bias anyway.

    Note: with Motherboard now locked before RAM in SELECTION_ORDER, this bias
    is additionally gated at the call site on the board not yet being locked
    (see select_build) — it only fires as a fallback when the board slot itself
    dead-ended, since RAM compatibility already resolves against a locked board.
    """
    try:
        req = resolve_requirements(brief)
        kits = PostgresClient().get_parts_in_band(
            ComponentSlot.ram, 0, _FULL_CATALOG_HIGH, in_stock=True
        )
    except Exception:  # noqa: BLE001 — degrade to pre-gate behaviour
        return True
    return any(
        _get_ddr_gen(k) == 4
        and (k.get("specs") or {}).get("capacity_gb", 0) >= req.ram_gb
        for k in kits
    )


def _ddr4_first(candidates: list[dict]) -> list[dict]:
    """Stable reorder: DDR4 parts first, then everything else.  Order within
    each group is preserved so price/fitness ranking is not otherwise disturbed."""
    ddr4 = [c for c in candidates if _get_ddr_gen(c) == 4]
    rest = [c for c in candidates if _get_ddr_gen(c) != 4]
    return ddr4 + rest


def _compatible_subset(
    neo4j: Neo4jClient,
    candidate_dicts: list[dict],
    locked_parts: dict[ComponentSlot, str],
    slot: ComponentSlot,
) -> list[dict]:
    """Return the compatibility-passing subset of candidate_dicts, preserving order."""
    if not candidate_dicts:
        return []
    ok = set(
        neo4j.compatibility_check(
            [c["product_id"] for c in candidate_dicts],
            locked_parts,
            slot,
        )
    )
    return [c for c in candidate_dicts if c["product_id"] in ok]


def _locked_desc(locked_parts: dict[ComponentSlot, str]) -> str:
    return ", ".join(f"{s.value}={pid}" for s, pid in locked_parts.items()) or "none"


def _no_compatible_message(
    slot: ComponentSlot, locked_parts: dict[ComponentSlot, str]
) -> str:
    return (
        f"No compatible {slot.value} exists anywhere in the catalog for your "
        f"already-selected parts ({_locked_desc(locked_parts)}). The selected "
        f"components share no valid socket/RAM-generation with any {slot.value} in "
        f"stock. Consider choosing a different CPU or RAM, or relaxing that requirement."
    )


def _over_budget_message(
    slot: ComponentSlot, cheapest_price: int, ceiling: int
) -> str:
    return (
        f"A compatible {slot.value} exists, but the cheapest option (₹{cheapest_price:,}) "
        f"would push the build past your ₹{ceiling:,} budget ceiling. Consider raising "
        f"the budget or choosing cheaper GPU/CPU anchors to free up room."
    )


# ── Fitness threshold cache ───────────────────────────────────────────────────

@dataclass
class ThresholdCache:
    """Mutable cache for derive_fitness_thresholds results.

    Create once at the start of a build session and thread through select_build
    and _select_build_with_pins so refinement restarts reuse the derived
    thresholds when the brief's use-case fields haven't changed.
    """
    thresholds: dict[ComponentSlot, float] | None = None
    key: dict | None = None


def _threshold_key(brief: UserBuildBrief) -> dict:
    """Cache key for derive_fitness_thresholds — exactly the seven brief fields it reads."""
    return {
        "primary_use_case": brief.purpose.primary_use_case,
        "sub_case": brief.purpose.sub_case,
        "secondary_use_cases": sorted(
            (s.use_case, s.weight) for s in brief.purpose.secondary_use_cases
        ),
        "software": sorted(
            (s.name, s.category, s.intensity) for s in brief.software
        ),
        "target_resolution": brief.performance.target_resolution,
        "target_framerate": brief.performance.target_framerate,
        "upgrade_path": brief.longevity.upgrade_path,
    }


# ── Fitness threshold derivation ──────────────────────────────────────────────

def derive_fitness_thresholds(brief: UserBuildBrief) -> dict[ComponentSlot, float]:
    """One LLM call upfront — returns a fitness threshold per component slot.

    Result is stored in build state by select_build and passed into every
    select_part call. Never re-derived per slot.
    """
    software_text = "; ".join(
        f"{s.name} ({s.category}, {s.intensity})" for s in brief.software
    ) or "none specified"
    secondary_text = "; ".join(
        f"{s.use_case} ({s.weight})" for s in brief.purpose.secondary_use_cases
    ) or "none"

    prompt = (
        "You are a PC component advisor. Given the user build brief below, assign a "
        "fitness threshold (0.0–1.0) to each of the nine component slots. The threshold "
        "captures how critical that slot is to the primary use case — higher means more "
        "critical and deserving of a stricter component bar.\n\n"
        f"Primary use case : {brief.purpose.primary_use_case}\n"
        f"Sub-case         : {brief.purpose.sub_case}\n"
        f"Secondary uses   : {secondary_text}\n"
        f"Software         : {software_text}\n"
        f"Budget           : ₹{brief.budget.comfortable_min:,}–₹{brief.budget.comfortable_max:,} "
        f"(ceiling ₹{brief.budget.ceiling:,})\n"
        f"Target           : {brief.performance.target_resolution} @ "
        f"{brief.performance.target_framerate} fps\n"
        f"Upgrade path     : {brief.longevity.upgrade_path}\n\n"
        "Return a JSON with float thresholds (0.0–1.0) for all nine slots: "
        "gpu, cpu, ram, storage, motherboard, psu, case, cooler, fans.\n"
        "Example (gaming, competitive FPS): "
        "gpu=0.85, cpu=0.65, ram=0.45, storage=0.35, motherboard=0.50, "
        "psu=0.40, case=0.20, cooler=0.30, fans=0.15"
    )
    result = call_structured(
        prompt, FitnessThresholds, model=THRESHOLD_MODEL, temperature=0
    )
    return {
        ComponentSlot.gpu: result.gpu,
        ComponentSlot.cpu: result.cpu,
        ComponentSlot.ram: result.ram,
        ComponentSlot.storage: result.storage,
        ComponentSlot.motherboard: result.motherboard,
        ComponentSlot.psu: result.psu,
        ComponentSlot.case: result.case,
        ComponentSlot.cooler: result.cooler,
        ComponentSlot.fans: result.fans,
    }


# ── Per-slot selection ────────────────────────────────────────────────────────

def select_part(
    slot: ComponentSlot,
    band: PriceBand,
    brief: UserBuildBrief,
    locked_parts: dict[ComponentSlot, str],
    fitness_thresholds: dict[ComponentSlot, float],
    neo4j_available: bool,
    req: ResolvedRequirements,
    remaining_budget: int | None = None,
    ddr4_bias: bool = False,
    min_psu_wattage: int | None = None,
) -> SlotOutcome:
    """Three-step funnel: Postgres catalog → Neo4j graph filter → LLM pick.

    TWO hard filters are applied at the catalog-query layer and are NEVER
    bypassed during escalation — only the price band (and later the fitness
    threshold) relax:
      • Requirement floor — GPU VRAM / CPU tier / RAM capacity / storage
        capacity+type from resolve_requirements(). A floor-violating part never
        reaches the shortlist (every fetch routes through _fetch_floor). For the
        PSU slot, min_psu_wattage is a THIRD hard floor at the same layer: the
        selected PSU must supply the locked build's TDP + headroom, so a build
        can't pass feasibility on a wattage assumption and then ship an
        underpowered PSU.
      • Compatibility against locked_parts (socket / DDR gen / form factor).

    If the (widened) band yields no candidate that passes both, the price band is
    escalated across the full catalog. Dead-ends: "no_floor" (no in-stock part
    meets the requirement floor at any price), "no_compatible" (floor-meeting
    parts exist but none is compatible with locked parts), "over_budget" (a
    valid part exists but the cheapest breaches the ceiling).

    Returns a SlotOutcome — inspect .part (None on any dead-end) and .status.
    """
    pg = PostgresClient()

    # ── Step 1: catalog query at the band, floor-filtered (20% widening retry) ─
    # _fetch_floor applies the resolved requirement floor as a HARD filter inside
    # the query layer, alongside in-stock and price-band filtering.
    candidates = _fetch_floor(pg, slot, band.low, band.high, req, brief, min_psu_wattage)
    if not candidates:
        widened_low = int(band.low * (1 - _BAND_WIDEN_FACTOR))
        widened_high = int(band.high * (1 + _BAND_WIDEN_FACTOR))
        logger.info(
            "[Node3] %s: no floor-meeting stock at [%d–%d]; widening band to [%d–%d]",
            slot.value, band.low, band.high, widened_low, widened_high,
        )
        candidates = _fetch_floor(
            pg, slot, widened_low, widened_high, req, brief, min_psu_wattage
        )

    # ── Step 1b: DDR4 preference bias (tight budget only) ───────────────────
    # Motherboard now locks before RAM in SELECTION_ORDER, so RAM compatibility
    # already resolves against a locked board's DDR generation (Step 2) — this
    # bias only reaches here as a fallback when the board slot itself dead-ended
    # (select_build gates ddr4_bias on ComponentSlot.motherboard not in
    # locked_parts). In that fallback case, an unbiased DDR5 pick could still
    # strand a later, independently-solved board slot toward a pricier DDR5
    # board (DDR5 LGA1700 boards cost significantly more than DDR4 equivalents).
    # Two-step logic:
    #   1. If no DDR4 exists in the current band, augment candidates with DDR4
    #      parts from [0, widened_high] — saving on RAM frees budget for the board.
    #   2. Stable DDR4-first reorder so the LLM shortlist sees DDR4 options first.
    # Graceful degradation: if the catalog has no DDR4 at all, the DDR5 list is
    # passed through unchanged — never a dead-end.
    if ddr4_bias:
        ddr4_in_band = [c for c in candidates if _get_ddr_gen(c) == 4]
        if not ddr4_in_band:
            # Band floor excludes cheaper DDR4 — pull from the full affordable range.
            # Floor-filtered: the DDR4 bias must not reintroduce a sub-floor kit
            # (e.g. a 16 GB DDR4 kit against a 32 GB floor).
            ddr4_ceiling = int(band.high * (1 + _BAND_WIDEN_FACTOR))
            extra_ddr4 = [
                c for c in _fetch_floor(pg, slot, 0, ddr4_ceiling, req, brief)
                if _get_ddr_gen(c) == 4
            ]
            if extra_ddr4:
                extra_ids = {c["product_id"] for c in extra_ddr4}
                candidates = extra_ddr4 + [c for c in candidates if c["product_id"] not in extra_ids]
                logger.info(
                    "[Node3] %s: tight-budget DDR4 pull — no DDR4 in band; "
                    "added %d DDR4 part(s) from below floor",
                    slot.value, len(extra_ddr4),
                )
        if candidates:
            ddr4_count = sum(1 for c in candidates if _get_ddr_gen(c) == 4)
            candidates = _ddr4_first(candidates)
            logger.info(
                "[Node3] %s: tight-budget DDR4 bias — %d DDR4 / %d total candidates",
                slot.value, ddr4_count, len(candidates),
            )

    # ── Step 2: Neo4j compatibility (HARD filter) + band escalation ───────────
    # compatibility_check "fails open" only for candidates ABSENT from the graph;
    # an in-graph candidate that shares no required spec with a locked part is a
    # genuine conflict and is dropped. We never restore dropped candidates. When
    # the band has zero compatible parts, we escalate the price band (not the
    # compatibility rule) across the full catalog before declaring a dead-end.
    compat_active = neo4j_available and bool(locked_parts)
    neo4j = Neo4jClient() if neo4j_available else None

    if compat_active:
        working = _compatible_subset(neo4j, candidates, locked_parts, slot)
        if not working:
            # Escalate the PRICE BAND across the full catalog. _fetch_floor keeps
            # the requirement floor a hard filter; _compatible_subset keeps
            # compatibility a hard filter. Neither is relaxed by escalation.
            full = _fetch_floor(pg, slot, 0, _FULL_CATALOG_HIGH, req, brief, min_psu_wattage)
            if not full:
                msg = _no_floor_message(slot, req, brief, min_psu_wattage)
                logger.warning("[Node3] %s: %s", slot.value, msg)
                return SlotOutcome(status="no_floor", message=msg)
            working = _compatible_subset(neo4j, full, locked_parts, slot)
            if not working:
                msg = _no_compatible_message(slot, locked_parts)
                logger.warning("[Node3] %s: %s", slot.value, msg)
                return SlotOutcome(status="no_compatible", message=msg)
            logger.info(
                "[Node3] %s: no compatible floor-meeting part within price band — "
                "escalated to full catalog, %d candidate(s) found",
                slot.value, len(working),
            )
    else:
        working = candidates
        if not working:
            # No compatibility rule applies (e.g. the first-locked slot), but the
            # floor still does. Escalate the price band across the full catalog
            # before declaring a dead-end — same ladder, floor never relaxed.
            full = _fetch_floor(pg, slot, 0, _FULL_CATALOG_HIGH, req, brief, min_psu_wattage)
            if not full:
                msg = _no_floor_message(slot, req, brief, min_psu_wattage)
                logger.warning("[Node3] %s: %s", slot.value, msg)
                return SlotOutcome(status="no_floor", message=msg)
            working = full
            logger.info(
                "[Node3] %s: no floor-meeting part within widened band — escalated "
                "to full catalog, %d candidate(s) found",
                slot.value, len(working),
            )

    # ── Step 2b: budget-pool ceiling check (DESIGN §2.4 drift safeguard) ───────
    # Distinguishes "compatible-but-unaffordable" from "no compatible part".
    if remaining_budget is not None:
        affordable = [c for c in working if int(c.get("price_inr", 0)) <= remaining_budget]
        if not affordable:
            cheapest = min(int(c.get("price_inr", 0)) for c in working)
            msg = _over_budget_message(slot, cheapest, brief.budget.ceiling)
            logger.warning("[Node3] %s: %s", slot.value, msg)
            return SlotOutcome(status="over_budget", message=msg)
        working = affordable

    # ── Step 2c: fitness ordering (relaxable — fail-open, never a hard cut) ────
    # fitness_ranked tracks whether `working`'s order actually reflects the fitness
    # signal, so Step 3's prompt can surface that ranking instead of silently
    # numbering a catalog-order list as if it meant something it doesn't.
    # This must come from FitnessRanking.is_real_ranking, not from ordered_ids being
    # non-empty: categories with zero GOOD_FOR coverage (motherboard, psu, case,
    # cooler, fans) still return a full ordered_ids list via fail-open, which would
    # make a truthiness check on the list itself always True and mislabel every
    # catalog-order passthrough as a real fitness ranking (see docs/context.md
    # open item 4 follow-up).
    fitness_ranked = False
    if neo4j_available:
        by_id = {c["product_id"]: c for c in working}
        ranking = neo4j.fitness_filter(
            list(by_id.keys()),
            brief.purpose.primary_use_case,
            fitness_thresholds[slot],
        )
        if ranking.ordered_ids:
            reordered = [by_id[pid] for pid in ranking.ordered_ids if pid in by_id]
            if reordered:
                working = reordered
                fitness_ranked = ranking.is_real_ranking
        else:
            logger.info(
                "[Node3] %s: fitness_filter returned empty (threshold %.2f) — keeping "
                "%d compatible part(s) unranked",
                slot.value, fitness_thresholds[slot], len(working),
            )

    shortlist_dicts = working[:_MAX_SHORTLIST]

    # ── Step 3: LLM final pick ────────────────────────────────────────────────
    # Step 3 previously had no visibility into fitness rank, so on high-threshold
    # builds it would independently "value-optimize" down from the top-fitness pick
    # to a cheaper, lower-tier one — a regression only exposed once fitness_filter
    # stopped hard-excluding low tiers and started handing it real alternatives
    # (docs/context.md open item 4). Surfacing the rank + threshold below makes
    # fitness a signal the LLM weighs, not one it's blind to — it is NOT a hard
    # pin to rank #1; a lower-ranked pick can still legitimately win on value.
    parts_text = "\n".join(
        "  [{idx}]{rank_tag} product_id={pid} | {name} by {brand} | ₹{price:,} | specs: {specs}".format(
            idx=i + 1,
            rank_tag=f" (fitness rank #{i + 1})" if fitness_ranked else "",
            pid=p.get("product_id", ""),
            name=p.get("name", "N/A"),
            brand=p.get("brand", "N/A"),
            price=int(p.get("price_inr", 0)),
            specs=p.get("specs") or p.get("key_specs") or "—",
        )
        for i, p in enumerate(shortlist_dicts)
    )
    constraints_text = "; ".join(
        f"{c.type}={c.value}" for c in brief.hard_constraints.must_have
    ) or "none"
    software_text = ", ".join(s.name for s in brief.software) or "none"

    fitness_context = ""
    if fitness_ranked:
        fitness_context = (
            f"  Fitness     : this build's derived requirement level for {slot.value} "
            f"is {fitness_thresholds[slot]:.2f} (0.0-1.0, higher = more critical to the "
            f"use case). Options are listed best-fit-first per that signal (not by "
            f"price) — weigh fitness rank alongside price/specs; don't default to a "
            f"lower-ranked option purely because it's cheaper, but it can still win on "
            f"genuine value.\n"
        )

    prompt = (
        f"You are a PC building expert. Select the single best {slot.value.upper()} "
        f"for this user build.\n\n"
        f"USER CONTEXT\n"
        f"  Use case    : {brief.purpose.primary_use_case} — {brief.purpose.sub_case}\n"
        f"  Software    : {software_text}\n"
        f"  Target      : {brief.performance.target_resolution} @ "
        f"{brief.performance.target_framerate} fps\n"
        f"  Constraints : {constraints_text}\n"
        f"{fitness_context}\n"
        f"SHORTLISTED {slot.value.upper()} OPTIONS ({len(shortlist_dicts)} parts"
        f"{', best-fit-first' if fitness_ranked else ''})\n"
        f"{parts_text}\n\n"
        "Pick the best value-for-money option for the use case, weighing the fitness "
        "signal above alongside price and specs. "
        "Return the exact product_id and a one-sentence justification."
    )
    picked = call_structured(prompt, SelectedPart)

    matched = next(
        (c for c in shortlist_dicts if c.get("product_id") == picked.product_id),
        None,
    )
    if matched is None:
        logger.warning(
            "[Node3] %s: LLM returned unknown product_id %r — falling back to first shortlist entry",
            slot.value, picked.product_id,
        )
        matched = shortlist_dicts[0]

    return SlotOutcome(
        part=BuildCardPart(
            slot=slot,
            product_id=matched["product_id"],
            name=matched.get("name", matched["product_id"]),
            price_inr=int(matched.get("price_inr", 0)),
            justification=picked.justification,
        ),
        status="ok",
        specs=matched.get("specs") if isinstance(matched.get("specs"), dict) else None,
    )


# ── Main orchestrator ─────────────────────────────────────────────────────────

def select_build(
    brief: UserBuildBrief,
    price_bands: PriceBands,
    feasibility_verdict: FeasibilityVerdict | None = None,
    cache: ThresholdCache | None = None,
) -> BuildCard:
    """Walk SELECTION_ORDER and fill each slot via the three-step funnel.

    Safeguards applied during the walk:
      • Requirement-floor hard filter: every catalog fetch drops parts below the
        slot's resolved floor (VRAM / CPU tier / RAM & storage capacity, storage
        type) before shortlisting — never a post-pick check.
      • Running budget-pool tracker: each slot is solved against the remaining
        headroom to the ceiling; a compatible-but-unaffordable slot dead-ends.
      • Lookahead probe after GPU + CPU lock: warns if no floor-meeting,
        compatible motherboard exists in the current band before continuing.
      • Post-lock compatibility validator: refuses to lock (does not merely log)
        any part Neo4j flags as conflicting with an already-locked part.
      • Dead-end surfacing: no_floor / no_compatible / over_budget outcomes are
        collected as plain-English warnings on the returned BuildCard.
    """
    # ── Upfront checks ────────────────────────────────────────────────────────
    neo4j_available: bool = Neo4jClient().ping()
    if not neo4j_available:
        logger.info("[Node3] Neo4j unreachable — graph filter disabled for all slots")

    # Resolved requirement floors, computed once and applied as a hard filter in
    # every catalog fetch (mirrors how compatibility thresholds are derived once).
    req = resolve_requirements(brief)

    tight_budget = (
        feasibility_verdict is not None
        and feasibility_verdict.verdict == "tight"
    )
    ddr4_bias = tight_budget and _ddr4_can_meet_ram_floor(brief)
    if tight_budget:
        logger.info(
            "[Node3] tight budget detected — DDR4 preference bias %s for RAM",
            "enabled" if ddr4_bias else
            "DISABLED (no in-stock DDR4 kit meets the resolved RAM floor)",
        )

    if cache is None:
        cache = ThresholdCache()
    current_key = _threshold_key(brief)
    if cache.thresholds is not None and cache.key == current_key:
        fitness_thresholds = cache.thresholds
        logger.info("[Node3] reusing cached fitness thresholds (brief unchanged)")
    else:
        fitness_thresholds = derive_fitness_thresholds(brief)
        cache.thresholds = fitness_thresholds
        cache.key = current_key
    logger.info(
        "[Node3] fitness thresholds: %s",
        {s.value: round(t, 2) for s, t in fitness_thresholds.items()},
    )

    # Slots the user is keeping from an existing build.
    reuse_slots = {r.slot for r in brief.existing.reuse_parts if r.action == "keep"}

    locked_parts: dict[ComponentSlot, str] = {}  # slot → product_id
    locked_specs: dict[ComponentSlot, dict] = {}  # slot → catalog specs (for PSU wattage)
    selected_parts: list[BuildCardPart] = []
    warnings: list[str] = []
    running_spend = 0

    for slot in SELECTION_ORDER:
        if slot in reuse_slots:
            logger.info("[Node3] %s: kept from existing build — skipping", slot.value)
            continue

        band = price_bands.root.get(slot)
        if band is None:
            logger.warning("[Node3] %s: no price band provided — skipping", slot.value)
            continue

        # ── Lookahead probe (after GPU + CPU are locked) ──────────────────────
        if (
            slot == ComponentSlot.motherboard
            and ComponentSlot.gpu in locked_parts
            and ComponentSlot.cpu in locked_parts
            and neo4j_available
        ):
            pg_probe = PostgresClient()
            mb_candidates = _fetch_floor(
                pg_probe, ComponentSlot.motherboard, band.low, band.high, req, brief
            )
            neo4j_probe = Neo4jClient()
            compatible_mb = neo4j_probe.compatibility_check(
                [c["product_id"] for c in mb_candidates],
                locked_parts,
                ComponentSlot.motherboard,
            )
            if not compatible_mb:
                logger.warning(
                    "[Node3] LOOKAHEAD WARNING: no motherboard in current band confirmed "
                    "compatible with GPU=%s + CPU=%s. Proceeding — LLM will pick best available.",
                    locked_parts.get(ComponentSlot.gpu),
                    locked_parts.get(ComponentSlot.cpu),
                )

        # ── Three-step funnel ─────────────────────────────────────────────────
        # PSU is selected after GPU + CPU lock (SELECTION_ORDER), so locked_specs
        # already holds the build's TDP-bearing parts. required_psu_wattage() is
        # the identical cpu_tdp+gpu_tdp+headroom bar the feasibility floor assumed,
        # applied here as a hard candidate filter so the pick can't undershoot it.
        min_psu_wattage = (
            required_psu_wattage(locked_specs) if slot == ComponentSlot.psu else None
        )
        remaining_budget = brief.budget.ceiling - running_spend
        outcome = select_part(
            slot=slot,
            band=band,
            brief=brief,
            locked_parts=locked_parts,
            fitness_thresholds=fitness_thresholds,
            neo4j_available=neo4j_available,
            req=req,
            remaining_budget=remaining_budget,
            ddr4_bias=(
                ddr4_bias
                and slot == ComponentSlot.ram
                and ComponentSlot.motherboard not in locked_parts
            ),
            min_psu_wattage=min_psu_wattage,
        )
        if outcome.part is None:
            # no_compatible / over_budget carry a plain-English message to surface;
            # no_stock (or any messageless status) just leaves the slot empty.
            if outcome.message:
                logger.warning("[Node3] %s dead-end: %s", slot.value, outcome.message)
                warnings.append(outcome.message)
            else:
                logger.warning("[Node3] %s: slot left empty in build", slot.value)
            continue

        part = outcome.part

        # ── Post-lock compatibility validator (blocks, does not merely log) ────
        # select_part already applies compatibility as a hard filter, so this is
        # defense in depth. compatibility_check fails open for graph-absent parts,
        # so an empty return here means a real, in-graph constraint violation — in
        # which case we REFUSE to lock rather than ship a known-bad build.
        if neo4j_available and locked_parts:
            check = Neo4jClient().compatibility_check(
                [part.product_id],
                locked_parts,
                slot,
            )
            if not check:
                msg = (
                    f"Blocked an incompatible {slot.value} ({part.product_id}) that "
                    f"conflicts with locked parts {[s.value for s in locked_parts]}; "
                    f"slot left unfilled to avoid a known-bad build."
                )
                logger.error("[Node3] %s: %s", slot.value, msg)
                warnings.append(msg)
                continue

        locked_parts[slot] = part.product_id
        if outcome.specs is not None:
            locked_specs[slot] = outcome.specs
        selected_parts.append(part)
        running_spend += part.price_inr

        logger.info(
            "[Node3] ✓ %s → %s | ₹%d | running spend ₹%d | budget headroom ₹%d",
            slot.value,
            part.product_id,
            part.price_inr,
            running_spend,
            brief.budget.ceiling - running_spend,
        )

    total = sum(p.price_inr for p in selected_parts)
    summary = (
        f"{brief.purpose.primary_use_case} ({brief.purpose.sub_case}) build — "
        f"{len(selected_parts)}/{len(SELECTION_ORDER)} slots filled — "
        f"total ₹{total:,}"
    )
    return BuildCard(
        parts=selected_parts,
        total_price_inr=total,
        summary=summary,
        warnings=warnings,
    )
