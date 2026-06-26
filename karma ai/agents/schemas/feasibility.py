from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class FeasibilityVerdict(BaseModel):
    verdict: Literal["comfortable", "tight", "impossible"]
    reason: str
    binding_constraint: str | None = None
    suggested_adjustments: list[str] = []
