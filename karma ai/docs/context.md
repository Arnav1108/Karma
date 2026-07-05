## Karma Advisor — state as of 2026-07-04

**Feasibility verdict investigation: both open questions CLOSED (2026-07-04),
no code changes. Do not re-open these as mysteries.**
- **budget_gamer "tight" is CORRECT** (re-confirmed — second time; see the
  2026-07-02 note "tight was always CORRECT"). Min build ₹66,000 = 104% of the
  core target ₹63,500; with the ₹1,500 OS the total ₹67,500 exceeds
  comfortable_max ₹65,000 and fits only under the ₹70,000 ceiling. Not a
  _TIGHT_RATIO calibration artifact (ratio is above 1.0, so any threshold ≤ 1.0
  still says tight) and not prompt calibration (verdict is code-owned since
  6ea3920; the LLM writes prose only and is overridden if it disagrees). The
  "expected comfortable" intuition is about real-world Indian market pricing,
  not this catalog: the cheapest in-stock GPU is ₹27,500 (RTX 4060; the only
  cheaper card, gpu-008 RX 7600 XT ₹26,500, is out of stock), eating 43% of the
  target pool. Even with all resolver floors relaxed the min build is ~₹62,500
  (98% of target) — still tight. Re-open only if the catalog gains a genuine
  budget GPU tier.
- **video_editor "impossible" DOES NOT REPRODUCE.** Live estimate_feasibility
  and the deterministic sweep both return tight (binding = brand preferences):
  hard/NVIDIA floor ₹154,700 > core ceiling ₹143,500 (cheapest in-stock NVIDIA
  16 GB card is ₹76,000), soft floor ₹120,700 (RX 7800 XT ₹42,000) fits with
  ₹22,800 headroom — hand-verified as a real compatible in-stock build (48 GB
  RAM floor forces the single 64 GB DDR5 kit ₹22,000 → DDR5 board; cheapest
  ≥8-core platform is i5-14400F ₹16,500 + DDR5 LGA1700 board ₹15,000). Stock
  drift ruled out: seed.sql has exactly one commit and the AMD 16 GB cards
  (gpu-009/013/014) have been in_stock=TRUE since day one. The "impossible"
  observation was almost certainly the legacy single-anchor LLM path — either a
  run predating 6ea3920 (LLM owned the verdict then; documented as flipping
  between identical runs) or a post-merge run with Postgres unreachable, which
  silently falls back to that same path.
- Diagnostic gap exposed: FeasibilityVerdict carries no provenance, so a silent
  LLM fallback is indistinguishable in output from a real deterministic
  verdict — which is how a stale "impossible" got reported as current
  behaviour. Ticket drafted for `basis: deterministic | llm_fallback` field
  (open item 3); implementation deferred.

## Karma Advisor — state as of 2026-07-03

**Node 3 requirement-floor enforcement: DONE (2026-07-03), query-layer hard filter.**
- Gap closed: resolve_requirements() computed per-slot floors but Node 3 never
  applied them, so it shipped floor-violating parts (HDD against an NVMe brief,
  sub-floor RAM kit). Repaired price bands made floor-satisfying parts reachable
  by price but did not constrain the pick to them.
- Enforced floors (the full set resolve_requirements yields that maps to catalog
  specs): GPU `vram_gb`, CPU `cpu_tier`→min cores, RAM `capacity_gb`, storage
  `capacity_gb` + `brief.storage.speed_tier` (NVMe interface). `form_factor` is
  compatibility (already graph-enforced); `brand_constraints` are PREFERENCES,
  deliberately NOT floored (a 'tight' verdict is defined as buildable only after
  relaxing them — flooring brand here would dead-end a gate-feasible build).
