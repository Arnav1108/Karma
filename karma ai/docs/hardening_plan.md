# Karma Advisor API — Phase 5 Hardening Plan

Planning document only — no implementation in this pass. Grounded in the real, current
code on branch api/phase4-builds, every file below read in full for this plan. Where
api_design.md (a design sketch, not a signature reference) disagrees with the code, the code
wins and the disagreement is called out — same discipline as the Phase 3/4 plans.

Phase 5 is the "Hardening" phase from api_design.md's own phased plan (§ "Phase 5 — Hardening":
"Rate limits, concurrency caps under load, TTL sweeps, structured logging (session_id/build_id
correlation), timeout tuning, resolve Q2–Q7"). This document designs each of those and collects
the Phase-5-scoped open items the Phase 3/4 plans deferred here.

---

## 0. Ground-truth verification (what actually exists today)

| Claim to check | Verified reality |
|---|---|
| api_design.md §9 rate-limit numbers | Present: intake turns ~20/min, session creates ~5/min, build creates ~3/hour, "in-process limiter, no external infra." Explicitly placeholders — §11 Q4: "the §9 numbers are placeholders to confirm." |
| api_design.md §5 background sweep | "Background sweep task evicts expired entries" is written as if decided, but nothing wires it (confirmed by intake_routes_plan.md §8 item 2 and by grep — no caller of sweep_expired). |
| sweep_expired() on both stores | Exists and unit-tested on InMemorySessionStore (session_store.py:164) and InMemoryJobRegistry (job_registry.py:234). Neither is called periodically — expiry is purely lazy (on get/peek/update). |
| config.py Settings fields | version, api_keys, cors_origins, max_concurrent_builds, build_timeout_s. No rate-limit fields, no TTL fields. max_concurrent_builds/build_timeout_s were added by Phase 4. |
| main.py lifespan | A FastAPI lifespan=_lifespan (asynccontextmanager) already exists (added Phase 4). It currently only build_executor.shutdown(wait=True) after yield. This is the hook a sweep task plugs into — no migration off on_event needed. |
| Singleton construction point | create_app() builds session_store = InMemorySessionStore() and InMemoryJobRegistry() with no constructor args → TTL/cap constants are hardcoded defaults. The store/registry are held privately inside the services (self._store, self._registry); only build_executor is on app.state. |
| middleware.py key access | _api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False) is a module-level shared instance; require_api_key depends on it plus Depends(get_settings). A limiter can Depends(_api_key_header) to get the key with zero header-parsing duplication. |
| Logging | Plain logging.getLogger(__name__) in every module; logger.exception(...) already used in errors.py, build_service.py, health.py. No basicConfig/dictConfig, no contextvars, no correlation IDs — the process relies on uvicorn's default root handler. |
| Postgres pool | ThreadedConnectionPool(minconn=1, maxconn=10) (postgres.py:23) — the shared ceiling both intake (persist_locked_brief) and builds draw from. getconn on an exhausted pool raises psycopg2.pool.PoolError. |

Extra ripple found (not in the task brief): routers/intake.py:36 imports the module
constants ASKING_TTL_SECONDS/LOCKED_TTL_SECONDS from session_store.py and uses them directly
to compute expires_at in three routes. Making TTLs env-configurable (§3) therefore must also
update how expires_at is computed, or the API would advertise the default 30 min / 24 h while the
store actually expires on the configured value — a silent contract drift. Called out in §3.

---

## 1. Background sweep task

### Mechanism

Extend the existing _lifespan in main.py. Before yield, spawn one long-lived
asyncio.Task running a sweep loop; after yield (shutdown), cancel and await it. This is the
idiomatic FastAPI pattern and fits the structure already present (Phase 4 already put
build_executor.shutdown in this same lifespan):

