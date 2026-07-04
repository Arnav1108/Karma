"""Node 3 refinement — pure, non-interactive refinement logic.

The interactive `input()`/`print()` loop lives in `run_pipeline.py` (the locked
decision: the conversation loop lives in the CLI harness, not inside a LangGraph
node). This module exposes only pure functions the harness drives:

  RefinementOps                       multi-op parse of one user turn
  parse_refinement_request(...)    -> RefinementOps      (LLM classify)
  route_field_edit(field)          -> "additive"|"structural"  (fixed table)
  dispatch_refinement(...)         -> RefinementResult   (applies ops per §3)
  diff_and_bias(old, new, ...)     -> BuildCard          (incumbent-biased)
  _select_build_with_pins(...)     -> BuildCard          (re-solve with pins)

Dispatch precedence (DESIGN §2.4 / task §3):
  restart_trigger → brief_edit → budget_change → pin/reject → re-solve → accept
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel

from ..db.neo4j import Neo4jClient
from ..feasibility.estimate import estimate_feasibility
from ..feasibility.resolver import resolve_requirements
from ..llm.client import call_structured
from ..nodes.node2_allocation import allocate_budget
from ..nodes.node3_selector import (
    _BAND_WIDEN_FACTOR,
    SELECTION_ORDER,
    ThresholdCache,
    _threshold_key,
    derive_fitness_thresholds,
    select_part,
)
from ..schemas.brief import RejectedPart, UserBuildBrief
from ..schemas.build_card import BuildCard, BuildCardPart
from ..schemas.price_bands import PriceBands
from ..schemas.slots import ComponentSlot

logger = logging.getLogger(__name__)

MAX_REFINEMENT_ROUNDS = 5


# ── Field routing table (task §2) ─────────────────────────────────────────────
# The classifier decides WHICH field is being edited; routing off that field
# name is a fixed, deterministic lookup — NOT an LLM judgment call.
#
# Additive fields patch the brief and re-run the feasibility check only.
# Structural fields patch the brief and restart the whole graph (run_from_brief).
ADDITIVE_FIELDS: frozenset[str] = frozenset(
    {"software", "performance", "extras", "physical", "longevity"}
)
STRUCTURAL_FIELDS: frozenset[str] = frozenset(
    {"primary_use_case", "budget.scope", "existing.reuse_parts"}
)

# Dotted-path resolution for patching a named field into the brief.
_FIELD_PATHS: dict[str, tuple[str, ...]] = {
    "software": ("software",),
    "performance": ("performance",),
    "extras": ("extras",),
    "physical": ("physical",),
    "longevity": ("longevity",),
    "primary_use_case": ("purpose", "primary_use_case"),
    "budget.scope": ("budget", "scope"),
    "existing.reuse_parts": ("existing", "reuse_parts"),
}


def route_field_edit(field_name: str) -> Literal["additive", "structural"]:
    """Deterministic routing of a field name to its dispatch bucket (task §2).

    A field outside the table defaults to "additive" with a warning — never a
    crash. This is the single source of truth for additive-vs-structural, so the
    LLM's choice of which op-slot it populated does not decide routing.
    """
    if field_name in STRUCTURAL_FIELDS:
        return "structural"
    if field_name in ADDITIVE_FIELDS:
        return "additive"
    logger.warning(
        "[Refine] field %r not in routing table — defaulting to additive", field_name
    )
    return "additive"


# ── Parse: one user turn → a set of ops ───────────────────────────────────────

class RefinementOps(BaseModel):
    """Multi-op classification of a single refinement message (task §1).

    A single message can populate multiple fields — e.g. "bump budget to 90k and
    give me an nvidia card" → budget_change=90000 plus a brief_edit/reject. The
    dicts stay loosely typed on purpose (call_structured runs JSON mode, not
    strict json-schema, so free-form dict payloads validate fine); dispatch reads
    them defensively.
    """

    brief_edit: dict | None = None        # {"field": str, "value": Any} — additive
    restart_trigger: dict | None = None   # {"field": str, "value": Any} — structural
    budget_change: int | None = None      # new ceiling in INR, if stated
    pin: ComponentSlot | None = None
    reject: dict | None = None            # {"slot": ComponentSlot, "product_id": str, "reason": str}
    accept: bool = False


def parse_refinement_request(
    user_input: str,
    brief: UserBuildBrief,
    build_card: BuildCard,
) -> RefinementOps:
    """LLM-classify a freeform refinement message into a RefinementOps set."""
    build_summary = "\n".join(
        f"  {p.slot.value}: {p.name} (product_id={p.product_id}, ₹{p.price_inr:,})"
        for p in build_card.parts
    ) or "  (no parts selected)"

    prompt = f"""You are parsing a user's refinement request for a PC build recommendation.

