# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Karma Advisor is a LangGraph-based agentic pipeline that interviews a customer, checks build feasibility, allocates a budget, and selects compatible PC parts from a live Postgres catalog (optionally cross-checked against a Neo4j compatibility graph). Built for Karma Computers, a wholesale PC parts business.

**Stack:** Python · LangGraph (`StateGraph`) · OpenAI API (`gpt-4o-mini` default, `gpt-4o` for fitness thresholds) · Postgres via Supabase (Session Pooler) · Neo4j (Enterprise edition, local Docker, live and seeded).

## Project layout

All source lives under `karma ai/` (note the space — this is the canonical directory name). Never create `karma-advisor/` or any other root-level source directory.

```
karma ai/
├── run_pipeline.py          # CLI driver; owns the conversation loop
├── requirements.txt
├── agents/
│   ├── llm/client.py        # call_structured / call_text / StructuredCallError
│   ├── graph.py             # LangGraph StateGraph (karma_graph)
│   ├── graph_runner.py      # run_from_brief(brief, price_bands) — API entry point
│   ├── state/pipeline_state.py
│   ├── schemas/             # source_flag, slots (ComponentSlot), brief, feasibility, price_bands, build_card
│   ├── nodes/
│   │   ├── node1_intake.py
│   │   ├── node2_allocation.py
│   │   ├── node3_selector.py
│   │   └── node3_refinement.py
│   ├── costs.py              # shared fixed-cost tables + core_pools() — single source for node2 + feasibility
│   ├── feasibility/
│   │   ├── resolver.py      # deterministic requirements resolution
│   │   ├── estimate.py      # feasibility gate — deterministic verdict from catalog_floor.py when Postgres is reachable (LLM writes prose only); single-anchor LLM estimate as fallback
│   │   └── catalog_floor.py # min-cost compatible in-stock build from resolved floors; shared by estimate.py, node2 band repair, node3 floor filter
│   ├── db/
│   │   ├── postgres.py      # ThreadedConnectionPool; get_min_catalog_price, get_parts_in_band
│   │   ├── neo4j.py         # ping, compatibility_check, fitness_filter
│   │   └── neo4j_schema.py  # constraints, indexes, apply_schema
│   └── output/formatter.py
├── data/
│   ├── catalog/seed.sql     # Postgres catalog (9 categories, INR prices, specs JSONB)
│   ├── fixtures/            # budget_gamer / video_editor / ml_workstation + edge_* adversarial fixtures
│   └── graph/seed_graph.py  # seeds Neo4j from Postgres catalog (idempotent MERGE)
├── scripts/
│   ├── test_db_connection.py
│   └── calibration_sweep.py # rerunnable ground-truth sweep: verdict/allocation/floor calibration vs live catalog stock
└── tests/                   # conftest.py + test_pipeline_integration.py
```

## Common commands

All commands must be run from inside `karma ai/`:

```bash
# Full conversational run
python run_pipeline.py

# Single fixture (skips intake)
python run_pipeline.py --fixture data/fixtures/budget_gamer.json

# All three fixtures in one pass
python run_pipeline.py --fixture-all

# Integration tests
pytest tests/

# Single test class
pytest tests/test_pipeline_integration.py::TestBudgetGamer -v

# Verify Supabase connection + catalog
python -m scripts.test_db_connection

# Seed Neo4j from Postgres catalog (once a live instance is running)
python -m data.graph.seed_graph
```

## Environment

Copy `.env.example` to `.env` and fill in values:

```
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-4o-mini          # default; gpt-4o used for fitness thresholds via KARMA_THRESHOLD_MODEL
KARMA_THRESHOLD_MODEL=gpt-4o
POSTGRES_URL=postgresql://postgres.<ref>:<password>@aws-0-ap-south-1.pooler.supabase.com:5432/postgres
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=...
```

**Critical:** `POSTGRES_URL` must be the **Session Pooler URL** (Dashboard → Connect → Session pooler). The direct host `db.<ref>.supabase.co` is retired. If Postgres is unreachable, feasibility verdicts are pessimistic and Node 3 returns empty build cards — this fails silently, not loudly, so check `scripts/test_db_connection.py` first when builds look wrong.

## Pipeline architecture

Linear flow: `Node 1 (Intake) → Feasibility Check → Node 2 (Budget Allocation) → Node 3 (Part Selection) → Refinement loop`

