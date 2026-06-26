from __future__ import annotations

from typing import Any, TypedDict

from agents.schemas import (
    BuildCard,
    ComponentSlot,
    FeasibilityVerdict,
    PriceBands,
    UserBuildBrief,
)


class PipelineState(TypedDict, total=False):
    current_brief: UserBuildBrief
    conversation_history: list[dict[str, str]]
    feasibility_verdict: FeasibilityVerdict
    price_bands: PriceBands
    build_card: BuildCard
    locked_parts: dict[ComponentSlot, dict[str, Any]]
    remaining_budget: int
    fitness_thresholds: dict[ComponentSlot, float]
    current_node: str


def new_state() -> PipelineState:
    return PipelineState(
        conversation_history=[],
        locked_parts={},
        fitness_thresholds={},
        current_node="node1",
    )