```python
@asynccontextmanager
async def _lifespan(app: FastAPI):
    sweeper = asyncio.create_task(_sweep_loop(app))
    try:
        yield
    finally:
        sweeper.cancel()
        try:
            await sweeper
        except asyncio.CancelledError:
            pass
        app.state.build_executor.shutdown(wait=True)

async def _sweep_loop(app: FastAPI):
    interval = app.state.settings.sweep_interval_s
    while True:
        await asyncio.sleep(interval)
        for name, store in (("sessions", app.state.session_store),
                            ("jobs", app.state.job_registry)):
            try:
                n = await store.sweep_expired()
                if n:
                    logger.info("sweep evicted %d expired %s", n, name)
            except Exception:
                logger.exception("sweep failed for %s", name)  # never let the loop die
```

Wiring prerequisite: create_app() must store app.state.session_store and
app.state.job_registry (today they live only inside the services). One line each — the services
keep their private refs; these are additional references for the sweeper. Both sweep_expired()
methods are already async and lock-guarded, so calling them from the loop is safe against live
requests.

One task, sequential sweeps (not two tasks): both sweeps are a single dict scan under a
short-held lock and cost microseconds; a second task adds no value.

### Interval — propose 300 s (5 min), env-tunable via KARMA_SWEEP_INTERVAL_S

Justification: the TTLs it backstops are 30 min (asking), 24 h (locked), 24 h (terminal jobs).
Expiry is already correct without the sweep — every read path lazily evicts — so the sweep's
only job is reclaiming memory from entries no one reads again (abandoned "asking" sessions;
never-re-polled terminal jobs). At 300 s an abandoned asking session lingers at most ~35 min
instead of exactly 30 — negligible. Cost is one locked dict scan per store every 5 min — trivial.
More frequent (e.g. 30 s) buys nothing and adds needless lock churn; rarer (e.g. 1 h) lets a burst
of abandoned sessions/jobs pile up for up to an hour. 300 s is the balance.

### Explicitly not folding the watchdog into the sweep

