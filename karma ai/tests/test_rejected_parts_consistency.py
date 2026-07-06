"""Regression: catalog_floor.min_viable_build must honour rejected parts.

hard_constraints.rejected_parts is honoured by Node 3's _fetch_floor (the actual
shortlist) and by diff_and_bias (incumbent bias), but historically NOT by
catalog_floor.min_viable_build — so a part the user explicitly rejected could
still anchor the feasibility verdict and Node 2's band-repair floor even though
Node 3 correctly excluded it from selection. All three now share ONE predicate
(catalog_floor.filter_rejected / rejected_product_ids).

These tests call min_viable_build directly with a synthetic in-memory catalog —
no Postgres, fully deterministic — over a fixture where the cheapest floor-meeting
GPU is the rejected one, so the fix is observable as a change in both the chosen
part and the total floor cost.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agents.feasibility.catalog_floor import min_viable_build
from agents.feasibility.resolver import CpuTier, GpuTier, ResolvedRequirements
from agents.schemas.brief import RejectedPart
from agents.schemas.slots import ComponentSlot


# A complete, compatible, in-stock synthetic build. Two GPUs meet the VRAM floor;
# the cheaper one (gpu-cheap, ₹12k) is the one the user rejects, leaving gpu-exp
# (₹18k) as the next-cheapest valid anchor. Every other slot has a single option,
# so the ONLY thing that moves the floor total is which GPU is picked.
def _synthetic_catalog() -> list[dict]:
    def part(pid, category, price, specs, name="part", brand="ACME"):
        return {
            "product_id": pid,
            "category": category,
            "name": name,
            "brand": brand,
            "price_inr": price,
            "in_stock": True,
            "specs": specs,
        }

    return [
        part("gpu-cheap", "gpu", 12000, {"vram_gb": 8, "tdp_watts": 130}, name="RTX 4060"),
        part("gpu-exp", "gpu", 18000, {"vram_gb": 8, "tdp_watts": 130}, name="RTX 4070"),
        part("cpu-1", "cpu", 8000, {"cores": 6, "socket": "AM5", "tdp_watts": 65}, brand="AMD"),
        part("mb-1", "motherboard", 6000, {"socket": "AM5", "form_factor": "ATX", "ddr_type": 5}),
        part("ram-1", "ram", 3000, {"capacity_gb": 16, "ddr_gen": 5}),
        part("st-1", "storage", 3000, {"capacity_gb": 512, "interface": "NVMe PCIe 4.0"}),
        part("psu-1", "psu", 4000, {"wattage": 650}),
        part("case-1", "case", 3000, {"form_factor_support": ["ATX", "mATX"]}),
        part("cooler-1", "cooler", 2000, {"socket_compat": ["AM5", "AM4"]}),
        part("fans-1", "fans", 1000, {}),
    ]


def _req() -> ResolvedRequirements:
    # Floors every synthetic part comfortably meets — the point of the test is the
    # rejected-part exclusion, not the requirement floor itself.
    return ResolvedRequirements(
        gpu_tier=GpuTier.mid,
        cpu_tier=CpuTier.mid,   # min 6 cores
        vram_gb=6,
        ram_gb=16,
        storage_gb=256,
    )


# Non-GPU slots cost the same regardless of GPU pick; only the GPU differs.
_NON_GPU_TOTAL = 8000 + 6000 + 3000 + 3000 + 4000 + 3000 + 2000 + 1000  # ₹30,000


class TestMinViableBuildHonoursRejectedParts:
    def test_baseline_cheapest_gpu_anchors_floor(self, budget_gamer_brief):
        """Sanity: with nothing rejected, the cheapest GPU is the floor anchor."""
        result = min_viable_build(_synthetic_catalog(), _req(), budget_gamer_brief)
        assert result is not None
        total, parts = result
        assert parts[ComponentSlot.gpu]["product_id"] == "gpu-cheap"
        assert total == _NON_GPU_TOTAL + 12000

    def test_rejected_cheapest_gpu_is_excluded(self, budget_gamer_brief):
        """The regression: a rejected part must not anchor the floor.

        gpu-cheap is the cheapest floor-meeting GPU, but the user rejected it, so
        the floor must fall back to gpu-exp and the total must reflect that.
        """
        brief = budget_gamer_brief.model_copy(deep=True)
        brief.hard_constraints.rejected_parts.append(
            RejectedPart(
                product_id="gpu-cheap",
                reason="too weak",
                rejected_at=datetime.now(timezone.utc),
            )
        )

        result = min_viable_build(_synthetic_catalog(), _req(), brief)
        assert result is not None
        total, parts = result
        assert parts[ComponentSlot.gpu]["product_id"] == "gpu-exp", (
            "rejected gpu-cheap still anchored the floor — min_viable_build is not "
            "honouring hard_constraints.rejected_parts"
        )
        assert total == _NON_GPU_TOTAL + 18000
