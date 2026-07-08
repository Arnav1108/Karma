"""Deterministic core of the Feasibility Check (DESIGN.md 2.2, steps 1 & 2).

This module is PURE deterministic Python over a `UserBuildBrief`:

    Step 1 - resolve_requirements()  -> ResolvedRequirements
    Step 2 - aggregate_scope()       -> ScopeAdjustments

NO LLM calls, NO network, NO inventory/price lookup. Step 3 (the LLM-assisted
cost estimate) is a separate later task and lives elsewhere.

Many lookup tables here are explicitly marked **STUB**: hand-picked placeholder
values good enough to exercise the aggregation logic on the Phase-0 fixtures.
They must be replaced with real benchmark / pricing data later. They are NOT a
real data source.
"""

from __future__ import annotations

import re
from enum import IntEnum
from typing import Literal

from pydantic import BaseModel, Field

from .. import costs as _costs
from ..schemas.brief import UserBuildBrief
from ..schemas.slots import ComponentSlot


# ---------------------------------------------------------------------------
# Tier vocabulary - ordered so that "peak demand wins" is a plain max().
# ---------------------------------------------------------------------------

class GpuTier(IntEnum):
    igpu = 0
    entry = 1
    mid = 2
    high = 3
    enthusiast = 4


class CpuTier(IntEnum):
    entry = 0
    mid = 1
    high = 2
    hedt = 3


def _bump(tier: IntEnum, levels: int) -> IntEnum:
    """Raise an ordered tier by `levels`, clamped to the enum's max member."""
    cls = type(tier)
    ceiling = max(int(member) for member in cls)
    return cls(min(int(tier) + levels, ceiling))


# ---------------------------------------------------------------------------
# Base-floor lookup  (software -> component floor)
# ---------------------------------------------------------------------------
# Per-title floors come from agents/software_specs.py: a Postgres-cached,
# LLM-backed lookup shared with node2_allocation.py (see that module's
# software-hints section). _CATEGORY_FALLBACK_STUB stays here as the shared
# fallback for when that lookup fails outright (LLM/Postgres unreachable).

class BaseFloor(BaseModel):
    gpu_tier: GpuTier
    cpu_tier: CpuTier
    vram_gb: int
    ram_gb: int
    storage_gb: int


# STUB: fallback floor by SoftwareEntry.category for unknown/unreachable titles.
_CATEGORY_FALLBACK_STUB: dict[str, BaseFloor] = {
    "game":  BaseFloor(gpu_tier=GpuTier.mid,  cpu_tier=CpuTier.mid,  vram_gb=6, ram_gb=16, storage_gb=80),
    "video": BaseFloor(gpu_tier=GpuTier.high, cpu_tier=CpuTier.high, vram_gb=8, ram_gb=32, storage_gb=100),
    "3d":    BaseFloor(gpu_tier=GpuTier.high, cpu_tier=CpuTier.high, vram_gb=8, ram_gb=32, storage_gb=60),
    "audio": BaseFloor(gpu_tier=GpuTier.igpu, cpu_tier=CpuTier.mid,  vram_gb=0, ram_gb=16, storage_gb=40),
    "dev":   BaseFloor(gpu_tier=GpuTier.entry, cpu_tier=CpuTier.mid, vram_gb=4, ram_gb=16, storage_gb=40),
    "other": BaseFloor(gpu_tier=GpuTier.mid,  cpu_tier=CpuTier.mid,  vram_gb=6, ram_gb=16, storage_gb=40),
}


def _lookup_base_floor(name: str, category: str) -> BaseFloor:
    """Per-title floor via the shared software_specs lookup.

    Local import: software_specs.py imports BaseFloor/GpuTier/CpuTier/
    _CATEGORY_FALLBACK_STUB from this module, so a top-level import here would
    be circular. This module never needs software_specs at import time — only
    when a title is actually looked up.
    """
    from .. import software_specs
    return software_specs.get_software_requirements(name, category)


