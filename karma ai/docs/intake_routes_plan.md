# Intake Routes — Implementation Plan (Phase 3)

**Planning document only — no implementation in this pass.** Grounded in the real, current code,
all read in full for this plan:
`karma ai/api/services/intake_service.py`, `karma ai/api/services/exceptions.py`,
`karma ai/api/services/session_store.py`, `karma ai/agents/nodes/node1_intake.py`,
`karma ai/agents/schemas/brief.py`, `karma ai/agents/schemas/slots.py`, `karma ai/api/main.py`,
`karma ai/api/middleware.py`, `karma ai/api/config.py`, `karma ai/api/routers/health.py`, and
`karma ai/docs/api_design.md` (§3.1–3.5, §6, §7, §9), plus its own follow-up plan
`karma ai/docs/intake_service_plan.md`. Where `api_design.md` disagrees with the real code, that
document's own §2 note applies — it is "intent, not accurate current signatures" — every
disagreement is called out rather than silently reconciled, exactly as `intake_service_plan.md`
already did for the service layer.

Current `karma ai/api/` contents at planning time: `main.py`, `config.py`, `middleware.py`,
`routers/health.py`, `services/{exceptions,session_store,intake_service}.py`. **No `errors.py`,
`dtos.py`, `mappers.py`, or `routers/intake.py` exist yet** — this plan designs all four.

---

## 1. Route-by-route contract

