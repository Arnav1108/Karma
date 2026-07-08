"""Shared non-component cost tables — the SINGLE source of truth.

Node 2 (budget pool computation) and the Feasibility Check (scope aggregation +
verdict pools) must subtract the same fixed costs, or their notions of the core
component budget silently drift apart. Before this module existed they did:
resolver.aggregate_scope charged ₹9,000 for an OEM Windows license and a flat
₹18,000 for any monitor, while node2_allocation charged ₹1,500 and ₹30,000
(1440p) for the same brief — a ₹19,500 disagreement on video_editor's core pool.

STUB: os_cost/monitor_cost/peripheral_cost values are hand-picked placeholders,
not market data — the catalog has no data for those categories. Replace with
live catalog/market data later — but replace them HERE, in one place.

reused_part_value(slot) is no longer a stub: it averages live in-stock
catalog prices per slot (see average_catalog_price below), falling back to
the old hand-picked table only when Postgres is unreachable or empty.

Public surface:
    os_cost(brief)               -> int   OS license cost by brief.operating_system.license
    monitor_cost(brief)          -> int   monitor cost if in scope and unowned, else 0
    core_fixed_costs(brief)      -> int   os_cost + monitor_cost (what Node 2 subtracts)
    core_pools(brief)            -> (floor, target, ceiling) core component budget pools
    peripheral_cost(type)        -> int   must-have peripheral stub cost
    reused_part_value(slot)      -> int   assumed value of a reused part (scope savings)
    average_catalog_price(slot)  -> int | None   cached live average in-stock price for slot
    refresh_catalog_price_cache()-> None  recompute the cache for every slot
"""

from __future__ import annotations

from .db.postgres import PostgresClient
from .schemas.brief import UserBuildBrief
from .schemas.slots import ComponentSlot

# ── OS license (INR) ──────────────────────────────────────────────────────────
_OS_COST: dict[str, int] = {
    "oem": 1500,
    "retail": 13000,
    "byo": 0,
    "na": 0,
}

# ── Monitor by target resolution (INR) ────────────────────────────────────────
_MONITOR_COST_BY_RESOLUTION: dict[str, int] = {
    "1080p": 10000,
    "1440p": 30000,
    "2560x1440": 30000,
    "4K": 55000,
    "3840x2160": 55000,
    "default": 20000,
}

_MONITOR_SCOPES = {"pc_plus_monitor", "full_setup"}

# ── Must-have peripherals (INR) ───────────────────────────────────────────────
_PERIPHERAL_COST_INR: dict[str, int] = {
    "keyboard": 3000, "mouse": 2000, "headset": 4000, "mic": 5000,
    "speakers": 4000, "drawing_tablet": 12000, "controller": 4500, "webcam": 3500,
}

# ── Assumed value of reused parts (INR) — fallback when the catalog average is
# unavailable (Postgres unreachable or zero in-stock parts for the slot) ──────
_REUSED_PART_VALUE_INR: dict[ComponentSlot, int] = {
    ComponentSlot.gpu: 25000, ComponentSlot.cpu: 15000, ComponentSlot.ram: 5000,
    ComponentSlot.storage: 8000, ComponentSlot.motherboard: 10000,
    ComponentSlot.psu: 6000, ComponentSlot.case: 5000, ComponentSlot.cooler: 3000,
    ComponentSlot.fans: 1500,
}

# Per-slot average in-stock catalog price (INR), computed once per process
# lifetime on first request. Call refresh_catalog_price_cache() to recompute.
_catalog_price_cache: dict[ComponentSlot, int] = {}


def _round_to_nearest_500(value: float) -> int:
    """Round-half-up to the nearest ₹500 (avoids round()'s banker's rounding)."""
    return int((value + 250) // 500) * 500


def _fetch_average_price(slot: ComponentSlot) -> int | None:
    """Query Postgres for slot's average in-stock price, rounded to ₹500.

    Returns None if Postgres is unreachable or the slot has zero in-stock
    parts — never raises.
    """
    try:
        avg = PostgresClient().get_avg_catalog_price(slot)
    except Exception:
        return None
    if not avg:
        return None
    return _round_to_nearest_500(avg)


def average_catalog_price(slot: ComponentSlot) -> int | None:
    """Average in-stock catalog price for slot (INR, rounded to nearest ₹500).

    Cached in-process after the first successful computation — never queries
    Postgres more than once per slot per process lifetime. Call
    refresh_catalog_price_cache() to recompute without restarting. Returns
    None if Postgres is unreachable or the slot has zero in-stock parts.
    """
    if slot in _catalog_price_cache:
        return _catalog_price_cache[slot]
    price = _fetch_average_price(slot)
    if price is not None:
        _catalog_price_cache[slot] = price
    return price


def refresh_catalog_price_cache() -> None:
    """Recompute the average in-stock catalog price for every slot.

    Rerunnable — safe to call any time the catalog changes, no restart
    required. Same rerunnable-sweep pattern as scripts/calibration_sweep.py.
    """
    for slot in ComponentSlot:
        price = _fetch_average_price(slot)
        if price is not None:
            _catalog_price_cache[slot] = price


def os_cost(brief: UserBuildBrief) -> int:
    return _OS_COST.get(brief.operating_system.license, 0)


def monitor_cost(brief: UserBuildBrief) -> int:
    """Monitor cost when in budget scope and not already owned, else 0."""
    if brief.budget.scope not in _MONITOR_SCOPES:
        return 0
    if brief.monitor.owned == "yes":
        return 0

    resolution = "default"
    if brief.monitor.target_specs and brief.monitor.target_specs.resolution:
        resolution = brief.monitor.target_specs.resolution.lower()

    for key in _MONITOR_COST_BY_RESOLUTION:
        if key.lower() in resolution or resolution in key.lower():
            return _MONITOR_COST_BY_RESOLUTION[key]
    return _MONITOR_COST_BY_RESOLUTION["default"]


def core_fixed_costs(brief: UserBuildBrief) -> int:
    """Fixed costs subtracted from the total budget to get the core component pool.

    Must-have peripherals are NOT subtracted here — pricing them requires the
    catalog (Node 3's responsibility). Keep this in lockstep with core_pools().
    """
    return os_cost(brief) + monitor_cost(brief)


def core_pools(brief: UserBuildBrief) -> tuple[int, int, int]:
    """(floor, target, ceiling) of the core component pool after fixed costs.

    This is THE definition of the budget pools: Node 2 allocates against these
    and the feasibility verdict is judged against these. One function, no drift.
    """
    fixed = core_fixed_costs(brief)
    floor = max(0, brief.budget.comfortable_min - fixed)
    target = max(floor, brief.budget.comfortable_max - fixed)
    ceiling = max(target, brief.budget.ceiling - fixed)
    return floor, target, ceiling


def peripheral_cost(peripheral_type: str) -> int:
    return _PERIPHERAL_COST_INR.get(peripheral_type, 0)


def reused_part_value(slot: ComponentSlot) -> int:
    """Assumed value of a reused part (INR) — live catalog average, falling
    back to the hand-picked stub table if Postgres is unreachable or the
    slot has zero in-stock parts. Never returns 0 silently where a stub
    value exists."""
    avg = average_catalog_price(slot)
    if avg is not None:
        return avg
    return _REUSED_PART_VALUE_INR.get(slot, 0)
