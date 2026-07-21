# Frontend Contract Package (Phase 6) — Plan

**Status:** planning. Depends on Phases 3+4 (done). Nothing here changes pipeline
behavior; every code change named below is an *additive* API-layer follow-up, listed so
the implementer knows the blast radius. This document is the frozen reference the
Next.js team builds against.

Audience: the Karma backend implementer (sections 1–3, 5, 7) **and** the Next.js
frontend developer (sections 4, 6, plus the error catalog in 5). Section 6 is written to
be handed over as-is.

---

## 1. OpenAPI export mechanism

FastAPI already serves a live, auto-generated spec at **`GET /openapi.json`** (OpenAPI
3.1) and renders it at `/docs` (Swagger UI) and `/redoc`. So "export the spec" is not
"build a generator" — it is closing the gaps that make the auto-generated spec
incomplete, plus shipping a snapshot the frontend can consume without a running server.

**What is actually needed beyond "it already exists":**

1. **A committed static snapshot.** Add `karma ai/api/contract/openapi.json`, generated
   from `app.openapi()` by a tiny script (e.g. `scripts/dump_openapi.py`) or a test in
   "regen" mode. The frontend team consumes this file directly (TypeScript codegen,
   Postman import, mock servers) without needing Postgres/Neo4j/an API key up. A CI
   test (section 7) regenerates and diffs it so it can never go stale.

2. **Close the `response_model` gap.** Every route already declares `response_model`
   **except `POST /intake/sessions/{id}/answers`** (`api/routers/intake.py`), which
   relies solely on the union return annotation `AnswerAskingResponse |
   AnswerLockedResponse`. FastAPI *does* infer an `anyOf` schema from that annotation, so
   the spec is not empty — but making it explicit (`response_model=AnswerAskingResponse |
   AnswerLockedResponse`) documents intent and guards against a future refactor dropping
   the annotation. Health routes (`/healthz`, `/readyz`) also have no `response_model`
   (plain dict / `JSONResponse`); low priority, but a 2-field model each makes `/readyz`'s
   `degraded` contract explicit.

3. **Document error responses per route** — the biggest gap. FastAPI's automatic
   generation documents only success responses (200/201/202) plus an auto-422 for routes
   with a body/params. It has **no knowledge of the custom exception handlers** in
   `api/errors.py`, so the generated spec advertises none of 401/404/409/429/502/503/500.
   Fix by adding a `responses={404: {...}, 409: {...}, ...}` map (referencing
   `ErrorEnvelope`) to each route decorator, or a shared responses dict per router. The
   canonical source for what each route can raise is the error catalog in section 5.
   **The per-route `responses={}` maps must NOT include a `504` entry on any route: `504`
   is designed but is not actually emitted by any code path today (see section 8 item 5),
   so documenting it would misrepresent the live contract.**

4. **Tag/group routes for a readable `/docs`.** The routers carry path prefixes
   (`/intake`, `/builds`) but no OpenAPI `tags`, so Swagger groups everything under
   "default". Add `tags=["intake"]`, `tags=["builds"]`, `tags=["health"]` at
   `include_router` / `APIRouter` level.

5. **App-level metadata.** Pass `description`, `version=get_settings().version`, and a
   `contact` into `FastAPI(...)` so the exported spec is self-describing.

All five are additive API-layer edits (decorators, `FastAPI(...)` kwargs, one new
script/test). None touch pipeline code. **Not built in this planning task.**

---

## 2. `/docs`, `/redoc`, `/openapi.json` access policy

**Current behavior (verified):** these three endpoints are registered by the `FastAPI()`
constructor at their defaults and are **not** behind `require_api_key` — that dependency
is attached only to the `intake` and `builds` routers at mount time (`api/main.py:150-151`),
not globally. So a frontend developer with **no API key can already open `/docs` and fetch
`/openapi.json`** against a running server today. `/healthz` and `/readyz` are likewise
ungated (intentionally — they are liveness/readiness probes).

**Recommendation for controlled/internal v1: leave the docs endpoints ungated, and lean
on the committed static snapshot as the primary artifact.**