Base path `/api/v1` per `api_design.md` §3 (health stays unprefixed at root — confirmed by
`main.py`'s current `app.include_router(health.router)` with no `prefix=`). All five routes live in
a new `api/routers/intake.py`, mounted as:
```python
app.include_router(intake.router, prefix="/api/v1", dependencies=[Depends(require_api_key)])
```

### 1.1 `POST /api/v1/intake/sessions`
- **Calls:** `IntakeService.create_session(client_ref: str | None)`
- **Request body:** `{ "client_ref": string | null }` (both keys optional; `client_ref` accepted
  and echoed nowhere — the service itself documents it is not persisted onto
  `IntakeSessionState`, see `intake_service_plan.md` §1)
- **201 response:**
  ```json
  { "session_id": "<uuid>", "status": "asking",
    "question": QuestionDTO, "progress": ProgressDTO, "expires_at": "<iso8601>" }
  ```
  `question` is never `null` here — `create_session` always returns a question on success (the
  13-question sequence cannot be exhausted on a blank brief). `expires_at` = `record.created_at`
  (== `last_accessed_at` on a fresh record) `+ ASKING_TTL_SECONDS` (imported from
  `api.services.session_store`, currently 1800s / 30 min — see §8 for the env-wiring gap).
- **Errors:** `create_session` raises only `LlmUpstreamError` (the first-question phrase call
  failing) → `502 LLM_UPSTREAM_ERROR`. `api_design.md` §3.1 additionally lists `503
  SERVICE_UNAVAILABLE` — no code path in `IntakeService` currently produces this; it would only
  arise from a startup-level dependency check (e.g. OpenAI client construction failing), which is
  out of scope for the route handler itself. Kept as documented-but-currently-unreachable, not
  wired.

### 1.2 `POST /api/v1/intake/sessions/{session_id}/answers`
- **Calls:** `IntakeService.submit_answer(session_id, answer)`
- **Path param:** `session_id: str` (not validated as UUID at the route layer — `SessionStore`
  keys are plain strings; an unparseable id simply misses the dict lookup and surfaces as the same
  `404 SESSION_NOT_FOUND` as an expired one, so no separate format-validation branch is needed)
- **Request body:** `{ "answer": string }`, length 1–2000 chars (`api_design.md` §3.2) — enforced
  via a Pydantic `Field(min_length=1, max_length=2000)` on the request DTO, which FastAPI turns
  into a `422` automatically (see §2, `VALIDATION_ERROR` row)
- **200 response**, discriminated on `status` (mirrors `submit_answer`'s `(record, question,
  locked)` return):
  ```json
  // locked == False
  { "status": "asking", "question": QuestionDTO, "progress": ProgressDTO, "expires_at": "<iso8601>" }
  // locked == True
  { "status": "locked", "brief_summary": BriefSummaryDTO, "progress": ProgressDTO }
  ```
  `expires_at` on the locked branch is intentionally omitted from `api_design.md`'s sketch too —
  the review screen doesn't need a countdown on a 24h-TTL locked session; add it later if the
  frontend asks (mapper-only change).
  `progress` is derived from the **returned** `record.state`, not a stale pre-turn snapshot.
- **Errors:** `SessionNotFoundError` → `404`, `SessionAlreadyLockedError` → `409`,
  `TurnInProgressError` → `409`, `LlmUpstreamError` → `502`, plus `422 VALIDATION_ERROR` from body
  validation (never reaches the service). `api_design.md` §3.2 also lists `504
  INTAKE_TURN_TIMEOUT` — **not implementable yet**: neither the service nor `agents/llm/client.py`
  has a timeout hook (`intake_service_plan.md` §8 item 1). Flagged, not built — see §8 below.

### 1.3 `GET /api/v1/intake/sessions/{session_id}`
- **Calls:** `IntakeService.get_snapshot(session_id)` (uses `SessionStore.peek()` — non-refreshing,
  per `intake_service_plan.md` §6; this is the one route that must **not** slide the TTL)
- **200 response:**
  ```json
  { "status": "asking" | "locked",
    "question": QuestionDTO | null,
    "progress": ProgressDTO,
    "brief_summary": BriefSummaryDTO | null,
    "expires_at": "<iso8601>" }
  ```
  `question` is populated only when `status == "asking"`, reconstructed **without** calling
  `intake_begin` (that would spend a live phrase LLM call on a read-only endpoint, contradicting
  §3.3's own "sync, no LLM" contract). See §4 for exactly how — this is a real design decision,
  not a trivial 1:1 mapping, and is flagged again in §8 as worth a core-side follow-up.
  `brief_summary` is populated only when `status == "locked"`.
- **Errors:** `SessionNotFoundError` → `404`.

### 1.4 `POST /api/v1/intake/sessions/{session_id}/lock`
- **Calls:** `IntakeService.lock_early(session_id)`
- **Request body:** none
- **200 response:** `{ "status": "locked", "brief_summary": BriefSummaryDTO }`
- **Errors:** `BriefFloorNotMetError` → `409 BRIEF_FLOOR_NOT_MET` with `details.missing` (taken
  verbatim from `exc.missing`), `SessionAlreadyLockedError` → `409`, `SessionNotFoundError` →
  `404`. **Gap in `api_design.md` §3.4:** it lists only `BRIEF_FLOOR_NOT_MET`,
  `SESSION_ALREADY_LOCKED`, and `404` — it omits `TurnInProgressError`, which `lock_early` can and
  does raise (same lock-check pattern as `submit_answer`, per `intake_service_plan.md` §3). This
  plan adds `409 TURN_IN_PROGRESS` to the route's real error surface; the design doc should be
  corrected to match.

### 1.5 `DELETE /api/v1/intake/sessions/{session_id}`
- **Calls:** `IntakeService.abandon(session_id)`
- **204 response**, always — `abandon()` never raises (its docstring: idempotent delete, `True`
  only if the session existed, but the return value is discarded). No exception handling needed on
  this route at all; it cannot produce a `404`, unlike what a naive REST-purist reading of "DELETE
  on unknown resource" might suggest. This matches `api_design.md` §3.5 exactly.

---

## 2. Exception-to-HTTP mapping

**Strategy: FastAPI `@app.exception_handler(ExcType)` registered per exception type in a new
`api/errors.py`, called once from `create_app()` — not per-route `try/except`.**

Justification:
- All four mutating routes (§1.2–1.5, `get_snapshot` included since it still raises
  `SessionNotFoundError`) share the same 3–4 exception types. Per-route `try/except` would
  duplicate the same 4–5 `except` blocks four times with identical bodies — pure repetition with
  no route-specific handling logic anywhere.
- The error envelope shape is uniform across every route (`api_design.md` §3's `{"error": {...}}`
  contract) — centralizing the mapping in one place is the only way to guarantee that uniformity
  can't drift route-by-route.
- Starlette's exception dispatch (`ExceptionMiddleware`) looks up the **most specific registered
  handler by walking each raised exception's `__mro__`**, so registering both a subclass-specific
  handler (e.g. `SessionNotFoundError`) and the `IntakeServiceError` base as a catch-all is safe —
  a `SessionNotFoundError` instance matches its own handler before Starlette ever considers the
  base class's.
- Matches `api_design.md`'s own intended file layout (`api/errors.py`: "error envelope, exception
  handlers, code constants") — this plan is implementing exactly that file, not inventing a new
  pattern.

Concrete mapping for all six `IntakeServiceError` classes (5 concrete + the base, per
`api/services/exceptions.py`):

| Exception | HTTP status | `error.code` | `retryable` | `error.details` |
|---|---|---|---|---|
| `SessionNotFoundError` | `404` | `SESSION_NOT_FOUND` | `false` | — |
| `SessionAlreadyLockedError` | `409` | `SESSION_ALREADY_LOCKED` | `false` | — |
| `TurnInProgressError` | `409` | `TURN_IN_PROGRESS` | `true` | — |
| `BriefFloorNotMetError` | `409` | `BRIEF_FLOOR_NOT_MET` | `false` | `{"missing": exc.missing}` |
| `LlmUpstreamError` | `502` | `LLM_UPSTREAM_ERROR` | `true` | — (never serialize `exc.cause` — log server-side via `logger.exception`, never leak raw OpenAI SDK exception internals to the client) |
| `IntakeServiceError` (base; catch-all for any future subclass a handler forgets to add explicitly) | `500` | `INTERNAL_ERROR` | `false` | — |

Envelope body shape (all six), matching `api_design.md`'s contract exactly:
```json
{ "error": { "code": "SESSION_NOT_FOUND", "message": "<human-readable>", "retryable": false, "details": {} } }
```
`details` is omitted (not `{}`) when empty — only `BriefFloorNotMetError` populates it.

Two additional handlers registered alongside the six above, both **outside** the
`IntakeServiceError` family:
- FastAPI's built-in `RequestValidationError` (Pydantic body validation) → `422
  VALIDATION_ERROR`, `retryable: false` — FastAPI raises this itself before the route body ever
  runs (e.g. `answer` outside 1–2000 chars), so it needs its own handler override to match the
  envelope shape instead of FastAPI's default `{"detail": [...]}` body.
- A bare `Exception` catch-all → `500 INTERNAL_ERROR`, `retryable: false` — for anything neither a
  Pydantic validation error nor an `IntakeServiceError` (e.g. a genuine bug). Logged with
  `logger.exception` server-side; body never includes the traceback.

---

## 3. DTOs needed

New file `api/dtos.py`. All Pydantic `BaseModel`s — no domain objects (`UserBuildBrief`,
`IntakeSessionState`, `SessionRecord`) ever appear in a response body.

### `QuestionDTO`
```python
class QuestionDTO(BaseModel):
    question_id: str | None
    text: str
    kind: Literal["sequence", "clarification", "confirm_default"]
```
Field-for-field identical to `IntakeQuestion` (`node1_intake.py:813`) — see §4, this is the one
genuinely 1:1 mapper.

### `ProgressDTO`
```python
class ProgressDTO(BaseModel):
    answered: int
    total: int      # == len(QUESTION_SEQUENCE), currently 13
    floor_met: bool
```

### `BriefSummaryDTO`
**Confirms the task's flag: `api_design.md` §6's sketch covers 9 of the 13 `QUESTION_SEQUENCE`
sections and silently drops `peripherals`, `physical`, `longevity`, and `extras`.** Traced
section-by-section against `_SECTION_TO_DUMP_KEY` (`node1_intake.py:471`), the 13 question ids
map to brief sections as: `budget→budget`, `primary_use_case→purpose`, `software→software`,
`performance→performance`, `monitor→monitor`, **`peripherals→peripherals`**,
`storage→storage`, `operating_system→operating_system`, `existing→existing`,
**`physical→physical`**, **`longevity→longevity`**, **`extras→extras`**,
`hard_constraints→hard_constraints`. The corrected field list, one block per section:

```python
class SecondaryUseCaseDTO(BaseModel):
    use_case: str
    weight: Literal["low", "medium", "high"]

class SoftwareEntryDTO(BaseModel):
    name: str
    category: str
    frequency: str
    intensity: str

class PeripheralDTO(BaseModel):
    type: str
    requirements: str | None
    priority: Literal["must_have", "nice_to_have"]

class ReusePartDTO(BaseModel):
    slot: str          # ComponentSlot value
    identifier: str
    action: Literal["keep", "replace"]

class SpecificPartRequestDTO(BaseModel):
    slot: str
    requested: str

class BriefSummaryDTO(BaseModel):
    answered_fields: list[str]              # see provenance discussion below
    completeness: dict                       # {required_complete, optional_filled, optional_skipped} — pass Completeness.model_dump() through as-is, it's already a flat public shape

    budget: dict                             # {comfortable_min, comfortable_max, ceiling, scope, currency, notes}
    purpose: dict                            # {primary_use_case, sub_case, secondary_use_cases: [SecondaryUseCaseDTO]}
    software: list[SoftwareEntryDTO]

    performance: dict                        # {target_resolution, target_framerate, hdr_wanted, source}
    monitor: dict                            # {owned, specs: str | None, count, source}  -- "specs" is a derived human-readable line, see §4
    peripherals: list[PeripheralDTO]
    storage: dict                            # {capacity_gb, speed_tier, data_profile, source}
    operating_system: dict                   # {os, license, source}

    reuse_parts: list[ReusePartDTO]          # from existing.reuse_parts
    brand_prefs: dict                        # {cpu: str|None, gpu: str|None} from existing.ecosystem_prefs

    physical: dict                           # {form_factor_pref, noise_tolerance, placement, portability_need}
    longevity: dict                          # {reliability_priority, upgrade_path, timeline}
    extras: dict                             # {rgb_pref, visual_style, connectivity_needs, specific_part_requests: [SpecificPartRequestDTO]}

    hard_constraints: dict                   # {must_have: [str], must_not: [str]}  -- text values only, not full Constraint objects (id/source/locked_at are internal bookkeeping); rejected_parts omitted entirely — it's populated by refinement, never by intake, so it's always [] at this stage
```
(Nested sections are typed as `dict` above for brevity in this plan; the real implementation
should give each its own small `BaseModel` — `BudgetDTO`, `PurposeDTO`, etc. — for response-schema
validation and OpenAPI generation. Listed as dicts here only to keep this document's field list
scannable.)

**Provenance — defaulted vs. user-answered.** The brief itself uses **two different mechanisms**
for this, not one:
- Four sections (`performance`, `monitor`, `storage`, `operating_system`) carry a native
  `SourceFlag` field (`user_stated | inferred | default_applied | skipped_by_user`) — real domain
  data, free to pass through.
- The other nine sections (`budget`, `purpose`, `software`, `peripherals`, `existing`, `physical`,
  `longevity`, `extras`, `hard_constraints`) have **no** source flag at all; "was this answered"
  for them is tracked externally, via `IntakeSessionState.asked_so_far` (populated by
  `intake_step` from the answered question's id plus `newly_filled_sections()` for opportunistic
  fills — `node1_intake.py:925-929`).

**Decision: `answered_fields: list[str]`** — the subset of the 13 `QUESTION_SEQUENCE` ids present
in `state.asked_so_far` — as the single, uniform provenance signal across all 13 sections, **plus**
keep the native `.source` string on the four DTOs that already carry one (`performance.source`,
`monitor.source`, `storage.source`, `operating_system.source`) since it costs nothing extra to
pass through and is strictly richer than the flat list for those four.

Rejected alternative: a per-field wrapper (`{"value": ..., "provenance": ...}`) on every field.
Two problems: (1) 9 of 13 sections have no native provenance concept to wrap — inventing one for
them (e.g. deriving a fake `SourceFlag` from `asked_so_far` membership) adds a translation layer
that doesn't exist in the domain model, for no behavioral gain over just exposing the boolean via
`answered_fields`; (2) it would force **every** DTO field in the entire summary into a wrapper
shape for consistency, doubling the JSON size and complicating the frontend's read path, when the
review screen's actual need (per `api_design.md`'s framing: "you told us this" vs "we defaulted
this") is a **section-level** distinction, not a value-level one. `answered_fields` answers exactly
that question with zero new derivation logic — it's a direct filter over data `IntakeSessionState`
already maintains.

### Per-route request/response models
```python
class CreateSessionRequest(BaseModel):
    client_ref: str | None = None

class SubmitAnswerRequest(BaseModel):
    answer: str = Field(min_length=1, max_length=2000)

class SessionAskingResponse(BaseModel):
    session_id: str | None = None   # present on create; omitted (or echoed) on answers — see note below
    status: Literal["asking"]
    question: QuestionDTO
    progress: ProgressDTO
    expires_at: datetime

class SessionLockedResponse(BaseModel):
    status: Literal["locked"]
    brief_summary: BriefSummaryDTO
    progress: ProgressDTO | None = None   # present on POST .../answers, omitted on POST .../lock per §1.4

class SnapshotResponse(BaseModel):
    status: Literal["asking", "locked"]
    question: QuestionDTO | None
    progress: ProgressDTO
    brief_summary: BriefSummaryDTO | None
    expires_at: datetime

class ErrorEnvelope(BaseModel):
    error: ErrorBody

class ErrorBody(BaseModel):
    code: str
    message: str
    retryable: bool
    details: dict | None = None
```
Note on `session_id`: `POST /sessions`'s 201 body includes it per §1.1; `POST .../answers`'s 200
body does not need it (already in the URL) — `api_design.md` §3.2 doesn't include it either. Model
these as two distinct response schemas (`CreateSessionResponse` includes `session_id`;
`AnswerAskingResponse`/`AnswerLockedResponse` don't) rather than one shared `SessionAskingResponse`
with an optional field, since FastAPI's `response_model` generates cleaner OpenAPI schemas from
route-specific models than from one model with conditionally-populated fields. (Simplified to one
model above for readability; the real `dtos.py` should split it.)

---

## 4. Mappers

New file `api/mappers.py`. Five functions, each taking real domain objects and returning DTOs —
no FastAPI imports here, keeping it unit-testable without spinning up the app.

- **`map_question(q: IntakeQuestion) -> QuestionDTO`** — genuinely 1:1; `QuestionDTO(**q.model_dump())`
  or equivalent. The only trivial mapper in this set.

- **`map_progress(state: IntakeSessionState, brief: UserBuildBrief) -> ProgressDTO`**
  ```python
  def map_progress(state, brief):
      return ProgressDTO(
          answered=len(set(state.asked_so_far)),   # every entry in asked_so_far is one of the 13 ids by construction
          total=len(QUESTION_SEQUENCE),             # imported from node1_intake — currently 13
          floor_met=floor_met(brief),                # pure, no IO — safe to call inline on the event loop
      )
  ```

- **`map_brief_summary(brief: UserBuildBrief, asked_so_far: list[str]) -> BriefSummaryDTO`** —
  the real work. Sketch (not full code):
  1. `answered_fields = [qid for qid in (q.id for q in QUESTION_SEQUENCE) if qid in set(asked_so_far)]`
     — preserves `QUESTION_SEQUENCE` order rather than `asked_so_far`'s insertion order, so the
     frontend can render a stable checklist.
  2. `budget`, `purpose`, `storage`, `operating_system`, `physical`, `longevity` — near-direct
     `model_dump()` of the corresponding `brief.<section>`, dropping any fields the DTO doesn't
     declare (Pydantic `model_dump(include={...})` or just constructing the DTO explicitly field
     by field — explicit is safer against accidentally leaking a future internal field).
  3. `software` / `peripherals` — direct list comprehension, one DTO per entry, no filtering (an
     empty list renders as "you haven't told us yet" on the frontend, which needs no special
     provenance handling since these are list-typed, not sentinel-typed).
  4. `performance` / `monitor` — same near-direct mapping, but include the native `.source` field
     verbatim (see §3's provenance discussion).
  5. `monitor.specs` — a **derived** human-readable line, not a direct field:
     ```python
     if brief.monitor.owned == "yes" and brief.monitor.owned_specs:
         s = brief.monitor.owned_specs
         specs = f"{s.resolution} @ {s.refresh_hz}Hz" + (" HDR" if s.hdr else "")
     elif brief.monitor.target_specs:
         t = brief.monitor.target_specs
         specs = f"{t.resolution} @ {t.refresh_hz}Hz" + (" HDR" if t.hdr else "")
     else:
         specs = None
     ```
  6. `reuse_parts` = `brief.existing.reuse_parts` mapped 1:1; `brand_prefs` = `{"cpu":
     brief.existing.ecosystem_prefs.cpu_brand_pref, "gpu":
     brief.existing.ecosystem_prefs.gpu_brand_pref}` — both pulled out of the single `existing`
     section into two top-level DTO fields, matching `api_design.md` §6's flattening (existing has
     no dedicated `QUESTION_SEQUENCE` counterpart of its own beyond the `"existing"` id, so nothing
     is lost by flattening its two sub-concerns).
  7. `extras` — direct mapping; `specific_part_requests` list comprehension.
  8. `hard_constraints` — **projects down**, not a direct dump: `must_have = [c.value for c in
     brief.hard_constraints.must_have]`, `must_not = [c.value for c in
     brief.hard_constraints.must_not]` (dropping `id`/`source`/`locked_at` — internal bookkeeping
     the review screen doesn't need); `rejected_parts` dropped entirely (always `[]` at intake
     time — populated later during refinement, out of this endpoint's scope).
  9. `completeness = brief.completeness.model_dump()` — already a flat, public-shaped object,
     passed through unchanged.

- **`map_snapshot_question(state: IntakeSessionState) -> QuestionDTO | None`** — **distinct from
  `map_question`**, used only by `GET /sessions/{id}` (§1.3). `get_snapshot()` returns a bare
  `SessionRecord` — no `IntakeQuestion` object, because no turn ran. Reconstructs one from
  already-persisted fields, with **no LLM call**:
  ```python
  def map_snapshot_question(state: IntakeSessionState) -> QuestionDTO | None:
      if state.brief.status == "locked" or state.current_question_id is None and not state.brief.open_questions:
          return None
      text = state.history[-1]["content"] if state.history else ""   # last assistant turn == the pending question, already phrased
      if state.brief.open_questions:
          oq = state.brief.open_questions[0]
          attempts = state.open_question_attempts.get(oq, 0)
          kind = "confirm_default" if attempts == 1 else "clarification"
      else:
          kind = "sequence"
      return QuestionDTO(question_id=state.current_question_id, text=text, kind=kind)
  ```
  This duplicates the *shape* of `intake_begin`'s branch condition (`node1_intake.py:862-876`)
  without calling it — flagged again in §8 as worth a core-side fix rather than a permanent
  mapper-side duplication.

- **`map_error(exc: IntakeServiceError) -> tuple[int, ErrorBody]`** — the table in §2, expressed
  as one function (or a small dispatch dict keyed by `type(exc)`) that both exception handlers
  (concrete + base-class catch-all) call into, so the status/code/retryable/details tuple lives in
  exactly one place.

---

## 5. Dependency injection

**Choice: a module-level singleton created once in `create_app()`, stored on `app.state`, fetched
per-request via a `Depends`-based accessor.**

```python
# api/main.py
def create_app() -> FastAPI:
    ...
    app.state.intake_service = IntakeService(InMemorySessionStore())
    app.include_router(intake.router, prefix="/api/v1", dependencies=[Depends(require_api_key)])
    return app

# api/routers/intake.py (or a shared api/deps.py)
def get_intake_service(request: Request) -> IntakeService:
    return request.app.state.intake_service

@router.post("/intake/sessions")
async def create_session(
    body: CreateSessionRequest,
    service: IntakeService = Depends(get_intake_service),
):
    ...
```

**Justification, confirmed explicitly per the task's instruction:** `SessionStore` (in-memory,
per `api/services/session_store.py`) **must** be a singleton across requests. A fresh
`InMemorySessionStore()` per request would mean every request sees an empty `_sessions` dict — the
very first `GET`/`POST .../answers` against a session created by a *previous* request would always
404, because the dict holding that session would no longer exist. This isn't a performance
optimization, it's correctness: `IntakeService` wraps exactly one `SessionStore` instance
(`intake_service.py:38`), so the API layer must construct exactly one `IntakeService` for the
process lifetime and hand every route the same instance.

`app.state` (over a bare module-level global in `routers/intake.py`) is preferred because:
- It's constructed inside `create_app()`, alongside `Settings` and the CORS/router wiring already
  there — one place that assembles the whole app, matching the existing factory pattern.
- Tests can build a `FastAPI` app via `create_app()` and then swap `app.state.intake_service` for a
  fake/mock before wrapping it in a `TestClient`, without needing `unittest.mock.patch` against a
  module-level name. (`tests/intake_service_fakes.py` already exists for service-level fakes —
  this composes with it at the route-test layer.)
- Matches the existing `Depends(get_settings)` pattern already used in `middleware.py` — one more
  `Depends(get_intake_service)` reads consistently alongside it, rather than introducing a second
  DI idiom.

---

## 6. Auth wiring

Confirmed against `main.py`'s own commented-out example (`main.py:22-25`) and `middleware.py`'s
existing `require_api_key`:
```python
from fastapi import Depends
from api.middleware import require_api_key
from api.routers import intake

app.include_router(intake.router, prefix="/api/v1", dependencies=[Depends(require_api_key)])
```
`require_api_key` already handles the "no `KARMA_API_KEYS` configured → auth disabled, warn once"
fallback (`middleware.py:19-27`) — no route-level change needed, it composes as-is.
`/healthz`/`/readyz` stay unauthenticated (`main.py:20` comment: "never gated") and outside the
`/api/v1` prefix, both unchanged by this plan.

---

## 7. Async execution discipline

**Hard rule: every intake route handler is `async def`, and calls `await
service.<method>(...)` directly — never `def` (sync).**

This isn't just an idiom preference; a `def` route handler is real breakage risk here. FastAPI
runs sync `def` route handlers in a threadpool (via `run_in_threadpool`), which means the handler
body would execute on a **worker thread**, not the event loop `IntakeService` was designed
against. `intake_service_plan.md` §3 is explicit that the per-session `record.lock.locked()` →
`async with record.lock:` fail-fast pattern is safe *only* because asyncio is single-threaded and
cooperative — "nothing in this design depends on the pre-check for correctness, only for UX," but
that correctness argument assumes every coroutine touching `record.lock` runs on the **same** event
loop. A sync route handler calling into async service code from a worker thread would need to
spin up its own event loop (or use `asyncio.run_coroutine_threadsafe`), at which point two
concurrent requests against the same session could genuinely be evaluating `record.lock.locked()`
from two different loops/threads with no shared cooperative-scheduling guarantee — reintroducing
exactly the race `intake_service_plan.md` §3 proves cannot happen under a single event loop.
Additionally, `IntakeService`'s own `run_in_executor` calls (`intake_service.py:49`, `:72`) assume
they're being awaited from a running loop (`asyncio.get_running_loop()` raises outside one) — a
sync handler wrapping an async call awkwardly would risk exactly this.

Concretely: `async def create_session(...)`, `async def submit_answer(...)`, `async def
get_snapshot(...)`, `async def lock_early(...)`, `async def abandon_session(...)` — all five,
no exceptions.

---

## 8. Open questions / risks to flag, not resolve

1. **Session TTL is not env-configurable yet, despite `api_design.md` §9 documenting
   `KARMA_SESSION_TTL_MIN` / `KARMA_LOCKED_SESSION_TTL_H`.** `api/config.py`'s `Settings` dataclass
   has no such fields — `session_store.py`'s `ASKING_TTL_SECONDS = 1800` /
   `LOCKED_TTL_SECONDS = 86400` are hardcoded module constants, only overridable by passing
   constructor args to `InMemorySessionStore()` directly (which §5's DI wiring does not currently
   do). Decide whether Phase 3 wires these through `Settings` before shipping, or defers to a
   follow-up — the route contracts in §1 don't change either way, only what values populate
   `expires_at`.

2. **No background TTL sweep is wired anywhere.** `SessionStore.sweep_expired()` exists and is
   tested (per `tests/test_session_store.py`), but nothing calls it periodically — expiry is
   currently purely lazy (checked on `get`/`peek`/`update`, per `session_store.py:119-158`). An
   abandoned "asking" session with no one ever polling it sits in `_sessions` until process
   restart. `api_design.md` §5 says "Background sweep task evicts expired entries" as if already
   decided — it isn't wired. Decide whether Phase 3 adds a FastAPI lifespan/startup background
   task calling `sweep_expired()` on an interval, or whether this is explicitly Phase 5
   ("Hardening") scope given `api_design.md`'s own phased plan lists TTL sweeps there.

3. **`GET /sessions/{id}`'s reconstructed `QuestionDTO` duplicates private branch logic from
   `intake_begin` (§4).** It works correctly today (nothing about `intake_begin`'s question-kind
   branch has changed since the question was originally served, because no turn has run in
   between), but it is a second, hand-maintained copy of that branching logic living in
   `api/mappers.py` instead of `node1_intake.py`. If `intake_begin`'s branching ever grows a new
   case, the mapper silently drifts out of sync with no test forcing the two into agreement. The
   clean fix is a small core-side follow-up: add a `last_question: IntakeQuestion | None` field to
   `IntakeSessionState`, set it wherever `intake_begin` currently sets `current_question_id`, and
   let the snapshot mapper just read it back verbatim. Not attempted here — this plan works within
   the current `IntakeSessionState` fields only, per the task's ground-truth-only instruction.

4. **Locked-brief Postgres persistence: DECIDED, not open.** `lock_early` and the
   auto-lock-on-sequence-exhaustion branch inside `submit_answer` (§1.2, §1.4) **both**
   synchronously write the newly-locked `UserBuildBrief` to Postgres, as part of the same request
   that flips `SessionRecord.status` to `"locked"` — **not** deferred to `POST /builds` time. This
   resolves `api_design.md` §5's own open item 2 ("Durability") for the locked-brief case
   specifically: the brief must survive a process restart the moment it's locked, not merely long
   enough to be read once by a build job, since the in-memory `SessionStore` (even at its 24h
   locked TTL) offers no such guarantee on its own.

   **Framing, chosen deliberately given §1's existing route contracts:** §1.2 and §1.4 specify only
   that the route calls `IntakeService.submit_answer`/`lock_early` and then handles the two
   outcomes those methods already expose — a returned `SessionRecord` on success, or a raised
   `IntakeServiceError` subclass on failure. Neither route inspects *how* the service produces that
   result. That means this decision can be implemented as a **second pass entirely inside
   `IntakeService`** (its `lock_early`/`submit_answer` bodies gain a Postgres write before
   returning) **without changing any of §1's path/method/request/response shapes** — the route
   contracts specified there do not need revisiting once this lands. The one thing this *will*
   change, later, is §2's exception table: a synchronous Postgres write that fails mid-lock is a
   new failure mode the current five-exception taxonomy has no class for, so that write path will
   need its own new `IntakeServiceError` subclass (e.g. `BriefPersistenceError`) and a
   corresponding new row in §2 — not designed here, and correctly out of scope for a route-contract
   document.

   **Concrete follow-up:** this requires a new Postgres table and write path (schema, transaction/
   write-timing semantics inside `lock_early`/`submit_answer`, and the new failure-mode exception
   noted above) that is not yet designed anywhere in the reviewed code. That is the next planning
   task after this one — scoped to `IntakeService`'s internals and a new persistence design doc,
   not to the routes, which stay exactly as specified in §1.

5. **`INTAKE_TURN_TIMEOUT` (504) has no implementation path yet.** Per
   `intake_service_plan.md` §8 item 1: neither `call_structured` nor `call_text` in
   `agents/llm/client.py` accept a `timeout=`, so there is nothing in core to bound a hung LLM
   call. A route-level `asyncio.wait_for(...)` around the `await service.submit_answer(...)` call
   could impose a wall-clock bound at the HTTP layer, but (as that plan notes) it can only stop
   *awaiting* — the underlying blocking OpenAI call keeps running in its threadpool slot
   regardless, so a canceled-at-the-route-layer turn still consumes a worker thread until the SDK
   times out on its own. Decide whether Phase 3 adds this route-level `wait_for` as a
   best-effort UX improvement despite the leak, or whether `504 INTAKE_TURN_TIMEOUT` stays
   unimplemented until core gets a real `timeout=` parameter (the "correct fix" per that plan).

6. **Rate limiting is entirely unbuilt.** `api_design.md` §9 lists concrete numbers (intake turns
   ~20/min, session creates ~5/min) but `api/config.py` has no rate-limit settings and no
   middleware exists. Out of scope for this route-contract plan (§1's routes are correct with or
   without it), but flagged since `api_design.md`'s own phased plan defers it to Phase 5 too — the
   route handlers designed here have no rate-limit awareness built in, by design, for now.

7. **`TurnInProgressError`'s retry contract is UX-only, not enforced.** The route returns `409
   TURN_IN_PROGRESS, retryable: true` — nothing server-side queues or retries the request; the
   client is expected to back off and re-POST. Worth confirming the frontend team's retry/backoff
   behavior lines up with this before shipping (`api_design.md` §7 already documents `retryable:
   true` for this code, so the contract itself isn't new — just flagging it needs a client-side
   implementation to be meaningful).

8. **`session_id` path-param format is unvalidated.** §1.2 notes this is fine functionally (an
   unparseable id just 404s), but it means a malformed id (e.g. accidental SQL-injection-shaped
   garbage from a hostile client) reaches `SessionStore.get()`'s dict lookup as a raw string with
   no format check first. Not a security issue today (dict `.get()` on an arbitrary string is
   safe), but worth a UUID-format validator on the path param if/when `SessionStore` grows a
   backend where malformed keys aren't free to look up (e.g. a future Redis/Postgres store per
   `api_design.md` §5's stated upgrade path).
