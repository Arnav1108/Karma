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

from pydantic import BaseModel

from ..db.neo4j import Neo4jClient
from ..db.postgres import PostgresClient
from ..llm.client import call_structured
from ..schemas.brief import UserBuildBrief
from ..schemas.build_card import BuildCard, BuildCardPart
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
) -> BuildCardPart | None:
    """Three-step funnel: Postgres catalog → Neo4j graph filter → LLM pick.

    Returns None when Postgres yields no in-stock results after band widening.
    Caller logs a warning and continues to the next slot — never crashes.
    """
    pg = PostgresClient()

    # ── Step 1: Postgres catalog query ────────────────────────────────────────
    candidates = pg.get_parts_in_band(slot, band.low, band.high, in_stock=True)
    if not candidates:
        widened_low = int(band.low * (1 - _BAND_WIDEN_FACTOR))
        widened_high = int(band.high * (1 + _BAND_WIDEN_FACTOR))
        logger.info(
            "[Node3] %s: catalog empty at [%d–%d]; widening band to [%d–%d]",
            slot.value, band.low, band.high, widened_low, widened_high,
        )
        candidates = pg.get_parts_in_band(slot, widened_low, widened_high, in_stock=True)
    if not candidates:
        logger.warning(
            "[Node3] %s: catalog still empty after 20%% band widening — slot skipped",
            slot.value,
        )
        return None

    # ── Step 2: Neo4j graph filter ────────────────────────────────────────────
    # Skipped entirely when neo4j_available is False.
    # compatibility_check and fitness_filter both "fail open": when the graph
    # has no data for a candidate it is kept, never penalised. So the fallback
    # path (graph sparse → use all candidates) is activated by an empty return
    # that means ALL candidates were excluded — i.e. real incompatibilities were
    # found for all of them — which in an empty graph never happens.
    shortlist_dicts = candidates
    if neo4j_available:
        neo4j = Neo4jClient()
        candidate_ids = [c["product_id"] for c in candidates]
        by_id = {c["product_id"]: c for c in candidates}

        # compatibility_check: takes list[str] IDs + typed locked_parts + slot.
        # Returns the compatible subset (fail-open for graph-absent candidates).
        compatible_ids = neo4j.compatibility_check(
            candidate_ids,
            locked_parts,
            slot,
        )
        if not compatible_ids:
            logger.info(
                "[Node3] %s: all %d candidates excluded by compatibility_check "
                "(real conflicts detected) — falling back to full Postgres list",
                slot.value, len(candidates),
            )
            compatible_ids = candidate_ids

        # fitness_filter: takes list[str] IDs, use_case, threshold (no slot arg).
        # Returns weighted passes (desc) + unweighted fail-opens.
        fit_ids = neo4j.fitness_filter(
            compatible_ids,
            brief.purpose.primary_use_case,
            fitness_thresholds[slot],
        )
        if fit_ids:
            shortlist_dicts = [by_id[pid] for pid in fit_ids if pid in by_id]
            if not shortlist_dicts:
                shortlist_dicts = [by_id[pid] for pid in compatible_ids if pid in by_id]
        else:
            logger.info(
                "[Node3] %s: fitness_filter returned empty (threshold %.2f) — "
                "falling back to %d compatible parts",
                slot.value, fitness_thresholds[slot], len(compatible_ids),
            )
            shortlist_dicts = [by_id[pid] for pid in compatible_ids if pid in by_id]

    shortlist_dicts = shortlist_dicts[:_MAX_SHORTLIST]

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

    return BuildCardPart(
        slot=slot,
        product_id=matched["product_id"],
        name=matched.get("name", matched["product_id"]),
        price_inr=int(matched.get("price_inr", 0)),
        justification=picked.justification,
    )


# ── Main orchestrator ─────────────────────────────────────────────────────────

def select_build(brief: UserBuildBrief, price_bands: PriceBands) -> BuildCard:
    """Walk SELECTION_ORDER and fill each slot via the three-step funnel.

    Safeguards applied during the walk:
      • Running budget tracker logged after every lock.
      • Lookahead probe after GPU + CPU lock: warns if no compatible
        motherboard exists in the current band before continuing.
      • Post-lock compatibility validator: logs any conflict returned by
        Neo4j after each new part is added to locked_parts.
    """
    # ── Upfront checks ────────────────────────────────────────────────────────
    neo4j_available: bool = Neo4jClient().ping()
    if not neo4j_available:
        logger.info("[Node3] Neo4j unreachable — graph filter disabled for all slots")

    fitness_thresholds = derive_fitness_thresholds(brief)
    logger.info(
        "[Node3] fitness thresholds: %s",
        {s.value: round(t, 2) for s, t in fitness_thresholds.items()},
    )

    # Slots the user is keeping from an existing build.
    reuse_slots = {r.slot for r in brief.existing.reuse_parts if r.action == "keep"}

    locked_parts: dict[ComponentSlot, str] = {}  # slot → product_id
    selected_parts: list[BuildCardPart] = []
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
        part = select_part(
            slot=slot,
            band=band,
            brief=brief,
            locked_parts=locked_parts,
            fitness_thresholds=fitness_thresholds,
            neo4j_available=neo4j_available,
        )
        if part is None:
            logger.warning("[Node3] %s: slot left empty in build", slot.value)
            continue

        # ── Post-lock compatibility validator ─────────────────────────────────
        # compatibility_check fails open when graph is unpopulated, so an empty
        # return specifically means real constraint violations were detected.
        if neo4j_available and locked_parts:
            check = Neo4jClient().compatibility_check(
                [part.product_id],
                locked_parts,
                slot,
            )
            if not check:
                logger.warning(
                    "[Node3] %s: compatibility conflict detected — %s may not be "
                    "compatible with one or more locked parts %s",
                    slot.value, part.product_id, list(locked_parts.keys()),
                )

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
    return BuildCard(parts=selected_parts, total_price_inr=total, summary=summary)
