# Karma Advisor — API Architecture Design (v1)

**Design document only — no implementation in this pass.** Grounded in live code on branch `cleanup/bucket-c-graph`, 2026-07-16. All prices INR. All source stays inside `karma ai/`.

## Context

Karma Advisor's pipeline (intake → feasibility → allocation → selection) currently runs only via the CLI (`run_pipeline.py`). This design defines the FastAPI layer that exposes it to a future Next.js storefront as a **one-shot builder**: a multi-turn conversational intake that locks a `UserBuildBrief`, then a single build generation returning a `BuildCard`. Refinement (v1/v2 engines) and negotiation (v3) are dormant and out of scope, but the design keeps them additive.

**User decisions already made:**
1. Intake bridge = **resumable step refactor** — extract `drive_intake`'s loop body into a pure per-turn function; `drive_intake` becomes a thin loop over it (single source of loop logic, serializable session state).
2. Lock→build UX = **review screen + explicit trigger** — on lock the frontend shows a curated `BriefSummary`; the user clicks "Generate my build" which POSTs `/builds`.

**Ground truth this design is built on (verified against code):**
- `drive_intake(brief, answer_fn, conversation_history=None, phrase_fn=None) -> (UserBuildBrief, history)` — `node1_intake.py:803`; blocking callback loop; bookkeeping (`asked_so_far`, `open_question_attempts`, `pending_open_question_field`) in loop locals; force-locks on exit.
- `run_from_brief(brief: UserBuildBrief) -> PipelineState` — `graph_runner.py:15`; **single arg, no price_bands**. Impossible verdict → state has `error_message` + `feasibility_verdict`, **no** `build_card`; it does not raise.
- `extract_turn` swallows `StructuredCallError` (returns brief unchanged); has a "done"/"stop" + `floor_met` early-lock path.
- `floor_met(brief)` = `budget.comfortable_max > 0 and purpose.sub_case != ""`.
- 13-question `QUESTION_SEQUENCE` + 3-attempt clarification/confirm-to-default sub-machine.
- Postgres: lazy `ThreadedConnectionPool(1, 10)` — **maxconn=10 caps API concurrency**. Neo4j: `ping()`-based graceful degradation already in core.
- Silent-failure trap: with Postgres down, feasibility can go pessimistic and Node 3 can return an empty build card **without raising** — the API must detect and surface this loudly.
- `tests/manual/smoke_api_readiness.py` does not exist (only `__init__.py`); the clean core-only call pattern is demonstrated by `tests/e2e/test_full_pipeline.py:201,214`.
- The LangGraph's own `node_intake` discards the question text (never returned in state) → not usable as the turn API; considered and rejected.

---

## 1. System architecture overview

```
Next.js storefront (future)
   │  HTTPS + JSON, X-API-Key, CORS-pinned
   ▼
FastAPI app  ("karma ai/api/")
   ├── routers/intake  ──► IntakeService ──► intake_step() [new core fn] ──► OpenAI (extract + phrase)
   ├── routers/builds  ──► BuildService  ──► run_from_brief() ──► karma_graph ──► OpenAI / Postgres / Neo4j
   ├── routers/health  ──► PostgresClient ping, neo4j.ping()
   ├── SessionStore    (intake sessions; in-memory + TTL behind an interface)
   └── BuildJobRegistry(async jobs; in-memory + ThreadPoolExecutor)
```

Responsibility map:
| Layer | Owns | Never does |
|---|---|---|
| Next.js frontend | Rendering questions, collecting answers, progress UI, build card display | Money math, option enumeration, brief mutation |
| FastAPI layer | HTTP contracts, session/job lifecycle, error→HTTP mapping, auth/rate limits, DTO mapping | Pipeline logic, pricing, LLM prompting |
| Core (`agents/`) | All pipeline logic, all INR arithmetic, LLM calls, DB access | HTTP, process exit (`sys.exit` stays CLI-only) |
| `run_pipeline.py` | CLI only | Being imported by the API (**forbidden**) |

The API imports **only** core functions: the new `intake_step` (+ `blank_brief`, `floor_met`, `lock_brief`, `QUESTION_SEQUENCE`) and `run_from_brief`. It never imports `run_pipeline.py`.

