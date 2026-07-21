# Build Service Plan — Karma Advisor API Phase 4

> **Status:** planning document for the Phase 4 build endpoints. Grounded in the
> **real** field shapes of the core objects, verified against current `main` (not the
> `api_design.md` sketch). Disagreements with the design doc are flagged inline.

## Context

Phase 3 shipped the intake layer: a client drives a conversational intake to a
**locked** `UserBuildBrief`, held in `InMemorySessionStore` and durably written to
the Postgres `locked_briefs` table. Phase 4 turns that locked brief into a finished
**build card** by driving the existing core pipeline (`run_from_brief`) behind an
async HTTP surface.

The core work — `run_from_brief(brief) -> PipelineState` — is **fully synchronous**
and slow: it fans out across feasibility, allocation, and up to nine per-slot
selection steps, making on the order of **6–12 sequential LLM calls plus many
Postgres queries**, for a 30–120 s wall time. That cannot block an HTTP request or
the event loop, so the build endpoints are **asynchronous**: `POST /builds` returns
`202 + {build_id}` immediately and the client polls `GET /builds/{id}` until a
terminal status. This document specifies the `JobRegistry`, `BuildService`,
execution model, concurrency cap, watchdog, result mapping, DTOs, and routes — all
built on the patterns already established by the intake layer.

### Ground-truth confirmations (read against current `main`)

| Claim | Verified reality |
|---|---|
| `run_from_brief` signature | `run_from_brief(brief: UserBuildBrief) -> PipelineState` — synchronous, body is `karma_graph.invoke(initial)`. (`agents/graph_runner.py`) |
| Raises on infra, returns on domain outcomes | **Yes.** LLM failures (`StructuredCallError` / `openai.OpenAIError`) and DB failures propagate out of the node functions through `invoke` and out of `run_from_brief`. Domain outcomes return normally: `impossible` verdict routes to `node_surface_failure`, which sets `error_message`, leaves `feasibility_verdict.verdict == "impossible"`, and produces **no `build_card`**. (`agents/graph.py`) |
| `cannot_proceed` reachable? | **No — structurally unreachable via `run_from_brief`.** The runner pre-loads a locked brief and jumps straight to `node_feasibility`; `node_cannot_proceed` is an intake-exhaustion terminal only reachable from `node_intake`. Design doc §7 agrees ("should be unreachable"). Status retained for completeness. |
| `PipelineState` fields | `current_brief, conversation_history, feasibility_verdict, price_bands, build_card, locked_parts, fitness_thresholds, fitness_thresholds_key, open_question_attempts, error_message, current_node` (TypedDict, `total=False`). **There is no `stage` field** — see watchdog/stage note. (`agents/state/pipeline_state.py`) |
| `BuildCard` fields | `parts: list[BuildCardPart]`, `total_price_inr: int`, `summary: str`, `warnings: list[str]`, `changed_slots: list[dict]`. `changed_slots` is empty on a fresh build (refinement-only). (`agents/schemas/build_card.py`) |
| `BuildCardPart` fields | `slot: ComponentSlot`, `product_id: str`, `name: str`, `price_inr: int`, `justification: str`, `brand: str \| None`. No per-part warnings field. |
| `FeasibilityVerdict` fields | `verdict: Literal["comfortable","tight","impossible"]`, `basis: Literal["deterministic","llm_fallback","stub"]`, `reason: str`, `binding_constraint: str \| None`, `suggested_adjustments: list[str]`. **`basis` is internal diagnostics** — dropped from the DTO. |
| LLM client timeout | **Still none.** `call_structured` / `call_text` call `client.chat.completions.create(...)` with no `timeout=`. (`agents/llm/client.py`) |

---

## 1. `JobRegistry` design

The analog to `InMemorySessionStore`, holding build jobs keyed by a server-generated
`build_id`. Placed at `api/services/job_registry.py`, decoupled from `agents/` in
spirit but — unlike `SessionStore` — it **may** hold a `PipelineState` as an opaque
`Any` (it never serializes it; only the mapper reads it).