Rationale:
- The v1 audience is internal/controlled (confirmed this session; see
  `hardening_plan.md` §7 item 8). The API surface is not itself a secret to that
  audience, and open docs are the single biggest DX win for the frontend team.
- The committed `openapi.json` snapshot (section 1) means a dev never needs the running
  server or a key just to see the contract — so gating the live docs costs us little
  either way.
- Gating `/openapi.json` behind `require_api_key` would also break Swagger UI's own
  in-browser "try it out" and any codegen pointed at the live URL, for no real
  confidentiality gain at this audience.

**Revisit before public launch:** when real per-user auth lands (the launch blocker in
section 6), decide then whether to gate or disable `/docs` in production. That is a
one-line change (`FastAPI(docs_url=None, redoc_url=None, openapi_url=None)` or wrapping
them in the auth dependency) and is explicitly deferred, not decided now.

---

## 3. Freezing the DTO contract

"Freeze" means two independent guarantees, and we want **both**:

**(a) Versioning convention (already half-real).** Routes are mounted under **`/api/v1`**
today (`api/main.py:150-151`). Formalize the rule in this doc: **all v1 request/response
DTOs are stable within `/api/v1`; any breaking shape change ships under a new `/api/v2`
prefix, never by mutating a v1 DTO in place.** Non-breaking additions (a new optional
field with a default) are allowed within v1. This is a written convention, not code.

**(b) A snapshot test that fails on unintended shape change** — the real protection
against an accidental frontend-breaking refactor. Two equivalent options; recommend the
first:
- **Committed `openapi.json` snapshot + diff test** (section 7). Because the spec is
  generated *from* the DTOs, any field rename/removal/type change shows up as a spec diff
  and fails CI. One artifact serves both "export" and "freeze". Regeneration is a
  deliberate, reviewable commit.
- *(Alternative / complement)* Per-DTO `model_json_schema()` snapshots committed under
  `api/contract/schemas/`. Finer-grained blame, but redundant with the openapi diff for
  v1. Skip unless per-DTO granularity is wanted later.

**Explicitly call out the untyped-`dict` fields** as a known freeze limitation:
`BriefSummaryDTO`'s `budget`/`purpose`/`performance`/`monitor`/`storage`/`operating_system`/
`brand_prefs`/`physical`/`longevity`/`extras`/`hard_constraints` and `completeness` are
typed `dict` (see `api/dtos.py`), so their inner shape is **not** captured by the OpenAPI
schema or the snapshot test — a change to what `map_brief_summary` puts inside them will
**not** trip the freeze. Until these are promoted to typed sub-models, the **example
fixtures (section 4) are the authoritative description of those inner shapes**, and the
round-trip test (section 7) only proves the outer envelope parses, not the dict contents.
Promoting them to typed models is a recommended, non-breaking follow-up (additive typing;
the JSON on the wire is unchanged).

---

## 4. Example fixtures per screen

Per `api_design.md` §8's four screens. Goal: a frontend dev builds every screen with
**zero live backend** by importing these JSON files into mocks/Storybook.

**Location:** `karma ai/api/contract/fixtures/`, one subdirectory per screen. Keeping them
under `api/` (next to the DTOs) lets the round-trip test (section 7) import both without
path gymnastics.

**How generated (not hand-written):** a small script constructs real DTO instances and
emits `model_dump_json(indent=2, exclude_none=True)`, so every fixture is parse-guaranteed
by construction. Seed the DTO instances from existing real data where possible —
`data/fixtures/*.json` briefs and `tests/e2e/intake_script.py`'s answer script for the
conversation — so the examples read like genuine sessions, not lorem ipsum. The
round-trip test then re-loads each file through the DTO as a regression guard.

**Concrete fixture set:**