## 2. The one core change (Phase 1, additive)

`drive_intake`'s loop body is extracted into a pure, resumable per-turn function in `node1_intake.py`. Proposed contract (names final at implementation):

```
class IntakeSessionState(BaseModel):          # fully JSON-serializable
    brief: UserBuildBrief
    history: list[dict]                        # conversation_history
    asked_so_far: set[str]                     # serialized as list
    open_question_attempts: dict[str, int]
    pending_open_question_field: str | None

intake_begin(state) -> (state, IntakeQuestion | None)
    # Computes + phrases the next question (LLM phrase call happens here).
    # None => sequence exhausted (caller decides lock vs cannot-proceed via floor_met).

intake_step(state, user_answer) -> (state, IntakeQuestion | None, locked: bool)
    # One full turn: extract_turn merge + bookkeeping + next question.
    # Mirrors drive_intake's loop body exactly.

class IntakeQuestion(BaseModel):
    question_id: str | None      # sequence field id, or the pending open-question field
    text: str
    kind: Literal["sequence", "clarification", "confirm_default"]
```

`drive_intake` is rewritten as `intake_begin` + a `while` loop over `intake_step` — behavior-identical; the existing intake unit tests and `tests/e2e/test_full_pipeline.py` prove parity. **Force-lock-on-interrupt stays inside `drive_intake` only** (CLI semantics); the API never force-locks an abandoned session.

## 3. Endpoint catalog

Base path `/api/v1`. Uniform error envelope on every non-2xx:
```
{ "error": { "code": "STRING_CODE", "message": "human-readable", "retryable": bool, "details": {…}? } }
```

### 3.1 `POST /intake/sessions` — start intake (sync, ~1–3s: one phrase LLM call)
Request: `{ "client_ref": string? }` (opaque frontend correlation id, optional)
Response `201`:
```
{ "session_id": uuid, "status": "asking",
  "question": QuestionDTO, "progress": ProgressDTO, "expires_at": iso8601 }
```
Errors: `502 LLM_UPSTREAM_ERROR` (retryable), `503 SERVICE_UNAVAILABLE`.

### 3.2 `POST /intake/sessions/{session_id}/answers` — submit one answer (sync, ~2–6s: extract LLM + phrase LLM)
Request: `{ "answer": string }` (1–2000 chars; "done"/"stop" flows through and may early-lock via core logic)
Response `200`, one of two shapes discriminated by `status`:
```
{ "status": "asking", "question": QuestionDTO, "progress": ProgressDTO, "expires_at": iso8601 }
{ "status": "locked", "brief_summary": BriefSummaryDTO, "progress": ProgressDTO }
```
Errors: `404 SESSION_NOT_FOUND` (unknown or expired), `409 SESSION_ALREADY_LOCKED`, `409 TURN_IN_PROGRESS` (concurrent answer to same session), `422 VALIDATION_ERROR`, `502 LLM_UPSTREAM_ERROR` (retryable — **session state unchanged**; client re-submits the same answer), `504 INTAKE_TURN_TIMEOUT` (retryable, state unchanged).

### 3.3 `GET /intake/sessions/{session_id}` — snapshot (sync, no LLM)
Response `200`: `{ "status": "asking"|"locked", "question": QuestionDTO?, "progress": ProgressDTO, "brief_summary": BriefSummaryDTO?, "expires_at": iso8601 }`
Errors: `404 SESSION_NOT_FOUND`.

### 3.4 `POST /intake/sessions/{session_id}/lock` — finish early (sync)
Locks via `lock_brief` iff `floor_met(brief)`.
Response `200`: `{ "status": "locked", "brief_summary": BriefSummaryDTO }`
Errors: `409 BRIEF_FLOOR_NOT_MET` with `details.missing: ["budget"|"primary_use_case", …]`, `409 SESSION_ALREADY_LOCKED`, `404`.

### 3.5 `DELETE /intake/sessions/{session_id}` — abandon (sync)
Response `204`. Idempotent (`204` even if already gone). Never force-locks.

