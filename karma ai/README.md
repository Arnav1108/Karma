# Karma AI

An AI-powered PC build recommendation pipeline built for **Karma Computers**, an Indian PC-parts wholesaler. Karma AI interviews a customer about their use case, checks build feasibility, allocates a budget across components, and selects compatible parts from a live Postgres catalog (optionally cross-checked against a Neo4j compatibility graph), producing a final "build card" that can be refined through a conversational loop (pin/reject/swap parts, adjust budget, etc.).

## Tech stack

- **Python 3.12**
- **[LangGraph](https://github.com/langchain-ai/langgraph)** (`StateGraph`) — orchestrates the pipeline as a graph of nodes
- **OpenAI API** — `gpt-4o-mini` by default, `gpt-4o` for fitness-threshold reasoning
- **Postgres** via Supabase (Session Pooler) — the live parts catalog
- **Neo4j** (Enterprise edition, local Docker) — compatibility graph (socket, DDR generation, form factor) and fitness benchmarks; the pipeline degrades gracefully to Postgres-only selection if Neo4j is unreachable

## Project layout

```
karma ai/
├── run_pipeline.py     # CLI driver; owns the conversation loop
├── requirements.txt
├── DESIGN.md            # architecture/design reference
├── docs/                 # context.md, lesson.md, plan.md — working notes
├── agents/               # LangGraph nodes, schemas, DB clients, LLM client
├── data/
│   ├── catalog/          # Postgres seed SQL (catalog, catalog expansion, software-spec cache)
│   ├── benchmarks/       # CPU/GPU benchmark CSVs
│   ├── fixtures/         # canned build briefs for fixture-driven runs and tests
│   └── graph/            # Neo4j seed scripts
├── scripts/               # DB connection check, calibration sweep, catalog/graph sync check
└── tests/                 # pytest suite, including tests/e2e/ (opt-in, live)
```

See `DESIGN.md` for the full architecture writeup.

## Setup

All commands below are run from inside `karma ai/`.

1. **Python environment** — Python 3.12, then install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

2. **Environment variables** — copy `.env.example` to `.env` and fill in the values:

   ```bash
   cp .env.example .env
   ```

   ```
   OPENAI_API_KEY=...
   # OPENAI_MODEL=gpt-4o-mini          # default
   # KARMA_THRESHOLD_MODEL=gpt-4o      # used for Node 3 fitness thresholds
   POSTGRES_URL=postgresql://postgres.<ref>:<password>@aws-0-ap-south-1.pooler.supabase.com:5432/postgres
   NEO4J_URI=bolt://localhost:7687
   NEO4J_USERNAME=neo4j
   NEO4J_PASSWORD=...
   ```

   `POSTGRES_URL` **must** be the Supabase **Session Pooler** URL (Dashboard → Connect → Session pooler) — the direct `db.<ref>.supabase.co` host is retired. If Postgres is unreachable, feasibility verdicts turn pessimistic and Node 3 returns empty build cards, so verify the connection first (step 3).

3. **Seed the Postgres catalog** — the catalog schema and data live in raw SQL files under `data/catalog/`, run against your Postgres database (e.g. via `psql` or the Supabase SQL editor):

   - `data/catalog/seed.sql` — creates the `catalog` table and seeds the base 9-category catalog
   - `data/catalog/seed_expansion.sql` — appends further catalog rows (append before `seed.sql`'s final `COMMIT;`, or run standalone in its own transaction — see the file header)
   - `data/catalog/software_specs_cache.sql` — creates the cache table for LLM-derived software hardware-requirement lookups

   Then verify the connection and catalog:

   ```bash
   python -m scripts.test_db_connection
   ```

4. **Seed Neo4j (optional)** — with a local Neo4j instance running (Enterprise edition, `NEO4J_ACCEPT_LICENSE_AGREEMENT=yes`; Community fails silently on the first constraint), seed it from the Postgres catalog:

   ```bash
   python -m data.graph.seed_graph
   ```

   Idempotent (uses `MERGE`), safe to rerun. Optionally also seed fitness benchmark data:

   ```bash
   python -m data.graph.seed_fitness_benchmarks
   ```

   Neo4j is optional — Node 3 detects reachability via `neo4j.ping()` and falls back to Postgres-only part selection if it's unavailable.

## Running the pipeline

```bash
# Full conversational run (interactive intake)
python run_pipeline.py

# Run against a single canned fixture (skips intake)
python run_pipeline.py --fixture data/fixtures/budget_gamer.json

# Run all fixtures in data/fixtures/ and print a summary
python run_pipeline.py --fixture-all

python run_pipeline.py --help
```

Fixtures live in `data/fixtures/` (e.g. `budget_gamer.json`, `high_end_gamer.json`, `video_editor.json`, `ml_workstation.json`, plus several `edge_*` adversarial cases).

## Tests

```bash
# Default suite — excludes end-to-end tests (see pytest.ini: `addopts = -m "not e2e"`)
pytest tests/

# A single test class
pytest tests/test_pipeline_integration.py::TestBudgetGamer -v

# Opt-in end-to-end tests (real LLM + Postgres/Neo4j calls, slower)
pytest -m e2e
```

`tests/manual/` holds standalone diagnostic scripts that are not part of the pytest suite.
