# Karma  — Agentic Workflow Design

> Living design document for the Karma ai recommendation pipeline (Karma Computers).
> **Status legend:** 🔒 Locked · 🛠️ Implemented · 🚧 In design · ❓ Open
> _Last updated: 2026-07-07 (full-system audit reconciliation, verified section-by-section against source, the live Postgres catalog, and the live Neo4j graph; refreshed to include the PSU-wattage and rejected-parts-consistency fixes)_
>
> **Implementation status:** Phases 0–4 code-complete and merged to `main`. The full pipeline (Node 1 → Feasibility → Node 2 → Node 3 + refinement) is built; Nodes 1–3 are wired into a LangGraph `StateGraph`, and the refinement loop runs as Phase 5 of the CLI harness (`run_pipeline.py`). Postgres (Session Pooler) and Neo4j (Enterprise edition, local Docker, seeded) are both live; all three compatibility families are enforced as hard filters; PSU wattage is enforced as a fourth hard floor shared between the feasibility gate and Node 3; the feasibility verdict is deterministic and catalog-grounded; GPU/CPU fitness edges carry real benchmark-derived tier/score. Test suite: **46 passed, 0 skipped** (live DBs). Remaining pre-production blockers: Neo4j migration off local Docker, and the absence of any automated end-to-end test — see §9.

---

## 1. System Overview

Karma AI is the multi-agent recommendation engine at the center of **Karma Computers**, a B2C e-commerce platform for PC parts and custom builds aimed at Indian consumers. It takes a user's needs expressed in natural language and produces a single, compatible, budget-fit PC build.

The pipeline is **design-first**: every agent is fully scoped and locked before implementation. It runs as a linear flow with one deterministic gate between intake and allocation.

```mermaid
flowchart TD
    U([User]) -->|freeform conversation| N1[Node 1<br/>Information Extraction Agent]
    N1 -->|User Build Brief JSON| FC{Feasibility Check<br/>deterministic catalog-floor verdict<br/>LLM prose only}
    FC -->|impossible| FAIL[Surface to user:<br/>lower demands / raise budget]
    FC -->|comfortable or tight| PRE[Deterministic pre-steps:<br/>shopping list + fixed-cost subtraction]
    PRE --> N2[Node 2<br/>Budget Allocation Agent]
    N2 -->|price bands per component| N3[Node 3<br/>Part Finder & Recommender]
    N3 -->|build card| CONFIRM([User confirmation])
    CONFIRM -->|product IDs| BACKEND[(Backend / cart)]
    N3 -. refinement loop .-> N3
```

---

## 2. Pipeline Architecture

### 2.1 Node One — Information Extraction Agent 🔒 🛠️

- **Role:** Conversation-first intake. A set of **predefined, structured questions**, each answered by the user in a **freeform paragraph**. Not dropdowns, not open-ended free chat — fixed questions, paragraph answers.
- **Intake-model decision (conversation-first over wizard):** chosen deliberately. The build-requirement space is combinatorial and cannot be enumerated as wizard branches; conversation is the agentic thesis of the product; and the choice is **reversible** because both intake modes would produce the *identical* Brief — a guided wizard can be added later as additive front-end UI without touching any downstream node.
- **Question flow:**
  - **One question per turn**, but extraction is **opportunistic against the full Brief schema** — anything the user volunteers (even if it answers a later question) is captured immediately and the corresponding questions are skipped (`newly_filled_sections` diff feeds `asked_so_far` in the harness).
  - **Questions are static / predefined, not dynamically branched.** A user's answer never changes *which* questions are asked; it only affects downstream nodes.
  - **Final question is open-ended**, asked after the others: *"any hard constraints / must-haves / must-nots?"* → populates the pinned `hard_constraints` block (`source: user_stated`).
- **Question set + stop condition:** there is **one finite, pre-prepared set of 13 questions** (`QUESTION_SEQUENCE`). By default the agent works through the **entire set**, then locks the Brief — the list itself is the bound (no arbitrary max count). Two stop rules govern this:
  - **Required floor:** **budget + primary use case must be answered before proceeding.** This is the gate — intake cannot move past it without both (as built: `comfortable_max > 0` AND `primary_use_case` non-empty, §7.6).
  - **User early exit:** once the floor is met, if the user says "done" / "stop" at any point, intake ends there and the Brief locks immediately (regex `\b(done|stop)\b` inside `extract_turn`).
  - Otherwise (no early exit), every question in the pre-prepared set is asked; on exhaustion the **harness** locks the Brief (`run_pipeline.py`), not the module.
- **Extraction + validation:** each paragraph answer → LLM returns JSON against the schema → **two-stage validation**: (1) JSON syntax (`JSON.parse`), (2) schema + enum conformance. Valid → merge into Brief. As built, both stages live in `llm/client.py::call_structured` with **2 corrective retries** then `StructuredCallError`; `extract_turn` catches the error and returns the Brief unchanged for that turn (the retry policy previously "deferred to testing" is decided in code).
- **Output:** Canonical **User Build Brief** JSON (full schema in **Appendix A**).
- **Not responsible for** feasibility or contradiction checking — Node One has no tier/benchmark data; its only jobs are asking questions and forming valid JSON. The Feasibility Check is the arbiter of buildability at budget.

**As-built deviations from the locked spec:**
- **Ask-if-ambiguous clarifications are not implemented.** The design carved out targeted clarifications (e.g. "video editing" → "which software?") as the sole exception to the static sequence; no mechanism for them exists — the sequence is strictly linear. Tracked in §9.
- **Dead spec surface:** `open_questions` (Appendix A: "drives follow-ups") is never populated by any code; `existing.existing_pc_build_id` is defined but consumed by nothing; the `inferred` and `skipped_by_user` source-flag values are never set programmatically (only `user_stated` via LLM instruction and `default_applied` as the initial sentinel occur). (`_QuestionDef.is_final` was the same kind of dead field — removed; exhaustion is signaled by `next_question` returning `None`, per line 45 above.)
- **Exit-regex looseness:** `\b(done|stop)\b` matches anywhere in an answer ("I'm done gaming by 10pm" triggers early exit if the floor is met).
- **Graph-mode edge case:** LangGraph `node_intake` is one-turn-only by design; if driven with an unlocked Brief whose questions are exhausted, `_route_after_intake` routes back to `node_intake` indefinitely (routing keys off `brief.status`, which nothing on that path locks). Unexercised in practice — every real graph entry (`run_from_brief`) supplies a locked Brief.
- **Test coverage: none.** No test imports `node1_intake`; all fixtures are pre-built locked Briefs. Real intake has only ever been exercised manually. See §9.

### 2.2 Feasibility Check 🔒 🛠️ _(rewritten to match the as-built deterministic verdict; the original LLM-estimate design survives only as the Postgres-down fallback)_

**Role:** Lightweight pre-Node-Two gate. Answers one question before two more nodes run: *can the user's requirements be built within their budget?* No part selection, no per-slot optimization — those are Node Three's job. The verdict is **deterministic and catalog-grounded**, not an LLM estimate.

**Three steps:**

1. **Requirements Resolver** (`feasibility/resolver.py`, pure/deterministic) — per `software` entry, look up base component floor (what GPU class, how much RAM, etc.), scale by the performance envelope (`resolution`, `framerate`, `hdr`), aggregate across the full workload:
   - GPU tier, CPU tier, VRAM: **max** across software (peak demand wins).
   - Storage: **additive** (workloads stack their capacity needs).
   - RAM: **max** single-app floor, plus a concurrency bump (+16 GB stub) if two or more heavy workloads run simultaneously.
   - Hard constraints that raise the floor (e.g. SFF/ITX form factor, brand exclusions) are folded in here.
   - Reused parts: their cost is zeroed; their constraints (socket, form factor) remain live as `live_constraints` notes.