### 3.6 `POST /builds` — trigger build generation (**async**: 202 + poll)
Request: `{ "session_id": uuid }` (v1; a future service-to-service variant may accept a raw `brief` — out of scope)
Response `202`: `{ "build_id": uuid, "status": "queued", "poll_after_ms": 2000 }`
Errors: `404 SESSION_NOT_FOUND`, `409 BRIEF_NOT_LOCKED`, `409 BUILD_ALREADY_ACTIVE` (one active build per session), `429 BUILD_CAPACITY` (concurrency cap reached, retryable), `503`.

**Why async:** `run_from_brief` = feasibility (LLM+DB) + allocation (LLM) + selection (gpt-4o threshold call + up to 9 per-slot LLM picks + Postgres/Neo4j round-trips) ≈ 30–120s. That exceeds sane HTTP/proxy/browser timeouts; a blocking response would also pin a server thread per user. Job + poll is the simplest robust shape and upgrades cleanly to SSE later (§10).

### 3.7 `GET /builds/{build_id}` — poll build status (sync)
Response `200`:
```
{ "build_id": uuid,
  "status": "queued"|"running"|"succeeded"|"infeasible"|"cannot_proceed"|"failed",
  "stage": "feasibility"|"allocation"|"selection"|null,      # coarse; best-effort in v1
  "verdict": VerdictDTO?,          # present on succeeded + infeasible
  "build": BuildCardDTO?,          # present on succeeded only
  "error": ErrorDTO?,              # present on failed
  "created_at": iso8601, "finished_at": iso8601? }
```
Errors: `404 BUILD_NOT_FOUND` (unknown or evicted after retention TTL).
Note: `infeasible` and `cannot_proceed` are **domain outcomes, not HTTP errors** — the poll returns `200` with the verdict/reason for the frontend to render (invariant 3: DB/LLM trouble is 5xx; a too-small budget is not).

