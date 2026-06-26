from __future__ import annotations

from pydantic import BaseModel

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