# ---------------------------------------------------------------------------
# STUB performance-envelope scaling  (resolution / framerate raise GPU + VRAM)
# ---------------------------------------------------------------------------
# !!! STUB !!! Crude heuristic. Only applied when a graphics target resolution is
# present (e.g. ML workloads with target_resolution=None get no graphics bump).

_RES_GPU_BUMP_STUB = {"1080p": 0, "1440p": 1, "4K": 2}   # STUB: GPU tier levels
_RES_VRAM_BUMP_STUB = {"1080p": 0, "1440p": 2, "4K": 4}  # STUB: extra VRAM (GB)
# STUB: per-resolution GPU tier ceiling. Prevents a high-fps bump from pushing
# a mid-intensity game (e.g. GTA V at 1080p) past the ceiling for that resolution.
_RES_GPU_CAP_STUB = {"1080p": GpuTier.mid, "1440p": GpuTier.high, "4K": GpuTier.enthusiast}


def _apply_performance(floor: BaseFloor, perf) -> BaseFloor:
    """STUB: scale a per-app floor by the performance envelope."""
    if perf is None or perf.target_resolution is None:
        return floor
    res = perf.target_resolution
    gpu = _bump(floor.gpu_tier, _RES_GPU_BUMP_STUB.get(res, 0))
    vram = floor.vram_gb + _RES_VRAM_BUMP_STUB.get(res, 0)
    # STUB: a high framerate target nudges the GPU one more tier.
    fps = perf.target_framerate
    if fps == "max" or (isinstance(fps, int) and fps >= 144):
        gpu = _bump(gpu, 1)
    cap = _RES_GPU_CAP_STUB.get(res)
    if cap is not None and int(gpu) > int(cap):
        gpu = cap
    return BaseFloor(
        gpu_tier=gpu,
        cpu_tier=floor.cpu_tier,
        vram_gb=vram,
        ram_gb=floor.ram_gb,
        storage_gb=floor.storage_gb,
    )


# STUB: a single concurrency RAM bump (GB) when 2+ heavy workloads coexist.
_CONCURRENCY_RAM_BUMP_GB_STUB = 16


# ---------------------------------------------------------------------------
# Hard-constraint registry  (free-text Constraint.type -> resolved floor field)
# ---------------------------------------------------------------------------
# STUB registry, easy to extend. Each recognized `type` maps onto a numeric floor
# that is merged via the SAME max() as software-derived floors, so the constraint
# wins only when it is higher. Unrecognized / unparseable constraints are NEVER
# dropped - they are collected into ResolvedRequirements.unhandled_constraints.

_CONSTRAINT_FLOOR_REGISTRY: dict[str, str] = {
    "min_vram_gb": "vram_gb",
    "min_ram_gb": "ram_gb",
    "min_storage_gb": "storage_gb",
}


def _parse_gb(value: str) -> int | None:
    """Extract the first integer run from a free-text value.

    Handles the fixtures' clean ``"16"`` and a messier ``"min 16GB VRAM"`` alike;
    returns None when no digit run is present (caller treats that as unhandled).
    """
    match = re.search(r"\d+", value)
    return int(match.group()) if match else None


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------

class SoftwareDemand(BaseModel):
    """Per-app resolved floor (after performance scaling) - for transparency."""
    name: str
    gpu_tier: GpuTier
    cpu_tier: CpuTier
    vram_gb: int
    ram_gb: int
    storage_gb: int


class UnhandledConstraint(BaseModel):
    """A must_have/must_not constraint the resolver did not fold into a floor."""
    type: str
    value: str
    reason: str


class ResolvedRequirements(BaseModel):
    gpu_tier: GpuTier
    cpu_tier: CpuTier
    vram_gb: int
    ram_gb: int
    storage_gb: int
    form_factor: str | None = None
    brand_constraints: list[str] = []
    reused_slots: list[ComponentSlot] = []
    live_constraints: list[str] = []
    unhandled_constraints: list[UnhandledConstraint] = []
    breakdown: list[SoftwareDemand] = []


