"""Tests for agents/software_specs.py's shared LLM-backed, Postgres-cached
software minimum-spec lookup.

Replaces two independent stub tables — resolver.py's old _BASE_FLOOR_STUB and
node2_allocation.py's old _SOFTWARE_SPECS — with one function both callers now
route through. These tests never touch a real database or the OpenAI API:
PostgresClient and call_structured are monkeypatched at their point of use in
the software_specs module namespace, following test_costs.py's pattern.
"""

from __future__ import annotations

import pytest

from agents import software_specs
from agents.feasibility import resolver
from agents.feasibility.resolver import BaseFloor, CpuTier, GpuTier, _CATEGORY_FALLBACK_STUB
from agents.nodes import node2_allocation


class _FakePG:
    """Fake PostgresClient exposing only the two software-spec-cache methods."""

    def __init__(self, cached=None, read_raises=False, write_raises=False):
        self.cached = cached
        self.read_raises = read_raises
        self.write_raises = write_raises
        self.read_calls = 0
        self.write_calls = 0
        self.written = None

    def get_software_spec_cache(self, name):
        self.read_calls += 1
        if self.read_raises:
            raise RuntimeError("Postgres unreachable")
        return self.cached

    def set_software_spec_cache(self, name, category, *, gpu_tier, cpu_tier,
                                 vram_gb, ram_gb, storage_gb, source):
        self.write_calls += 1
        if self.write_raises:
            raise RuntimeError("Postgres unreachable")
        self.written = dict(
            name=name, category=category, gpu_tier=gpu_tier, cpu_tier=cpu_tier,
            vram_gb=vram_gb, ram_gb=ram_gb, storage_gb=storage_gb, source=source,
        )


class _FakeLLMResponse:
    def __init__(self, gpu_tier="mid", cpu_tier="mid", vram_gb=8, ram_gb=16, storage_gb=50):
        self.gpu_tier = gpu_tier
        self.cpu_tier = cpu_tier
        self.vram_gb = vram_gb
        self.ram_gb = ram_gb
        self.storage_gb = storage_gb