### `JobRecord` (plain `@dataclass`)

| Field | Type | Notes |
|---|---|---|
| `build_id` | `str` | uuid4, server-generated |
| `session_id` | `str` | the source locked session |
| `status` | `BuildStatus` | see below |
| `created_at` | `datetime` | when queued |
| `started_at` | `datetime \| None` | when the worker picked it up (queued→running) |
| `finished_at` | `datetime \| None` | terminal transition time; TTL is measured from here |
| `state` | `Any` | the **full final `PipelineState`** (brief, bands, card, `locked_parts`, fitness thresholds) — retained per design §5 so dormant refinement v2 (§10) has its inputs. `None` until terminal. |
| `error_code` | `str \| None` | e.g. `BUILD_TIMEOUT`, `DATABASE_UNAVAILABLE`, `LLM_UPSTREAM_ERROR`, `DEGRADED_DEPENDENCY` — set only on `failed` |
| `error_message` | `str \| None` | human-safe message (never raw SDK internals) |
| `warnings` | `list[str]` | build-level warnings synthesized post-run (Neo4j-degraded notice; see §6) |

**No per-record `asyncio.Lock`** (see reasoning below).

```
BuildStatus = Literal[
    "queued", "running",            # non-terminal
    "succeeded", "infeasible",      # terminal, domain outcomes
    "cannot_proceed", "failed",     # terminal (cannot_proceed structurally unreachable)
]
```

### Statuses

Exactly the six from design §3.7 / §7. `queued`→`running`→ one of the four terminal
states. `cannot_proceed` is retained for contract completeness but is unreachable
through `run_from_brief`.

### TTL / retention