| Screen | File | DTO | Notes |
|---|---|---|---|
| Chat | `chat/create_session.response.json` | `CreateSessionResponse` (201) | first question, `status:"asking"`, `progress`, `expires_at` |
| Chat | `chat/submit_answer.request.json` | `SubmitAnswerRequest` | `{ "answer": "..." }` |
| Chat | `chat/answer_asking.response.json` | `AnswerAskingResponse` | next `kind:"sequence"` question |
| Chat | `chat/answer_clarification.response.json` | `AnswerAskingResponse` | `kind:"clarification"` |
| Chat | `chat/answer_confirm_default.response.json` | `AnswerAskingResponse` | `kind:"confirm_default"` (yes/no chips) |
| Chat | `chat/conversation.transcript.json` | ordered list of `{request,response}` | full floor-met→lock transcript, generated from `intake_script.py` |
| Review | `review/answer_locked.response.json` | `AnswerLockedResponse` | auto-lock on sequence exhaustion; full `BriefSummaryDTO` |
| Review | `review/lock.response.json` | `LockResponse` | "Finish early" path; `BriefSummaryDTO` |
| Review | `review/snapshot_asking.response.json` / `snapshot_locked.response.json` | `SnapshotResponse` | both branches of `GET /sessions/{id}` |
| Progress | `progress/build_accepted.response.json` | `BuildAcceptedDTO` (202) | `status:"queued"`, `poll_after_ms` |
| Progress | `progress/build_status_queued.json` / `build_status_running.json` | `BuildStatusResponse` | in-flight polls |
| Result | `result/build_status_succeeded.json` | `BuildStatusResponse` | full `BuildCardDTO` + `VerdictDTO`, non-empty `warnings` |
| Result | `result/build_status_infeasible.json` | `BuildStatusResponse` | `VerdictDTO.reason` + `suggested_adjustments`, no `build` |
| Result | `result/build_status_cannot_proceed.json` | `BuildStatusResponse` | `reason` set, no verdict/build |
| Result | `result/build_status_failed.json` | `BuildStatusResponse` | in-band `error` body (see §5) |
| Errors | `errors/*.json` | `ErrorEnvelope` | one per transport error code in §5 |

Fixtures live in the repo (not inline in this doc) so they are directly importable and
kept honest by the round-trip test. This doc references them by path; it does not paste
their bodies.

---

## 5. Error catalog documentation

FastAPI does not auto-document custom exception handlers, so the catalog lives **here**
(human reference) and is mirrored into the spec via per-route `responses=` (section 1.3)
and the `errors/*.json` fixtures (section 4). There are **two distinct error classes** —
do not conflate them.

### 5a. Transport-level HTTP errors — envelope `{ "error": {code, message, retryable, details?} }`

Source of truth: `api/errors.py` + `api/services/exceptions.py` + `api/rate_limit.py`.

| Code | HTTP | retryable | Retry-After | details | Raised when |
|---|---|---|---|---|---|
| `VALIDATION_ERROR` | 422 | false | — | — | request body/params fail Pydantic validation |
| `UNAUTHORIZED` | 401 | false | — | — | missing/invalid `X-API-Key` |
| `SESSION_NOT_FOUND` | 404 | false | — | — | unknown or expired session |
| `SESSION_ALREADY_LOCKED` | 409 | false | — | — | turn/lock attempt on a locked session |
| `TURN_IN_PROGRESS` | 409 | true | `KARMA_TURN_RETRY_AFTER_S` (default 1) | — | concurrent turn on the same session |
| `BRIEF_FLOOR_NOT_MET` | 409 | false | — | `{missing:[...]}` | lock before budget/primary-use answered |
| `BRIEF_NOT_LOCKED` | 409 | false | — | — | `POST /builds` before the session is locked |
| `BUILD_ALREADY_ACTIVE` | 409 | false | — | `{build_id}` | second build for a session already building |
| `BUILD_NOT_FOUND` | 404 | false | — | — | unknown or TTL/LRU-evicted `build_id` |
| `BUILD_CAPACITY` | 429 | true | 30 (fixed) | — | at `KARMA_MAX_CONCURRENT_BUILDS` |
| `RATE_LIMITED` | 429 | true | dynamic (window remaining) | — | per-key/IP quota exceeded |
| `LLM_UPSTREAM_ERROR` | 502 | true | — | — | an intake LLM call failed |
| `DATABASE_UNAVAILABLE` | 503 | true | — | — | locked-brief Postgres write failed |
| `INTERNAL_ERROR` | 500 | false | — | — | any unhandled / catch-all |