Current build:
{build_summary}

Current use case : {brief.purpose.primary_use_case} / {brief.purpose.sub_case}
Current ceiling  : ₹{brief.budget.ceiling:,}

User said: "{user_input}"

Classify the message into a RefinementOps JSON. A single message MAY populate
several fields at once. Leave a field null/false when it does not apply.

- accept (bool): true only if the user is happy and wants to finalize.
    Examples: "looks good", "that's fine", "ship it", "accept".

- restart_trigger ({{"field": str, "value": ...}}): a STRUCTURAL change to
    requirements. Use ONLY for these fields:
      • "primary_use_case" (value: gaming | content_creation | work_productivity |
        storage_homeserver | general_use)
      • "budget.scope" (value: pc_only | pc_plus_monitor | pc_plus_peripherals | full_setup)
      • "existing.reuse_parts" (value: list of reuse-part objects)
    Examples: "actually this is for video editing now" → field=primary_use_case.

- brief_edit ({{"field": str, "value": ...}}): an ADDITIVE preference change.
    Use for: "software", "performance", "extras", "physical", "longevity".
    Examples: "target 1440p 144fps" → field=performance,
      value={{"target_resolution":"1440p","target_framerate":144,"source":"user_stated"}}.

- budget_change (int): a NEW TOTAL budget ceiling in INR. "90k" → 90000,
    "1.5 lakh" → 150000.

- pin (slot enum): user wants to KEEP one part and re-solve the rest.
    slot ∈ gpu, cpu, ram, storage, motherboard, psu, case, cooler, fans.
    Examples: "keep the GPU", "lock in this CPU".

- reject ({{"slot": <slot>, "product_id": <id from the build above>, "reason": str}}):
    user rejects a specific part; find a replacement. Use the product_id shown
    in the current build for that slot.
    Examples: "I don't like the GPU", "the PSU is too expensive",
      "give me an AMD card instead" (reject the current gpu).

