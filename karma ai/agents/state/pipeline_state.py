from __future__ import annotations

from typing import TypedDict

from agents.schemas import (
    BuildCard,
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
    locked_parts: dict[str, str] | None          # slot name → product_id
    fitness_thresholds: dict[str, float] | None      # slot name → threshold
    fitness_thresholds_key: dict | None              # cache key used to derive fitness_thresholds
    open_question_attempts: dict[str, int] | None    # open-question text → attempt count (ephemeral; not on the brief)
    error_message: str | None                    # for routing failures
    current_node: str | None


def new_state() -> PipelineState:
    return PipelineState(
        conversation_history=[],
        locked_parts=None,
        fitness_thresholds=None,
        open_question_attempts=None,
        error_message=None,
        current_node="node_intake",
    )
