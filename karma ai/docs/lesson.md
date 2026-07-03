## Neo4j standup (2026-07-01)

- **NODE_KEY (composite) constraints are Enterprise-only.** Community Edition Docker
  fails apply_schema() at the first NODE_KEY constraint. AuraDB Free runs Enterprise
  under the hood, so schema written for Aura REQUIRES Enterprise locally too. Fix:
  `neo4j:5-enterprise` image + `NEO4J_ACCEPT_LICENSE_AGREEMENT=yes` (free for dev/eval).
- **Aura Free provisioning is unreliable** — two instances stuck 30-40min on "Creating"
  with no error/status page incident. Local Docker was the unblock. Migration to Aura
  later = swap `.env` URI (bolt:// → neo4j+s://), NO code changes; seed is idempotent MERGE.
- **Socket vocabulary IS consistent** across catalog — AM4/AM5/LGA1700/LGA1851 identical
  char-for-char between cpu & motherboard rows. The silent-junction-failure risk did not
  materialize.
- **Catalog categories are lowercase** (`cpu`, `motherboard`) — queries using uppercase
  return empty silently, no error. Verify node3_selector.py / neo4j.py use lowercase.
- **Rel-name mismatch (real gap):** seed writes SUPPORTS_DDR / SUPPORTS_FORM_FACTOR;
  neo4j.py queries REQUIRES_DDR / REQUIRES_FORM_FACTOR. OPTIONAL MATCH makes this
  fail-open → **DDR-gen and form-factor compatibility are NOT actually enforced yet.**
- Minor: NEO4JLABS_PLUGINS renamed to NEO4J_PLUGINS since Neo4j 5.0.
- When building against a cloud DB that runs Enterprise features by default (e.g. Aura's NODE_KEY constraints), local dev must use the Enterprise image too — Community rejects Enterprise-only schema features, and does so at apply_schema(), not later.
- Before seeding a graph, verify join-key vocabulary is character-for-character identical across source tables (e.g. socket strings). A mismatch silently prevents relationship formation — no error thrown, just an empty edge that looks like correct data.
- When a previously-unavailable system goes live, audit every fallback/relaxation branch written for the "unavailable" era. A guard built as "if graph returns nothing, ignore the graph" is correct when the graph never worked and becomes a live bug the moment it does.
- Relationship/field names must be checked seed-vs-query explicitly, never assumed matching. A name mismatch (REQUIRES_DDR vs SUPPORTS_DDR) causes total silent exclusion, not an error — it will not show up until you specifically test that path.
- Sparse or missing edges in seed data can hide behind unrelated catalog gaps (e.g. form-factor edge missing, hidden by empty case stock). Absence of a failure is not confirmation of correctness — test edge coverage deliberately, don't infer it from a clean run.
- A validator that only logs conflicts isn't enforcement. If it doesn't block the bad outcome, it's not protecting anything — treat "log only" as a TODO, not a safeguard.
- Selection ordering (which slot locks first) can create real downstream resource tensions, not just bugs. When one surfaces, it needs an explicit design decision (documented, e.g. RAM→DDR4 bias under tight budgets), not a silent patch.

## Verdict + allocation calibration (2026-07-02)

- **Two components that judge the same quantity must consume the same primitive,
  not two independently-tuned approximations.** estimate.py guessed build costs
  the LLM way while Node 2 allocated them the percentage way; each looked fine
  alone and they disagreed with each other and with the catalog. The fix wasn't
  better tuning — it was one shared function (catalog_floor.min_viable_build)
  both read. Same for fixed costs: resolver and node2 had separate stub tables
  for OS/monitor that disagreed by ₹19,500 on one fixture's core pool.
- **An LLM verdict without a computable anchor is a random variable, not an
  estimate.** estimate_feasibility flipped tight→impossible between two
  identical consecutive runs (ml_workstation), and missed on both sides: "tight"
  for a build ₹2.5k past its ceiling AND "tight" for one with 18% headroom.
  When ground truth is computable (min-cost compatible build from live stock),
  compute it and let the LLM write prose only — code owns the verdict field.
- **Percent-based allocation cannot see catalog price cliffs.** The catalog is
  quantized: cheapest in-stock discrete GPU ₹27,500, the only ≥48 GB RAM kit
  ₹22,000, cheapest DDR5 LGA1700 board ₹15,000. Any percentage split of a small
  budget straddles these cliffs (gaming GPU at 35% needs a ~₹78k core pool
  before the band reaches the cheapest GPU). Bands must be repaired against
  real stock after normalization, not just widened by a blanket 20%.
- **A band's LOW bound can be as harmful as its high.** At ≥₹80k gaming budgets
  the 8% RAM band's floor (₹6,000+) excluded every DDR4 kit (₹3,800–4,200),
  silently forcing DDR5 RAM → stranding the motherboard band → ₹15k escalation.
  The DDR4-bias patch treated this symptom only under 'tight' verdicts; the
  comfortable Intel build hit it unprotected. Query lower bounds should never
  exclude parts a viable minimum build would use.
- **A conditional patch keyed on a fuzzy state fires in states it was never
  designed for.** The DDR4 bias triggered on verdict=='tight', but a verdict
  can be tight for reasons unrelated to memory (GPU brand pref pushing past the
  ceiling) — and the catalog stocks no DDR4 kit above 32 GB, so the bias dragged
  a 64 GB-floor ML build onto 16 GB DDR4. Gate patches on the actual
  precondition (an in-stock DDR4 kit can meet the resolved RAM floor), not on a
  correlated verdict label.
- **"Verdict says buildable" and "the funnel ships an incomplete build" must not
  coexist.** edge_tight_amd (truly ₹2.5k over ceiling) got 'tight' from the old
  gate, reached Node 3, and shipped 8/9 slots with ₹200 headroom, dropping the
  fans dead-end into warnings. The feasibility gate exists precisely to make
  that run never start.
- **Calibrate with a rerunnable sweep, not by nudging constants until fixtures
  pass.** scripts/calibration_sweep.py computes ground truth (min compatible
  build vs pools) for 5 profiles including deliberately-adversarial edges (an
  Intel build to hit narrow motherboard bands, a budget at the catalog's
  discrete-GPU floor to probe the impossible boundary). It imports the
  production primitives so it validates what ships. The tight/comfortable
  threshold (0.85) is pinned to measured anchors (1.04 tight vs 0.82
  comfortable) with the sweep as the re-derivation procedure — the constant
  carries its own justification and its own way to re-check it.
- Note: budget_gamer's 'tight' — the symptom that started this — was CORRECT
  (min build ₹66,000 vs ₹63,500 core target). Verify a suspicious verdict
  against ground truth before "fixing" it; the bug was the verdict's
  instability and inconsistency with allocation, not this particular label.

## Node 3 requirement-floor enforcement (2026-07-03)

- **Enforce at the query, not after the pick — same shape as compatibility.**
  resolve_requirements() produced per-slot floors (VRAM / CPU tier / RAM &
  storage capacity, storage type) that Node 3 never applied, so it shipped an
  HDD against an NVMe brief and sub-floor RAM. The fix is a hard filter inside
  every catalog fetch (_fetch_floor), exactly where in-stock and price-band
  filtering already live — a floor-violating part never reaches the shortlist.
  This is the "a validator that only logs isn't enforcement" lesson applied
  preemptively: no detect-after-pick check was added, only exclusion.
- **Reuse the predicate that already defines correctness.** Node 3's floor
  filter IS catalog_floor.slot_requirement_filter — the same function that
  builds the min-viable build the price bands are pinned to. Had Node 3
  re-implemented "meets floor," it could drift from the band-repair floor and
  from the verdict floor. One predicate, three consumers (verdict, band repair,
  Node 3 pick), zero drift by construction.
- **A hard filter must survive every escalation rung, or it isn't hard.** The
  price-band ladder is band → +20% → full catalog. Compatibility already
  survived all three; floors now do too (every fetch, including the DDR4-bias
  pull and both full-catalog escalations, routes through _fetch_floor). The
  audit question for any relaxation ladder: "which filters widen, and which are
  invariant?" Price band and fitness widen; floor and compatibility never do.
- **The DDR4 bias was the exact thing that could smuggle a sub-floor part back
  in.** It pulls cheaper DDR4 kits from below the band — precisely the move that
  reintroduces a 16 GB kit against a 32 GB floor. Floor-filtering that pull was
  the non-obvious but load-bearing part of the change. When a feature
  deliberately reaches outside the normal candidate set, re-apply every hard
  filter to what it drags in.
- **Preferences are not floors — don't harden the soft ones.** Brand
  (ecosystem_prefs) is returned by resolve_requirements alongside the real
  floors, but flooring it would dead-end builds the feasibility gate already
  called 'tight' (= buildable only after relaxing brand). Node 3 filters brand
  with enforce_brand=False. Enumerate what a function returns, then classify
  each field as hard vs soft — don't enforce the whole struct because it's
  convenient.
- **Well-repaired bands hide the bug the enforcement fixes — design adversarial
  fixtures deliberately.** Band repair pins band.low to the cheapest
  floor-satisfying part, so on a repaired band the naive cheapest is usually
  already floor-satisfying. A violating part only stays in-band when it is
  priced between the (under-allocated) raw low and the floor part. The reliable
  construction: an EXPENSIVE floor part with CHEAP violating alternatives —
  2 TB NVMe floor (₹13.5k) with 2 TB HDD/SATA at ₹4–8.5k that no band excludes.
  A fixture whose adversarial condition is masked by band repair (first attempt
  put storage-weight high, so repair clamped low up onto the NVMe price and the
  HDD fell out of band) proves nothing — the sweep asserts each adversarial
  fixture actually rescues ≥1 slot, so a fixture that stops biting fails loudly.
- **Assert the property end-to-end, and prove the guard is load-bearing.** The
  sweep asserts 0 floor violations across all 7 profiles (the invariant) AND
  that the adversarial fixtures' naive cheapest-in-band WOULD violate (proving
  the filter changed the outcome, not that the fixture was already clean). An
  invariant assertion that passes vacuously is worse than none — it reads as
  coverage.