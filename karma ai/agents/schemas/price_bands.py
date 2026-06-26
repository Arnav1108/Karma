from __future__ import annotations

from pydantic import BaseModel, RootModel, model_validator

from .slots import ComponentSlot


class PriceBand(BaseModel):
    low: int
    mid: int
    high: int

    @model_validator(mode="after")
    def check_ordering(self) -> PriceBand:
        if not (self.low <= self.mid <= self.high):
            raise ValueError(
                f"price band must satisfy low <= mid <= high, got {self.low} / {self.mid} / {self.high}"
            )
        return self


class PriceBands(RootModel[dict[ComponentSlot, PriceBand]]):
    def total_low(self) -> int:
        return sum(band.low for band in self.root.values())

    def total_mid(self) -> int:
        return sum(band.mid for band in self.root.values())

    def total_high(self) -> int:
        return sum(band.high for band in self.root.values())