- **Node 1** (`node1_intake.py`): Stateless conversational intake. Exposes `blank_brief`, `floor_met`, `next_question`, `extract_turn`, `newly_filled_sections`. The CLI harness (`run_pipeline.py`) owns the loop; Node 1 is one-turn-only in the LangGraph.
- **Feasibility Check** (`feasibility/estimate.py`): Three-state gate (`comfortable | tight | impossible`). Calls `resolver.py` deterministically, then an LLM with one live Postgres price anchor (cheapest GPU). `impossible` routes to failure surface; other verdicts proceed.
- **Node 2** (`node2_allocation.py`): LLM emits relative weights; Python converts to INR bands via largest-remainder normalization (`_distribute` / `_compute_bands`). Sums hold by construction.
- **Node 3** (`node3_selector.py`): Three-step funnel per slot — Postgres catalog query → Neo4j compatibility + fitness filter → LLM final pick. Selection order: GPU → CPU → RAM → Storage → Motherboard → PSU → Case → Cooler → Fans. Fitness thresholds are derived **once upfront** via `gpt-4o`, stored in build state, never re-derived per slot. Degrades gracefully when Neo4j is unavailable (Postgres-only).
- **Refinement loop** (`node3_refinement.py`): Actions — `pin | open | swap | accept | restart`. `MAX_REFINEMENT_ROUNDS = 5`.
- **LangGraph** (`graph.py`): `StateGraph` compiling the above. `graph_runner.run_from_brief` is the ready API entry point.

## Model allocation policy

| Call | Model |
|---|---|
| Node 1 extraction, feasibility verdict, Node 2 allocation skew, Node 3 final part pick, refinement parse | `gpt-4o-mini` (via `OPENAI_MODEL`) |
| **Node 3 fitness thresholds** | **`gpt-4o`** (via `KARMA_THRESHOLD_MODEL`) |

Rule: tasks requiring multi-dimensional tradeoff reasoning without explicit scaffolding use `gpt-4o`; schema-constrained or prompt-scaffolded tasks stay on `gpt-4o-mini`.

## Key data contracts

- **`ComponentSlot`** (`agents/schemas/slots.py`): the canonical enum for all nine slots. Use this everywhere — never raw strings for slot names in new code. Exception: `PipelineState.locked_parts` uses string keys for graph-state serializability.
- **`UserBuildBrief`** (`agents/schemas/brief.py`): the artifact Node 1 emits and every downstream stage reads. Never stores prices. Every field carries a `source` flag (`user_stated | inferred | default_applied | skipped_by_user`).
- **`PriceBands`** (`agents/schemas/price_bands.py`): `root` is a dict of `ComponentSlot → PriceBand(low, mid, high)`. `total_mid()` == core budget target; `total_high()` == ceiling.
- **`agents/costs.py`**: single source of truth for fixed non-component costs (OS license, monitor, peripherals, reused-part value) and `core_pools(brief) -> (floor, target, ceiling)`, the core component budget pool. Both `node2_allocation.py` and `feasibility/estimate.py` read this — never duplicate a cost table elsewhere.
- **`agents/feasibility/catalog_floor.py`**: `slot_requirement_filter(slot, parts, req, brief, enforce_brand)` is the canonical per-slot requirement-floor predicate (GPU VRAM, CPU tier→min cores, RAM/storage capacity, storage type). `compute_catalog_floor(brief, req) -> CatalogFloor | None` derives the min-cost compatible in-stock build. Three consumers share this — `estimate.py` (verdict), `node2_allocation.py` (band repair), `node3_selector.py` (hard floor filter) — so they cannot drift; never reimplement "meets floor" locally.

## Neo4j status

Live — Enterprise edition, local Docker (`neo4j:5-enterprise`, `NEO4J_ACCEPT_LICENSE_AGREEMENT=yes`; Community fails silently on the first `NODE_KEY` constraint). Seeded via `python -m data.graph.seed_graph` (idempotent MERGE). All three compatibility families — socket (CPU↔motherboard, cooler↔CPU), DDR generation (motherboard↔RAM), form factor (case↔motherboard) — are enforced as hard filters, never bypassed by the price-band relaxation ladder. Node 3 detects reachability via `neo4j.ping()` and degrades to Postgres-only selection if unreachable. Not yet migrated off local Docker to a hosted instance reachable by a deployed backend — pre-production blocker.

## Git workflow

Feature branches: `phase{N}/feature-name`. Always stage with specific paths:
```bash
git add "karma ai/agents/..."
```
Never `git add .` — the repo root accumulates `node_modules/`, `__pycache__/`, and stray files. Always merge with an explicit message to avoid the editor opening:
```bash
git merge <branch> -m "Merge branch '...'"
```

## Hard rules

- Never create `karma-advisor/` or any directory other than `karma ai/` for source
- Never `git add .`
- `POSTGRES_URL` must be the Session Pooler URL, never `db.<ref>.supabase.co`
- Use `ComponentSlot` for slot references everywhere except `PipelineState.locked_parts`
- Never store prices on `UserBuildBrief`