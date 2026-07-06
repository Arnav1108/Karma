"""Catalog-grounded build floor — the shared primitive behind verdicts AND bands.

Computes, from live Postgres stock, the cheapest COMPLETE build that satisfies
the resolved requirement floors and the three hard compatibility families the
graph enforces (CPU↔MB socket, MB↔RAM DDR generation, MB↔Case form factor,
plus Cooler↔CPU socket and a PSU wattage sanity margin).

Both consumers read THIS number, so they cannot drift apart:
  - estimate.py derives the feasibility verdict from min-build cost vs the core
    budget pools (deterministic; the LLM writes prose, not the verdict).
  - node2_allocation repairs its price bands so every slot's band contains the
    part that the minimum viable build would put there.

Calibrated empirically by scripts/calibration_sweep.py — rerun it whenever the
catalog, the allocation profiles, or the verdict thresholds change.

Degradation: if Postgres is unreachable, compute_catalog_floor returns None and
both consumers fall back to their previous (unanchored) behaviour.

Public surface:
    CatalogFloor                          (dataclass)
    compute_catalog_floor(brief, req)   -> CatalogFloor | None
    min_viable_build(catalog, req, brief, enforce_brand) -> (total, parts) | None
    slot_requirement_filter(...)          per-slot floor filter (used by the sweep)
    rejected_product_ids(brief)           canonical rejected-part id set (shared)
    filter_rejected(parts, brief)         drop rejected catalog rows (shared)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..db.postgres import PostgresClient
from ..schemas.brief import UserBuildBrief
from ..schemas.slots import ComponentSlot
from .resolver import CpuTier, ResolvedRequirements, resolve_requirements

logger = logging.getLogger(__name__)

# CPU tier -> minimum core count. Proxy mapping: resolver tiers are abstract and
# the catalog has no tier column, so cores stand in for tier. STUB-adjacent —
# revisit when real benchmark data lands (context.md open item 3).
_TIER_MIN_CORES = {CpuTier.entry: 2, CpuTier.mid: 6, CpuTier.high: 8, CpuTier.hedt: 12}

# PSU wattage sanity: wattage >= cpu_tdp + gpu_tdp + headroom. Enforced in TWO
# places off this single constant so they cannot drift: the min-viable-build
# floor below (which the feasibility verdict promises is buildable) and Node 3's
# PSU slot selection (via required_psu_wattage) — the actual pick is held to the
# identical bar the floor assumed.
_PSU_HEADROOM_W = 150

_NVIDIA_MARKERS = ("RTX", "GTX")


def _gpu_vendor(part: dict) -> str:
    """Chip vendor inferred from the product name (brand column is the AIB)."""
    name = part["name"].upper()
    if any(m in name for m in _NVIDIA_MARKERS):
        return "NVIDIA"
    if name.startswith("RX") or "RX " in name:
        return "AMD"
    return "UNKNOWN"


def rejected_product_ids(brief: UserBuildBrief) -> set[str]:
    """Product IDs the user has explicitly rejected in prior refinement rounds.

    THE single definition of "rejected" for the whole pipeline. All three
    consumers of the requirement floor read it, so they cannot drift:
      - min_viable_build below (feasibility verdict + Node 2 band repair),
      - node3_selector's _fetch_floor (the actual Node 3 shortlist),
      - node3_refinement's diff_and_bias (incumbent-bias validity check).

    hard_constraints.rejected_parts carries no slot field, but product_id is the
    catalog PRIMARY KEY and 1:1 with a single category/slot (data/catalog/seed.sql)
    — a product_id is never shared across slots. So this flat id set is inherently
    slot-scoped: a rejected GPU product_id can never match a RAM (or any other
    slot's) candidate.
    """
    return {r.product_id for r in brief.hard_constraints.rejected_parts}


def filter_rejected(parts: list[dict], brief: UserBuildBrief) -> list[dict]:
    """Drop catalog rows whose product_id the user has rejected. Order-preserving."""
    rejected = rejected_product_ids(brief)
    if not rejected:
        return parts
    return [p for p in parts if p.get("product_id") not in rejected]


def slot_requirement_filter(
    slot: ComponentSlot,
    parts: list[dict],
    req: ResolvedRequirements,
    brief: UserBuildBrief,
    enforce_brand: bool = True,
) -> list[dict]:
    """Parts in `slot` that satisfy the resolved requirement floors."""
    out = []
    prefs = brief.existing.ecosystem_prefs
    for p in parts:
        s = p["specs"]
        if slot == ComponentSlot.gpu:
            if s.get("vram_gb", 0) < req.vram_gb:
                continue
            if enforce_brand and prefs.gpu_brand_pref:
                if _gpu_vendor(p) != prefs.gpu_brand_pref.upper():
                    continue
        elif slot == ComponentSlot.cpu:
            if s.get("cores", 0) < _TIER_MIN_CORES[req.cpu_tier]:
                continue
            if enforce_brand and prefs.cpu_brand_pref:
                if p["brand"].upper() != prefs.cpu_brand_pref.upper():
                    continue
        elif slot == ComponentSlot.ram:
            if s.get("capacity_gb", 0) < req.ram_gb:
                continue
        elif slot == ComponentSlot.storage:
            if s.get("capacity_gb", 0) < req.storage_gb:
                continue
            if brief.storage.speed_tier == "nvme" and "NVMe" not in s.get("interface", ""):
                continue
        out.append(p)
    return out


def required_psu_wattage(locked_specs: dict[ComponentSlot, dict]) -> int:
    """Minimum PSU wattage for an already-selected set of parts.

    Sum of every locked component's ``tdp_watts`` plus the same headroom margin
    the feasibility floor (``min_viable_build``) assumes. Sharing ``_PSU_HEADROOM_W``
    is the whole point: the floor promises a PSU with this much headroom exists
    inside budget, so Node 3's PSU pick must clear the identical bar or that
    promise is empty. Only GPU and CPU carry ``tdp_watts`` in the catalog, but the
    sum is generic over all locked specs so a future power-drawing slot is caught.
    """
    total_tdp = sum(
        (specs or {}).get("tdp_watts", 0) for specs in locked_specs.values()
    )
    return total_tdp + _PSU_HEADROOM_W


def min_viable_build(
    catalog: list[dict],
    req: ResolvedRequirements,
    brief: UserBuildBrief,
    enforce_brand: bool = True,
) -> tuple[int, dict[ComponentSlot, dict]] | None:
    """Cheapest complete compatible in-stock build meeting the requirement floors.

    Brute force over the platform chain (GPU × CPU × MB × RAM) with cheapest
    valid picks for the dependent slots. Reused slots are excluded (cost zero).
    Returns (total_inr, {slot: catalog_row}) or None if no complete build exists.
    """
    reused = set(req.reused_slots)
    by_slot: dict[ComponentSlot, list[dict]] = {}
    for slot in ComponentSlot:
        parts = [p for p in catalog if p["category"] == slot.value and p["in_stock"]]
        # Honour user-rejected parts here too — the SAME exclusion Node 3 applies
        # in _fetch_floor and diff_and_bias — so a rejected part can never anchor
        # the feasibility verdict or the Node 2 band-repair floor.
        parts = filter_rejected(parts, brief)
        by_slot[slot] = slot_requirement_filter(slot, parts, req, brief, enforce_brand)

    def cheapest(parts: list[dict]) -> dict | None:
        return min(parts, key=lambda p: p["price_inr"]) if parts else None

    storage = None if ComponentSlot.storage in reused else cheapest(by_slot[ComponentSlot.storage])
    fans = None if ComponentSlot.fans in reused else cheapest(by_slot[ComponentSlot.fans])
    if ComponentSlot.storage not in reused and storage is None:
        return None
    if ComponentSlot.fans not in reused and fans is None:
        return None

    case_by_ff: dict[str, dict] = {}
    for c in by_slot[ComponentSlot.case]:
        for ff in c["specs"].get("form_factor_support", []):
            if ff not in case_by_ff or c["price_inr"] < case_by_ff[ff]["price_inr"]:
                case_by_ff[ff] = c

    psus = sorted(by_slot[ComponentSlot.psu], key=lambda p: p["price_inr"])
    coolers = sorted(by_slot[ComponentSlot.cooler], key=lambda p: p["price_inr"])

    gpus = by_slot[ComponentSlot.gpu] if ComponentSlot.gpu not in reused else [None]
    rams = by_slot[ComponentSlot.ram] if ComponentSlot.ram not in reused else [None]

    best: tuple[int, dict[ComponentSlot, dict]] | None = None
    for gpu in gpus:
        gpu_tdp = gpu["specs"].get("tdp_watts", 0) if gpu else 0
        for cpu in by_slot[ComponentSlot.cpu]:
            need_w = cpu["specs"].get("tdp_watts", 0) + gpu_tdp + _PSU_HEADROOM_W
            psu = next((p for p in psus if p["specs"].get("wattage", 0) >= need_w), None)
            if psu is None:
                continue
            cooler = next(
                (c for c in coolers
                 if cpu["specs"]["socket"] in c["specs"].get("socket_compat", [])),
                None,
            )
            if cooler is None:
                continue
            for mb in by_slot[ComponentSlot.motherboard]:
                if mb["specs"]["socket"] != cpu["specs"]["socket"]:
                    continue
                case = case_by_ff.get(mb["specs"]["form_factor"])
                if case is None:
                    continue
                for ram in rams:
                    if ram is not None and ram["specs"]["ddr_gen"] != mb["specs"]["ddr_type"]:
                        continue
                    build = {
                        ComponentSlot.gpu: gpu, ComponentSlot.cpu: cpu,
                        ComponentSlot.ram: ram, ComponentSlot.storage: storage,
                        ComponentSlot.motherboard: mb, ComponentSlot.psu: psu,
                        ComponentSlot.case: case, ComponentSlot.cooler: cooler,
                        ComponentSlot.fans: fans,
                    }
                    total = sum(p["price_inr"] for p in build.values() if p is not None)
                    if best is None or total < best[0]:
                        best = (total, {s: p for s, p in build.items() if p is not None})
    return best


@dataclass
class CatalogFloor:
    """The cheapest viable builds for a brief, from live catalog stock.

    hard_* honours ecosystem brand preferences; soft_* relaxes them. Either may
    be None when no complete compatible build exists under that pref regime.
    """
    hard_total: int | None = None
    hard_parts: dict[ComponentSlot, dict] = field(default_factory=dict)
    soft_total: int | None = None
    soft_parts: dict[ComponentSlot, dict] = field(default_factory=dict)

    def best_within(self, ceiling: int) -> dict[ComponentSlot, dict] | None:
        """Cheapest build that fits the ceiling, preferring the pref-honouring one."""
        if self.hard_total is not None and self.hard_total <= ceiling:
            return self.hard_parts
        if self.soft_total is not None and self.soft_total <= ceiling:
            return self.soft_parts
        return None


def compute_catalog_floor(
    brief: UserBuildBrief,
    req: ResolvedRequirements | None = None,
) -> CatalogFloor | None:
    """Compute the catalog floor, or None if the catalog is unreachable.

    Never raises: consumers degrade to their unanchored behaviour on None.
    """
    if req is None:
        req = resolve_requirements(brief)
    try:
        catalog = PostgresClient().get_all_products()
    except Exception as exc:  # noqa: BLE001 — degrade, never abort the pipeline
        logger.warning("[CatalogFloor] catalog unreachable (%s) — floor unavailable",
                       type(exc).__name__)
        return None

    floor = CatalogFloor()
    hard = min_viable_build(catalog, req, brief, enforce_brand=True)
    if hard is not None:
        floor.hard_total, floor.hard_parts = hard
    prefs = brief.existing.ecosystem_prefs
    if prefs.cpu_brand_pref or prefs.gpu_brand_pref:
        soft = min_viable_build(catalog, req, brief, enforce_brand=False)
        if soft is not None:
            floor.soft_total, floor.soft_parts = soft
    else:
        floor.soft_total, floor.soft_parts = floor.hard_total, floor.hard_parts
    return floor