class TestGetSoftwareRequirements:
    def test_cache_hit_avoids_llm_call(self, monkeypatch):
        cached_row = {
            "category": "game", "gpu_tier": int(GpuTier.mid), "cpu_tier": int(CpuTier.entry),
            "vram_gb": 6, "ram_gb": 12, "storage_gb": 60,
        }
        fake_pg = _FakePG(cached=cached_row)
        llm_calls = []
        monkeypatch.setattr(software_specs, "PostgresClient", lambda: fake_pg)
        monkeypatch.setattr(
            software_specs, "call_structured",
            lambda *a, **k: llm_calls.append(1) or _FakeLLMResponse(),
        )

        floor = software_specs.get_software_requirements("GTA V", "game")

        assert floor == BaseFloor(
            gpu_tier=GpuTier.mid, cpu_tier=CpuTier.entry,
            vram_gb=6, ram_gb=12, storage_gb=60,
        )
        assert llm_calls == []
        assert fake_pg.write_calls == 0

    def test_cache_miss_calls_llm_once_and_persists(self, monkeypatch):
        fake_pg = _FakePG(cached=None)
        monkeypatch.setattr(software_specs, "PostgresClient", lambda: fake_pg)
        calls = []

        def fake_call_structured(prompt, response_model, **kwargs):
            calls.append(1)
            return _FakeLLMResponse(
                gpu_tier="high", cpu_tier="high", vram_gb=8, ram_gb=32, storage_gb=100,
            )

        monkeypatch.setattr(software_specs, "call_structured", fake_call_structured)

        floor = software_specs.get_software_requirements("DaVinci Resolve", "video")

        assert floor == BaseFloor(
            gpu_tier=GpuTier.high, cpu_tier=CpuTier.high,
            vram_gb=8, ram_gb=32, storage_gb=100,
        )
        assert len(calls) == 1
        assert fake_pg.write_calls == 1
        assert fake_pg.written["name"] == "davinci resolve"
        assert fake_pg.written["gpu_tier"] == int(GpuTier.high)
        assert fake_pg.written["source"] == "llm"

    def test_llm_failure_falls_back_to_category_stub_without_caching(self, monkeypatch):
        fake_pg = _FakePG(cached=None)
        monkeypatch.setattr(software_specs, "PostgresClient", lambda: fake_pg)

        def raising_call_structured(*a, **k):
            raise RuntimeError("LLM error")

        monkeypatch.setattr(software_specs, "call_structured", raising_call_structured)

        floor = software_specs.get_software_requirements("Some Unheard Of Game", "game")

        assert floor == _CATEGORY_FALLBACK_STUB["game"]
        assert fake_pg.write_calls == 0

    def test_malformed_llm_response_falls_back_without_caching(self, monkeypatch):
        fake_pg = _FakePG(cached=None)
        monkeypatch.setattr(software_specs, "PostgresClient", lambda: fake_pg)
        # Bad tier name -> KeyError inside _query_llm's GpuTier[...] lookup.
        monkeypatch.setattr(
            software_specs, "call_structured",
            lambda *a, **k: _FakeLLMResponse(gpu_tier="not_a_real_tier"),
        )

        floor = software_specs.get_software_requirements("Weird Title", "dev")

        assert floor == _CATEGORY_FALLBACK_STUB["dev"]
        assert fake_pg.write_calls == 0

    def test_postgres_read_failure_still_attempts_llm(self, monkeypatch):
        fake_pg = _FakePG(read_raises=True)
        monkeypatch.setattr(software_specs, "PostgresClient", lambda: fake_pg)
        monkeypatch.setattr(software_specs, "call_structured", lambda *a, **k: _FakeLLMResponse())

        floor = software_specs.get_software_requirements("Valorant", "game")

        assert floor.gpu_tier == GpuTier.mid

    def test_postgres_write_failure_does_not_crash_and_returns_llm_result(self, monkeypatch):
        fake_pg = _FakePG(cached=None, write_raises=True)
        monkeypatch.setattr(software_specs, "PostgresClient", lambda: fake_pg)
        monkeypatch.setattr(
            software_specs, "call_structured",
            lambda *a, **k: _FakeLLMResponse(vram_gb=12),
        )

        floor = software_specs.get_software_requirements("Blender", "3d")

        assert floor.vram_gb == 12

    def test_lookup_key_is_lowercased_and_stripped(self, monkeypatch):
        fake_pg = _FakePG(cached=None)
        monkeypatch.setattr(software_specs, "PostgresClient", lambda: fake_pg)
        monkeypatch.setattr(software_specs, "call_structured", lambda *a, **k: _FakeLLMResponse())

        software_specs.get_software_requirements("  Cyberpunk 2077  ", "game")

        assert fake_pg.written["name"] == "cyberpunk 2077"


class TestSharedAcrossCallers:
    """resolver.py and node2_allocation.py must route through the same function
    — no duplicate per-title lookup logic left in either module."""

    def test_resolver_routes_through_get_software_requirements(self, monkeypatch):
        calls = []

        def fake_get(name, category):
            calls.append((name, category))
            return BaseFloor(
                gpu_tier=GpuTier.entry, cpu_tier=CpuTier.entry,
                vram_gb=2, ram_gb=4, storage_gb=10,
            )

        monkeypatch.setattr(software_specs, "get_software_requirements", fake_get)

        result = resolver._lookup_base_floor("Some Game", "game")

        assert calls == [("Some Game", "game")]
        assert result.vram_gb == 2

    def test_node2_allocation_routes_through_get_software_requirements(
        self, monkeypatch, budget_gamer_brief,
    ):
        calls = []

        def fake_get(name, category):
            calls.append((name, category))
            return BaseFloor(
                gpu_tier=GpuTier.high, cpu_tier=CpuTier.mid,
                vram_gb=8, ram_gb=16, storage_gb=50,
            )

        monkeypatch.setattr(software_specs, "get_software_requirements", fake_get)

        hints = node2_allocation._build_software_hints(budget_gamer_brief)

        assert calls == [
            (entry.name, entry.category) for entry in budget_gamer_brief.software
        ]
        assert "Valorant" in hints
        assert "GPU tier=high" in hints
        assert "CPU tier=mid" in hints
        assert "RAM=16GB" in hints

    def test_no_duplicate_stub_tables_remain(self):
        assert not hasattr(resolver, "_BASE_FLOOR_STUB")
        assert not hasattr(node2_allocation, "_SOFTWARE_SPECS")
