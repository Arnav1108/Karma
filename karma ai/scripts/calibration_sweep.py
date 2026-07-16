"""Calibration sweep — empirical ground truth for Node 2 bands vs estimate.py verdicts.

Answers, per build profile, from REAL catalog stock (live Postgres, in-stock only):

  1. What is the minimum-cost COMPLETE build that satisfies the resolved requirement
     floors AND the three hard compatibility families (socket, DDR gen, form factor)?
     -> the ground truth the feasibility verdict is calibrated against.
  2. Do Node 2's price bands (static profile skew, no LLM) contain viable stock per
     slot — before AND after the catalog-grounding repair pass?
  3. Does a deterministic Node-3 walk over those bands complete without escalations
     or dead-ends?

IMPORTANT: this script imports the PRODUCTION primitives (catalog_floor,
estimate._deterministic_verdict, node2._repair_bands_to_catalog) rather than
copying their logic — it validates what actually ships. Re-run it whenever the
catalog, the allocation profiles, or the verdict thresholds change.

Deterministic by design (static allocation profiles, cheapest-pick walk proxy).
No LLM calls unless --live-verdict is passed (which additionally records the full
estimate_feasibility output, prose included).

Run from `karma ai/`:
    python -m scripts.calibration_sweep [--live-verdict]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import costs as _costs
from agents.db.postgres import PostgresClient
from agents.feasibility.catalog_floor import (
    CatalogFloor,
    min_viable_build,
    slot_requirement_filter,
)
from agents.feasibility.estimate import _TIGHT_RATIO, _deterministic_verdict
from agents.feasibility.resolver import resolve_requirements
from agents.nodes.node2_allocation import (
    _build_shopping_list,
    _compute_bands,
    _get_profile,
    _repair_bands_to_catalog,
)
from agents.nodes.node3_selector import (
    SELECTION_ORDER,
    _BAND_WIDEN_FACTOR,
    _floor_desc,
)
from agents.schemas.brief import UserBuildBrief
from agents.schemas.slots import ComponentSlot

_FIXTURES = Path(__file__).resolve().parent.parent / "data" / "fixtures"

PROFILES = [
    ("budget_gamer", _FIXTURES / "budget_gamer.json"),
    ("video_editor", _FIXTURES / "video_editor.json"),
    ("ml_workstation", _FIXTURES / "ml_workstation.json"),
    ("edge_intel_gamer", _FIXTURES / "edge_intel_gamer.json"),
    ("edge_tight_amd", _FIXTURES / "edge_tight_amd.json"),
    ("edge_floor_violating_cheapest", _FIXTURES / "edge_floor_violating_cheapest.json"),
    ("edge_floor_at_band_low", _FIXTURES / "edge_floor_at_band_low.json"),
]

# Fixtures whose whole point is to exercise Node 3 floor enforcement.
#   _RESCUE: the cheapest part in-band violates a floor → the filter must exclude it.
#   _BOUNDARY: a floor part sits exactly on the repaired band's >= low edge → the
#              fetch's inclusive lower bound must still return it.
_ADVERSARIAL_RESCUE = {"edge_floor_violating_cheapest"}
_ADVERSARIAL_BOUNDARY = {"edge_floor_at_band_low"}


def _load_brief(path: Path) -> UserBuildBrief:
    return UserBuildBrief.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _simulate_node3_walk(catalog, bands, brief, req, verdict_tight):
    """Deterministic proxy for Node 3's funnel: cheapest floor-meeting pick per
    slot, band -> 20% widen -> full-catalog escalation -> over-budget dead-end.

    The requirement floor is applied as a HARD filter over the whole per-slot
    pool (mirroring node3._fetch_floor), so every candidate the walk ever
    considers already meets the floor — band widening and escalation relax price
    only, never the floor. DDR4 bias mirrors production: fires when the verdict
    is tight AND an in-stock DDR4 kit meets the resolved RAM floor.
    Returns (spend, locked{slot:part}, events).
    """
    reused = set(req.reused_slots)
    locked: dict[ComponentSlot, dict] = {}
    events, spend = [], 0
    in_stock = [p for p in catalog if p["in_stock"]]

    # NOTE: This is a known-approximate offline proxy for the production
    # compatibility rules in agents/db/neo4j.py (_CONSTRAINT_MAP /
    # compatibility_check), NOT an exact mirror. Known drifts:
    #   1. Missing the (cooler, motherboard) socket-compatibility pair that
    #      _CONSTRAINT_MAP enforces — compat_ok does not check it.
    #   2. Uses a ddr_gen -> ddr_type fallback for the RAM/motherboard DDR
    #      check that the production _DDR_COMPAT_QUERY has no equivalent for.
    #   3. No "fail open on missing data" semantics — production passes
    #      through candidates absent from the graph (fail open), but
    #      compat_ok resolves missing spec keys to None-comparisons instead.
    # This proxy can drift from _CONSTRAINT_MAP over time and should be
    # re-validated against it if the sweep is ever relied on as a go/no-go
    # signal.
    def compat_ok(part, slot):
        s = part["specs"]
        for lslot, lp in locked.items():
            ls = lp["specs"]
            pair = (slot, lslot)
            if pair in ((ComponentSlot.cpu, ComponentSlot.motherboard),
                        (ComponentSlot.motherboard, ComponentSlot.cpu)):
                if s.get("socket") != ls.get("socket"):
                    return False
            if pair in ((ComponentSlot.ram, ComponentSlot.motherboard),
                        (ComponentSlot.motherboard, ComponentSlot.ram)):
                a = s.get("ddr_gen", s.get("ddr_type"))
                b = ls.get("ddr_gen", ls.get("ddr_type"))
                if a != b:
                    return False
            if pair == (ComponentSlot.case, ComponentSlot.motherboard):
                if ls.get("form_factor") not in s.get("form_factor_support", []):
                    return False
            if pair == (ComponentSlot.cooler, ComponentSlot.cpu):
                if ls.get("socket") not in s.get("socket_compat", []):
                    return False
        return True

    for slot in SELECTION_ORDER:
        if slot in reused:
            continue
        band = bands.root[slot]
        raw_pool = [p for p in in_stock if p["category"] == slot.value]
        # HARD floor filter at the pool level — the sweep's mirror of _fetch_floor.
        pool = sorted(
            slot_requirement_filter(slot, raw_pool, req, brief, enforce_brand=False),
            key=lambda p: p["price_inr"],
        )
        if not pool:
            events.append(f"{slot.value}: DEAD-END no_floor (no in-stock part meets floor)")
            continue
        cands = [p for p in pool if band.low <= p["price_inr"] <= band.high]
        if not cands:
            lo, hi = int(band.low * (1 - _BAND_WIDEN_FACTOR)), int(band.high * (1 + _BAND_WIDEN_FACTOR))
            cands = [p for p in pool if lo <= p["price_inr"] <= hi]
            if cands:
                events.append(f"{slot.value}: band empty, widened 20%")

        if verdict_tight and slot == ComponentSlot.ram and cands:
            ddr4 = [c for c in cands if c["specs"].get("ddr_gen") == 4]
            if not ddr4:
                ceiling4 = int(band.high * (1 + _BAND_WIDEN_FACTOR))
                extra = [p for p in pool if p["price_inr"] <= ceiling4
                         and p["specs"].get("ddr_gen") == 4]
                if extra:
                    ids = {p["product_id"] for p in extra}
                    cands = extra + [c for c in cands if c["product_id"] not in ids]
                    events.append(f"{slot.value}: DDR4 pulled from below band floor")
            cands = ([c for c in cands if c["specs"].get("ddr_gen") == 4]
                     + [c for c in cands if c["specs"].get("ddr_gen") != 4])

        ok = [c for c in cands if compat_ok(c, slot)]
        if not ok:
            ok = [p for p in pool if compat_ok(p, slot)]
            if ok:
                events.append(
                    f"{slot.value}: no compatible part in band — FULL-CATALOG "
                    f"escalation (cheapest compatible ₹{ok[0]['price_inr']:,} vs "
                    f"band high ₹{band.high:,})"
                )
            else:
                events.append(f"{slot.value}: DEAD-END no_compatible")
                continue

        remaining = brief.budget.ceiling - spend
        afford = [c for c in ok if c["price_inr"] <= remaining]
        if not afford:
            events.append(
                f"{slot.value}: DEAD-END over_budget (cheapest compatible "
                f"₹{min(c['price_inr'] for c in ok):,} > remaining ₹{remaining:,})"
            )
            continue

        pick = afford[0]
        locked[slot] = pick
        spend += pick["price_inr"]
        drift = ""
        if pick["price_inr"] > band.high:
            drift = f"  [OVER band.high ₹{band.high:,}]"
        events.append(f"{slot.value}: ₹{pick['price_inr']:,} {pick['product_id']}{drift}")

    return spend, locked, events


def _band_coverage(catalog, bands, req, brief, shopping):
    """Per-slot: does the band contain stock at all / stock meeting the floors?"""
    rows = []
    in_stock = [p for p in catalog if p["in_stock"]]
    for slot in SELECTION_ORDER:
        if slot not in shopping:
            continue
        band = bands.root[slot]
        pool = [p for p in in_stock if p["category"] == slot.value]
        floor_ok = slot_requirement_filter(slot, pool, req, brief)
        wlo, whi = int(band.low * (1 - _BAND_WIDEN_FACTOR)), int(band.high * (1 + _BAND_WIDEN_FACTOR))
        in_band = [p for p in pool if band.low <= p["price_inr"] <= band.high]
        in_wide = [p for p in pool if wlo <= p["price_inr"] <= whi]
        floor_in_band = [p for p in floor_ok if band.low <= p["price_inr"] <= band.high]
        floor_in_wide = [p for p in floor_ok if wlo <= p["price_inr"] <= whi]
        below = [p for p in floor_ok if p["price_inr"] < band.low]
        cheapest_ok = min((p["price_inr"] for p in floor_ok), default=None)
        flag = ""
        if cheapest_ok is None:
            flag = "NO STOCK MEETS FLOOR"
        elif not floor_in_wide:
            flag = ("FLOOR-MEETING STOCK ONLY BELOW BAND" if below
                    else f"BAND MISSES FLOOR (cheapest viable ₹{cheapest_ok:,} > widened high ₹{whi:,})")
        rows.append((slot.value, band.low, band.mid, band.high,
                     len(in_band), len(in_wide), len(floor_in_band),
                     len(floor_in_wide), cheapest_ok, flag))
    return rows


def _print_coverage(catalog, bands, req, brief, shopping, title):
    print(f"\n  {title}:")
    print(f"    {'slot':<12} {'low':>8} {'mid':>8} {'high':>8} "
          f"{'#band':>5} {'#wide':>5} {'#ok':>4} {'#okW':>4} {'min-viable':>10}  flag")
    flags = 0
    for row in _band_coverage(catalog, bands, req, brief, shopping):
        slot, lo, mi, hi, nb, nw, nfb, nfw, cheap, flag = row
        cheap_s = f"₹{cheap:,}" if cheap else "—"
        flags += bool(flag)
        print(f"    {slot:<12} {lo:>8,} {mi:>8,} {hi:>8,} "
              f"{nb:>5} {nw:>5} {nfb:>4} {nfw:>4} {cheap_s:>10}  {flag}")
    return flags


def run_profile(name, path, catalog, live_verdict=False):
    brief = _load_brief(path)
    req = resolve_requirements(brief)

    shopping = set(_build_shopping_list(brief))
    fixed = _costs.core_fixed_costs(brief)
    floor, target, ceiling = _costs.core_pools(brief)
    profile = _get_profile(brief)
    skew = {s: float(profile.get(s, 5)) for s in shopping}
    bands = _compute_bands(skew, floor, target, ceiling)

    print(f"\n{'=' * 78}\nPROFILE: {name}")
    print(f"  budget ₹{brief.budget.comfortable_min:,}–₹{brief.budget.comfortable_max:,} "
          f"(ceiling ₹{brief.budget.ceiling:,}), scope={brief.budget.scope}, "
          f"fixed costs ₹{fixed:,} → core pool floor/target/ceiling "
          f"₹{floor:,}/₹{target:,}/₹{ceiling:,}")
    print(f"  resolved floor: gpu_tier={req.gpu_tier.name} cpu_tier={req.cpu_tier.name} "
          f"vram≥{req.vram_gb}GB ram≥{req.ram_gb}GB storage≥{req.storage_gb}GB "
          f"reused={[s.value for s in req.reused_slots] or '—'}")
    prefs = brief.existing.ecosystem_prefs
    print(f"  brand prefs: cpu={prefs.cpu_brand_pref or '—'} gpu={prefs.gpu_brand_pref or '—'}")

    # Ground truth via the PRODUCTION primitive.
    cf = CatalogFloor()
    hard = min_viable_build(catalog, req, brief, enforce_brand=True)
    if hard is not None:
        cf.hard_total, cf.hard_parts = hard
    if prefs.cpu_brand_pref or prefs.gpu_brand_pref:
        soft = min_viable_build(catalog, req, brief, enforce_brand=False)
        if soft is not None:
            cf.soft_total, cf.soft_parts = soft
    else:
        cf.soft_total, cf.soft_parts = cf.hard_total, cf.hard_parts

    def show(tag, total, parts):
        if total is None:
            print(f"  {tag}: NO complete compatible build exists in catalog")
            return
        line = ", ".join(f"{s.value}={p['product_id']}₹{p['price_inr']:,}"
                         for s, p in parts.items())
        print(f"  {tag}: ₹{total:,}  [{line}]")

    show("MIN BUILD (floors + brand prefs)", cf.hard_total, cf.hard_parts)
    show("MIN BUILD (floors, prefs relaxed)", cf.soft_total, cf.soft_parts)

    verdict, basis = _deterministic_verdict(cf, target, ceiling)
    print(f"  DETERMINISTIC verdict: {verdict}  [{basis}]")
    print(f"    [rule: soft>ceiling→impossible, hard>ceiling→tight(relax prefs), "
          f"hard>{_TIGHT_RATIO:.0%}·target→tight]")

    # Band coverage before and after the production repair pass.
    flags_before = _print_coverage(
        catalog, bands, req, brief, shopping,
        "BAND COVERAGE — RAW (static profile skew, before repair)")

    repaired = bands
    viable = cf.best_within(ceiling)
    if viable is not None:
        floor_prices = {s: p["price_inr"] for s, p in viable.items() if s in shopping}
        r = _repair_bands_to_catalog(bands, floor_prices)
        if r is not None:
            repaired = r
    flags_after = _print_coverage(
        catalog, repaired, req, brief, shopping,
        "BAND COVERAGE — REPAIRED (production _repair_bands_to_catalog)")

    # Repair invariants.
    assert repaired.total_mid() == bands.total_mid(), "sum(mid) changed by repair"
    assert repaired.total_high() == bands.total_high(), "sum(high) changed by repair"
    for s, b in repaired.root.items():
        assert b.low <= b.mid <= b.high, f"band ordering broken for {s.value}"
    if viable is not None and repaired is not bands:
        for s, p in viable.items():
            if s in shopping:
                b = repaired.root[s]
                assert b.low <= p["price_inr"] <= b.high, (
                    f"floor part not in repaired band for {s.value}")

    # Node 3 walk simulation over the repaired bands (with the DDR4-bias gate).
    ddr4_ok = any(
        p["specs"].get("ddr_gen") == 4 and p["specs"].get("capacity_gb", 0) >= req.ram_gb
        for p in catalog
        if p["category"] == "ram" and p["in_stock"]
    )
    tight = verdict == "tight" and ddr4_ok
    spend, locked, events = _simulate_node3_walk(catalog, repaired, brief, req, tight)
    filled = len(locked)
    print(f"\n  NODE-3 WALK SIM on repaired bands (cheapest floor-meeting pick, "
          f"ddr4_bias={'on' if tight else 'off'}):")
    for e in events:
        print(f"    {e}")
    print(f"    → {filled}/{len(shopping)} slots filled, total ₹{spend:,} "
          f"(budget ceiling ₹{brief.budget.ceiling:,})")

    # ── PRIMARY ASSERTION: every picked part meets its slot's resolved floor ──
    floor_violations = []
    for slot, part in locked.items():
        if not slot_requirement_filter(slot, [part], req, brief, enforce_brand=False):
            floor_violations.append((slot.value, part["product_id"]))
    assert not floor_violations, (
        f"{name}: Node-3 picked floor-VIOLATING parts: {floor_violations}")

    # ── Floor-enforcement demonstration over the operative (exact repaired) band ──
    # For each shopping slot, compare the cheapest part in-band IGNORING the floor
    # against the floor filter. A "rescue" = the naive cheapest would violate the
    # floor and the filter excluded it. Adversarial fixtures MUST rescue ≥1 slot.
    rescues, boundary_hits = [], []
    in_stock = [p for p in catalog if p["in_stock"]]
    for slot in SELECTION_ORDER:
        if slot not in shopping:
            continue
        b = repaired.root[slot]
        band_pool = sorted(
            (p for p in in_stock if p["category"] == slot.value
             and b.low <= p["price_inr"] <= b.high),
            key=lambda p: p["price_inr"],
        )
        if not band_pool:
            continue
        naive = band_pool[0]
        floored = slot_requirement_filter(slot, band_pool, req, brief, enforce_brand=False)
        if not slot_requirement_filter(slot, [naive], req, brief, enforce_brand=False):
            kept = floored[0]["product_id"] if floored else "—(dead-end/escalate)"
            rescues.append((slot, naive["product_id"], naive["price_inr"], kept))
        # Boundary: a floor-meeting part sitting exactly on the >= low edge.
        if floored and min(p["price_inr"] for p in floored) == b.low:
            cheap_floor = min(floored, key=lambda p: p["price_inr"])
            boundary_hits.append((slot.value, cheap_floor["product_id"], b.low))

    if rescues:
        print(f"\n  FLOOR RESCUES (cheapest in-band would violate; filter excluded it):")
        for slot, viol, price, kept in rescues:
            print(f"    {slot.value}: naive cheapest {viol} ₹{price:,} VIOLATES floor "
                  f"({_floor_desc(slot, req, brief)}) → kept {kept}")
    if boundary_hits:
        print(f"  FLOOR-AT-BAND-LOW (part priced exactly on the >= low fetch edge):")
        for slot_v, pid, lo in boundary_hits:
            print(f"    {slot_v}: {pid} at band.low ₹{lo:,} — included by >= low fetch")

    if name in _ADVERSARIAL_RESCUE:
        assert rescues, (
            f"{name}: expected the cheapest in-band part to violate a floor, but no "
            f"slot was rescued — fixture no longer bites, redesign it")
    if name in _ADVERSARIAL_BOUNDARY:
        assert boundary_hits, (
            f"{name}: expected a floor part exactly on the repaired band's >= low "
            f"edge, but none found — fixture no longer exercises the boundary")

    live = None
    if live_verdict:
        from agents.feasibility.estimate import estimate_feasibility
        v = estimate_feasibility(brief)
        live = v.verdict
        print(f"\n  LIVE estimate.py verdict: {v.verdict} "
              f"(binding={v.binding_constraint}; {v.reason})")
        if v.suggested_adjustments:
            for adj in v.suggested_adjustments:
                print(f"    suggest: {adj}")

    return {
        "name": name, "target": target, "ceiling": ceiling,
        "strict": cf.hard_total, "relaxed": cf.soft_total,
        "verdict": verdict, "live": live,
        "flags_before": flags_before, "flags_after": flags_after,
        "filled": filled, "shopping": len(shopping), "sim_spend": spend,
        "violations": len(floor_violations), "rescues": len(rescues),
    }


def main():
    live = "--live-verdict" in sys.argv
    catalog = PostgresClient().get_all_products()
    print(f"Catalog: {len(catalog)} products "
          f"({sum(1 for p in catalog if p['in_stock'])} in stock)")

    results = [run_profile(n, p, catalog, live) for n, p in PROFILES]

    print(f"\n{'=' * 78}\nCROSS-PROFILE SUMMARY")
    print(f"  {'profile':<30} {'verdict':>11} {'live':>11} {'flags b→a':>9} "
          f"{'floor viol':>10} {'rescues':>8} {'sim':>14}")
    for r in results:
        print(f"  {r['name']:<30} {r['verdict']:>11} {(r['live'] or '—'):>11} "
              f"{r['flags_before']}→{r['flags_after']:>4} "
              f"{r['violations']:>10} {r['rescues']:>8} "
              f"{r['filled']}/{r['shopping']}:{r['sim_spend']:,}")
    total_viol = sum(r["violations"] for r in results)
    print(f"\n  TOTAL FLOOR VIOLATIONS ACROSS ALL PROFILES: {total_viol} "
          f"(must be 0 — every picked part meets its slot's resolved floor)")


if __name__ == "__main__":
    main()