class ScopeLineItem(BaseModel):
    label: str
    slot: ComponentSlot | None = None
    amount_inr: int
    kind: Literal["add", "subtract"]


class ScopeAdjustments(BaseModel):
    total_inr: int
    line_items: list[ScopeLineItem] = []


# ---------------------------------------------------------------------------
# Step 1 - Requirements Resolver
# ---------------------------------------------------------------------------

def resolve_requirements(brief: UserBuildBrief) -> ResolvedRequirements:
    """Aggregate a component floor across the whole workload (DESIGN.md 2.2 step 1).

    Aggregation rules:
      - GPU tier, CPU tier, VRAM: MAX across software (peak demand wins).
      - Storage: ADDITIVE across software, then MAX with the brief's stated capacity.
      - RAM: MAX single-app floor, plus a concurrency bump if 2+ heavy workloads.
      - Hard constraints that raise the floor are folded in via the same MAX.
      - Reused parts: constraints stay live (cost is zeroed in the aggregator).
    """
    demands: list[SoftwareDemand] = []
    gpu = GpuTier.igpu
    cpu = CpuTier.entry
    vram = 0
    ram = 0
    storage_sum = 0
    heavy_count = 0

    for sw in brief.software:
        floor = _apply_performance(_lookup_base_floor(sw.name, sw.category), brief.performance)
        demands.append(SoftwareDemand(
            name=sw.name,
            gpu_tier=floor.gpu_tier,
            cpu_tier=floor.cpu_tier,
            vram_gb=floor.vram_gb,
            ram_gb=floor.ram_gb,
            storage_gb=floor.storage_gb,
        ))
        gpu = max(gpu, floor.gpu_tier)
        cpu = max(cpu, floor.cpu_tier)
        vram = max(vram, floor.vram_gb)
        ram = max(ram, floor.ram_gb)            # MAX single-app RAM floor
        storage_sum += floor.storage_gb         # ADDITIVE
        if sw.intensity == "heavy":
            heavy_count += 1

    live: list[str] = []

    # RAM concurrency bump: 2+ heavy workloads running together.
    if heavy_count >= 2:
        ram += _CONCURRENCY_RAM_BUMP_GB_STUB
        live.append(
            f"RAM concurrency bump +{_CONCURRENCY_RAM_BUMP_GB_STUB}GB "
            f"({heavy_count} heavy workloads)"
        )

    # Storage: stack workloads, then honour any stated capacity.
    storage = storage_sum
    if brief.storage.capacity_gb is not None:
        storage = max(storage, brief.storage.capacity_gb)

    # Fold in hard constraints (must_have) that raise a numeric floor.
    resolved_numeric = {"vram_gb": vram, "ram_gb": ram, "storage_gb": storage}
    unhandled: list[UnhandledConstraint] = []

    for c in brief.hard_constraints.must_have:
        field = _CONSTRAINT_FLOOR_REGISTRY.get(c.type)
        if field is None:
            unhandled.append(UnhandledConstraint(
                type=c.type, value=c.value, reason="unrecognized constraint type",
            ))
            live.append(f"unhandled must_have constraint: {c.type}={c.value}")
            continue
        parsed = _parse_gb(c.value)
        if parsed is None:
            unhandled.append(UnhandledConstraint(
                type=c.type, value=c.value, reason="could not parse numeric GB from value",
            ))
            live.append(f"unhandled must_have constraint (unparseable): {c.type}={c.value}")
            continue
        before = resolved_numeric[field]
        if parsed > before:
            live.append(f"{field} floor {before}->{parsed} raised by hard constraint {c.type}")
        resolved_numeric[field] = max(before, parsed)

    vram = resolved_numeric["vram_gb"]
    ram = resolved_numeric["ram_gb"]
    storage = resolved_numeric["storage_gb"]

    # must_not constraints aren't floor-raising, but must not vanish either.
    for c in brief.hard_constraints.must_not:
        unhandled.append(UnhandledConstraint(
            type=c.type, value=c.value, reason="must_not exclusion (not a floor)",
        ))
        live.append(f"exclusion (must_not): {c.type}={c.value}")

    # Physical form factor - small enclosures raise the floor / stay live.
    form_factor = None
    ff = brief.physical.form_factor_pref
    if ff != "no_preference":
        form_factor = ff
        if ff in ("sff_itx", "compact_matx"):
            live.append(f"form factor {ff} constrains part selection (raises floor)")

    # Brand preferences / exclusions stay live.
    brand_constraints: list[str] = []
    prefs = brief.existing.ecosystem_prefs
    if prefs.cpu_brand_pref:
        brand_constraints.append(f"cpu_brand_pref={prefs.cpu_brand_pref}")
    if prefs.gpu_brand_pref:
        brand_constraints.append(f"gpu_brand_pref={prefs.gpu_brand_pref}")

    # Reused parts: cost is zeroed by the aggregator; constraints stay live here.
    reused_slots: list[ComponentSlot] = []
    for part in brief.existing.reuse_parts:
        if part.action != "keep":
            continue
        reused_slots.append(part.slot)
        live.append(
            f"reused {part.slot.value}: {part.identifier} "
            f"(cost zeroed, slot/socket/form-factor constraint retained)"
        )

    return ResolvedRequirements(
        gpu_tier=gpu,
        cpu_tier=cpu,
        vram_gb=vram,
        ram_gb=ram,
        storage_gb=storage,
        form_factor=form_factor,
        brand_constraints=brand_constraints,
        reused_slots=reused_slots,
        live_constraints=live,
        unhandled_constraints=unhandled,
        breakdown=demands,
    )