2. **Scope aggregator** (`resolver.aggregate_scope`) — add non-component line-items depending on `budget.scope`: monitor (if unowned and in scope), OS license, must-have peripherals. Subtract reused-part costs. All fixed-cost values come from the shared `agents/costs.py` tables (single source with Node Two — the two stages cannot disagree on a cost).

3. **Deterministic catalog-floor verdict** (`feasibility/estimate.py` + `feasibility/catalog_floor.py`) — the primary path when Postgres is reachable:
   - `compute_catalog_floor(brief, req)` brute-forces the **cheapest complete, compatible, in-stock build** that meets every resolved requirement floor — including the PSU wattage floor (§2.4) — producing a **hard floor** (brand preferences honoured) and a **soft floor** (brand preferences relaxed). Rejected parts (`hard_constraints.rejected_parts`) are excluded from every slot before the floor is computed, via the shared `filter_rejected`/`rejected_product_ids` predicate (§2.4).
   - The floor is compared against the core component pool from `costs.core_pools(brief)`; the verdict is pure arithmetic: floor > ceiling → `impossible`; floor > `_TIGHT_RATIO` (= 0.85, empirically calibrated via `scripts/calibration_sweep.py`) × target → `tight`; otherwise `comfortable`.
   - **The LLM writes prose only.** It narrates the deterministic result for the user; if its structured output tries to flip the verdict, the deterministic verdict is forcibly restored (`result.model_copy(update={"verdict": ...})`).
   - **Fallback (Postgres down):** the original single-anchor LLM estimate — aggregated floor + budget picture + one live minimum-price anchor (cheapest GPU) — runs instead, flagged as such.
   - **Provenance:** every `FeasibilityVerdict` carries `basis: deterministic | llm_fallback | stub`, so downstream consumers and logs can always tell a catalog-grounded verdict from an estimated one. **Not yet surfaced in the CLI's verdict printout** — `_print_verdict` in `run_pipeline.py` prints verdict/reason/binding-constraint but never `basis`, even though the field is always populated. See §9.

**Verdict — three-state 🔒:**
- `comfortable` — budget has meaningful headroom above the catalog floor.
- `tight` — buildable but little flexibility; expect compromises.
- `impossible` — floor materially exceeds the ceiling.

**Routing:** `comfortable` or `tight` → proceed to Node Two. `impossible` → Type Two failure: surface to the user with the binding constraint and suggested adjustments (raise budget, lower resolution target, relax form-factor constraint, etc.). Node One Brief is re-entered if the user adjusts.

**What this is not:** the Feasibility Check does not run the Node Three funnel, does not rank or justify parts, and does not pick anything the user sees. The catalog floor is a min-cost existence proof, not a recommendation.

**Open items (updated):**
- ~~Realistic-min buffer calibration~~ ✅ **RESOLVED** — the tight/comfortable boundary is the empirically calibrated `_TIGHT_RATIO = 0.85` (anchored between a 1.04-ratio tight case and a 0.82-ratio comfortable case; re-derivable any time stock shifts via `scripts/calibration_sweep.py`).
- ~~Reused-part value~~ ✅ **RESOLVED** — no longer a flat stub: `costs.reused_part_value(slot)` now averages live in-stock catalog prices per category (`costs.average_catalog_price`, cached per process, `refresh_catalog_price_cache()` to recompute), falling back to the old hand-picked table only when Postgres is unreachable or the category has zero in-stock parts.
- Non-component cost estimates (OS license, monitor, must-have peripherals) remain rough hand-picked STUB values in `costs.py` (centralized, but not sourced from real pricing) — no catalog data source exists for these categories.
- Reused-parts constraint propagation — a Node Three concern, not a feasibility gap: `existing.existing_pc_build_id` (PC-of-record) is consumed by nothing, and `min_viable_build` excludes reused slots from cost entirely without applying their socket/form-factor constraints to the rest of the build (e.g. a reused case's form factor doesn't constrain the motherboard the floor picks).

### 2.3 Node Two — Budget Allocation Agent 🔒 🛠️

- **Role:** Takes three deterministically compiled, server-side inputs, reasons across them, and outputs price bands per component. No other responsibility.

**Deterministic pre-steps (before the agent runs):**
1. Generate the shopping list by cross-referencing the brief's existing/reused parts against the full component list — only components that need to be purchased proceed (`_build_shopping_list`; `action == "keep"` slots are excluded).
2. Subtract fixed costs (OS license, specified monitor, peripherals) via the shared `agents/costs.py` tables. Node Two only allocates the remaining **core-component pool**.

> The Brief carries these fixed-cost inputs explicitly — `operating_system`, `monitor`, and `peripherals` sections (Appendix A) — which the fixed-cost subtraction reads.

**Three inputs:**
1. **Default allocation profile** — per-use-case skew predetermined by Karma Computers (gaming → GPU, editing → VRAM + storage, ML → RAM + GPU VRAM, programming → CPU + RAM). As built: `_ALLOCATION_PROFILES` STUB tables, including an ML sub-profile keyed off `sub_case`.
2. **User brief** from Node One.
3. **Software minimum specs** — designed as runtime web search from authoritative sources (Steam, Epic Games, official vendor pages); **as built, a clearly-marked hardcoded STUB dict** (`_SOFTWARE_SPECS`). Web-search retrieval was never built (§10).

> **Boundary (amended):** the original locked boundary — *"catalog price floors are not a Node Two input"* — was deliberately relaxed. A deterministic post-step, `_repair_bands_to_catalog`, pins each band to the corresponding min-viable-build part price from the shared catalog floor (`catalog_floor.py`), so Node Three's hard floor filter can never face a band that excludes every floor-meeting part. Allocation *reasoning* (the LLM weight skew) still never sees catalog prices; only the deterministic post-step does.

**Output:** JSON price bands (low / mid / high in INR) per shopping-list component only. Constraints:
- midpoints sum to the core budget target (**holds by construction**),
- high ends sum to the ceiling (**holds by construction**),
- low ends sum to the floor — **relaxed by design**: `_repair_bands_to_catalog` may raise individual `low` values to the catalog floor price, so the low-end sum can exceed the nominal floor.

No rationale, flex flags, or metadata — Node Three has the full brief and derives intent itself. Node Three hunts for components clustered around the midpoints as the sweet spot.

### 2.4 Node Three — Part Finder & Recommender 🔒 🛠️