### 3.8 `GET /healthz` (liveness) and `GET /readyz` (readiness)
`/healthz` → `200 { "status": "ok", "version": string }` always (process alive).
`/readyz` → `200 { "postgres": "up", "neo4j": "up"|"down", "status": "ok"|"degraded" }`; `503 { "postgres": "down", … }` when Postgres is unreachable (Postgres is required; Neo4j down = degraded-but-ready, matching core's built-in degradation).

## 4. Intake session lifecycle — state machine

```
            POST /sessions                     answer (extract+phrase)
  (none) ────────────────► ASKING ──────────────────────────────► ASKING   (loop, ≤13 sequence Qs
                              │                                              + clarification cycles)
                              │ answer ⇒ core locks (sequence exhausted,
                              │          or "done"+floor_met)                ┌────────────┐
                              ├────────────────────────────────────────────► │   LOCKED   │
                              │ POST /lock  (floor_met)                      └─────┬──────┘
                              ├────────────────────────────────────────────►──────┘│
                              │ TTL expiry / DELETE                                │ POST /builds
                              ▼                                                    ▼
                          EXPIRED / DELETED  (terminal; brief discarded,      build job created
                                              NEVER force-locked)             (session stays LOCKED)

  Build job:  QUEUED ──► RUNNING ──► SUCCEEDED | INFEASIBLE | CANNOT_PROCEED | FAILED   (all terminal)
                                      └─ evicted after retention TTL ⇒ 404 on poll
```

- `LOCKED` sessions get a longer TTL (survive the review screen + build wait).
- `CANNOT_PROCEED` should be unreachable via the API (builds require a locked brief, and locking requires `floor_met`) — kept as a mapped status as a safety net since the graph can emit it.

## 5. State management strategy

**Choice: server-side session store, in-memory + TTL in v1, behind a `SessionStore` interface.**

Trade-offs considered:
- *Client-carried state* (echo full `IntakeSessionState` each turn): stateless server, restart-safe — but ships the raw internal brief (source flags, envelope UUIDs, sentinel conventions) to the browser, invites tampering with a model the pipeline trusts, adds ~10–20 KB per request each way, and freezes the internal schema into the public contract. Rejected.
- *DB-backed sessions from day one*: durable, multi-instance — but adds tables + migrations for a business tool whose sessions live ~10 minutes. Deferred; the interface makes it a swap, not a rewrite (open question Q2).
- *In-memory server-side* (chosen): opaque `session_id` (UUIDv4) is the only thing the client holds; `IntakeSessionState` is JSON-serializable (thanks to the Phase 1 refactor) so the store can move to Redis/Postgres without touching handlers.

Mechanics:
- Sliding TTL: 30 min while `ASKING`, 24 h once `LOCKED` (env-tunable). Background sweep task evicts expired entries.
- **Concurrency:** one `asyncio.Lock` per session; a second answer while one is in flight → `409 TURN_IN_PROGRESS` (fail fast, don't queue). Two different users = two sessions = no shared mutable state (Postgres pool and the `costs.py` price cache are already thread-safe/benign).
- **Atomicity:** a turn's state mutation is committed to the store only after the full `intake_step` succeeds; any LLM failure/timeout leaves the previous state intact so the client can retry the same answer safely.
- Build job registry: in-memory `{build_id: JobRecord}`; `JobRecord` retains the **full final `PipelineState`** (brief, bands, card, locked_parts, fitness thresholds) — invisible in v1 DTOs but exactly what dormant refinement v2 needs later (§10). Retention 24 h or LRU cap.
- Deployment consequence (explicit): v1 is **single-process** (one uvicorn instance). Restart drops in-flight sessions/jobs — acceptable for a business tool; the store/registry interfaces are the upgrade seam.

## 6. Data models / DTOs

Internal domain models (`UserBuildBrief`, `PipelineState`, `PriceBands`) never cross the wire. API-facing DTOs (defined in `api/dtos.py`, mapped in `api/mappers.py`):

**QuestionDTO** — from the new `IntakeQuestion`:
```
{ "question_id": string|null, "text": string,
  "kind": "sequence"|"clarification"|"confirm_default" }
```

**ProgressDTO** — derived from the brief (13 = `len(QUESTION_SEQUENCE)`):
```
{ "answered": int, "total": 13, "floor_met": bool }
```

**BriefSummaryDTO** — curated read-only projection for the review screen (no envelope UUIDs, no source flags, no sentinels):
```
{ "budget": { "comfortable_min": int, "comfortable_max": int, "ceiling": int, "scope": string, "currency": "INR" },
  "purpose": { "primary_use_case": string, "sub_case": string,
               "secondary_use_cases": [ { "use_case": string, "weight": string } ] },
  "software": [ { "name": string, "intensity": string } ],
  "performance": { "target_resolution": string?, "target_framerate": int|"max"|null, "hdr_wanted": bool },
  "monitor": { "owned": "yes"|"no", "specs": string? },            # human-readable spec line
  "storage": { "capacity_gb": int?, "speed_tier": string },
  "operating_system": { "os": string, "license": string },
  "reuse_parts": [ { "slot": string, "identifier": string } ],
  "brand_prefs": { "cpu": string?, "gpu": string? },
  "hard_constraints": { "must_have": [string], "must_not": [string] },
  "completeness": { "required_complete": bool, "optional_filled": int, "optional_skipped": int } }
```
(Field set to be confirmed — open question Q5.)

**VerdictDTO** — from `FeasibilityVerdict`, minus internal `basis` (logged server-side):
```
{ "verdict": "comfortable"|"tight"|"impossible", "reason": string,
  "binding_constraint": string?, "suggested_adjustments": [string] }
```

**BuildCardDTO / BuildPartDTO** — from `BuildCard`/`BuildCardPart`, minus `changed_slots` (refinement-only):
```
BuildPartDTO: { "slot": "gpu"|"cpu"|"ram"|"storage"|"motherboard"|"psu"|"case"|"cooler"|"fans",
                "product_id": string, "name": string, "brand": string?,
                "price_inr": int, "justification": string }
BuildCardDTO: { "parts": [BuildPartDTO], "total_price_inr": int,
                "summary": string, "warnings": [string] }
```
`PriceBands` stays internal (allocation detail); a `?include=price_bands` debug flag can expose it later if wanted.

## 7. Error handling & status-code mapping

Core principle (invariant 3): **no core failure may kill the process**. Every route body and the job worker wrap core calls; `sys.exit` never enters the API path (it lives only in `run_pipeline.py`, which the API never imports).

| Core failure mode | Detected by | HTTP / job outcome | `error.code` | retryable |
|---|---|---|---|---|
| Malformed request body | Pydantic | `422` | `VALIDATION_ERROR` | no |
| Unknown/expired session or build | store miss | `404` | `SESSION_NOT_FOUND` / `BUILD_NOT_FOUND` | no |
| Answer to locked session | session status | `409` | `SESSION_ALREADY_LOCKED` | no |
| Concurrent turn, same session | per-session lock held | `409` | `TURN_IN_PROGRESS` | yes (after in-flight turn) |
| Early lock, floor unmet | `floor_met()` false | `409` | `BRIEF_FLOOR_NOT_MET` (+`details.missing`) | no |
| Build on unlocked brief | brief status | `409` | `BRIEF_NOT_LOCKED` | no |
| Build concurrency cap | semaphore full | `429` | `BUILD_CAPACITY` | yes |
| OpenAI error/timeout during intake turn | OpenAI SDK exc / `StructuredCallError` from phrasing | `502` (state rolled back) | `LLM_UPSTREAM_ERROR` | yes |
| Intake turn exceeds wall budget (~30 s) | API-level timeout | `504` (state rolled back) | `INTAKE_TURN_TIMEOUT` | yes |
| Postgres unreachable at request time | pool `RuntimeError` / ping fail | `503` | `DATABASE_UNAVAILABLE` | yes |
| Postgres/LLM dies **mid-build** | exception in worker | poll → `status:"failed"` | `DATABASE_UNAVAILABLE` / `LLM_UPSTREAM_ERROR` | yes (re-POST /builds) |
| **Silent degradation**: empty `build_card.parts` | post-run check: 0 parts ⇒ probe `PostgresClient` ping | poll → `status:"failed"` | `DEGRADED_DEPENDENCY` | yes |
| Verdict `impossible` | job result state | poll → `status:"infeasible"` + VerdictDTO | — (domain outcome) | n/a |
| Graph `cannot_proceed` | `error_message`, no verdict | poll → `status:"cannot_proceed"` | — (domain outcome; should be unreachable) | n/a |
| Build exceeds hard timeout (5 min) | worker watchdog | poll → `status:"failed"` | `BUILD_TIMEOUT` | yes |
| Anything else | catch-all handler | `500` / `status:"failed"` | `INTERNAL_ERROR` | no |

Note grounded in code: `extract_turn` already swallows `StructuredCallError` (brief unchanged, question re-asked) — so extraction failures usually surface as "same question again", not a 5xx; only the *phrasing* call and SDK-level errors bubble to `502`.

## 8. Frontend–backend contract (per screen)

1. **Chat screen** — `POST /intake/sessions` → render `question.text` as an assistant bubble; progress bar from `progress.answered/13`. Each user reply → `POST …/answers` with a typing indicator (expect 2–6 s). `kind:"clarification"` renders as a normal follow-up; `kind:"confirm_default"` may render yes/no quick-reply chips. A "Finish early" button appears once `progress.floor_met` → `POST …/lock`. On `502/504` show "retry" on the same message; on `404` (expired) offer restart.
2. **Review screen** — on `status:"locked"`, render `BriefSummaryDTO` as "here's what I understood", with a **Generate my build** button → `POST /builds`. (No edit-in-place in v1 — a wrong summary means restarting intake; refinement lands here later, §10.)
3. **Progress screen** — poll `GET /builds/{id}` every `poll_after_ms` (2 s). Show stage label if present ("Checking feasibility… / Allocating budget… / Selecting parts…"), else a generic animation with elapsed time. Polling (not SSE/WebSocket) in v1: trivial to implement on both sides, proxy-friendly, and the job model upgrades to SSE without contract breakage.
4. **Result screen** — `succeeded`: render `BuildCardDTO` — parts table (slot, name, brand, `price_inr` formatted as ₹), `summary`, `total_price_inr`, and `warnings` displayed prominently (they are dead-end notices, not fine print). `infeasible`: render `VerdictDTO.reason` + `suggested_adjustments` with a "start over" CTA. `failed`: error message + retry.

The frontend performs **zero arithmetic** beyond formatting `price_inr` integers (invariant 4).

## 9. Auth, rate limiting, config & deployment

**Auth (v1):** static API key(s) in `X-API-Key`, validated by middleware against `KARMA_API_KEYS` (comma-separated, rotatable). Sufficient for a business tool; `/healthz` is exempt. Forward path: swap the API-key dependency for per-user JWT when the storefront lands — `UserBuildBrief` already carries `user_id`/`chat_id` UUIDs (currently random placeholders per session); the auth principal will populate `user_id` with no schema change.

**Rate limiting (v1):** per-key+IP: intake turns ~20/min (each costs 2 LLM calls), session creates ~5/min, build creates ~3/hour; plus the global `MAX_CONCURRENT_BUILDS` semaphore. In-process limiter (e.g. slowapi-style) — no external infra.

**Concurrency budget:** Postgres pool `maxconn=10` (module-global in `agents/db/postgres.py`) ⇒ `KARMA_MAX_CONCURRENT_BUILDS=2` default (each build makes many sequential DB calls; intake makes none). Sync core calls run in a bounded thread pool — never on the event loop.

**Env vars:** existing (`OPENAI_API_KEY`, `OPENAI_MODEL`, `KARMA_THRESHOLD_MODEL`, `POSTGRES_URL` — Session Pooler URL only, `NEO4J_URI`/`NEO4J_USERNAME`/`NEO4J_PASSWORD`) plus new: `KARMA_API_KEYS`, `KARMA_SESSION_TTL_MIN=30`, `KARMA_LOCKED_SESSION_TTL_H=24`, `KARMA_MAX_CONCURRENT_BUILDS=2`, `KARMA_BUILD_TIMEOUT_S=300`, `KARMA_INTAKE_TURN_TIMEOUT_S=30`, `KARMA_CORS_ORIGINS`, `KARMA_API_HOST/PORT`. Core modules already `load_dotenv()`; the API config module reads the same `.env`, validates required vars at startup, and **fails readiness (not the process)** if missing.

**Neo4j portability:** the API never constructs Neo4j connections — it only calls core, which reads `NEO4J_URI`. The local-Docker → Aura migration is a pure env change (`bolt://` → `neo4j+s://…`); nothing in the API may assume localhost. `readyz` reports Neo4j down as `degraded`, mirroring core's Postgres-only fallback.

**Deployment (v1):** single uvicorn process (Windows dev; any single host in prod). In-memory state ⇒ no horizontal scale until the SessionStore/JobRegistry backends move to Redis/Postgres — a documented, deliberate constraint.

**Backend structure** (all inside `karma ai/`, per the hard rule):
```
karma ai/api/
├── main.py            # create_app() factory; router mounting; startup checks
├── config.py          # env parsing/validation (Settings)
├── errors.py          # error envelope, exception handlers, code constants
├── dtos.py            # all request/response models (§6)
├── mappers.py         # domain → DTO projections (brief→summary, card→DTO, verdict→DTO)
├── routers/
│   ├── intake.py      # §3.1–3.5
│   ├── builds.py      # §3.6–3.7
│   └── health.py      # §3.8
└── services/
    ├── session_store.py   # SessionStore interface + InMemorySessionStore (TTL, locks, sweep)
    ├── intake_service.py  # wraps intake_begin/intake_step; atomic commit-on-success
    └── build_service.py   # JobRegistry + ThreadPoolExecutor worker around run_from_brief
```

## 10. Forward compatibility (design for, do not build)

- **Refinement v2** (dormant `parse_refinement_request_v2` / `dispatch_refinement_v2`): slots in as `POST /builds/{build_id}/refinements`. Its exact inputs — brief, price_bands, build_card, locked_parts, ThresholdCache — are all in the `JobRecord`'s retained `PipelineState` (§5), so this is a new router + a job-state mutation, no v1 rewrite. `BuildCardDTO` would then start exposing `changed_slots`.
- **Negotiation v3:** reuses the session infrastructure; sessions gain a `kind` discriminator (`intake` | `negotiation`).
- **E-commerce backend:** a sibling `routers/catalog.py` over `PostgresClient` (`get_all_products`, `get_parts_in_band`); "accept build" becomes `POST /builds/{id}/accept` returning ordered `product_id`s (mirroring `RefinementResult.product_ids`) feeding a cart/order service.
- **Streaming progress:** add an optional additive `progress_fn` param to `run_from_brief` (or drive `karma_graph.stream()` in `build_service`) to populate `stage` in real time; expose `GET /builds/{id}/events` (SSE). The poll contract stays valid regardless.

## 11. Open questions & decisions needed

1. **LLM timeout/retry policy** — `agents/llm/client.py` was not audited in this pass; confirm whether `call_structured`/`call_text` set explicit OpenAI timeouts/retries. If not, the API's turn/build timeouts (§7) are the only guard — acceptable, but core-level timeouts are better. *(Verify at Phase 2 start.)*
2. **Durability** — is losing in-flight sessions/jobs on restart acceptable for v1 (recommended: yes), or should locked briefs + finished builds persist to Postgres from day one (two small tables)?
3. **Builds per session** — recommended: 1 active at a time, re-generation allowed after a terminal state (each run costs real LLM money). Confirm.
4. **Rate-limit numbers & audience** — who holds API keys in v1 (internal staff only?); the §9 numbers are placeholders to confirm.
5. **BriefSummaryDTO field set** — confirm the review-screen projection in §6 (add/remove fields is a mapper-only change).
6. **Hosting target** — unspecified; nothing structural depends on it, but Postgres egress (Supabase pooler) and Neo4j reachability from the host must be checked before deploy.
7. **Build result retention** — 24 h in-memory proposed; confirm.

## Phased execution plan (each phase independently implementable)

**Phase 1 — Core intake refactor** *(no API code; touches `karma ai/agents/nodes/node1_intake.py` only)*
Extract `IntakeSessionState` + `intake_begin` + `intake_step` + `IntakeQuestion`; rewrite `drive_intake` as a thin loop over them. Depends on: nothing.
*Verify:* `pytest tests/` (intake units + `tests/e2e/test_full_pipeline.py` must pass unchanged — they are the parity proof); `python run_pipeline.py` conversational smoke.

**Phase 2 — API skeleton** *(new `karma ai/api/` package)*
App factory, `config.py`, error envelope + exception handlers, `/healthz` + `/readyz`, API-key middleware, CORS. Audit `agents/llm/client.py` timeout behavior (Q1). Depends on: nothing (parallel with Phase 1).
*Verify:* `uvicorn api.main:app` from `karma ai/`; curl `/readyz` with Postgres up and down (expect `200` vs `503`, process alive throughout).

**Phase 3 — Intake endpoints**
`SessionStore` (in-memory, TTL, per-session locks, sweep), `IntakeService`, routes §3.1–3.5, DTOs + mappers for questions/progress/brief summary. Depends on: Phases 1+2.
*Verify:* scripted full conversation over HTTP (reuse answer scripts from `tests/e2e/intake_script.py`) → locked brief; expiry, double-answer `409`, early-lock `409/200`, LLM-failure rollback tests.

**Phase 4 — Build endpoints**
`JobRegistry` + executor + watchdog around `run_from_brief` (unmodified), routes §3.6–3.7, verdict/card mappers, empty-card degradation check. Depends on: Phase 2 (not 3 — testable by injecting fixture briefs from `data/fixtures/` into a stubbed session).
*Verify:* POST fixture-brief session → poll to `succeeded`; force `impossible` via an edge fixture → `infeasible`; kill Postgres mid-run → `failed`/`DEGRADED_DEPENDENCY`, server stays up.

**Phase 5 — Hardening**
Rate limits, concurrency caps under load, TTL sweeps, structured logging (session_id/build_id correlation), timeout tuning, resolve Q2–Q7. Depends on: 3+4.
*Verify:* two parallel scripted users end-to-end without collision; restart-behavior documented; capacity `429` path exercised.

**Phase 6 — Frontend contract package**
Export OpenAPI spec from FastAPI, freeze the DTO contract, produce example request/response fixtures per screen (§8) for the Next.js team. Depends on: 3+4.
*Verify:* OpenAPI validates; fixtures round-trip against a live server.
