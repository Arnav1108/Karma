"""Tests for agents/costs.py's catalog-derived reused_part_value().

average_catalog_price() replaces the old hardcoded _REUSED_PART_VALUE_INR
stub table with a live Postgres average, cached in-process for the rest of
the process lifetime. These tests never touch a real database — PostgresClient
is monkeypatched with a fake, following test_psu_wattage.py's pattern of
patching the callable at its point of use in the module namespace.
"""

from __future__ import annotations

import pytest

from agents import costs
from agents.schemas.slots import ComponentSlot


@pytest.fixture(autouse=True)
def _clear_cache():
    """The cache is module-level state — isolate it across tests."""
    costs._catalog_price_cache.clear()
    yield
    costs._catalog_price_cache.clear()


class _FakePG:
    def __init__(self, avg_by_slot=None, raises=False):
        self.avg_by_slot = avg_by_slot or {}
        self.raises = raises
        self.calls = 0

    def get_avg_catalog_price(self, slot):
        self.calls += 1
        if self.raises:
            raise RuntimeError("Postgres unreachable")
        return self.avg_by_slot.get(slot)


class TestAverageCatalogPrice:
    def test_computed_correctly_from_synthetic_catalog(self, monkeypatch):
        # 24763 is nearest to the 500-multiple 25000 (49.526 rounds up to 50).
        fake = _FakePG(avg_by_slot={ComponentSlot.gpu: 24763})
        monkeypatch.setattr(costs, "PostgresClient", lambda: fake)

        assert costs.average_catalog_price(ComponentSlot.gpu) == 25000

    def test_rounds_down_when_below_midpoint(self, monkeypatch):
        # 24249 is nearest to the 500-multiple 24000 (48.498 rounds down to 48).
        fake = _FakePG(avg_by_slot={ComponentSlot.ram: 24249})
        monkeypatch.setattr(costs, "PostgresClient", lambda: fake)

        assert costs.average_catalog_price(ComponentSlot.ram) == 24000

    def test_returns_none_when_postgres_raises(self, monkeypatch):
        fake = _FakePG(raises=True)
        monkeypatch.setattr(costs, "PostgresClient", lambda: fake)

        assert costs.average_catalog_price(ComponentSlot.cpu) is None

    def test_returns_none_when_slot_has_zero_in_stock_parts(self, monkeypatch):
        # AVG() over zero rows comes back NULL -> get_avg_catalog_price returns None.
        fake = _FakePG(avg_by_slot={ComponentSlot.psu: None})
        monkeypatch.setattr(costs, "PostgresClient", lambda: fake)

        assert costs.average_catalog_price(ComponentSlot.psu) is None

    def test_cache_is_not_requeried_on_second_call(self, monkeypatch):
        fake = _FakePG(avg_by_slot={ComponentSlot.storage: 8000})
        monkeypatch.setattr(costs, "PostgresClient", lambda: fake)

        first = costs.average_catalog_price(ComponentSlot.storage)
        second = costs.average_catalog_price(ComponentSlot.storage)

        assert first == second == 8000
        assert fake.calls == 1, "second call should be served from the cache"

    def test_refresh_recomputes_a_cached_value(self, monkeypatch):
        fake = _FakePG(avg_by_slot={ComponentSlot.case: 5000})
        monkeypatch.setattr(costs, "PostgresClient", lambda: fake)

        assert costs.average_catalog_price(ComponentSlot.case) == 5000
        assert fake.calls == 1

        fake.avg_by_slot[ComponentSlot.case] = 6000
        costs.refresh_catalog_price_cache()

        assert costs.average_catalog_price(ComponentSlot.case) == 6000
        # refresh() queries every slot once; average_catalog_price() afterwards
        # must be served from the cache, not trigger a further query.
        assert fake.calls == len(ComponentSlot) + 1


class TestReusedPartValue:
    def test_uses_catalog_average_when_available(self, monkeypatch):
        fake = _FakePG(avg_by_slot={ComponentSlot.motherboard: 11000})
        monkeypatch.setattr(costs, "PostgresClient", lambda: fake)

        assert costs.reused_part_value(ComponentSlot.motherboard) == 11000

    def test_falls_back_to_stub_when_postgres_raises(self, monkeypatch):
        fake = _FakePG(raises=True)
        monkeypatch.setattr(costs, "PostgresClient", lambda: fake)

        assert costs.reused_part_value(ComponentSlot.gpu) == (
            costs._REUSED_PART_VALUE_INR[ComponentSlot.gpu]
        )

    def test_falls_back_to_stub_when_zero_in_stock_parts(self, monkeypatch):
        fake = _FakePG(avg_by_slot={ComponentSlot.fans: None})
        monkeypatch.setattr(costs, "PostgresClient", lambda: fake)

        assert costs.reused_part_value(ComponentSlot.fans) == (
            costs._REUSED_PART_VALUE_INR[ComponentSlot.fans]
        )

    def test_never_returns_zero_silently_where_stub_exists(self, monkeypatch):
        fake = _FakePG(raises=True)
        monkeypatch.setattr(costs, "PostgresClient", lambda: fake)

        for slot in ComponentSlot:
            assert costs.reused_part_value(slot) != 0, (
                f"{slot} has a nonzero stub value but reused_part_value returned 0"
            )