# ---------------------------------------------------------------------------
# Step 2 - Scope Aggregator
# ---------------------------------------------------------------------------
# Cost tables live in agents/costs.py — the SAME tables Node 2 subtracts as
# fixed costs. Two independent stub tables here previously disagreed with Node 2
# by ₹19,500 on video_editor's core pool (monitor 18k vs 30k, OEM OS 9k vs 1.5k).

_PERIPHERAL_SCOPES = {"pc_plus_peripherals", "full_setup"}


def aggregate_scope(brief: UserBuildBrief) -> ScopeAdjustments:
    """Add non-component line items by budget.scope, minus reused-part savings.

    (DESIGN.md 2.2 step 2.)
    """
    scope = brief.budget.scope
    items: list[ScopeLineItem] = []

    # Monitor: only if unowned AND in scope (resolution-aware, shared with Node 2).
    monitor_inr = _costs.monitor_cost(brief)
    if monitor_inr > 0:
        items.append(ScopeLineItem(
            label="monitor", amount_inr=monitor_inr, kind="add",
        ))

    # OS license: charge by license type (shared table with Node 2).
    os_inr = _costs.os_cost(brief)
    if os_inr > 0:
        items.append(ScopeLineItem(
            label="os_license", amount_inr=os_inr, kind="add",
        ))

    # Must-have peripherals: only when peripherals are in scope.
    if scope in _PERIPHERAL_SCOPES:
        for p in brief.peripherals:
            if p.priority != "must_have":
                continue
            items.append(ScopeLineItem(
                label=f"peripheral:{p.type}",
                amount_inr=_costs.peripheral_cost(p.type),
                kind="add",
            ))

    # Reused parts: subtract their assumed value (cost zeroed in the build).
    for part in brief.existing.reuse_parts:
        if part.action != "keep":
            continue
        items.append(ScopeLineItem(
            label=f"reused:{part.identifier}",
            slot=part.slot,
            amount_inr=_costs.reused_part_value(part.slot),
            kind="subtract",
        ))

    total = sum(i.amount_inr if i.kind == "add" else -i.amount_inr for i in items)
    return ScopeAdjustments(total_inr=total, line_items=items)