Return ONLY the JSON object."""

    return call_structured(prompt, RefinementOps)


# ── Brief patch helpers (pure) ────────────────────────────────────────────────

def patch_brief_field(
    brief: UserBuildBrief, field_name: str, value: Any
) -> UserBuildBrief:
    """Return a new brief with `field_name` set to `value`, re-validated.

    Uses the dotted-path table so both additive ("software") and structural
    ("budget.scope") fields resolve to the right nested location. Falls back to a
    flat top-level attribute for an unmapped field. Re-validates through
    UserBuildBrief so a malformed LLM value raises a ValidationError the caller
    can trap rather than silently corrupting the brief.
    """
    data = brief.model_dump(mode="python")
    path = _FIELD_PATHS.get(field_name, (field_name,))
    ref = data
    for key in path[:-1]:
        ref = ref[key]
    ref[path[-1]] = value
    return UserBuildBrief.model_validate(data)


def rescale_budget(brief: UserBuildBrief, new_ceiling: int) -> UserBuildBrief:
    """Rescale comfortable_min/max proportionally to a new ceiling (task §3).

    Same formula as the retired refinement_loop: scale = new_ceiling/old_ceiling
    applied to both comfortable bounds, then ceiling := new_ceiling. Returns a
    re-validated brief.
    """
    data = brief.model_dump(mode="python")
    old_ceiling = brief.budget.ceiling
    if old_ceiling > 0:
        scale = new_ceiling / old_ceiling
        data["budget"]["comfortable_min"] = int(brief.budget.comfortable_min * scale)
        data["budget"]["comfortable_max"] = int(brief.budget.comfortable_max * scale)
    data["budget"]["ceiling"] = new_ceiling
    return UserBuildBrief.model_validate(data)


def apply_reject(
    brief: UserBuildBrief, product_id: str, reason: str | None
) -> UserBuildBrief:
    """Append a RejectedPart to hard_constraints.rejected_parts (in place)."""
    brief.hard_constraints.rejected_parts.append(
        RejectedPart(
            product_id=product_id,
            reason=reason or "user rejected",
            rejected_at=datetime.now(timezone.utc),
        )
    )
    return brief


# ── Re-solve with pinned slots ────────────────────────────────────────────────

def _pinned_parts_from_locked(
    locked_parts: dict[str, str], build_card: BuildCard
) -> dict[ComponentSlot, BuildCardPart]:
    """Reconstruct the {slot → BuildCardPart} map _select_build_with_pins wants.

    The loop-scope `locked_parts` holds {slot_name → product_id} (matching
    PipelineState.locked_parts, which is string-keyed for serializability). Pins
    are always taken from the currently-shown card, and re-solves keep pinned
    slots, so the full part object is recoverable from build_card. A pin whose
    product_id is no longer in the card is dropped with a warning rather than
    reconstructed with a bogus price.
    """
    by_slot: dict[ComponentSlot, BuildCardPart] = {}
    for slot_name, product_id in locked_parts.items():
        try:
            slot = ComponentSlot(slot_name)
        except ValueError:
            logger.warning("[Refine] pinned slot name %r is not a ComponentSlot", slot_name)
            continue
        part = next(
            (p for p in build_card.parts if p.slot == slot and p.product_id == product_id),
            None,
        )
        if part is None:
            logger.warning(
                "[Refine] pinned %s=%s not in current build card — cannot re-pin",
                slot_name, product_id,
            )
            continue
        by_slot[slot] = part
    return by_slot


def _select_build_with_pins(
    brief: UserBuildBrief,
    price_bands: PriceBands,
    pinned_parts: dict[ComponentSlot, BuildCardPart],
    cache: ThresholdCache | None = None,
) -> BuildCard:
    """Re-solve the whole build, holding `pinned_parts` fixed.

    Interface unchanged from the retired module (task §6): callers pass a
    {slot → BuildCardPart} map. Use `_pinned_parts_from_locked` to build that map
    from a {slot_name → product_id} locked-parts dict.
    """
    if cache is None:
        cache = ThresholdCache()
    current_key = _threshold_key(brief)
    if cache.thresholds is not None and cache.key == current_key:
        fitness_thresholds = cache.thresholds
        logger.info("[Refine] reusing cached fitness thresholds (brief unchanged)")
    else:
        fitness_thresholds = derive_fitness_thresholds(brief)
        cache.thresholds = fitness_thresholds
        cache.key = current_key
    neo4j_available = Neo4jClient().ping()
    req = resolve_requirements(brief)
    locked_parts: dict[ComponentSlot, str] = {}
    result_parts: list[BuildCardPart] = []

    for slot in SELECTION_ORDER:
        if slot in pinned_parts:
            part = pinned_parts[slot]
            result_parts.append(part)
            locked_parts[slot] = part.product_id
        else:
            remaining_budget = brief.budget.ceiling - sum(p.price_inr for p in result_parts)
            outcome = select_part(
                slot,
                price_bands.root[slot],
                brief,
                locked_parts,
                fitness_thresholds,
                neo4j_available,
                req=req,
                remaining_budget=remaining_budget,
            )
            if outcome.part is not None:
                result_parts.append(outcome.part)
                locked_parts[slot] = outcome.part.product_id
            elif outcome.message:
                logger.warning("Slot %s dead-ended during refinement: %s", slot, outcome.message)
            else:
                logger.warning("No part found for slot %s during refinement", slot)

    total = sum(p.price_inr for p in result_parts)
    return BuildCard(parts=result_parts, total_price_inr=total, summary="Refined build")


# ── Incumbent-biased diff (task §4) ───────────────────────────────────────────

def _incumbent_validity(
    old_part: BuildCardPart,
    slot: ComponentSlot,
    price_bands: PriceBands,
    decided: dict[ComponentSlot, str],
    rejected_ids: set[str],
    neo4j_available: bool,
) -> str:
    """Return "" if the old part is still a valid pick, else the reason it isn't.

    Reasons, in the order §4 names them: "rejected", "out_of_band", "incompatible".
    The price band uses the same 20% widening tolerance the selector itself treats
    as in-band, so the bias keeps a part the selector would still have considered.
    """
    if old_part.product_id in rejected_ids:
        return "rejected"

    band = price_bands.root.get(slot)
    if band is not None:
        low = int(band.low * (1 - _BAND_WIDEN_FACTOR))
        high = int(band.high * (1 + _BAND_WIDEN_FACTOR))
        if not (low <= old_part.price_inr <= high):
            return "out_of_band"

    if neo4j_available and decided:
        ok = Neo4jClient().compatibility_check([old_part.product_id], decided, slot)
        if not ok:
            return "incompatible"

    return ""


def diff_and_bias(
    old_card: BuildCard,
    new_card: BuildCard,
    locked_parts: dict[str, str],
    brief: UserBuildBrief,
    price_bands: PriceBands,
    neo4j_available: bool | None = None,
) -> BuildCard:
    """Incumbent-biased reconciliation of a re-solved card against the old one.

    For every slot that is NOT user-pinned and whose new pick differs from the
    old one, keep the OLD part if it is still valid (in band, compatible with the
    parts decided so far, not rejected); otherwise keep the NEW pick. The result's
    `changed_slots` lists only slots whose FINAL part differs from the old card —
    so a bias-retained incumbent reads as "unchanged" to the user (task §4).

    Compatibility is checked against parts decided so far in SELECTION_ORDER
    (seeded with the user pins), mirroring the incremental way select_build locks.
    """
    if neo4j_available is None:
        neo4j_available = Neo4jClient().ping()

    old_by_slot = {p.slot: p for p in old_card.parts}
    new_by_slot = {p.slot: p for p in new_card.parts}
    rejected_ids = {r.product_id for r in brief.hard_constraints.rejected_parts}

    decided: dict[ComponentSlot, str] = {}
    for slot_name, product_id in locked_parts.items():
        try:
            decided[ComponentSlot(slot_name)] = product_id
        except ValueError:
            continue

    final_parts: list[BuildCardPart] = []
    changed_slots: list[dict] = []

    for slot in SELECTION_ORDER:
        new_part = new_by_slot.get(slot)
        if new_part is None:
            continue
        old_part = old_by_slot.get(slot)
        slot_locked = slot.value in locked_parts

        chosen = new_part
        reason = "added" if old_part is None else "changed"

        if (
            not slot_locked
            and old_part is not None
            and old_part.product_id != new_part.product_id
        ):
            invalid_reason = _incumbent_validity(
                old_part, slot, price_bands, decided, rejected_ids, neo4j_available
            )
            if invalid_reason == "":
                chosen = old_part          # incumbent bias: keep the old part
            else:
                reason = invalid_reason    # old part invalid → new pick wins

        final_parts.append(chosen)
        decided[slot] = chosen.product_id

        if old_part is None or chosen.product_id != old_part.product_id:
            changed_slots.append(
                {
                    "slot": slot.value,
                    "old_product_id": old_part.product_id if old_part else None,
                    "new_product_id": chosen.product_id,
                    "reason": reason,
                }
            )

    total = sum(p.price_inr for p in final_parts)
    return BuildCard(
        parts=final_parts,
        total_price_inr=total,
        summary=new_card.summary,
        warnings=new_card.warnings,
        changed_slots=changed_slots,
    )


# ── Dispatch (task §3) ────────────────────────────────────────────────────────

@dataclass
class RefinementResult:
    """Outcome of applying one turn's ops. The harness reads this and does the I/O.

    build_card   : the card to show next (may be unchanged).
    brief        : the (possibly patched) brief to carry forward.
    price_bands  : the (possibly re-allocated) bands to carry forward.
    accepted     : True → the user accepted; exit the loop.
    product_ids  : on accept, the list to ship to the backend.
    message      : optional plain-English note to print (e.g. an impossible verdict).
    """

    build_card: BuildCard
    brief: UserBuildBrief
    price_bands: PriceBands
    accepted: bool = False
    product_ids: list[str] = field(default_factory=list)
    message: str | None = None


def _gather_field_edits(ops: RefinementOps) -> list[tuple[str, Any]]:
    """Collect (field, value) from restart_trigger and brief_edit, deduped by field.

    Routing is decided later by route_field_edit — NOT by which op-slot the LLM
    happened to use — so both buckets are gathered here and classified downstream.
    """
    edits: list[tuple[str, Any]] = []
    seen: set[str] = set()
    for bucket in (ops.restart_trigger, ops.brief_edit):
        if isinstance(bucket, dict) and bucket.get("field"):
            field_name = str(bucket["field"])
            if field_name not in seen:
                seen.add(field_name)
                edits.append((field_name, bucket.get("value")))
    return edits


def dispatch_refinement(
    ops: RefinementOps,
    brief: UserBuildBrief,
    price_bands: PriceBands,
    build_card: BuildCard,
    locked_parts: dict[str, str],
    cache: ThresholdCache | None = None,
) -> RefinementResult:
    """Apply one turn's ops in the fixed precedence (task §3).

    Precedence: restart_trigger → brief_edit → budget_change → pin/reject →
    re-solve → accept. `locked_parts` (slot_name → product_id) is mutated in place
    for pin/reject. Structural edits restart the graph and skip all other ops.
    """
    if cache is None:
        cache = ThresholdCache()

    edits = _gather_field_edits(ops)
    structural = [(f, v) for f, v in edits if route_field_edit(f) == "structural"]
    additive = [(f, v) for f, v in edits if route_field_edit(f) == "additive"]

    # ── 1. restart_trigger (structural) — patch, restart, skip everything else ──
    if structural:
        field_name, value = structural[0]
        try:
            brief = patch_brief_field(brief, field_name, value)
        except Exception as exc:  # noqa: BLE001 — bad LLM value must not crash the loop
            logger.warning("[Refine] structural patch of %r failed: %s", field_name, exc)
            return RefinementResult(
                build_card=build_card, brief=brief, price_bands=price_bands,
                message=f"Could not apply change to {field_name}: {exc}",
            )
        # locked_parts and rejected_parts PERSIST across a restart (task §3).
        new_bands = allocate_budget(brief)
        from ..graph_runner import run_from_brief  # lazy: avoids graph import at module load

        final_state = run_from_brief(brief, new_bands)
        new_card = final_state.get("build_card") or build_card
        return RefinementResult(
            build_card=new_card,
            brief=final_state.get("current_brief", brief),
            price_bands=final_state.get("price_bands", new_bands),
            message=f"Restarted after structural change to {field_name}.",
        )

    changed = False

    # ── 2. brief_edit (additive) — patch, feasibility-recheck only ──────────────
    for field_name, value in additive:
        try:
            brief = patch_brief_field(brief, field_name, value)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Refine] additive patch of %r failed: %s", field_name, exc)
            return RefinementResult(
                build_card=build_card, brief=brief, price_bands=price_bands,
                message=f"Could not apply change to {field_name}: {exc}",
            )
        changed = True
    if additive:
        verdict = estimate_feasibility(brief)
        if verdict.verdict == "impossible":
            # Do NOT re-solve this turn (task §3).
            return RefinementResult(
                build_card=build_card, brief=brief, price_bands=price_bands,
                message=(
                    "That change makes the build impossible within budget: "
                    f"{verdict.reason} — reverting is up to you; not re-solving this turn."
                ),
            )

    # ── 3. budget_change ────────────────────────────────────────────────────────
    if ops.budget_change is not None:
        brief = rescale_budget(brief, ops.budget_change)
        price_bands = allocate_budget(brief)
        changed = True

    # ── 4. pin ──────────────────────────────────────────────────────────────────
    if ops.pin is not None:
        part = next((p for p in build_card.parts if p.slot == ops.pin), None)
        if part is not None:
            locked_parts[ops.pin.value] = part.product_id
            changed = True
        else:
            logger.warning("[Refine] pin %s: slot not in current build card", ops.pin.value)

    # ── 5. reject ───────────────────────────────────────────────────────────────
    if isinstance(ops.reject, dict):
        slot_obj: ComponentSlot | None = None
        raw_slot = ops.reject.get("slot")
        if raw_slot is not None:
            try:
                slot_obj = ComponentSlot(raw_slot)
            except ValueError:
                logger.warning("[Refine] reject: %r is not a valid slot", raw_slot)
        product_id = ops.reject.get("product_id")
        if not product_id and slot_obj is not None:
            part = next((p for p in build_card.parts if p.slot == slot_obj), None)
            product_id = part.product_id if part else None
        if product_id:
            brief = apply_reject(brief, product_id, ops.reject.get("reason"))
            if slot_obj is not None:
                locked_parts.pop(slot_obj.value, None)  # a rejected slot can't stay pinned
            changed = True
        else:
            logger.warning("[Refine] reject: could not resolve a product_id to reject")

    # ── 6. re-solve (incumbent-biased) ──────────────────────────────────────────
    if changed:
        pinned = _pinned_parts_from_locked(locked_parts, build_card)
        candidate = _select_build_with_pins(brief, price_bands, pinned, cache=cache)
        biased = diff_and_bias(build_card, candidate, locked_parts, brief, price_bands)
        return RefinementResult(build_card=biased, brief=brief, price_bands=price_bands)

    # ── 7. accept ───────────────────────────────────────────────────────────────
    if ops.accept:
        return RefinementResult(
            build_card=build_card, brief=brief, price_bands=price_bands,
            accepted=True,
            product_ids=[p.product_id for p in build_card.parts],
        )

    # Nothing actionable parsed.
    return RefinementResult(
        build_card=build_card, brief=brief, price_bands=price_bands,
        message="No actionable change detected — try 'pin <slot>', 'reject <slot>', "
                "a new budget, or 'accept'.",
    )