**Note — the 401 envelope (resolved in Step 1):** `UNAUTHORIZED` was originally raised via
`HTTPException` in `api/middleware.py` **without** going through `_envelope`, so its body
lacked the `retryable` field every other envelope carries. This is now **normalized**: the
401 handler routes its body through the same `_envelope` helper, so `UNAUTHORIZED` returns
`retryable: false` like every other transport error. The frontend can rely on `retryable`
being present on every error body, including 401.

### 5b. In-band build failures — inside a **200** `GET /builds/{id}` when `status:"failed"`

These are **not** HTTP errors. The poll returns `200` with an `error` body
(`ErrorBody`), because a failed build is a job outcome, not a transport failure
(`api_design.md` invariant 3). Codes + retryable from `api/mappers.py`
`_RETRYABLE_ERROR_CODES`:

| Code (in `error.code`) | retryable |
|---|---|
| `BUILD_TIMEOUT` | true |
| `LLM_UPSTREAM_ERROR` | true |
| `DEGRADED_DEPENDENCY` | true |
| `DATABASE_UNAVAILABLE` | true |
| `INTERNAL_ERROR` | false |

Also 200 **domain outcomes** (not errors, no `error` body): `status:"infeasible"`
(render `VerdictDTO`) and `status:"cannot_proceed"` (render `reason`). The frontend must
branch on `status`, not on HTTP code, for everything build-related.

---

## 6. CORS / auth handoff notes (for the frontend team — hand this section over as-is)

**Auth model (v1): browser-direct + static shared key.**
- Send `X-API-Key: <key>` on every request to `/api/v1/*`. Keys come from
  `KARMA_API_KEYS` (comma-separated, rotatable) on the server.
- `/healthz`, `/readyz`, `/docs`, `/redoc`, `/openapi.json` need **no** key.
- The key is a **shared static secret shipped to the browser** — it is not a per-user
  credential and provides no user identity or isolation. This is acceptable **only**
  because v1's audience is internal/controlled.
- **⚠ Launch blocker:** real per-user auth (per-user JWT; `UserBuildBrief` already
  carries `user_id`/`chat_id` for exactly this) **must** replace the static key before
  any public/storefront release. This constraint predates this doc and must not be lost
  now that we're producing developer-facing material — a static browser key is a v1
  internal-tool measure, not a shippable public auth story.

**CORS — what must be configured before a browser frontend works:**
- The server reads allowed origins from **`KARMA_CORS_ORIGINS`** (comma-separated). It
  **defaults to empty**, which means **browsers block all cross-origin calls** — a
  brand-new deploy will reject the frontend until this is set. This is deliberate: the
  server never defaults to `*`.
- Set `KARMA_CORS_ORIGINS` to the frontend's exact origin(s), e.g.
  `https://app.karma.example,http://localhost:3000` for local dev.
- `allow_credentials=True`; methods and headers are unrestricted (`*`). Because
  credentials are allowed, origins **must** be explicit — `*` is not a legal combination
  and is not used.
- The auth header is `X-API-Key` (custom header) — ensure it is not stripped by any
  proxy/CDN in front of the API.

**Deployment reality the frontend should know:** single uvicorn process, in-memory
session/job/rate-limit state ⇒ no horizontal scale in v1; a server restart drops
in-flight sessions and builds (the frontend re-POSTs). Locked briefs persist to Postgres;
build results do not (regenerable from the brief).

---

## 7. Verification approach (prove the exported contract is accurate)

Three tests, all runnable in CI without external services beyond what the app needs to
construct (`create_app()` does not require live Postgres/Neo4j):

1. **OpenAPI well-formedness.** Boot the app via `TestClient`, `GET /openapi.json`,
   assert it parses as JSON, `openapi` starts with `3.`, and `paths` covers every
   mounted route. Optionally validate with `openapi-spec-validator` for full 3.x
   conformance.

2. **Snapshot/freeze diff.** Regenerate the spec from `app.openapi()` and assert it
   equals the committed `api/contract/openapi.json`. Any DTO shape change fails here —
   this is the section 3 freeze. A `--regen` flag (env var or pytest option) rewrites the
   snapshot as a deliberate, reviewable step.

