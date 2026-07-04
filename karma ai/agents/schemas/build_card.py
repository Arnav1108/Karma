from __future__ import annotations

from pydantic import BaseModel, Field

from .slots import ComponentSlot


class BuildCardPart(BaseModel):
    slot: ComponentSlot
    product_id: str
    name: str
    price_inr: int
    justification: str


class BuildCard(BaseModel):
    parts: list[BuildCardPart]
    total_price_inr: int
    summary: str
    # Plain-English dead-end notices for slots that could not be filled without
    # violating compatibility or the budget ceiling (e.g. "no compatible
    # motherboard"). Empty on a clean build. Backward-compatible default.
    warnings: list[str] = Field(default_factory=list)
    # Per-slot deltas produced by an incumbent-biased refinement re-solve
    # (node3_refinement.diff_and_bias). Each entry:
    #   {"slot": str, "old_product_id": str | None, "new_product_id": str, "reason": str}
    # Empty on a fresh build or a restart; lets the refinement loop print a diff
    # instead of a full card each round. Backward-compatible default.
    changed_slots: list[dict] = Field(default_factory=list)