build_service_plan.md §5 floated "the sweeper also doubles as the watchdog" — flipping stuck
running jobs to failed/BUILD_TIMEOUT. Recommend against it in v1: _run_and_store's
asyncio.wait_for(..., timeout=build_timeout_s) already flips a timed-out build to failed while
its task is alive, and a registry-level watchdog can neither stop the orphaned worker thread nor
correctly decrement BuildService._active_builds (that counter lives on the service, not the
registry, and is reclaimed on the future's real resolution). Keep the sweep purely evictive; the
existing per-task wait_for is the watchdog. Flag as a possible future add only if tasks are ever
observed to vanish without resolving.

---

## 2. Rate limiting

### Approach — in-process sliding-window log, no external infra

Per api_design.md §9 ("in-process limiter … no external infra"): implement directly, do not
pull in Redis-backed slowapi/fastapi-limiter. For the tiny limits here (3/hour … 20/min) a
sliding-window log — a deque of hit timestamps per (key, category), evicting timestamps
older than the window on each check — is both simplest and exact (no fixed-window edge-burst where
a client gets 2× the limit across a boundary). Memory is a few timestamps per active key; pruned on
access and by the §1 sweep.

A single RateLimiter object holds dict[(bucket_key, category)] -> deque[float] guarded by an
asyncio.Lock, constructed once in create_app() and stored on app.state.rate_limiter. One
subtlety: the sliding-window log needs a monotonic clock; use time.monotonic() inside the limiter
(not datetime) so it's immune to wall-clock adjustment.

### Where it plugs in — a FastAPI dependency factory, keyed off the shared header

Mirror require_api_key's shape. A factory returns a dependency bound to a category; attach it
per-route so each category gets its own limit:

```python
# api/rate_limit.py
def rate_limit(category: str):
    async def _dep(
        request: Request,
        api_key: str | None = Depends(_api_key_header),   # reuse middleware's instance — no dup parsing
    ) -> None:
        limiter = request.app.state.rate_limiter
        bucket = api_key or f"ip:{request.client.host}"   # key by API key; IP only when auth disabled
        if not await limiter.allow(bucket, category):
            raise RateLimitError(retry_after=await limiter.retry_after(bucket, category))
    return _dep
```

Attach at the route level (categories differ per endpoint), e.g. on POST /intake/sessions
add dependencies=[Depends(rate_limit("session_create"))]; on .../answers
Depends(rate_limit("intake_turn")); on POST /builds Depends(rate_limit("build_create")).
Router-wide require_api_key stays as-is and runs alongside.

Import _api_key_header from api.middleware so the limiter and the auth check parse the header
exactly once, from one source. When auth is disabled (KARMA_API_KEYS unset), all callers fall
back to the IP bucket — noted as a limitation in §7.

### Three tiers — confirmed, lightly re-tuned

| Category | Endpoint | Limit | Cost / rationale |
|---|---|---|---|
| session_create | POST /intake/sessions | 5 / min / key (keep) | 1 phrase LLM call each; generous for one human starting builds. |
| intake_turn | POST …/answers | 20 / min / key (keep) | ≤2 LLM calls each; 20/min ≈ one per 3 s, faster than anyone types. |
| build_create | POST /builds | 3 / hour / key (keep) | The expensive one: 6–12 LLM calls + 30–120 s each. This is the money guard. |

Reads/poll/DELETE are left unlimited in v1. GET /builds/{id} polled every 2 s = 30/min is
legitimate and cheap; rate-limiting it would fight the frontend's own poll contract. GET
/intake/sessions/{id} and DELETE are similarly cheap. Flag: if abuse appears, add a generous
coarse default (e.g. 120/min/key) later — not needed now.

build_create interacts with BuildCapacityError. They are different 429s and must stay
distinct: BUILD_CAPACITY = "2 builds already running, retry in ~30 s" (transient, capacity);
RATE_LIMITED = "you've used your 3 builds this hour" (quota, retry after the window). A client
hitting the concurrency cap should back off seconds; one hitting the hourly quota should back off
minutes. Different codes, different Retry-After.

### 429 response shape — reuse the envelope, new RATE_LIMITED code

Reuse the existing ErrorEnvelope/ErrorBody and the @app.exception_handler pattern in
errors.py — do not invent a second error shape. Add a RateLimitError(retry_after: int)
exception (alongside the BuildServiceError/IntakeServiceError families) and register a handler:

```python
@app.exception_handler(RateLimitError)
async def _rate_limited(request, exc):
    return JSONResponse(
        status_code=429,
        content=_envelope("RATE_LIMITED",
                          "Rate limit exceeded. Please retry later.", True),
        headers={"Retry-After": str(exc.retry_after)},
    )
```

retry_after = seconds until the oldest in-window hit ages out (computed by the limiter). This
matches BuildCapacityError's existing Retry-After precedent (errors.py:45,165) and the
envelope's retryable: true convention. Starlette dispatches exceptions raised inside a dependency
through the same handler chain, so raising RateLimitError from the dependency lands in this
handler cleanly.

### Config (new Settings fields)

KARMA_RATE_LIMIT_ENABLED=true, KARMA_RL_SESSION_CREATE_PER_MIN=5,
KARMA_RL_INTAKE_TURN_PER_MIN=20, KARMA_RL_BUILD_CREATE_PER_HOUR=3. When disabled, the dependency
is a no-op (useful for load tests in §5 and local dev). The limiter reads these once at
construction.

---

## 3. Env-configurable TTLs

### New Settings fields

KARMA_SESSION_TTL_MIN=30, KARMA_LOCKED_SESSION_TTL_H=24 (both already documented in
api_design.md §9 but never wired), plus — reaching the same durability items — the job registry:
KARMA_BUILD_RESULT_TTL_H=24 and KARMA_MAX_JOB_RECORDS=500 (build_service_plan §8 item 4). Parse
to seconds in get_settings() (minutes×60, hours×3600) so the constructors receive seconds
directly.

### Wiring at the singleton-construction point

The constructors already accept these — no store/registry code changes needed, only
create_app() passes the values instead of relying on defaults:

```python
session_store = InMemorySessionStore(
    asking_ttl_seconds=settings.session_ttl_min * 60,
    locked_ttl_seconds=settings.locked_session_ttl_h * 3600,
)
job_registry = InMemoryJobRegistry(
    terminal_ttl_seconds=settings.build_result_ttl_h * 3600,
    max_records=settings.max_job_records,
)
```

Keep the existing module constants (ASKING_TTL_SECONDS = 1800, etc.) as the constructor
defaults so anything constructing a bare store (tests) is unaffected.

### The expires_at ripple (must fix, or the contract drifts)

routers/intake.py:36 currently does `from api.services.session_store import ASKING_TTL_SECONDS, LOCKED_TTL_SECONDS` and computes `expires_at = record.<ts> + timedelta(seconds=ASKING_TTL_SECONDS)`
in three places. Once the store's real TTL is env-driven, this constant no longer matches the
store's behavior. Fix: compute expires_at from the configured value, not the imported
constant. Cleanest option given the store already stores its effective TTL as
self._asking_ttl_seconds/self._locked_ttl_seconds: expose them (e.g. a
SessionStore.ttl_seconds_for(status) accessor or read the record's owning store) and have the
route/mapper use that. Simpler acceptable option: inject Depends(get_settings) into the three
routes and compute expires_at from settings.* directly. Either way, the imported-constant path
must go. This is the one non-obvious change the TTL wiring forces beyond create_app().

---

## 4. Structured logging (correlation IDs)

### Mechanism — contextvars + a logging filter, configured once

Idiomatic for FastAPI and right-sized for a two-person team (plain text with correlation fields —
not JSON logs, not an aggregation pipeline):

- api/logging_config.py: define request_id_var, session_id_var, build_id_var as
contextvars.ContextVar (default "-"); a ContextInjectingFilter(logging.Filter) that copies
their current values onto every LogRecord; a StreamHandler with the filter and a formatter like:
`%(asctime)s %(levelname)s %(name)s [req=%(request_id)s sid=%(session_id)s bid=%(build_id)s] %(message)s`.
Call configure_logging() at the top of create_app().
- A request middleware (@app.middleware("http")): generate a uuid4 per request, set
request_id_var, and reset it in a finally. This alone correlates every log line of one HTTP
request (including errors.py's logger.exception catch-alls, which then carry the id for free).
- session_id / build_id: these come from path params / request bodies, so set their
contextvars inside the route (or service) the moment they're known — e.g. submit_answer sets
session_id_var, start_build/_run_and_store set build_id_var. That threads a single user's
trail across intake → lock → build: intake turns and the lock share the session_id; the
build's own logs carry both its build_id and the originating session_id (already on
JobRecord.session_id).

### The worker-thread caveat (state it, don't over-engineer around it)

run_from_brief (and intake's intake_step) run via loop.run_in_executor(...) in a thread,
and contextvars are not automatically propagated into run_in_executor workers. So core-side
log lines emitted inside the pipeline thread won't carry the ids by default. Two honest choices:
1. Accept it (recommended default): correlate only the API-layer logs — route entry/exit,
service transitions, errors.py handlers, build_service status writes. That already answers
the actual ask ("trace one user's request across intake → lock → build without grepping
timestamps"), because those API-layer lines are where the tracing happens.
2. Optional, cheap upgrade for builds: wrap the executor submit in a copied context —
`ctx = contextvars.copy_context(); executor.submit(lambda: ctx.run(run_from_brief, brief))` — so
the worker thread inherits build_id_var. Worth doing for build_service specifically (build
correlation across the long worker run is the highest-value trace); optional for intake's
short turns. Recommend doing it for builds, skipping it for intake.

No new dependency; contextvars and logging are stdlib.

---

## 5. Real concurrency tuning under load

### What to validate

build_service_plan.md §8 item 2 sized KARMA_MAX_CONCURRENT_BUILDS=2 against the shared
maxconn=10 pool by inspection only, with the invariant "KARMA_MAX_CONCURRENT_BUILDS + intake
concurrency ≤ pool headroom." The real instantaneous demand is active DB operations, not active
builds (each _cursor() grabs one connection and returns it per query), so the headroom math needs
empirical confirmation.

### Approach — one standalone script, no framework

Add tests/manual/load_build_concurrency.py (per CLAUDE.md, manual smoke scripts live in
tests/manual/, not CI). Drives the real API over HTTP with httpx.AsyncClient (already a
transitive dep via OpenAI; asyncio.gather M of them.
2. Capacity check: fire K > max_concurrent concurrent POST /builds → assert exactly
max_concurrent go 202/running and the rest get 429 BUILD_CAPACITY (not RATE_LIMITED — run
with KARMA_RATE_LIMIT_ENABLED=false to isolate capacity from quota). Poll all to terminal.
3. Shared-pool stress: while those builds run, concurrently run J intake conversations that each
lock (each lock = one persist_locked_brief = one pooled connection) — this is the intake side
of the shared maxconn=10. Measure whether any request returns a pool-exhaustion error.
4. Sweep the ceiling: re-run step 2–3 with KARMA_MAX_CONCURRENT_BUILDS = 3, 4, 5 … until
pool-exhaustion errors appear, to find the empirical ceiling and confirm 2 is conservative.

### Make exhaustion observable first

Today an exhausted pool raises psycopg2.pool.PoolError from getconn, which in a build worker
lands as failed/INTERNAL_ERROR and in an intake turn as a bare 500 — indistinguishable from
other bugs. Recommend (small, and it makes the load test meaningful) mapping pool exhaustion to
503 DATABASE_UNAVAILABLE (request-time) / DEGRADED_DEPENDENCY (build worker), per
build_service_plan.md §8 item 2(b). Success criterion: at cap=2 with realistic intake
concurrency, zero pool-exhaustion errors and all builds reach succeeded; the ceiling sweep shows
the first exhaustion well above 2.

---

## 6. TurnInProgressError retry semantics — recommendation

Recommendation: add a short Retry-After header (matching BuildCapacityError's pattern), and
keep it otherwise documentation-only. Do NOT build server-side queueing.

Rationale. record.lock is held only for the duration of one intake_step (a few seconds of LLM),
and a concurrent duplicate is almost always a double-submit (double-click / impatient retry), not
genuine contention — the fail-fast design is deliberate (intake_service_plan.md §3: "fail fast,
don't queue"). So server-side enforcement (queue/retry) would be the wrong thing to build. But the
current 409 TURN_IN_PROGRESS, retryable: true with no Retry-After leaves the client guessing
how long to back off, while the sibling BuildCapacityError already ships a concrete
Retry-After: 30. Closing that inconsistency is nearly free: add Retry-After: 1 (one second — the
lock frees within a turn) to the _turn_in_progress handler in errors.py, optionally env-tunable
as KARMA_TURN_RETRY_AFTER_S. Document the client contract explicitly: on 409 TURN_IN_PROGRESS,
wait ~1 s and re-POST the same answer — safe because this path leaves session state unchanged
(intake_service.py mutates the store only after intake_step succeeds). This is a concrete header
+ documented contract — more than "documentation-only," far less than enforcement — the right
middle.

---

## 7. Open questions carried forward (genuinely unresolved after this plan)

1. Durable cancellation of hung LLM/DB calls (highest-value, out of scope here). The §1 sweep
and the per-task wait_for bound reported status, not resource usage — a wedged
run_from_brief still holds an executor slot + a pooled connection + an uncapped in-flight LLM
call until it returns on its own (build_service_plan.md §5 / §8 item 1). The real fix is
core-level timeouts: timeout= on call_structured/call_text in agents/llm/client.py
and a Postgres statement_timeout. Not a hardening-layer change; flagged as the top follow-up.
2. INTAKE_TURN_TIMEOUT (504) still unimplementable for the same root cause — no core timeout
hook to bound a hung intake LLM call (intake_routes_plan.md §8 item 5). A route-level
wait_for would only stop awaiting; the thread leaks. Ties to #1.
3. No read-back path for locked briefs. PostgresClient.persist_locked_brief has no getter
(locked_briefs is write-only) so SessionStore is the only build-time brief source. With the
now-configurable locked TTL (§3), a locked session that expires before the user clicks Generate
makes the build impossible despite a durable Postgres brief (build_service_plan.md §8 item 3).
Recommend a future get_locked_brief(session_id | brief_id) fallback.
4. Partial-parts classification ambiguity. A 6/9 build with Postgres up is a real degraded
succeeded; the same shape from a mid-run flap that recovered is infra-degraded, and the
post-run ping can't tell them apart (build_service_plan.md §8 item 5). Needs a policy decision,
not a hardening mechanism.
5. Neo4j degradation signal is best-effort. BuildService's post-run ping() can disagree with
during-run availability; the durable fix is core recording neo4j_available onto the
card/state (build_service_plan.md §8 item 6).
6. Job-result durability across restart — deliberately deferred. Recommendation stands:
in-memory only in v1 (builds are regenerable from the locked brief); a builds Postgres table
waits until audit/history/restart-survival is required (build_service_plan.md §8 item 4).
7. NEW single-process constraint introduced by this plan. The in-process rate limiter (§2) joins
the in-memory SessionStore/JobRegistry as state that is not shared across processes — so
running more than one uvicorn worker would split both session state and rate-limit counters
(each worker enforcing its own fraction of the limit). v1 is already single-process
(api_design.md §5/§9); this plan hardens that constraint rather than relaxing it. Must be
documented loudly: the limiter, the session store, and the job registry all move to Redis
together if/when horizontal scale is needed — they share the same seam.
8. Rate-limit audience / IP-fallback policy (product input needed). api_design.md §11 Q4:
who holds API keys in v1 (internal staff only?). Affects whether the auth-disabled IP-bucket
fallback (§2) is ever exercised in practice and whether the placeholder numbers hold for the real
caller set. [Already resolved earlier this session: v1 audience is controlled/internal, not
public — this answers the question.]

---

## Verification (how to test Phase 5 once implemented)

- Sweep: set a tiny KARMA_SWEEP_INTERVAL_S and short TTLs, create an asking session, wait past
TTL without touching it, assert it's gone from the store before any lazy read (inspect
len(store._sessions) in a test, or an admin/log line). Confirm the loop survives a forced
sweep_expired exception (monkeypatch to raise once → log line, loop continues).
- Rate limiting: with limits set low, fire N+1 POST /builds (or .../answers) within the
window → assert the (N+1)th returns 429 RATE_LIMITED, retryable: true, a sane Retry-After,
and — critically — that it's distinguishable from 429 BUILD_CAPACITY. Confirm a second API key
gets its own bucket.
- Env TTLs: start with KARMA_SESSION_TTL_MIN=1, assert both the store's real eviction and
the expires_at field in the create/answer responses reflect 1 min (guards the §3 ripple).
- Structured logging: drive one intake → lock → build end-to-end and grep the logs for the
build's sid= / bid= — assert the whole trail (intake turns, lock, build transitions, any
handler exception) is retrievable by those ids alone, no timestamp correlation.
- Concurrency: run tests/manual/load_build_concurrency.py (§5) — capacity 429s at cap,
zero pool-exhaustion at cap=2 under concurrent intake, and a demonstrable ceiling above 2.
- TurnInProgress: fire two concurrent .../answers on one session → loser gets 409
TURN_IN_PROGRESS with Retry-After; a re-POST after the header's delay succeeds.
- Two parallel scripted users end-to-end without collision (the api_design.md Phase-5 exit
check), and document restart behavior (in-flight sessions/jobs/limit-counters dropped —
expected).