3. **Fixture round-trip.** For every file in `api/contract/fixtures/`, load the JSON and
   `DTO.model_validate(...)` it through the real Pydantic model it claims to represent
   (map filename→DTO). Catches hand-edited or drifted fixtures. **Caveat:** because the
   `BriefSummaryDTO` sub-objects are untyped `dict` (section 3), this proves the outer
   envelope parses, not the inner dict contents — so the fixtures remain the human
   authority for those inner shapes until they are promoted to typed models.

Live-server smoke (manual, optional): run `uvicorn api.main:app`, open `/docs`, confirm
the error responses and tags render, and that `GET /openapi.json` matches the committed
snapshot byte-for-byte.

---

## 8. Open questions / risks — flag, not resolve

Collected from this doc and the "open questions" sections of `intake_routes_plan.md`,
`build_service_plan.md`, `hardening_plan.md`, and `api_design.md §11`:

1. **TypeScript client generation is out of scope** unless the frontend team asks. The
   committed `openapi.json` is codegen-ready (`openapi-typescript`, `orval`), but whether
   *we* own/publish a generated client, and where it would live, is undecided. Recommend:
   ship the spec + fixtures; let the frontend own codegen in v1.

2. **Publishing the snapshot beyond the repo** (a docs portal, a shared package registry,
   a versioned artifact) is unspecified. In-repo under `api/contract/` is the v1 answer;
   external hosting is a later call tied to how the frontend team consumes it.

3. **Untyped `dict` DTO fields** (section 3) — the freeze test and OpenAPI schema do not
   cover their inner shape. Promoting them to typed sub-models is a recommended,
   non-breaking follow-up but not part of this package.

4. **401 `UNAUTHORIZED` envelope inconsistency — RESOLVED, not open.** The 401 handler is
   normalized in Step 1 to route its body through the same `_envelope` helper as every
   other error, so `UNAUTHORIZED` now includes `retryable: false` like the rest of the
   catalog (section 5a). This is a decided, implemented change — not a documentation-only
   note and not an open question.

5. **`504 INTAKE_TURN_TIMEOUT` is designed but unimplemented.** `api_design.md §8` tells
   the frontend to "on 502/504 show retry", but no `504` is emitted today — there is no
   core LLM `timeout=` hook to bound a hung intake turn (`intake_routes_plan.md §8 item 5`,
   `hardening_plan.md §7 item 2`). The frontend should treat `504` as *reserved/aspirational*
   and rely on `502` for upstream-LLM failure in v1. Because no code path emits it, the
   per-route `responses={}` documentation (section 1.3) deliberately omits any `504` entry
   — documenting it would misrepresent the live contract. Flag so the "designed" and
   "emitted" error sets aren't conflated.

6. **`TURN_IN_PROGRESS` (409, retryable) is UX-only** — nothing server-side queues or
   retries; the client must implement short backoff + re-POST for the contract to mean
   anything (`intake_routes_plan.md §8 item 7`). Confirm the frontend's retry behavior
   matches.

7. **Single-process constraint** — session store, job registry, and rate limiter are all
   in-memory (`hardening_plan.md §7 item 7`). No contract impact, but the frontend/ops
   should know restarts drop in-flight state and only one uvicorn worker is valid until
   these move to Redis/Postgres together.

8. **No read-back path for locked briefs** — `persist_locked_brief` has no getter, so a
   locked session that TTL-expires before the user clicks *Generate* makes the build
   impossible despite a durable Postgres brief (`build_service_plan.md §8 item 3`). A
   frontend concern only insofar as it should not let a locked session sit past its TTL.

9. **`session_id` path-param is unvalidated format-wise** (`intake_routes_plan.md §8
   item 8`) — an arbitrary string just 404s as `SESSION_NOT_FOUND`. Harmless; noted so the
   frontend doesn't expect a distinct "malformed id" error.

---

## Delivery checklist (this planning task)

- [ ] Write the content above (from the "# Frontend Contract Package" heading down) to
      `karma ai/docs/frontend_contract_plan.md`.
- [ ] No `.py` files created or modified.
- [ ] Paste the full file contents back to the user.

Everything in sections 1–7 marked "follow-up" / "not built" is deliberately left for a
later implementation phase; Phase 6's planning deliverable is this document only.
