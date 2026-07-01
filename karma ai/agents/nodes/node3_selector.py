"""Node 3 — Part Finder & Recommender.

Selection sequence (locked, DESIGN.md §2.4):
  GPU → CPU → RAM → Storage → Motherboard → PSU → Case → Cooler → Fans

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
from ..llm.client import call_structured
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
    ComponentSlot.ram,
    ComponentSlot.storage,
    ComponentSlot.motherboard,
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
      "ok"            → part is a valid, compatible, in-budget pick.
      "no_stock"      → catalog has no in-stock candidate in the band (graph-off path).
      "no_compatible" → a compatible part exists nowhere in the catalog for the
                        locked parts (real dead-end, message set).
      "over_budget"   → a compatible part exists but the cheapest one exceeds the
                        remaining budget-pool / ceiling (message set).
    """
    part: BuildCardPart | None = None
    status: str = "ok"
    message: str | None = None


def _get_ddr_gen(candidate: dict) -> int | None:
    specs = candidate.get("specs")
    if isinstance(specs, dict):
        return specs.get("ddr_gen")
    return None


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
    result = call_structured(prompt, FitnessThresholds)
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
    remaining_budget: int | None = None,
    ddr4_bias: bool = False,
) -> SlotOutcome:
    """Three-step funnel: Postgres catalog → Neo4j graph filter → LLM pick.

    Compatibility against locked_parts is a HARD filter that is never bypassed:
    the price band and the fitness threshold relax; compatibility does not. If the
    (widened) band yields no compatible candidate, the band is escalated across the
    full catalog to find one. Only when the entire catalog has no compatible part is
    a "no_compatible" dead-end returned. When remaining_budget is supplied, a
    compatible part whose price would breach the ceiling yields an "over_budget"
    dead-end instead of a silent over-budget lock.

    Returns a SlotOutcome — inspect .part (None on any dead-end) and .status.
    """
    pg = PostgresClient()

    # ── Step 1: Postgres catalog query (band, with one 20% widening retry) ────
    candidates = pg.get_parts_in_band(slot, band.low, band.high, in_stock=True)
    if not candidates:
        widened_low = int(band.low * (1 - _BAND_WIDEN_FACTOR))
        widened_high = int(band.high * (1 + _BAND_WIDEN_FACTOR))
        logger.info(
            "[Node3] %s: catalog empty at [%d–%d]; widening band to [%d–%d]",
            slot.value, band.low, band.high, widened_low, widened_high,
        )
        candidates = pg.get_parts_in_band(slot, widened_low, widened_high, in_stock=True)

    # ── Step 1b: DDR4 preference bias (tight budget only) ───────────────────
    # On a tight budget the RAM slot is selected before Motherboard, so an
    # unbiased DDR5 pick can strand the board slot (DDR5 LGA1700 boards cost
    # significantly more than DDR4 equivalents). Two-step logic:
    #   1. If no DDR4 exists in the current band, augment candidates with DDR4
    #      parts from [0, widened_high] — saving on RAM frees budget for the board.
    #   2. Stable DDR4-first reorder so the LLM shortlist sees DDR4 options first.
    # Graceful degradation: if the catalog has no DDR4 at all, the DDR5 list is
    # passed through unchanged — never a dead-end.
    if ddr4_bias:
        ddr4_in_band = [c for c in candidates if _get_ddr_gen(c) == 4]
        if not ddr4_in_band:
            # Band floor excludes cheaper DDR4 — pull from the full affordable range.
            ddr4_ceiling = int(band.high * (1 + _BAND_WIDEN_FACTOR))
            extra_ddr4 = [
                c for c in pg.get_parts_in_band(slot, 0, ddr4_ceiling, in_stock=True)
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
            full = pg.get_parts_in_band(slot, 0, _FULL_CATALOG_HIGH, in_stock=True)
            working = _compatible_subset(neo4j, full, locked_parts, slot)
            if not working:
                msg = _no_compatible_message(slot, locked_parts)
                logger.warning("[Node3] %s: %s", slot.value, msg)
                return SlotOutcome(status="no_compatible", message=msg)
            logger.info(
                "[Node3] %s: no compatible part within price band — escalated to full "
                "catalog, %d compatible candidate(s) found",
                slot.value, len(working),
            )
    else:
        working = candidates
        if not working:
            logger.warning(
                "[Node3] %s: catalog still empty after 20%% band widening — slot skipped",
                slot.value,
            )
            return SlotOutcome(status="no_stock")

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
    if neo4j_available:
        by_id = {c["product_id"]: c for c in working}
        fit_ids = neo4j.fitness_filter(
            list(by_id.keys()),
            brief.purpose.primary_use_case,
            fitness_thresholds[slot],
        )
        if fit_ids:
            reordered = [by_id[pid] for pid in fit_ids if pid in by_id]
            if reordered:
                working = reordered
        else:
            logger.info(
                "[Node3] %s: fitness_filter returned empty (threshold %.2f) — keeping "
                "%d compatible part(s) unranked",
                slot.value, fitness_thresholds[slot], len(working),
            )

    shortlist_dicts = working[:_MAX_SHORTLIST]

    # ── Step 3: LLM final pick ────────────────────────────────────────────────
    parts_text = "\n".join(
        "  [{idx}] product_id={pid} | {name} by {brand} | ₹{price:,} | specs: {specs}".format(
            idx=i + 1,
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

    prompt = (
        f"You are a PC building expert. Select the single best {slot.value.upper()} "
        f"for this user build.\n\n"
        f"USER CONTEXT\n"
        f"  Use case    : {brief.purpose.primary_use_case} — {brief.purpose.sub_case}\n"
        f"  Software    : {software_text}\n"
        f"  Target      : {brief.performance.target_resolution} @ "
        f"{brief.performance.target_framerate} fps\n"
        f"  Constraints : {constraints_text}\n\n"
        f"SHORTLISTED {slot.value.upper()} OPTIONS ({len(shortlist_dicts)} parts)\n"
        f"{parts_text}\n\n"
        "Pick the best value-for-money option for the use case. "
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
    )


# ── Main orchestrator ─────────────────────────────────────────────────────────

def select_build(
    brief: UserBuildBrief,
    price_bands: PriceBands,
    feasibility_verdict: FeasibilityVerdict | None = None,
) -> BuildCard:
    """Walk SELECTION_ORDER and fill each slot via the three-step funnel.

    Safeguards applied during the walk:
      • Running budget-pool tracker: each slot is solved against the remaining
        headroom to the ceiling; a compatible-but-unaffordable slot dead-ends.
      • Lookahead probe after GPU + CPU lock: warns if no compatible
        motherboard exists in the current band before continuing.
      • Post-lock compatibility validator: refuses to lock (does not merely log)
        any part Neo4j flags as conflicting with an already-locked part.
      • Dead-end surfacing: no_compatible / over_budget outcomes are collected as
        plain-English warnings on the returned BuildCard.
    """
    # ── Upfront checks ────────────────────────────────────────────────────────
    neo4j_available: bool = Neo4jClient().ping()
    if not neo4j_available:
        logger.info("[Node3] Neo4j unreachable — graph filter disabled for all slots")

    tight_budget = (
        feasibility_verdict is not None
        and feasibility_verdict.verdict == "tight"
    )
    if tight_budget:
        logger.info("[Node3] tight budget detected — DDR4 preference bias enabled for RAM")

    fitness_thresholds = derive_fitness_thresholds(brief)
    logger.info(
        "[Node3] fitness thresholds: %s",
        {s.value: round(t, 2) for s, t in fitness_thresholds.items()},
    )

    # Slots the user is keeping from an existing build.
    reuse_slots = {r.slot for r in brief.existing.reuse_parts if r.action == "keep"}

    locked_parts: dict[ComponentSlot, str] = {}  # slot → product_id
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
            mb_candidates = pg_probe.get_parts_in_band(
                ComponentSlot.motherboard, band.low, band.high, in_stock=True
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
        remaining_budget = brief.budget.ceiling - running_spend
        outcome = select_part(
            slot=slot,
            band=band,
            brief=brief,
            locked_parts=locked_parts,
            fitness_thresholds=fitness_thresholds,
            neo4j_available=neo4j_available,
            remaining_budget=remaining_budget,
            ddr4_bias=(tight_budget and slot == ComponentSlot.ram),
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