- Terminal jobs retained **24 h** measured from `finished_at` (design §5: "24 h or
  LRU cap") so the frontend can re-poll the result screen. This mirrors
  `LOCKED_TTL_SECONDS = 86400`.
- `queued`/`running` jobs have **no TTL** — they are active and must not be evicted
  from under their own worker.
- Recommend **both** a 24 h TTL **and** an LRU cap (e.g. 500 records) to bound
  unbounded growth (flagged in §8).
- Lazy expiry on access + an explicit `sweep_expired()`, exactly mirroring
  `InMemorySessionStore`. The sweeper also doubles as the watchdog (§5).

### Locking — do NOT copy `SessionStore`'s per-record lock reflexively

`SessionRecord.lock` exists because two concurrent `POST /answers` turns can race
the **same** session — the client mutates in-flight session state. **A build job has
no such hazard:** it is written by exactly one writer (its own `_run_and_store`
task) and only ever *read* by pollers. Clients never mutate an in-flight build (v1
has no `PATCH /builds/{id}`; refinement is a **future, separate** endpoint that
produces new state, design §10). Therefore:

- **No per-`JobRecord` lock.** Drop the field entirely.
- **One store-level `asyncio.Lock`** guarding the `_jobs` dict across concurrent
  `create` / `get` / `update` / `sweep` — identical to
  `InMemorySessionStore._store_lock`.

This is the one place the intake pattern should be *narrowed*, not copied.

---

## 2. Execution model

`run_from_brief` is synchronous and CPU/IO-bound across many blocking LLM + DB calls,
so it **must not** run on the event loop. Chosen model:

- **A dedicated `ThreadPoolExecutor(max_workers=KARMA_MAX_CONCURRENT_BUILDS)`**,
  created in `create_app()` and stored on `app.state.build_executor` (shut down on
  app shutdown). A dedicated pool — not the default `run_in_executor(None, ...)`
  pool that intake shares — so long builds cannot starve intake's short LLM turns,
  and so bounding `max_workers` *is* the concurrency cap (§4).
- **`asyncio` task wrapping `run_in_executor`**, not raw `executor.submit` +
  `add_done_callback`. Rationale: the `JobRegistry` is async (store-level
  `asyncio.Lock`) and must be mutated **on the event loop**; a bare thread callback
  would have to hop back onto the loop anyway. So the terminal write happens
  naturally inside the awaiting coroutine.

### Flow

```
POST /builds
  → BuildService.start_build(session_id):
      validate + capacity check (§4)
      registry.create(JobRecord(status="queued"))
      task = asyncio.create_task(self._run_and_store(build_id, brief))
      self._track(task)                 # keep a strong ref so it isn't GC'd
      return build_id                   # HANDLER RETURNS 202 IMMEDIATELY — fire-and-forget

_run_and_store(build_id, brief):        # runs on the loop, awaits the worker
      registry.update(build_id, status="running", started_at=now)
      try:
          state = await asyncio.wait_for(
              loop.run_in_executor(self._executor, run_from_brief, brief),
              timeout=KARMA_BUILD_TIMEOUT_S,
          )
          terminal = self._classify(state)      # succeeded / infeasible / degraded
          registry.update(build_id, status=terminal, state=state, warnings=..., finished_at=now)
      except asyncio.TimeoutError:
          registry.update(build_id, status="failed", error_code="BUILD_TIMEOUT", ...)
      except (openai.OpenAIError, StructuredCallError) as e:
          registry.update(build_id, status="failed", error_code="LLM_UPSTREAM_ERROR", ...)
      except Exception as e:
          registry.update(build_id, status="failed", error_code="DATABASE_UNAVAILABLE" | "INTERNAL_ERROR", ...)
      finally:
          # capacity slot is reclaimed when the executor future ACTUALLY resolves,
          # not on wait_for timeout — see §4 and §5.
```

The HTTP handler does **not** await the future or any short-bounded version of it —
it returns `202` the moment the job is registered and the task scheduled
(fire-and-forget). All result-writing happens inside `_run_and_store`, back on the
event loop, through the async registry — no cross-thread registry mutation.

---

## 3. `BuildService` contract

`api/services/build_service.py`, injected as an `app.state.build_service` singleton
(same DI pattern as `get_intake_service`, via a `get_build_service(request)` helper).

```python
class BuildService:
    def __init__(
        self,
        registry: JobRegistry,
        session_store: SessionStore,      # to read the locked brief
        executor: ThreadPoolExecutor,     # dedicated build pool
        *,
        max_concurrent: int,              # KARMA_MAX_CONCURRENT_BUILDS
        timeout_s: float,                 # KARMA_BUILD_TIMEOUT_S
    ) -> None: ...

    async def start_build(self, session_id: str) -> str:        # -> build_id
    async def get_build_status(self, build_id: str) -> JobRecord
```

### `start_build(session_id) -> build_id`

1. `record = await session_store.get(session_id)`; `None` → `SessionNotFoundError`.
2. `record.status != "locked"` → `BriefNotLockedError`.
3. Capacity: active builds `>= max_concurrent` → `BuildCapacityError` (§4).
4. **Pull the locked brief from `record.state.brief`.**
5. `registry.create(...)` with `status="queued"`; schedule `_run_and_store`; increment active count.
6. Return `build_id`.

### Which brief source is authoritative — SessionStore vs `locked_briefs`?

**SessionStore is the sole authoritative and *only readable* source today.**
`PostgresClient` exposes `persist_locked_brief(...)` but **no read-back method** —
`locked_briefs` is currently a write-only durability sink. So the build reads the
brief from the in-memory session. **Gap (flagged in §8):** if the locked session
TTL-expires (24 h) before the user clicks *Generate*, the brief is durably in
Postgres yet unreachable, and the build cannot start. The clean fix is a
`PostgresClient.get_locked_brief(session_id | brief_id)` so the service can fall back
to Postgres when the session has expired — not built here.

### `get_build_status(build_id) -> JobRecord`

`registry.get(build_id)`; `None` → `BuildNotFoundError`. The router maps the record
to the discriminated poll DTO (§7).

### Exception taxonomy (mirrors `IntakeServiceError`)

`api/services/exceptions.py` gains a parallel `BuildServiceError` base + subclasses:

| Exception | Meaning |
|---|---|
| `BuildServiceError` | base (catch-all handler, like `IntakeServiceError`) |
| `SessionNotFoundError` | **reuse** intake's — session unknown/expired |
| `BriefNotLockedError` | session exists but `status != "locked"` |
| `BuildNotFoundError` | `build_id` unknown or evicted |
| `BuildCapacityError` | at `KARMA_MAX_CONCURRENT_BUILDS` — retryable |
| `BuildAlreadyActiveError` | a build for this session is already queued/running. Non-retryable (a blind re-`POST` fails identically); carries `details.build_id` so the client polls the existing build instead of retrying — matches `api_design.md` §3.6's `BUILD_ALREADY_ACTIVE` contract |

Crucial distinction: these map to **request-time HTTP errors** on `POST`/`GET`. The
**async job's own** failure (Postgres dies mid-build) is **not** an HTTP error — the
`POST` already returned `202`. It surfaces as a `200` poll body with
`status:"failed"` (§6/§7). This extends the design's "domain outcomes are 200" rule:
in the async model even *infra* failures of the running job are reported via the poll
body, never a late 5xx.

---

## 4. Concurrency cap enforcement (`KARMA_MAX_CONCURRENT_BUILDS`, default 2)

The design (§9) sets the default to **2** precisely because the Postgres pool is
`maxconn=10` (module-global in `agents/db/postgres.py`), shared with intake, and each
build makes many sequential DB calls. Enforcement is **two-layered**:

1. **Bound the executor:** `ThreadPoolExecutor(max_workers=max_concurrent)`. This is
   the hard ceiling — even if a job is scheduled, no more than `max_concurrent`
   `run_from_brief` calls execute at once.
2. **Fail-fast admission counter:** an `asyncio.Lock`-guarded integer
   `_active_builds` in `BuildService`. `start_build` rejects with
   `BuildCapacityError` when `_active_builds >= max_concurrent`, rather than silently
   queueing behind the executor. With cap=2 and 30–120 s builds, a hidden queue would
   make `202` a lie ("queued for 4 minutes"); fail-fast is better UX.

`_active_builds` is incremented at admission and **decremented only when the executor
future actually resolves** (the thread truly returns) — deliberately *not* on a
`wait_for` timeout (§5), so admission accounting never over-admits into a slot a
still-running orphaned thread occupies.

**At-cap response:** `429` with code `BUILD_CAPACITY`, `retryable: true`, and a
`Retry-After` header. Proposed value ~30 s (build-duration order); exact value
tunable — flagged in §8. Matches design §7's capacity intent.

---

## 5. Watchdog / timeout (`KARMA_BUILD_TIMEOUT_S`, default 300 s)

There is **still no core-level timeout** (LLM client has no `timeout=`; no Postgres
`statement_timeout`). So a route/task-level timeout has the **same fundamental
limit** it had for intake — but at higher stakes.

### Mechanism

`_run_and_store` wraps the worker await in
`asyncio.wait_for(run_in_executor(...), timeout=KARMA_BUILD_TIMEOUT_S)`. On overrun →
mark the job `failed` / `BUILD_TIMEOUT` in the registry. A background **sweeper**
(the same periodic task as `sweep_expired`) additionally scans for still-`running`
jobs whose age exceeds the timeout and flips them to `failed`/`BUILD_TIMEOUT`, so the
reported timeout fires **even when no client is polling** and even if the awaiting
task were lost.

### Honest caveat (must be documented, not hidden)

`asyncio.wait_for` cancels the **awaiting coroutine, not the underlying thread.** The
executor worker keeps running `run_from_brief` to completion — up to core's
**uncapped** duration — still holding:

- one of only `KARMA_MAX_CONCURRENT_BUILDS` (=2) executor worker slots,
- a Postgres connection from the `maxconn=10` pool,
- an in-flight (uncapped) LLM call.

So the watchdog **bounds the API's *reported* status, not actual resource usage.**
This is why the capacity slot is reclaimed on the future's *real* resolution, not on
the reported timeout — otherwise a new build admitted into the "freed" slot would
contend with the orphan for the same 2 executor workers. Two genuinely-stuck builds
wedge the entire build subsystem until the process restarts.

### The real fix (recommended follow-up, not built here)

Durable cancellation requires **core-level timeouts**: add `timeout=` to
`call_structured` / `call_text` and a per-connection `statement_timeout` on Postgres.
That, not a route watchdog, actually reclaims a hung worker. Flagged in §8 as the
highest-value follow-up — see also the "no `stage`" note: because `PipelineState` has
no `stage` field, v1 progress is **coarse** (`queued`/`running`/terminal) only; the
per-node "Checking feasibility… / Selecting parts…" labels of design §6 require a
core `progress_fn` (design §10) and are **not achievable in v1**.

---

## 6. Result mapping (against the REAL shapes)

Mappers live in a new `api/builds_mappers.py` (or extend `api/mappers.py`), no
FastAPI imports, unit-testable in isolation like the intake mappers.

### Classification of the final `PipelineState` → terminal status

| Observed state | Status | Body |
|---|---|---|
| `build_card` present, `parts` non-empty | `succeeded` | `BuildCardDTO` + `VerdictDTO` (comfortable/tight) |
| `feasibility_verdict.verdict == "impossible"`, no `build_card` | `infeasible` | `VerdictDTO` (reason + suggested_adjustments) |
| `error_message` present, no verdict, no card (shouldn't occur via runner) | `cannot_proceed` | reason = `error_message` |
| `run_from_brief` raised | `failed` | error code by exception type |

### `VerdictDTO`

Surfaces `verdict`, `reason`, `binding_constraint`, `suggested_adjustments`.
**Drops `basis`** (`deterministic`/`llm_fallback`/`stub` is internal diagnostics).

### `BuildCardDTO` / `BuildPartDTO`

`BuildPartDTO`: `slot` (str — `ComponentSlot.value`), `product_id`, `name`, `brand`
(`str | None`), `price_inr` (int), `justification`. `BuildCardDTO`: `parts`,
`total_price_inr`, `summary`, `warnings`. `changed_slots` is **omitted in v1**
(refinement-only; always empty on a fresh build) — additive later when refinement
v2 lands.

### Empty / degraded-card detection — is design §7's check still accurate?

Design §7 row: *empty `build_card.parts` ⇒ probe `PostgresClient` ping ⇒
`failed`/`DEGRADED_DEPENDENCY`.* Against the real shape (`BuildCard.parts` is the
list), the field is correct, **but the check needs refining** because an empty
`parts` list has **two** causes:

1. **Infra degradation** — Postgres flapped mid-select, `get_parts_in_band`
   returned nothing. → post-run `PostgresClient().get_min_catalog_price(...)`/ping
   **fails** → `failed` + `DEGRADED_DEPENDENCY`, retryable.
2. **Genuine dead-end** — Postgres up, but every slot dead-ended on
   compatibility/budget (Node 3 populated `build_card.warnings`, `parts` empty). →
   ping **succeeds** → this is a real (degenerate) outcome, **not** a 5xx-class infra
   failure. Surface as `succeeded` with the non-empty `warnings` explaining the
   dead-ends (frontend shows them prominently).

So: **empty-or-partial `parts` ⇒ probe Postgres; ping-fail ⇒ `failed`/`DEGRADED_DEPENDENCY`;
ping-ok ⇒ classify by whether `warnings` explain it.** If `parts` empty **and**
`warnings` empty **and** ping ok (an unexpected state) → `failed`/`INTERNAL_ERROR`.

**Partial parts (e.g. 6 of 9 slots filled) is fuzzier than §7's "0 parts" row** — a
6/9 build with Postgres up is a real, usable, degraded build (`succeeded` +
warnings); the same shape from a Postgres flap that *recovered* mid-run is
infra-degraded and the post-run ping cannot distinguish them (it only reflects
health *now*). Flagged as an open question in §8.

### Neo4j-degraded warning — must NOT be silent (earlier project decision)

`select_build` computes `neo4j_available = Neo4jClient().ping()` once and, when the
graph is down, **degrades silently to Postgres-only selection** — it logs
(`logger.warning`) but does **not** append a "compatibility graph unavailable" notice
to `build_card.warnings` (that list only collects per-slot dead-end messages). The
final `PipelineState` carries **no** `neo4j_available` flag. So to honor the "not
silent" decision, `BuildService` performs its **own post-run** `Neo4jClient().ping()`
in `_run_and_store`; if `False`, it injects a synthetic warning into the record's
`warnings` (surfaced in `BuildCardDTO.warnings`):

> "Compatibility graph was unavailable; parts were selected on catalog data only —
> cross-compatibility and fitness checks were skipped."

Caveat: the post-run ping can disagree with during-run availability (Neo4j could
recover or drop between selection and probe) — a best-effort signal. The durable fix
is for `select_build` to record `neo4j_available` onto the card/state — flagged in §8
as a small core enhancement.

---

## 7. DTOs and routes

New DTOs in `api/dtos.py` (following the split-per-route convention already used
there), reusing the existing `ErrorBody` / `ErrorEnvelope`.

### Request / response DTOs

```python
class StartBuildRequest(BaseModel):
    session_id: str

class BuildAcceptedDTO(BaseModel):          # 202 body
    build_id: str
    status: Literal["queued"]
    poll_after_ms: int = 2000

class VerdictDTO(BaseModel):
    verdict: Literal["comfortable", "tight", "impossible"]
    reason: str
    binding_constraint: str | None = None
    suggested_adjustments: list[str] = []

class BuildPartDTO(BaseModel):
    slot: str
    product_id: str
    name: str
    brand: str | None = None
    price_inr: int
    justification: str

class BuildCardDTO(BaseModel):
    parts: list[BuildPartDTO]
    total_price_inr: int
    summary: str
    warnings: list[str] = []

class BuildStatusResponse(BaseModel):       # 200 poll body (discriminated by status)
    build_id: str
    status: Literal["queued","running","succeeded","infeasible","cannot_proceed","failed"]
    poll_after_ms: int | None = None        # present while non-terminal
    verdict: VerdictDTO | None = None        # succeeded + infeasible
    build: BuildCardDTO | None = None        # succeeded
    error: ErrorBody | None = None           # failed (code/message/retryable)
    reason: str | None = None                # cannot_proceed
```

### Routes (`api/routers/builds.py`, mounted like intake)

Mount-agnostic router (no prefix/auth of its own); wired in `create_app()` as
`app.include_router(builds.router, prefix="/api/v1", dependencies=[Depends(require_api_key)])`.
Every handler is `async def` (hard rule — a sync `def` handler would run in FastAPI's
worker-thread pool off the loop the registry lock depends on).

- **`POST /builds`** → `202` `BuildAcceptedDTO`.
  `build_id = await service.start_build(body.session_id)`; return `{build_id, "queued", 2000}`.
- **`GET /builds/{build_id}`** → `200` `BuildStatusResponse`.
  `record = await service.get_build_status(build_id)`; map record → discriminated body.
  Non-terminal → `status` + `poll_after_ms`. `succeeded` → `build` + `verdict`.
  `infeasible` → `verdict`. `failed` → `error`. `cannot_proceed` → `reason`.
  **Invariant:** `infeasible` / `cannot_proceed` / `failed` all return **HTTP 200** —
  the async job's outcome lives in the body, never in the HTTP status (design §3.7,
  invariant 3).

### Exception → HTTP mapping (new `BuildServiceError` family)

Follows `api/errors.py`'s `@app.exception_handler` pattern exactly — either extend
`register_exception_handlers` or add a parallel `register_build_exception_handlers`.

| Exception | HTTP | code | retryable |
|---|---|---|---|
| `SessionNotFoundError` | 404 | `SESSION_NOT_FOUND` | false |
| `BriefNotLockedError` | 409 | `BRIEF_NOT_LOCKED` | false |
| `BuildNotFoundError` | 404 | `BUILD_NOT_FOUND` | false |
| `BuildCapacityError` | 429 | `BUILD_CAPACITY` | true (+ `Retry-After`) |
| `BuildAlreadyActiveError` | 409 | `BUILD_ALREADY_ACTIVE` | false (+ `details.build_id`) |
| `BuildServiceError` (catch-all) | 500 | `INTERNAL_ERROR` | false |

These are **request-time** errors on `POST`/`GET` only. Job-runtime infra failures
(`DATABASE_UNAVAILABLE`, `LLM_UPSTREAM_ERROR`, `DEGRADED_DEPENDENCY`, `BUILD_TIMEOUT`)
are **not** HTTP errors — they are `error.code` inside a `200` poll body.

`BUILD_ALREADY_ACTIVE` is `retryable: false` deliberately: a blind re-`POST /builds`
for the same session would fail identically every time, so the response carries
`details.build_id` of the already-active build and the client should switch to polling
that `build_id` via `GET /builds/{build_id}` rather than retrying the request.

---

## 8. Open questions / risks (flag, not resolve)

1. **Uncapped LLM calls — biggest exposure (bigger than intake).** A build makes
   ~6–12 sequential, **uncapped** LLM calls (`agents/llm/client.py` has no
   `timeout=`); intake made 1–2. A single hung call wedges one of only **2** executor
   workers **indefinitely**, and the §5 watchdog can report `BUILD_TIMEOUT` but
   **cannot reclaim the slot**. Two stuck builds wedge the subsystem. **Durable fix =
   core-level timeouts** (`timeout=` on the LLM client + Postgres `statement_timeout`),
   not the route watchdog. Highest-value follow-up.

2. **Thread-safety of concurrent `run_from_brief` — investigated; NOT a new serious
   finding.** Verified: `run_from_brief` is safe to run concurrently for **different**
   briefs. `node3_selector` has only immutable module constants
   (`SELECTION_ORDER`, `_BAND_WIDEN_FACTOR`, …); `ThresholdCache` is per-call.
   `neo4j._driver` and the OpenAI `_client` are thread-safe singletons designed for
   concurrent use. The `costs._catalog_price_cache` dict and `software_specs`
   Postgres cache have benign check-then-set / idempotent-write races (same value
   written; no corruption). The **only** real coupling is the Postgres pool
   **`maxconn=10`**, shared with intake — and design §9 **already** caps builds at
   **2** for exactly this reason. Recommendations: (a) document the invariant
   `KARMA_MAX_CONCURRENT_BUILDS + intake concurrency ≤ pool headroom`; (b) add a
   pool-exhaustion → `503 DATABASE_UNAVAILABLE` mapping if `getconn` ever raises;
   (c) optionally pre-warm `_pool` / `_driver` / the price cache at startup to erase
   the benign lazy-init races. **Not** a data-race; a known, already-sized capacity
   constraint.

3. **No read-back path for locked briefs.** `PostgresClient` has
   `persist_locked_brief` but **no getter** — `locked_briefs` is write-only today. So
   SessionStore is the sole brief source at build time; a locked session that
   TTL-expires (24 h) before *Generate* makes the build impossible **despite** a
   durable brief in Postgres. Recommend adding `get_locked_brief(session_id |
   brief_id)`. Genuine gap.

4. **Retention window & durability.** 24 h assumed (design §5). Confirm vs an LRU cap;
   recommend enforcing **both** to bound growth. **Should job results persist to
   Postgres?** Builds are **regenerable** from the locked brief (modulo LLM
   non-determinism), so durability is lower-value than for the brief itself.
   Recommendation: **in-memory only in v1** — a process restart loses in-flight and
   completed jobs; the frontend re-`POST`s. This is a *deliberate* divergence from the
   locked-brief persistence decision. A `builds` Postgres table (`build_id`,
   `session_id`, `brief_id` FK, `status`, `result JSONB`, timestamps) can wait until
   audit/history or restart-survival is required.

5. **Partial-parts classification.** Design §7 specifies only "0 parts." A partial
   card (e.g. 6/9 slots) from Postgres-up is a real usable degraded build
   (`succeeded` + warnings); from a mid-run flap that recovered it is infra-degraded —
   and a post-run ping cannot distinguish them. Needs a policy decision.

6. **Neo4j degradation signal is best-effort.** The post-run `ping()` can disagree
   with during-run availability. Durable fix: `select_build` records
   `neo4j_available` onto the card/state (small core change) so the API reads a true
   flag instead of re-probing.

7. **`Retry-After` for 429 `BUILD_CAPACITY`** — proposed ~30 s (build-duration order);
   tunable, unconfirmed.

8. **Rate limiting** (design §11: build creates ~3/hour per key+IP) is **not
   implemented anywhere today** — no limiter exists in `api/`. Out of scope here;
   flagged as unbuilt.

9. **Coarse stage only.** `PipelineState` has no `stage` field, so v1 status is
   `queued`/`running`/terminal — the per-node progress labels of design §6 need a core
   `progress_fn` (design §10) and are not achievable in v1.

---

## Verification (how to test once implemented)

Per design §12's Phase-4 checklist, without needing Phase 3 wired end-to-end
(inject fixture briefs from `data/fixtures/` into a stubbed/seeded session):

1. **Happy path:** create a locked session (or seed one) → `POST /builds` → `202` →
   poll `GET /builds/{id}` every 2 s until `succeeded` → assert `BuildCardDTO` has
   9 parts, `total_price_inr` ≈ sum, `warnings` empty.
2. **Infeasible:** drive with an `edge_*` adversarial fixture whose budget forces
   `impossible` → poll resolves to `infeasible` with `VerdictDTO.reason` /
   `suggested_adjustments`, **HTTP 200**.
3. **Infra failure:** kill Postgres mid-run → poll resolves to `failed` with
   `DATABASE_UNAVAILABLE` **or** empty-card → `DEGRADED_DEPENDENCY`; **server stays
   up** (no unhandled 5xx crash).
4. **Neo4j degraded:** stop the Neo4j container → build still `succeeded`, but
   `BuildCardDTO.warnings` contains the compatibility-graph-unavailable notice
   (not silent).
5. **Capacity:** with `KARMA_MAX_CONCURRENT_BUILDS=2`, fire 3 concurrent
   `POST /builds` → the 3rd gets `429 BUILD_CAPACITY`, `retryable:true`, `Retry-After`.
6. **Timeout:** with a low `KARMA_BUILD_TIMEOUT_S` and a stubbed slow `run_from_brief`,
   confirm the job flips to `failed`/`BUILD_TIMEOUT` and the reported-vs-actual caveat
   holds (slot reclaimed only when the stub finally returns).
7. **Unit:** mappers (`BuildCard`/`FeasibilityVerdict` → DTOs, `basis` dropped,
   empty/partial-card classification) and the `JobRegistry` TTL/sweep — all without
   spinning up the app.