- Implementation mirrors compatibility, not the old DDR4 bias: new
  `node3_selector._fetch_floor` wraps every catalog fetch (band, widened band,
  DDR4 pull, both full-catalog escalations, lookahead probe) and drops
  floor-violating parts before they reach the shortlist — reusing
  `catalog_floor.slot_requirement_filter(enforce_brand=False)`, the SAME
  predicate that defines the min-viable build the bands are pinned to. No
  post-hoc "log a violation" check anywhere.
- Escalation ladder confirmed: only the price band widens (band→+20%→full
  catalog); floor and compatibility are both hard filters that survive every
  step. New `no_floor` dead-end status when no in-stock part meets the floor at
  any price (distinct from `no_compatible` / `over_budget`).
- select_part gained a required `req: ResolvedRequirements` param (resolved once
  per build in select_build / _select_build_with_pins and threaded down).
- Verified: real select_build on edge_floor_violating_cheapest picks 2TB **NVMe**
  storage + **32 GB** RAM (cheaper HDD/SATA/16 GB parts excluded); `no_floor`
  fires for an impossible min_vram_gb=48 floor after full-catalog escalation.
- calibration_sweep.py now asserts every picked part meets its slot's resolved
  floor across all 7 profiles (0 violations), and two new adversarial fixtures:
  edge_floor_violating_cheapest (cheapest in-band part is floor-violating →
  filter must rescue) and edge_floor_at_band_low (floor part sits exactly on the
  repaired band's `>= low` edge). budget_gamer + edge_tight_amd also register
  rescues — the HDD-against-NVMe bug was live in the shipping fixtures.
- 10/10 integration tests pass.

## Karma Advisor — state as of 2026-07-02

**Neo4j knowledge graph: live and enforced.**
- Enterprise edition (local Docker) — required for NODE_KEY constraints; Community fails silently on apply_schema()
- Seeded: 103 products, 9 ComponentClass, 9 Spec, 5 UseCase, 4 Performance nodes + relationships
- All 3 compatibility families (socket, DDR generation, form-factor) are live, hard-filtered (never bypassed during relaxation), verified bidirectionally + end-to-end
- Original bug (LGA1700 CPU + AM4 board) is now impossible

**Verdict + allocation calibration: DONE (2026-07-02), catalog-grounded.**
- New shared primitive `agents/feasibility/catalog_floor.py`: min-cost COMPLETE
  compatible in-stock build meeting the resolved floors (socket/DDR/form-factor
  chains + PSU wattage sanity), computed twice — brand prefs honoured ("hard")
  and relaxed ("soft"). Both estimate.py and Node 2 consume THIS, so they cannot
  drift independently.
- `estimate.py`: verdict is now DETERMINISTIC when Postgres is reachable
  (LLM writes prose only; code owns the verdict field). Rule:
  soft>core_ceiling → impossible; hard>core_ceiling → tight (suggest relaxing
  prefs); hard > 0.85×core_target → tight; else comfortable. 0.85 calibrated by
  sweep (budget_gamer 1.04 tight vs edge_intel_gamer 0.82 comfortable).
  Legacy single-anchor LLM estimate survives only as the Postgres-down fallback
  (sweep showed it flipping verdicts between identical runs).
- `node2_allocation.py`: new deterministic post-step `_repair_bands_to_catalog`
  pins every band to the min-viable-build part price (raise deficient highs
  funded from surplus highs; lower lows that excluded cheaper viable stock).
  Preserves sum(mid)==target and sum(high)==ceiling exactly; deliberately
  relaxes sum(low)==floor (lows are query bounds, not spend plans).
- `agents/costs.py`: single source of truth for OS/monitor/peripheral/reused
  stub costs + `core_pools()`. Previously resolver and node2 disagreed by
  ₹19,500 on video_editor's core pool (monitor 18k vs 30k, OEM OS 9k vs 1.5k).
- DDR4 bias now gated by `_ddr4_can_meet_ram_floor`: fires only when an
  in-stock DDR4 kit meets the resolved RAM floor (a verdict can be tight for
  pref reasons; catalog has no DDR4 kit >32 GB — biasing a 64 GB build stranded
  the floor).
- `scripts/calibration_sweep.py`: rerunnable harness over 5 profiles
  (3 fixtures + data/fixtures/edge_intel_gamer.json + edge_tight_amd.json).
  Imports the PRODUCTION primitives (not copies). Run it whenever the catalog,
  allocation profiles, or verdict thresholds change:
  `python -m scripts.calibration_sweep [--live-verdict]`
- Sweep results (2026-07-02): budget_gamer tight (min ₹66,000 vs target
  ₹63,500 — tight was always CORRECT); video_editor tight-not-impossible
  (₹154,700 hard / ₹120,700 soft vs ceiling ₹143,500 — hinges on NVIDIA pref);
  ml_workstation tight (RTX 4090 ₹175k is the only 24 GB NVIDIA card);
  edge_intel_gamer comfortable, now builds fully in-band (was: DDR5 RAM pick
  stranding the motherboard band → ₹15k escalation); edge_tight_amd impossible
  (catalog's cheapest complete discrete-GPU build is ₹66,000 — was reaching
  Node 3 and shipping 8/9 builds with ₹200 headroom).
- 10/10 integration tests pass.

## Open items, priority order
1. **Fitness/GOOD_FOR weights** — still stubbed from placeholder table, not
   real benchmark data (separate edge family from compatibility). Note the
   CPU-tier→min-cores map in catalog_floor._TIER_MIN_CORES is also a stub proxy
   (no tier column in the catalog); it is now a HARD filter in Node 3, so
   replacing it with real data is higher-stakes than before.
2. **Aura migration** — infra is local Docker only, not reachable by deployed
   backend; required pre-production (env swap only, seed is idempotent)
3. **Verdict provenance** — add `basis: deterministic | llm_fallback` to
   FeasibilityVerdict so the silent Postgres-unreachable fallback is
   distinguishable in output (ticket drafted 2026-07-04, see verdict
   investigation above; implementation deferred to a separate session)
4. **Gaming fitness threshold calibration — RESOLVED (2026-07-04,
   `phase4/fitness-filter-softgate`).** Root cause confirmed via live
   diagnostics against all 7 fixtures + a new `high_end_gamer` control fixture:
   `fitness_filter` converted the budget-blind `derive_fitness_thresholds`
   value into an absolute catalog-wide `required_tier` via
   `min(4, int(threshold*5))` and hard-excluded anything below it. Gaming's
   GPU threshold is uniformly 0.85 → tier 4 (catalog flagships) regardless of
   budget, and no gaming fixture's price band ever reached tier 3+, so
   `fitness_filter` returned `[]` for nearly every gaming build, silently
   triggering the fail-open in `node3_selector.py` and leaving fitness with no
   real influence over GPU/CPU selection except at the top of the market.
   Confirmed present (lower frequency, masked by one non-monotonic outlier)
   on CPU too.
   Fix, two parts:
   - `agents/db/neo4j.py::fitness_filter` — `required_tier` is now a soft
     tie-break, never a hard cutoff. All in-band candidates with a `GOOD_FOR`
     edge are ranked by continuous `score` (descending); no-edge candidates
     still pass through unranked (fail-open semantics unchanged).
   - `agents/nodes/node3_selector.py::select_part` Step 3 — the LLM final-pick
     prompt previously had no visibility into fitness rank, so once the
     shortlist was widened by the fix above, it would independently
     "value-optimize" down from the top-fitness pick to a cheaper, lower-tier
     part — a regression only exposed by fixing the exclusion bug. The prompt
     now states each candidate's fitness rank and the slot's derived
     threshold, as a signal to weigh alongside price/specs — deliberately not
     a hard pin to rank 1.
   Verified live (real Node 3 path, not `calibration_sweep.py`) across all 8
   fixtures: fail-open never fires (0/16 slot checks across two full runs);
   `fitness_filter`'s own ranking always puts the top-tier/top-score candidate
   at rank 1 (confirmed independently of Step 3). `high_end_gamer` picks
   gpu-012 (tier 4, rank 1) end to end. One residual: `ml_workstation`'s CPU
   pick landed at rank 2/tier 3 in the verification run because
   `derive_fitness_thresholds` itself returned 0.75 instead of 0.80 that run
   (required_tier 3 vs 4) — separate, pre-existing LLM-threshold variance,
   not a regression from this fix; not further pursued per this session's
   scope.
5. **RAM/STORAGE `GOOD_FOR` edges are 100% pre-migration stub data — no real
   tier/score.** Surfaced 2026-07-05 while fixing a second bug found during
   item 4 verification: `fitness_ranked` in `node3_selector.py` was set to
   `True` whenever `fitness_filter` returned a non-empty list, but the
   function fails open (returns every candidate, unranked) for categories
   with zero `GOOD_FOR` coverage — so motherboard/psu/case/cooler/fans (0
   edges, confirmed via direct graph query) were mislabeled as
   fitness-ranked in the Step 3 LLM prompt ("best-fit-first" / "(fitness rank
   #N)") despite having no signal at all. Fixed in `agents/db/neo4j.py` /
   `agents/nodes/node3_selector.py`
   (`fix(graph): make fitness_ranked reflect real GOOD_FOR data, not
   fail-open non-emptiness`, `phase4/fitness-filter-softgate`):
   `fitness_filter` now returns `FitnessRanking(ordered_ids, is_real_ranking)`
   instead of a plain list, with `is_real_ranking = bool(scored)` computed
   from data the function already gathers — no extra query, no
   category-level pre-check that could drift from the per-call ranking.
   While live-verifying that fix across all 8 fixtures, RAM and STORAGE
   turned up with `is_real_ranking=False` too, despite a direct graph query
   showing 55 and 60 `GOOD_FOR` edges respectively (i.e. real coverage by
   count). A follow-up query explains it: 0/55 RAM edges and 0/60 STORAGE
   edges carry `tier`/`score` — every one of them still only has the
   pre-migration `weight` property from before `1a69bb0`
   ("migrate fitness_filter from GOOD_FOR.weight to tier/score") and
   `a1f3410` ("remove GOOD_FOR stub weights that block fitness_filter
   fail-open"). Only GPU (42 edges) and CPU (48 edges) were ever re-seeded
   with real benchmark-derived tier/score data by `1dc9f32`; RAM/STORAGE
   were left on the old stub schema and never migrated. Practical effect:
   fitness signal today can only ever influence GPU/CPU selection — RAM and
   STORAGE always fail open, correctly now (previously incorrectly masked as
   "ranked" by the item-4-adjacent bug above) — even though
   `derive_fitness_thresholds` still produces a plausible-looking 0.0–1.0
   threshold for both slots every time. NOT fixed as part of this commit —
   same class of gap as item 1 (fitness/GOOD_FOR weights still stubbed), but
   scoped specifically to RAM/STORAGE now that GPU/CPU are resolved. Needs
   real benchmark-derived tier/score edges seeded for RAM/STORAGE (wherever
   `1dc9f32`'s GPU/CPU seeding logic lives, likely `data/graph/seed_graph.py`
   or a sibling script) before fitness ranking means anything for those two
   slots.

**Housekeeping — DONE (2026-07-03).** CLAUDE.md, karma ai/DESIGN.md, docs/
synced against the calibration + floor-enforcement commits and merged to main:
`6ea3920` (feat(feasibility): catalog-grounded verdict + shared cost/floor
primitive) and `3e37315` (feat(node3): enforce resolved requirement floors as
a hard query filter). CLAUDE.md's Neo4j status / file tree / data contracts
and DESIGN.md's §3 compatibility-family status + §9 stale blockers now match
shipped state. docs/plan.md reset (cycle closed, no accumulated history).
