from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel

from ..db.neo4j import Neo4jClient
from ..llm.client import call_structured
from ..nodes.node2_allocation import allocate_budget
from ..nodes.node3_selector import (
    SELECTION_ORDER,
    derive_fitness_thresholds,
    select_build,
    select_part,
)
from ..schemas.brief import RejectedPart, UserBuildBrief
from ..schemas.build_card import BuildCard, BuildCardPart
from ..schemas.price_bands import PriceBands
from ..schemas.slots import ComponentSlot

logger = logging.getLogger(__name__)

MAX_REFINEMENT_ROUNDS = 5


class RefinementRequest(BaseModel):
    action: Literal["pin", "open", "swap", "accept", "restart"]
    slot: ComponentSlot | None = None
    reason: str | None = None
    new_budget: int | None = None


def parse_refinement_request(
    user_input: str,
    build_card: BuildCard,
) -> RefinementRequest:
    build_summary = "\n".join(
        f"  {p.slot}: {p.name} (₹{p.price_inr:,})" for p in build_card.parts
    )
    prompt = f"""You are parsing a user's refinement request for a PC build recommendation.

Current build:
{build_summary}

User said: "{user_input}"

Classify this into one of these actions and return a RefinementRequest JSON:

- "accept": User is happy with the build. Examples: "looks good", "that's fine", "let's go with this", "perfect", "accept"
- "restart": User wants to change their requirements entirely. Examples: "I want a different use case", "start over", "let me reconsider my needs"
- "pin" + slot: Keep one specific part, re-run everything else. Examples: "keep the CPU", "I want to lock in this GPU", "don't change the RAM"
- "open" + slot (optional): Re-run a specific slot or everything. Examples: "give me different GPU options", "try different options for storage"
- "swap" + slot: Reject the current part for that slot and find a replacement. Examples: "I don't like the GPU", "change the RAM", "try a different CPU", "the PSU is too expensive"
- "swap" + new_budget (integer INR): User states a new total budget. Examples: "my budget is now 90k" → new_budget=90000, "I can spend 1.5 lakh" → new_budget=150000

Rules:
- slot must be one of: gpu, cpu, ram, storage, motherboard, psu, case, cooler, fans
- new_budget is an integer in INR (1 lakh = 100000)
- reason should capture why the user wants this change, if stated
- If slot cannot be determined from context, leave it null
- Prefer "swap" over "open" when the user is rejecting a specific part"""

    return call_structured(prompt, RefinementRequest)


def _select_build_with_pins(
    brief: UserBuildBrief,
    price_bands: PriceBands,
    pinned_parts: dict[ComponentSlot, BuildCardPart],
) -> BuildCard:
    fitness_thresholds = derive_fitness_thresholds(brief)
    neo4j_available = Neo4jClient().ping()
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
                price_bands[slot],
                brief,
                locked_parts,
                fitness_thresholds,
                neo4j_available,
                remaining_budget=remaining_budget,
            )
            if outcome.part is not None:
                result_parts.append(outcome.part)
                locked_parts[slot] = outcome.part.product_id
            elif outcome.message:
                logger.warning(
                    "Slot %s dead-ended during refinement: %s", slot, outcome.message
                )
            else:
                logger.warning("No part found for slot %s during refinement", slot)

    total = sum(p.price_inr for p in result_parts)
    return BuildCard(parts=result_parts, total_price_inr=total, summary="Refined build")


def apply_refinement(
    request: RefinementRequest,
    build_card: BuildCard,
    brief: UserBuildBrief,
    price_bands: PriceBands,
) -> BuildCard | None:
    if request.action in ("accept", "restart"):
        return None

    if request.action == "open":
        return select_build(brief, price_bands)

    if request.action == "pin":
        if request.slot is None:
            logger.warning("pin action received with no slot; falling back to full re-run")
            return select_build(brief, price_bands)
        part = next((p for p in build_card.parts if p.slot == request.slot), None)
        if part is None:
            logger.warning("Pinned slot %s not in current build; falling back to full re-run", request.slot)
            return select_build(brief, price_bands)
        return _select_build_with_pins(brief, price_bands, {request.slot: part})

    if request.action == "swap":
        if request.slot is None:
            logger.warning("swap action received with no slot; falling back to full re-run")
            return select_build(brief, price_bands)
        current_part = next((p for p in build_card.parts if p.slot == request.slot), None)
        if current_part is None:
            logger.warning("Swap slot %s not in current build; falling back to full re-run", request.slot)
            return select_build(brief, price_bands)
        brief.hard_constraints.rejected_parts.append(
            RejectedPart(
                product_id=current_part.product_id,
                reason=request.reason or "user rejected",
                rejected_at=datetime.now(timezone.utc),
            )
        )
        pinned_parts = {
            p.slot: p for p in build_card.parts if p.slot != request.slot
        }
        return _select_build_with_pins(brief, price_bands, pinned_parts)

    logger.warning("Unknown refinement action: %s", request.action)
    return build_card


def _print_build_card(build_card: BuildCard) -> None:
    col_slot = 12
    col_name = 30
    col_price = 12
    header = f"{'Slot':<{col_slot}}  {'Part':<{col_name}}  {'Price':>{col_price}}"
    divider = "-" * len(header)
    print(f"\n{header}")
    print(divider)
    for part in build_card.parts:
        price_str = f"₹{part.price_inr:,}"
        print(f"{part.slot.value:<{col_slot}}  {part.name:<{col_name}}  {price_str:>{col_price}}")
    print(divider)
    total_str = f"₹{build_card.total_price_inr:,}"
    print(f"{'TOTAL':<{col_slot}}  {'':>{col_name}}  {total_str:>{col_price}}\n")


def refinement_loop(
    build_card: BuildCard,
    brief: UserBuildBrief,
    price_bands: PriceBands,
) -> BuildCard:
    for round_num in range(MAX_REFINEMENT_ROUNDS):
        _print_build_card(build_card)
        user_input = input(f"Refine your build [{round_num + 1}/{MAX_REFINEMENT_ROUNDS}] (or 'accept'): ").strip()

        request = parse_refinement_request(user_input, build_card)
        logger.debug("Parsed refinement request: %s", request)

        if request.new_budget is not None:
            old_ceiling = brief.budget.ceiling
            if old_ceiling > 0:
                scale = request.new_budget / old_ceiling
                brief.budget.comfortable_min = int(brief.budget.comfortable_min * scale)
                brief.budget.comfortable_max = int(brief.budget.comfortable_max * scale)
            brief.budget.ceiling = request.new_budget
            price_bands = allocate_budget(brief)
            logger.info("Budget updated to ₹%s; re-allocated price bands", request.new_budget)

        result = apply_refinement(request, build_card, brief, price_bands)

        if result is None:
            if request.action == "accept":
                print("Build accepted.")
                return build_card
            if request.action == "restart":
                print("Restarting from the beginning...")
                return build_card
        else:
            build_card = result

    print(f"Maximum refinement rounds ({MAX_REFINEMENT_ROUNDS}) reached.")
    _print_build_card(build_card)
    return build_card