**Selection sequence:** GPU → CPU → Motherboard → RAM → Storage → PSU → Case → Cooler → Fans.
_(Motherboard locks right after the performance anchors (GPU, CPU) so RAM's DDR-generation compatibility resolves against an already-locked board, instead of the board having to adapt to whatever RAM generation was picked first — the prior GPU→CPU→RAM→...→Motherboard order could strand a locked DDR4/DDR5 RAM pick against no compatible board later. PSU is selected after GPU + CPU so their TDP is already known.)_

**Per-slot selection loop (three-step funnel):**

```mermaid
flowchart LR
    A[Catalog query<br/>price band + in-stock + requirement floor<br/>+ PSU wattage (PSU slot only)<br/>Postgres] --> B[Graph filter<br/>hard compatibility + soft fitness rank<br/>vs locked parts · Neo4j]
    B --> C[LLM final pick<br/>from shortlist of ≤7]
```

- **Requirement-floor hard filter:** every catalog fetch goes through one choke point (`_fetch_floor`), which applies the shared `catalog_floor.slot_requirement_filter` predicate (GPU VRAM, CPU tier→min cores, RAM/storage capacity, storage type) and excludes `hard_constraints.rejected_parts` via the shared `filter_rejected`/`rejected_product_ids` predicate. The same two predicates back the feasibility verdict (`min_viable_build`), Node Two's band repair, and (for rejection) `node3_refinement.diff_and_bias`'s incumbent-bias check — **one set of predicates, all four consumers, no drift.** (This was a known gap — rejection was previously checked locally in Node 3 and independently in `diff_and_bias`, and not at all in `min_viable_build` — closed by consolidating both into `catalog_floor.py`, with dedicated regression coverage in `tests/test_rejected_parts_consistency.py`.)
- **PSU wattage — a fourth hard floor, shared with the feasibility gate:** `required_psu_wattage(locked_specs)` sums the already-locked GPU+CPU `tdp_watts` plus `_PSU_HEADROOM_W` (150 W) — the identical constant `catalog_floor.min_viable_build` uses when it promises a PSU with that headroom exists inside budget. `select_build` accumulates each locked slot's specs (`locked_specs`) and passes the computed wattage floor into the PSU slot's `_fetch_floor` call, which applies it as a hard filter (`_psu_wattage_filter`) alongside the requirement floor and rejected-parts exclusion — never relaxed by the price-band escalation ladder. A build can no longer pass feasibility on a wattage assumption and then ship an underpowered PSU. Covered by `tests/test_psu_wattage.py` (unit test on `required_psu_wattage`, an underpowered-PSU-excluded regression, an all-underpowered dead-end case, and an end-to-end wiring test proving `select_build` computes the floor from locked TDP correctly).
- **Fitness is a soft ranking, never a cutoff.** `fitness_filter` orders candidates by benchmark tier/score (`FitnessRanking`); components below the derived threshold are ranked lower, not dropped, and `is_real_ranking` gates whether the LLM prompt mentions fitness at all (categories with no fitness edges get no fake signal). **Fitness thresholds** are derived once upfront by the LLM reading the brief (`gpt-4o`, `temperature=0`), stored in build state (`ThresholdCache`, round-tripped through graph state), and never re-derived per slot.
- **Safeguards (as built):**
  - **Relaxation ladder** for empty shortlists: price band → **widen band 20%** → **full catalog**. The requirement floor, PSU wattage floor, and hard compatibility survive every rung — only price relaxes. _(The originally designed "lower fitness threshold" rung no longer exists and cannot: fitness stopped being a filter when it became a soft rank.)_
  - **Lookahead probe — one, warn-only:** a single motherboard-compatibility probe runs (Neo4j only), positioned right before Motherboard's turn in `SELECTION_ORDER` — i.e. immediately after GPU+CPU lock, per the SELECTION_ORDER fix (motherboard now locks right after the performance anchors, ahead of RAM). It **logs a warning and proceeds** ("LLM will pick best available") — it does not block, backtrack, or change the selection in any way. The design's "lookahead probes to prevent downstream dead-ends" (plural, preventive) is not what exists. Since the SELECTION_ORDER fix already eliminates the dead-end scenario this probe was originally meant to catch in the common case (Motherboard no longer has to adapt to a RAM pick made before it), the probe now covers only rarer remaining cases — e.g. a CPU socket with near-zero in-stock compatible boards. See the decision recorded in §9.
  - Running budget-pool tracking (`remaining_budget` vs ceiling; `over_budget` dead-end) to catch drift across slots.
  - **Post-lock compatibility validator — blocking:** runs after every lock and refuses to lock a conflicting part (surfaces a warning on the build card) rather than merely logging.
- **Build state carries:** locked parts, locked specs (for PSU wattage accumulation), derived thresholds, remaining budget, user brief.
- **Output:** A single build (not multiple options). The **build card** is a human-readable summary of parts, prices, and justifications sent to the user for confirmation; product IDs are sent to the backend on confirmation. Dead-end slots produce plain-English `warnings` entries on the card.
- **Failure communication:** plain English (e.g., "your budget cannot support this configuration; either lower demands or increase budget; the best available within constraints is X").

**Refinement loop — Approach B (pin / open model):** 🛠️
- All slots re-solve on each refinement; the compatibility validator surfaces conflicts conversationally rather than maintaining a dependency graph.
- Budget-level changes are routed through the budget updater (re-run `allocate_budget` with the new budget, then re-solve).
- Brief-level changes restart at Node One.
- **As built and wired (`node3_refinement.py` + `run_pipeline.py` Phase 5, `run_refinement`):** the conversation loop lives in the CLI harness; the module itself is pure and non-interactive. The loop runs on every interactive and single-`--fixture` invocation (`--fixture-all` stops after allocation). **It is not a LangGraph node** — `graph_runner.run_from_brief` ends at a build card with no refinement capability, which is the standing gap for the future API layer (§9). A freeform user message is parsed via `call_structured` into a `RefinementOps` — a **multi-op** classification (not a single action): `brief_edit`, `restart_trigger`, `budget_change`, `pin`, `reject`, `accept` may all populate in one turn (e.g. "bump budget to 90k and give me an nvidia card" → `budget_change` + `reject`).
  - **Field routing** (`route_field_edit`) is a fixed, hardcoded table — never an LLM judgment call: `software` / `performance` / `extras` / `physical` / `longevity` → **additive** (`brief_edit`); `primary_use_case` / `budget.scope` / `existing.reuse_parts` → **structural** (`restart_trigger`). A field outside the table defaults to additive with a logged warning, never a crash. The table decides routing even if the LLM puts a structural field name in `brief_edit` (or vice versa).
  - **List-valued additive fields merge, not replace:** `software` is upserted by name (`_merge_list_field`) rather than overwritten wholesale, since the LLM classifying one turn only sees that turn's message — asking it to echo the full existing list back would risk silently dropping entries it didn't mention.
  - **Dispatch precedence**, fixed per turn: `restart_trigger → brief_edit → budget_change → pin/reject → re-solve → accept`. `restart_trigger` patches the brief and calls `graph_runner.run_from_brief`, skipping every other op that turn; `locked_parts` and `rejected_parts` persist across it. `brief_edit` patches the brief and re-runs `estimate_feasibility` only (not full Node One) — an `impossible` verdict skips the re-solve. `budget_change` rescales `comfortable_min/max` proportionally, sets the new ceiling, and re-runs `allocate_budget`. `pin` records `locked_parts[slot] = product_id`; `reject` (`apply_reject`) appends a `RejectedPart` and unpins that slot if it was pinned. Any of the above triggers one incumbent-biased re-solve (`_select_build_with_pins` + `diff_and_bias`); `accept` (only reachable if nothing else fired) ships `build_card.product_ids`.
  - **`diff_and_bias`** reconciles a fresh re-solve against the prior card: for each non-pinned slot whose pick changed, it keeps the OLD part if still valid (in the widened price band, not rejected — via the shared `catalog_floor.rejected_product_ids`, not a locally duplicated set — compatible with parts decided so far) — otherwise the new pick wins. Only genuine changes land in `BuildCard.changed_slots` (`{slot, old_product_id, new_product_id, reason}`), so the harness prints a diff instead of a full card each round.
  - `MAX_REFINEMENT_ROUNDS = 5`.
  - **Test coverage boundary:** the pure-function layer (routing table, dispatch precedence, pin/reject round-trips, all four `diff_and_bias` cases, merge-preservation) is well tested offline (27 tests, LLM/DB/solve monkeypatched). `run_refinement` itself — the input loop, `ThresholdCache` threading across rounds, diff-vs-full-card display, MAX_ROUNDS — has no automated coverage, and no test exercises an un-mocked re-solve (§9).

---

## 3. Knowledge Graph Design — Neo4j 🔒 🛠️

**Two edge families:**
1. **Compatibility family** — unweighted junction nodes. Components connect to shared spec nodes (sockets, chipsets) rather than directly to each other.
2. **Fitness family** — edges encoding how well a component serves a specific use case, carrying benchmark-derived `tier` and `score` properties.

**Node taxonomy:** component · spec · use-case · performance · component-class.

**Key choices:**
- **One node per distinct product** (not per chip model) — board-partner variants can differ meaningfully in cooling, noise, and sustained performance.
- **Single database:** Neo4j handles both compatibility and fitness traversal. A Postgres/relational approach for compatibility was evaluated and rejected — the agentic system benefits from traversing both in the same semantic space without context switching. Compatibility edges are weightless but still traversed as graph relationships.

**As built:**
- `agents/db/neo4j_schema.py` — label constants (`COMPONENT`, `SPEC`, `USE_CASE`, `PERFORMANCE`, `COMPONENT_CLASS`), node-key constraints, indexes, and an idempotent `apply_schema(driver)`. Requires the **Enterprise** image (`neo4j:5-enterprise`, `NEO4J_ACCEPT_LICENSE_AGREEMENT=yes`) — Community fails silently on the first `NODE_KEY` constraint.
- `data/graph/seed_graph.py` — populates the graph from the Postgres catalog using `MERGE` throughout (idempotent). Creates `:Component` nodes; `[:BELONGS_TO]` → `:ComponentClass`; compatibility junctions (`:Spec` nodes for socket / DDR-gen / form-factor, with cooler `socket_compat`, motherboard `ddr_support`, case + motherboard `form_factor`, all read from the catalog `specs` JSONB); `[:GOOD_FOR]` → `:UseCase` edges for GPU and CPU; and `[:HAS_VRAM {gb}]` → `:Performance` for GPUs.
- `data/graph/seed_fitness_benchmarks.py` — seeds real benchmark-derived `tier` + `score` onto `GOOD_FOR` edges from `data/benchmarks/gpu_benchmarks.csv` / `cpu_benchmarks.csv`.
- `agents/db/neo4j.py` — real parametrized Cypher (no f-strings): `compatibility_check(candidate_ids, locked_parts, candidate_slot)` (fail-open only for components absent from the graph), `fitness_filter(...)` returning a `FitnessRanking` (soft ordering — **never excludes**; `is_real_ranking` tells the caller whether any real fitness signal exists), `get_component_fitness(product_id, use_case)`, and `ping()` for availability detection.

**Live and enforced (verified against the running instance):** Enterprise edition, local Docker, seeded — 103 `:Component`, 9 `:ComponentClass`, 9 `:Spec`, 5 `:UseCase`, 4 `:Performance` nodes (matching the 103-product / 9-category Postgres catalog); Node 3 detects it via `ping()`. All **three compatibility families — socket (CPU↔motherboard, cooler↔CPU), DDR generation (motherboard↔RAM), form factor (case↔motherboard)** — are hard-filtered: never bypassed by the price-band relaxation ladder, verified bidirectionally end-to-end. Node 3 additionally hard-filters the *resolved requirement floor* (VRAM / CPU tier / RAM & storage capacity / storage type) and the *PSU wattage floor* at the same catalog-query layer — see `agents/feasibility/catalog_floor.py`.

**Fitness coverage (as seeded):** `GOOD_FOR` edges exist for **GPU (42 edges: 14 parts × gaming / content_creation / general_use — work_productivity deliberately absent) and CPU (48 edges: 12 parts × 4 use-cases)**, all carrying benchmark `tier` + `score`. The other seven categories (RAM, storage, motherboard, PSU, case, cooler, fans) have **zero fitness edges by design** — earlier hand-picked stub weights for RAM/STORAGE were removed because fake signal is worse than none; `is_real_ranking` keeps those slots' LLM prompts silent about fitness. The GPU/CPU edges still also carry the legacy stub `weight` property (`seed_graph._GOOD_FOR_WEIGHTS` writes it; nothing reads it — inert).

**Still open:** extending real fitness beyond GPU/CPU (or formally deciding not to); `derive_fitness_thresholds` still spends a `gpt-4o` call producing thresholds for all nine slots when only two are usable. Neo4j runs local Docker only — not yet migrated to a hosted instance reachable by a deployed backend (see §9).

---

## 4. Data Contracts (what moves where)

| Stage | Produces | Shape / notes |
|---|---|---|
| Node One | User Build Brief | JSON; budget + primary use case mandatory; also carries software/workload, monitor, peripherals, storage, OS, existing/reused parts, and pinned `hard_constraints` — full schema in **Appendix A** |
| Feasibility Check | verdict + reason + basis | `comfortable \| tight \| impossible`, `basis: deterministic \| llm_fallback \| stub`; comfortable/tight → proceed to Node Two; impossible → surface to user with binding constraint + suggested adjustments |
| Node Two pre-steps | shopping list + core budget pool | deterministic; fixed costs already subtracted (shared `costs.py`) |
| Node Two | price bands | JSON low/mid/high INR per shopping-list component; post-repaired to the catalog floor |
| Node Three | build card | human-readable summary + `warnings` + `changed_slots`; product IDs sent to backend on confirm |

---

## 5. Platform Features 🔒

- **Hidden business-intelligence ranking layer** surfaces high-margin and overstock products without user visibility; admin-configurable via weight controls.
- **Access:** logged-in users only; 2–3 active chat cap (intentional funnel discipline); saved builds uncapped.
- **Two-tier memory:** long-term and short-term with auto-compaction. The durable build object stores product IDs and intent snapshots — **never prices**.
- **Tally ERP integration** via XML/ODBC for bidirectional stock and sales-voucher sync.

---

## 6. Decision Log — the *why*

| Decision | Reasoning |
|---|---|
| Conversation-first intake over guided wizard | Build-requirement space is combinatorial (can't enumerate branches); conversation is the agentic thesis; reversible since both modes emit the identical Brief, so a wizard can be added later as additive UI. |
| Structured questions, freeform paragraph answers | Keeps conversational feel while bounding each turn's scope — easier extraction, cleaner state, lower token use than reverse-engineering one long dump. |
| Static (non-branching) question set | Answers drive downstream nodes, not which questions are asked; avoids the wizard's hand-authored branch explosion. (Ask-if-ambiguous clarifications were the designed exception; not yet built.) |
| Budget + primary use case as the proceed floor | With those two a build estimate is possible; they're the gate to proceed. Everything else is the rest of one fixed pre-prepared set, asked in full unless the user says "done" / "stop" (no arbitrary question cap). |
| Hard constraints captured via a final open-ended question + pinned block | Non-negotiables (no-RGB, SFF, brand bans) live in structured pinned state separate from the prose summary, so they survive compaction and are never re-suggested. |
| Node One does no feasibility/contradiction check | It has no tier/benchmark data; its only jobs are asking and forming valid JSON. Feasibility Check is the arbiter. |
| Motherboard selected after GPU/CPU but before RAM; PSU after GPU/CPU | Prevents over-constraining the build; the board adapts to the GPU/CPU anchors rather than driving them, and RAM's DDR-generation compatibility then resolves against the already-locked board instead of risking a stranded board later. PSU wattage depends on GPU+CPU TDP, so it can only be sized once both are locked. |
| Fitness thresholds derived once upfront | Avoids redundant per-slot LLM calls; thresholds live in build state. |
| Fitness is a soft rank, never a hard cutoff *(supersedes the threshold-as-filter design)* | A hard fitness gate silently emptied shortlists and interacted badly with the relaxation ladder; ranking preserves the signal without ever costing a viable build. `is_real_ranking` keeps categories with no fitness data honest (no fabricated signal in the pick prompt). |
| Product-level graph nodes | Board-partner variants of the same chip differ enough in real-world performance to warrant individual nodes. |
| ~~Catalog price floors excluded from Node Two~~ **Superseded: deterministic band repair against the catalog floor** | Original boundary kept allocation reasoning pure, but bands that exclude every floor-meeting part guarantee Node Three dead-ends. The LLM skew still never sees catalog prices; a deterministic post-step (`_repair_bands_to_catalog`) pins bands to the min-viable-build prices, sharing the exact floor predicate with feasibility and Node Three so the three stages cannot drift. |
| Neo4j over Postgres for compatibility | Same-semantic-space traversal is worth the architectural simplicity; weightless edges are still valid graph relationships. |
| RAG over fine-tuning | Fine-tuning can't track volatile daily Indian pricing, goes stale on hardware launches, and is a black box. |
| ~~Feasibility Check is LLM-assisted, not LLM-free~~ **Superseded: deterministic catalog-floor verdict, LLM prose-only** | The original argument (pure determinism would require an inventory search) dissolved once `catalog_floor.py` existed anyway for band repair — the min-cost existence proof is cheap and already shared. LLM verdicts were unfalsifiable and drifted run-to-run; a deterministic verdict is reproducible, calibratable (`calibration_sweep.py`), and the LLM still writes the user-facing prose. The single-anchor LLM estimate survives as the Postgres-down fallback. |
| Feasibility Check does not search inventory *(nuanced)* | Still true in spirit: the gate never runs the Node Three funnel, never ranks or justifies parts. It does now compute a min-cost existence proof over the catalog — but that is an aggregate floor, not a recommendation, and it reuses Node Three's own floor predicates (requirement + PSU wattage + rejected-parts) rather than duplicating selection logic. |
| One live price anchor (Postgres) injected into the LLM prompt | Retained for the fallback path only: when the deterministic catalog floor is unavailable, the GPU anchor keeps the legacy estimate from going stale on the one number that matters most. |
| PSU wattage sized off the SAME constant in both the feasibility floor and Node 3's PSU pick | The floor promises a PSU with `cpu_tdp + gpu_tdp + _PSU_HEADROOM_W` exists inside budget; if Node 3 didn't enforce the identical bar, a build could pass feasibility on an assumption and ship an underpowered PSU. Sharing `_PSU_HEADROOM_W` and `required_psu_wattage()` between `catalog_floor.py` and `node3_selector.py` makes that promise actually hold. |
| Rejected-parts exclusion consolidated into one shared predicate (`catalog_floor.rejected_product_ids` / `filter_rejected`) | Previously Node 3's `_fetch_floor` had its own local reject filter, `node3_refinement.diff_and_bias` had its own inline set comprehension, and `catalog_floor.min_viable_build` had none at all — meaning a rejected part could still anchor the feasibility verdict and the Node 2 band-repair floor even though Node 3 correctly refused to select it. Three independent implementations of "rejected" is exactly the kind of drift the shared-predicate architecture exists to prevent; consolidating to one function (with regression coverage) closes it for good. |
| Supabase as managed Postgres host (not full BaaS) | Fastify handles the API layer, Prisma the ORM. Does not replace Neo4j, Redis, or Meilisearch. |

---

## 7. Phase 1 Implementation Notes 🛠️

### 7.1 LLM Provider

DESIGN.md specifies the **Anthropic Claude API**; Phase 1 implementation uses **OpenAI `gpt-4o-mini`**. The model is configurable via the `OPENAI_MODEL` environment variable.

### 7.2 Conversation Loop Architecture

Node 1 does **not** own the conversation loop. The CLI harness (`run_pipeline.py`) drives the loop turn-by-turn. Node 1 exposes a stateless API:

- `blank_brief()` — returns an empty Brief skeleton.
- `floor_met(brief)` — returns True when the proceed gate is satisfied.
- `next_question(brief, asked_so_far)` — returns the next question string, or `None` when all questions are exhausted.
- `extract_turn(answer, brief, history)` — runs LLM extraction for one turn and returns the updated Brief.
- `newly_filled_sections(old_brief, new_brief)` — diff helper for reporting what changed.

This keeps Node 1 stateless and independently testable — though note that no automated test currently exercises it (§9).

### 7.3 LLM Arithmetic Constraint — Locked Decision 🔒

Asking the LLM to produce exact INR band values across nine component slots fails arithmetic constraints reliably.

**Locked pattern:** the LLM produces **relative weights only** → Python computes INR values deterministically using **largest-remainder normalization** on 500-INR tokens. Sums hold by construction, not by asking the LLM to do arithmetic. `_distribute()` and `_compute_bands()` in `node2_allocation.py` implement this.

### 7.4 Feasibility Check — Live Price Anchor (fallback path only)

The single live Postgres price anchor (cheapest in-stock GPU) now matters only on the **`llm_fallback`** path — the primary verdict is deterministic (§2.2) and reads the whole catalog, not one anchor. Historical rationale retained: without the anchor, fallback verdicts are pessimistic — the model over-estimates GPU cost using stale priors.

- The Supabase **direct host** (`db.<ref>.supabase.co`) is retired; the **Session Pooler URL** must be used.
- `get_min_catalog_price` returns `0` on DB failure; `estimate.py` flags the anchor as `UNAVAILABLE` in the prompt and continues rather than aborting.

### 7.5 Software Extraction — Intensity/Frequency Rules 🔒

Default `gpt-4o-mini` behaviour marks all software as moderate intensity and ignores stated primary/secondary use-case priority when assigning frequency. Explicit prompt rules are required.

**Locked rules added to `_EXTRACT_SYSTEM`:**
- AAA titles and local LLMs → `heavy` intensity.
- Frequency derives from stated use-case priority, not software count.

### 7.6 floor_met() Definition

DESIGN.md defines the proceed floor as *budget + primary_use_case*. The implementation relaxes the budget side to match what the Brief actually captures:

- **Gate condition:** `comfortable_max > 0` **AND** `primary_use_case` non-empty.
- `sub_case` is **not** required for floor — it is optional metadata.

---

## 8. Phase 2–4 Implementation Notes 🛠️

### 8.1 Node 3 — Part Finder (as built)

`agents/nodes/node3_selector.py` implements the three-step funnel:

- `derive_fitness_thresholds(brief)` — **one** upfront `call_structured` call returning a per-slot threshold dict (stored in build state as `ThresholdCache`, never re-derived per slot). This call uses a **stronger model** (`gpt-4o` via `KARMA_THRESHOLD_MODEL`, `temperature=0`) because the per-slot reasoning quality drives every downstream pick; the per-slot final pick stays on `gpt-4o-mini`. Determinism levers not yet pulled: no `seed=`, no `system_fingerprint` logging (§9).
- `select_part(...)` — Step 1: Postgres fetch through the `_fetch_floor` choke point (band + in-stock + requirement floor + rejected-parts exclusion + PSU wattage floor on the PSU slot), escalating band → +20% widen → full catalog on empty; Step 2: Neo4j `compatibility_check` (hard) then `fitness_filter` (soft rank; skipped when `neo4j_available` is False); Step 3: `call_structured` final pick from a shortlist capped at 7, falling back to the top-ranked candidate on a hallucinated `product_id`.
- `select_build(brief, price_bands)` — walks `SELECTION_ORDER` (GPU → CPU → Motherboard → RAM → Storage → PSU → Case → Cooler → Fans), skips `reuse_parts` with `action == "keep"`, tracks running budget, accumulates `locked_specs` (per-slot catalog specs) to compute the PSU wattage floor from locked GPU+CPU TDP, runs the (warn-only) motherboard lookahead probe after GPU+CPU lock, and runs the **blocking** compatibility validator after each lock.
- **Graceful degradation (verified):** with Neo4j down *and* Postgres unreachable, `select_build` walks all nine slots, never crashes, and returns an empty `BuildCard` (every slot `None`). This is the designed degraded path — the funnel is structurally correct; only live data is missing. Note this failure is **silent, not loud** — check `scripts/test_db_connection.py` first when builds look empty.

### 8.2 LangGraph Wiring

`agents/graph.py` compiles a `StateGraph` matching the §1 flowchart: `node_intake → node_feasibility → {node_allocate → node_select → END | node_surface_failure → END}`, with conditional routing on the feasibility verdict. **The refinement loop is not a graph node** — it lives in `run_pipeline.py` Phase 5 (§2.4, §9). All node imports are **defensive** (`try/except ImportError`) so the graph compiles even if a downstream module is absent. `node_intake` is **one turn only** — designed for checkpointer resumption; the conversation loop still lives in `run_pipeline.py`, which remains the CLI driver. `node_select` rehydrates the `ThresholdCache` from graph state, so thresholds derived pre-graph are never re-derived inside it. `agents/graph_runner.py` exposes `run_from_brief(brief, price_bands) -> PipelineState` for fixture/API invocation, pre-seeding state and entering via the locked-brief route. This is the entry point the future API layer will call — with the caveat that it terminates at the build card (no refinement) until refinement is reachable outside the CLI; confirmed by direct inspection that `graph_runner.py` never references `dispatch_refinement`.

`PipelineState` (`agents/state/pipeline_state.py`) carries `fitness_thresholds`, `locked_parts` (string slot names → product_id), `remaining_budget`, and `error_message`. **Note:** `locked_parts` keys are **string slot names**, not `ComponentSlot` enums, to keep the graph-state contract serializable.

### 8.3 Output Formatter

`agents/output/formatter.py` centralizes user-facing text: `format_build_card` (**wired** into `run_pipeline.py`), `format_price_bands` (byte-identical to the harness's inline printer — **the swap is still pending**; `_print_price_bands` remains inline and is what `run_pipeline.py` actually calls), `format_impossible` (numbered adjustment list), and `format_tight_warning`.

### 8.4 Test Suite (audited)

**46 passed, 0 skipped** against live Postgres + Neo4j (`pytest tests/`, ~25s). Composition:

- `test_pipeline_integration.py` (10) — feasibility verdicts + allocation-sum/slot/skew assertions across the three canonical fixtures; live Postgres; skips cleanly via the `db_available` session fixture when the DB is down.
- `test_node3_selector.py` (2) — `_fetch_floor` rejected-parts exclusion + slot-scoping, live.
- `test_graph_node_select.py` (1) — `ThresholdCache` graph-state round-trip (mocked `select_part`).
- `test_node3_refinement.py` (27) — routing table, dispatch precedence, pin/reject, `diff_and_bias`, list-merge preservation; fully offline (LLM/DB/solve monkeypatched).
- `test_psu_wattage.py` (3, new) — `required_psu_wattage` unit test, an underpowered-PSU-excluded regression (non-vacuous: a mocked "value-optimizing" LLM tries to ship the cheap underpowered unit and is blocked), an all-underpowered dead-end case, and an end-to-end wiring test proving `select_build` derives the wattage floor from locked GPU+CPU TDP.
- `test_rejected_parts_consistency.py` (2, new) — `min_viable_build` over a synthetic catalog proving a rejected cheapest-GPU is excluded from the feasibility floor and the next-cheapest compatible GPU anchors it instead.

**Coverage boundaries (what no test touches):** Node 1 intake (zero tests), an un-mocked `select_part`/`select_build` run, `run_refinement`'s harness loop, `run_from_brief`, and any end-to-end flow — see §9. `scripts/calibration_sweep.py` provides rerunnable ground-truth calibration of verdict/allocation/floor against live stock, but its Node-3 walk is an explicit cheapest-pick **proxy**, not the real funnel.

### 8.5 Model Allocation Policy 🔒

| Call | Model | Reason |
|---|---|---|
| Node 1 extraction | `gpt-4o-mini` | Schema-constrained, prompt does the work |
| Feasibility prose (verdict is deterministic) | `gpt-4o-mini` | Narration only; verdict cannot be flipped by the LLM |
| Node 2 allocation skew | `gpt-4o-mini` | Weights only; Python does the math |
| **Node 3 fitness thresholds** | **`gpt-4o`**, `temperature=0` | Multi-slot reasoning; quality drives all picks |
| Node 3 final part pick | `gpt-4o-mini` | Constrained shortlist with explicit specs |
| Node 3 refinement parse | `gpt-4o-mini` | Freeform → structured multi-op |

Rule of thumb: tasks requiring **reasoning about tradeoffs across multiple dimensions without explicit scaffolding** get `gpt-4o`; **schema-constrained or prompt-scaffolded** tasks stay on `gpt-4o-mini`.

---

## 9. Open Questions / On the Horizon 🚧

_Re-verified section by section against source and live DB state; every claim below was directly confirmed on the current `main` HEAD, not carried forward from a prior session summary._

**Completed since the previous revision** (moved out of this list): deterministic feasibility verdict + `basis` provenance field, `_TIGHT_RATIO` buffer calibration, Node 2 catalog band repair, fitness softgate + benchmark tier/score seeding (GPU/CPU), refinement loop wiring into `run_pipeline.py` Phase 5, Neo4j stand-up + seed + three-family enforcement, Supabase Session Pooler migration, **PSU wattage enforcement in Node 3** (shared `required_psu_wattage`/`_PSU_HEADROOM_W` with the feasibility floor, regression-tested), **rejected-parts consistency** (`catalog_floor.rejected_product_ids`/`filter_rejected` now the single shared predicate across `min_viable_build`, Node 3's `_fetch_floor`, and `diff_and_bias`, regression-tested).

**Correctness gaps (verified in code; not hygiene — each one changes behaviour or crashes):**
- **(a) No end-to-end test exists.** Nothing automated runs intake → feasibility → allocation → real (un-mocked) selection → ≥1 refinement op → accept in one continuous flow. Node 1 intake has zero test coverage of any kind; no test calls `select_build` or `run_from_brief` un-mocked; `run_refinement`'s harness loop is untested. The closest artifact is a *manual* `python run_pipeline.py --fixture ...` run. This is the single most important gap in the system.
- **(b) Refinement is unreachable from the API entry point.** `graph_runner.run_from_brief` — the designated future API surface — terminates at the build card; only the CLI harness reaches `dispatch_refinement` (confirmed: `graph_runner.py` has zero references to it). An API layer wrapped around the graph today would ship builds with no pin/reject/re-solve capability.
- **(c) `agents/db/postgres.py` never calls `load_dotenv()` — a real crash risk in the primary catalog client, not a hygiene nit.** It reads `os.environ["POSTGRES_URL"]` bare, so any entry point that imports it before a dotenv-loading module (a new script, a REPL session, a future API worker) dies with `KeyError: 'POSTGRES_URL'` at pool creation. It currently works only by import-order luck: `neo4j.py` and `llm/client.py` both load `.env` first. Confirmed by direct read: postgres.py is still the only env-reading module in the codebase without `load_dotenv()`.

**Environment / infra:**
- **Neo4j hosted migration:** Enterprise local Docker only — not reachable by a deployed backend (e.g. Aura migration). Standing pre-production blocker.
- **API layer:** `graph_runner.run_from_brief` is the ready entry point; needs a Fastify/FastAPI wrapper — and a refinement path (item b) to be product-complete.

**Small wiring / hygiene (verified still open):**
- Swap `run_pipeline.py`'s inline `_print_price_bands` for `formatter.format_price_bands` — confirmed still inline (`_print_price_bands` defined and called at both call sites; `format_price_bands` not imported).
- Surface `FeasibilityVerdict.basis` in the CLI verdict printout — confirmed `_print_verdict` still prints only verdict/reason/binding_constraint/suggested_adjustments, never `basis`, despite the field always being populated.
- Remove the five stale `# TODO: Remove when X merges` fallback stub comments in `run_pipeline.py` (lines ~128, 143, 156, 173, 190) — confirmed still present; every referenced module merged long ago.
- Deduplicate `get_driver()` in `agents/db/neo4j.py` (defined twice, verbatim).
- Delete or implement the empty `agents/harness/` and `agents/tools/` packages (bare `__init__.py`, referenced nowhere) — confirmed still empty.
- Node 1 dead spec surface: never-populated `open_questions`; never-set `inferred`/`skipped_by_user` source flags; exit-regex over-matching (`\b(done|stop)\b` anywhere in an answer); graph-mode `node_intake` routing loop on an unlocked, question-exhausted brief. (`_QuestionDef.is_final` removed — no longer applicable.)

**Still genuinely open (design questions):**
- **Fitness beyond GPU/CPU (❓):** extend benchmark tier/score to more categories, or formally scope fitness to the two performance anchors? Until decided, `derive_fitness_thresholds` spends a `gpt-4o` call on nine slots of which seven are unusable; the legacy `weight` property seeded by `seed_graph.py` is inert and should be dropped from the seeder when this is settled.
- **Ask-if-ambiguous clarifications (§2.1):** the one designed exception to the static question sequence; no mechanism exists.
- ~~Node 3 lookahead~~ ✅ **RESOLVED**: warn-only accepted as sufficient. The originally designed preventive/blocking probes (plural) were considered and deliberately not built. The SELECTION_ORDER fix (Motherboard now locks immediately after CPU, before RAM) already substantially reduces the dead-end scenario the probe existed to catch, leaving only rarer residual cases (e.g. a CPU socket with near-zero in-stock compatible boards) — for those, a best-effort diagnostic warning is judged sufficient; real preventive lookahead/backtracking was weighed against its cost/complexity relative to this now-rare benefit and rejected.
- **Business-intelligence ranking layer (§5):** hidden margin/overstock weighting injected into Node 3's final-pick prompt; admin-configurable weights. Designed, not built.
- **Feasibility remaining open items:** non-component cost estimates (rough STUB values in `costs.py`); reused-parts compatibility (PC-of-record via `existing.existing_pc_build_id` consumed by nothing; reused-part constraints not applied inside `min_viable_build`). `_TIER_MIN_CORES` in `catalog_floor.py` is a stub proxy for CPU tier.
- **Threshold determinism, last lever:** `seed=` + `system_fingerprint` logging on the `gpt-4o` threshold call (temperature=0 alone does not guarantee reproducibility across backend fleets).
- **Software-spec retrieval:** runtime web search (Steam/Epic/vendor pages) vs the current `_SOFTWARE_SPECS` stub dict.
- **Context window management strategy:** flagged as its own dedicated session topic.

---

## 10. Tech Stack

| Layer | Planned | As built (Phase 0–4) |
|---|---|---|
| AI API | Anthropic Claude API (tool calling) | **OpenAI** `gpt-4o-mini` default, `gpt-4o` for fitness thresholds (`KARMA_THRESHOLD_MODEL`, temperature=0); shared wrapper in `agents/llm/client.py` |
| Pipeline orchestration | — | **LangGraph** `StateGraph` (`agents/graph.py`); refinement loop in the CLI harness, not the graph |
| Relational DB / product catalog | Supabase (managed Postgres) | Supabase Postgres via `psycopg2` `ThreadedConnectionPool` (`agents/db/postgres.py`); **direct host retired → Session Pooler required**; live: 103 products / 9 categories |
| ORM | Prisma | not yet in the Python pipeline (raw SQL via psycopg2) |
| API layer | Fastify | not yet built; `graph_runner.run_from_brief` is the ready entry point (no refinement path yet — §9) |
| Knowledge graph | Neo4j | **live**: Enterprise edition, local Docker, seeded (103 components; three compatibility families + PSU wattage + rejected-parts enforced; benchmark tier/score fitness on GPU/CPU); hosted migration pending |
| Schema validation | — | **Pydantic v2** throughout |
| Session / short-term memory | Redis | not yet wired |
| Product search | Meilisearch | not yet wired |
| ERP integration | Tally ERP via XML/ODBC | not yet wired |
| Software specs retrieval | Runtime web search (Steam, Epic, vendor pages) | currently a clearly-marked STUB dict in Node 2 |
| Testing | — | **pytest**: 46 tests (feasibility/allocation integration, `_fetch_floor` regressions, graph cache round-trip, offline refinement unit suite, PSU wattage, rejected-parts consistency); no end-to-end test (§9) |

---

## Appendix A — User Build Brief schema 🔒

The single structured artifact Node One emits and every downstream stage reads. A **living object**, re-filled in place on send-back / build-edit. **No prices stored.**

**Conventions**
- **Source flags:** `user_stated | inferred | default_applied | skipped_by_user` — so any node can tell a real answer from an assumption, and re-engagement can target only `inferred` / `skipped` fields. **As built:** the flag exists on four sections (`performance`, `monitor`, `storage`, `operating_system`), not every field; and only `user_stated` / `default_applied` are ever set in practice (§2.1 deviations).
- **Field tiers:** `required` (blocks lock — the must-ask budget + primary use case), `ask_if_ambiguous`, `optional` (skippable → explicit default).
- **Soft vs hard:** preference fields (purpose, physical, longevity, extras) are soft signals the LLM weighs; the `hard_constraints` block is non-negotiable, **pinned, and never compacted**.

```yaml
# 0 — Envelope
brief_id, user_id, chat_id, build_id, schema_version
status: draft | locked | revisiting
completeness: { required_complete: bool, optional_filled: int, optional_skipped: int }
open_questions: [string]            # drives follow-ups — NOTE: not yet populated by any code (§2.1)
created_at, updated_at

# 1 — Budget (REQUIRED)
budget:
  currency: INR
  comfortable_min: int
  comfortable_max: int
  ceiling: int                      # max stretch
  scope: pc_only | pc_plus_monitor | pc_plus_peripherals | full_setup
  notes: string | null

# 2 — Purpose (REQUIRED)
purpose:
  primary_use_case: gaming | content_creation | work_productivity | storage_homeserver | general_use
  sub_case: string                  # competitive_fps | open_world_aaa | video_editing | 3d_modeling | music_production | ...
  secondary_use_cases: [ { use_case, weight: low|medium|high } ]

# 3 — Software & workload  — drives requirement floors at the feasibility gate
software:
  - name: string                    # "Red Dead Redemption 2", "Premiere Pro", "Blender", "VS Code"
    category: game | video | 3d | audio | dev | other
    frequency: primary | secondary | occasional
    intensity: casual | moderate | heavy

# 4 — Performance targets (required for gaming, else optional)
performance:
  target_resolution: 1080p | 1440p | 4K | null
  target_framerate: int | "max"
  hdr_wanted: bool                  # default false
  source: <flag>

# 5 — Monitor (single source of truth)
monitor:
  owned: yes | no
  owned_specs: { resolution, refresh_hz, hdr, size_inch } | null   # if owned
  target_specs: { resolution, refresh_hz, hdr } | null             # if not owned and in scope
  count: int                        # default 1
  source: <flag>

# 6 — Peripherals (meaningful only when budget.scope includes peripherals)
peripherals:
  - type: keyboard | mouse | headset | mic | speakers | drawing_tablet | controller | webcam
    requirements: string | null     # "mechanical, low-latency", "high DPI wireless"
    priority: must_have | nice_to_have

# 7 — Storage (Node Two must size this)
storage:
  capacity_gb: int | null
  speed_tier: nvme | sata_ssd | hdd | mixed
  data_profile: cold | warm | hot | mixed
  source: <flag>

# 8 — Operating system (real budget line + affects part selection)
operating_system:
  os: windows | linux | dual_boot | none_reuse
  license: oem | retail | byo | na
  source: <flag>

# 9 — Existing assets & ecosystem (REQUIRED to ask)
existing:
  has_existing_parts: yes | no
  reuse_parts: [ { slot, identifier, action: keep|replace } ]
  existing_pc_build_id: uuid | null # PC-of-record for upgrades — NOTE: consumed by nothing yet (§2.2 open items)
  ecosystem_prefs: { cpu_brand_pref, gpu_brand_pref }   # SOFT

# 10 — Physical & environment (optional → defaults)
physical:
  form_factor_pref: full_tower | atx_mid | compact_matx | sff_itx | no_preference
  noise_tolerance: silent_priority | balanced | dont_care
  placement: open_desk | enclosed_cabinet | hot_room | normal
  portability_need: bool
  size_notes: string | null

# 11 — Reliability & longevity (optional → defaults)
longevity:
  reliability_priority: consumer | high_stability_alwayson | mission_critical
  upgrade_path: future_proof | balanced | set_and_forget
  timeline: buy_now | flexible_for_deals

# 12 — Aesthetics & extras (optional → defaults)
extras:
  rgb_pref: want_rgb | minimal | none | no_preference
  visual_style: showcase_glass | clean_sleeper | no_preference
  connectivity_needs: [wifi | bluetooth | thunderbolt | 10gbe | many_usb]
  specific_part_requests: [ { slot, requested } ]   # SOFT, validated vs live stock

# 13 — Hard constraints (PINNED, never compacted, append-only unless retracted)
hard_constraints:
  must_have:   [ { id, type, value, source: user_stated|derived, locked_at } ]
  must_not:    [ { id, type, value, source, locked_at } ]
  rejected_parts: [ { product_id, reason, rejected_at } ]   # Node Three must never re-surface
```

**Consumer map**

| Stage | Reads |
|---|---|
| Feasibility Check | budget, purpose, software, performance, hard_constraints (size/form-factor raise the floor; rejected_parts excluded from the floor), existing (reuse zeroing) |
| Node Two | budget, full purpose, software, performance, monitor + peripherals + OS (fixed-cost subtraction), existing.reuse_parts, longevity, hard_constraints |
| Node Three | everything — esp. software, ecosystem_prefs, extras, connectivity, hard_constraints, rejected_parts, reuse_parts |

**Mutability / re-fill rules**
- Reloaded and updated in place on send-back; `status → revisiting`, `open_questions` repopulated.
- Required fields may be revised but are never null once locked.
- `hard_constraints` accumulate across the session (append-only unless retracted) and are the one block guaranteed to survive compaction.
- Editing a saved build seeds a fresh Brief from the build's `intent_snapshot` + part list.
- A skipped optional field is recorded as `source: default_applied` (or `skipped_by_user`) — never silently assumed. _(As built, `skipped_by_user` is never set — §2.1.)_

---

## Appendix B — Repository Map (as built)

```
karma ai/
├── run_pipeline.py                 # CLI driver; owns the conversation + refinement loops;
│                                   #   --fixture / --fixture-all (fixture-all stops after allocation)
├── requirements.txt                # openai, langgraph, pydantic, psycopg2-binary, neo4j, ...
├── DESIGN.md                       # this document
├── docs/
│   ├── context.md                  # session-state log (open items, resolutions)
│   ├── lesson.md                   # post-mortem lessons
│   └── plan.md                     # active plan (empty when no plan in flight)
├── agents/
│   ├── llm/client.py               # call_structured / call_text / StructuredCallError (OpenAI wrapper)
│   ├── graph.py                    # LangGraph StateGraph (karma_graph) — no refinement node
│   ├── graph_runner.py             # run_from_brief(brief, price_bands) — API/fixture entry (ends at build card)
│   ├── state/pipeline_state.py     # PipelineState TypedDict + new_state()
│   ├── schemas/                    # source_flag, slots (ComponentSlot — canonical), brief,
│   │                               #   feasibility (incl. basis), price_bands, build_card
│   ├── costs.py                    # shared fixed-cost STUB tables + core_pools() — single source
│   │                               #   for Node 2 + feasibility
│   ├── nodes/
│   │   ├── node1_intake.py         # blank_brief, floor_met, next_question, extract_turn, ...
│   │   ├── node2_allocation.py     # allocate_budget; _distribute / _compute_bands (largest-remainder);
│   │   │                           #   _repair_bands_to_catalog
│   │   ├── node3_selector.py       # derive_fitness_thresholds, select_part, select_build, _fetch_floor,
│   │   │                           #   SELECTION_ORDER, ThresholdCache, PSU wattage wiring (locked_specs)
│   │   └── node3_refinement.py     # RefinementOps, route_field_edit, dispatch_refinement, apply_reject,
│   │                               #   diff_and_bias (pure; loop lives in run_pipeline.py)
│   ├── feasibility/
│   │   ├── resolver.py             # resolve_requirements + aggregate_scope (deterministic; STUB floor tables)
│   │   ├── estimate.py             # estimate_feasibility — deterministic verdict primary,
│   │   │                           #   LLM prose-only, llm_fallback path
│   │   └── catalog_floor.py        # slot_requirement_filter + required_psu_wattage +
│   │                               #   rejected_product_ids/filter_rejected + compute_catalog_floor /
│   │                               #   min_viable_build — shared by estimate, band repair,
│   │                               #   Node 3 floor filter, and diff_and_bias's incumbent-bias check
│   ├── db/
│   │   ├── postgres.py             # PostgresClient, get_min_catalog_price, get_parts_in_band
│   │   │                           #   (NOTE: no load_dotenv — §9 correctness gap c)
│   │   ├── neo4j.py                # ping, compatibility_check, fitness_filter → FitnessRanking,
│   │   │                           #   get_component_fitness
│   │   └── neo4j_schema.py         # constraints + indexes + apply_schema (Enterprise required)
│   ├── harness/                    # EMPTY package (bare __init__.py) — §9
│   ├── tools/                      # EMPTY package (bare __init__.py) — §9
│   └── output/formatter.py         # format_build_card (wired) / price_bands (swap pending) /
│                                   #   impossible / tight_warning
├── data/
│   ├── catalog/seed.sql            # catalog table (9 categories, INR prices, in_stock, specs JSONB)
│   ├── benchmarks/                 # gpu_benchmarks.csv + cpu_benchmarks.csv (fitness tier/score source)
│   ├── fixtures/                   # budget_gamer / video_editor / ml_workstation (canonical)
│   │                               #   + high_end_gamer + edge_intel_gamer / edge_tight_amd /
│   │                               #   edge_floor_violating_cheapest / edge_floor_at_band_low
│   └── graph/
│       ├── seed_graph.py           # seeds Neo4j from the Postgres catalog (idempotent MERGE)
│       └── seed_fitness_benchmarks.py  # seeds GOOD_FOR tier/score from benchmark CSVs (GPU + CPU)
├── scripts/
│   ├── test_db_connection.py       # self-service Supabase connection + catalog verifier
│   └── calibration_sweep.py        # ground-truth sweep: verdict/allocation/floor vs live stock
│                                   #   (Node-3 walk is a cheapest-pick PROXY, not select_build)
└── tests/                          # conftest.py (db_available) + test_pipeline_integration.py (10)
                                    #   + test_node3_selector.py (2) + test_graph_node_select.py (1)
                                    #   + test_node3_refinement.py (27) + test_psu_wattage.py (3)
                                    #   + test_rejected_parts_consistency.py (2) — 46 total; no E2E (§9)
```

**Git workflow:** feature branches `phase{N}/feature-name`, conventional commits, PRs merged to `main`. Always stage with specific paths (`git add "karma ai/agents/..."`), never `git add .` — the repo root accumulates Node/`__pycache__`/stray files that `.gitignore` now covers. Always merge with `git merge <branch> -m "..."` to avoid the editor opening.

---
