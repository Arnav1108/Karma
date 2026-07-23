# Frontend Implementation Plan (Phase 7) — Karma Advisor Next.js Client

**Status:** planning. Depends on Phase 6 (`frontend_contract_plan.md`, done — contract
frozen, OpenAPI snapshot + fixtures committed). The scaffold described in section 0 is
already written but **not yet committed**. Nothing in this plan changes backend
behavior: the v1 DTO contract is frozen, and every phase below adapts the frontend to it.
A live/contract mismatch is *reported*, never patched in `api/`.

Audience: whoever implements the next frontend increment. Each phase is scoped to one
Claude Code prompt plus one human-verified checkpoint — no phase writes several
unreviewed files before a review gate.

---

## 0. Ground truth (what actually exists today)

Read against the working tree, not assumed.

| Claim | Verified state |
|---|---|
| Scaffold | Next.js 16.2.11 (App Router), React 19.2.4, TypeScript, Tailwind v4. 7 components, 5 `lib/` modules. |
| Wired to real endpoints | Yes. `lib/api.ts` covers create-session / answer / lock / abandon / start-build / poll-build. No mock `setTimeout` transitions remain. |
| State machine | `components/KarmaAdvisorApp.tsx` — `start → intake → review → generating → result`. |
| Env / connectivity | `KARMA_API_KEYS` + `KARMA_CORS_ORIGINS` set locally; `/healthz`, `/readyz`, and CORS preflight confirmed green by curl. |
| **Committed to git** | **No.** The whole `frontend/` directory is untracked (`git status` → `?? ./`). `.gitignore` correctly excludes `.env*` with an `!.env*.example` exception. |
| `lib/types.ts` vs `api/dtos.py` | Field-for-field correct for every DTO it covers. |
| Contract artifacts | `api/contract/openapi.json` + 31 fixtures — `chat/` 6, `errors/` 14, `progress/` 3, `result/` 4, `review/` 4. |
| `lib/summarize.ts` dict keys | Match `fixtures/review/answer_locked.response.json` exactly — `budget`, `purpose`, `performance`, `monitor`, `storage`, `operating_system`, `brand_prefs`, `physical`, `longevity`, `extras`, `hard_constraints` all check out. |

**Not done:** live end-to-end walkthrough against real backend data; any browser-level
QA; any test suite; error-state UX verified against the real error catalog; any
build/deploy config beyond `next dev`.

### 0.1 Defects found while reading the scaffold

Each is assigned to a phase. Defects 2, 3, and 5 go to Phase 3 because they are live-UI
failure modes — how the app behaves when something goes wrong, visible only in a
browser. Defects 4, 6, and 8 go to Phase 4 because each is tied to one error code's
handling.

1. **`SnapshotResponse` has no TS binding.** `GET /intake/sessions/{id}` exists
   (`api/routers/intake.py:144`) and has two committed fixtures, but neither
   `lib/types.ts` nor `lib/api.ts` mentions it. **Decided: out of scope for v1**
   (section 8) — recorded as a deliberate coverage gap, not an oversight.
2. **[Phase 3] `ErrorBanner`'s retryable branch is dead code.**
   `KarmaAdvisorApp.tsx:200-204` renders `<ErrorBanner>` with `message` / `retryable` /
   `onDismiss` but never `onRetry`; `ErrorBanner.tsx:18` gates the button on
   `retryable && onRetry`. "Try again" can render nowhere in the app.
3. **[Phase 3] A poll failure permanently kills the build.** `KarmaAdvisorApp.tsx:64-66`
   catches a `getBuildStatus` error, sets the banner, and returns — the timer is never
   rescheduled. The user sits on the `GeneratingScreen` spinner forever. One network
   blip during a ~60 s build is unrecoverable.
4. **[Phase 4] No client-side answer length cap.** `SubmitAnswerRequest.answer` is
   `min_length=1, max_length=2000` (`api/dtos.py`); `IntakeScreen` only trims and blocks
   empty, so a long paste returns a raw Pydantic 422.
5. **[Phase 3] Blank-screen branches in `ResultScreen`.** `succeeded` renders only when
   `status.build` is non-null, `infeasible` only when `status.verdict` is non-null.
   Either null yields an empty page — no message, no CTA.
6. **[Phase 4] 404 `SESSION_NOT_FOUND` has no restart CTA.** `api_design.md` §8 item 1
   requires "on `404` (expired) offer restart"; today it is an anonymous dismissible
   banner.
7. **[Phase 6, documented only] `expires_at` is fetched and never used.** No TTL warning
   anywhere. Relevant to `frontend_contract_plan.md` §8 item 8 — a locked session that
   TTL-expires makes the build impossible despite a durable Postgres brief.
8. **[Phase 4] `handleRetryBuild` lacks the `BUILD_ALREADY_ACTIVE` recovery** that
   `handleGenerate` has (`KarmaAdvisorApp.tsx:145-153`).

---

## 1. Phase 0 — Commit the scaffold

**Status: not started.**

Branch `phase7/frontend-verification`. Stage with explicit paths per the hard rule —
never `git add .`, since the repo root accumulates `node_modules/` and `__pycache__/`.

- Confirm `.env.local` is excluded and `.env.local.example` is committed.
- Add a `README` note on the two required env vars (`NEXT_PUBLIC_API_BASE_URL`,
  `NEXT_PUBLIC_API_KEY`), cross-referenced to `frontend_contract_plan.md` §6.

*Why first:* every later phase produces a diff. Without a baseline commit there is
nothing to review those diffs against.

**Checkpoint:** `git status` clean apart from intended files; `node_modules/` and
`.env.local` confirmed absent from the staged set.

---

## 2. Phase 1 — Live contract verification (curl)

**Status: not started.**

Prove `lib/types.ts` matches what the server actually emits — field for field, against
live responses, not just against the OpenAPI snapshot.

Drive a full multi-turn intake and build with `curl`, capturing every raw response body
to `frontend/.contract-capture/` (gitignored). Then diff each captured body against the
TypeScript declarations.

- Must cover: `POST /intake/sessions` → N× `POST …/answers` hitting all three `kind`
  values (`sequence`, `clarification`, `confirm_default`) → **both** the auto-lock path
  (sequence exhaustion) and the early `POST …/lock` path → `POST /builds` → poll
  `GET /builds/{id}` to a terminal status.
- Also assert the captured bodies match `api/contract/fixtures/**`. Where live and
  fixture disagree, the **fixture is authoritative for the untyped-dict interiors**
  (`frontend_contract_plan.md` §3) and the divergence is a backend bug to report — not
  to patch here.

**Cross-check against:** `api/contract/openapi.json`; `tests/test_openapi_contract.py`;
`tests/test_contract_fixtures.py`; `frontend_contract_plan.md` §4 (fixture set) and §3
(the untyped-`dict` freeze limitation).

**Fixes in-phase:** defect 1's documentation note; any genuine `types.ts` drift found.

**Checkpoint:** a captured-vs-declared diff table pasted back for review, plus
`npx tsc --noEmit` green.

---

## 3. Phase 2 — Fixture stub server (enabler)

**Status: not started.**

**Ownership: a committed, reusable dev tool** — the frontend parallel to
`tests/manual/load_build_concurrency.py`, the repo's only other manual harness. It lives
at `frontend/scripts/fixture-server.mjs`, is committed with a header comment naming its
owner phases, and is **not** deleted after Phase 4: every future change touching error or
terminal-state rendering needs it again.

One file, roughly 80 lines, no dependencies. Serves `api/contract/fixtures/**` over HTTP
with `Access-Control-Allow-Origin: http://localhost:3000` and `allow-credentials`, plus a
scenario switch so any response — including `Retry-After` headers and 4xx/5xx envelopes —
can be served on demand.

*Why it exists:* `cannot_proceed` is designed-unreachable in real runs
(`api_design.md` §7), `failed` / 502 / 503 / 500 require breaking a dependency to
provoke, and `BRIEF_FLOOR_NOT_MET` / `BRIEF_NOT_LOCKED` are unreachable through the UI at
all — the "Finish early" button only appears once `floor_met` is true. Serving the
*committed* fixtures means Phases 3-4 exercise the exact bytes the round-trip test
already validates; and since Phase 1 proves fixture == live, rendering the fixture proves
rendering the real thing. Zero backend changes.

**Checkpoint:** the stub serves each fixture; a browser `fetch` from `localhost:3000`
succeeds, proving the CORS headers are correct.

---

## 4. Phase 3 — Browser QA pass (screenshot-verified)

**Status: not started.**

**Precondition — hard gate.** Before anything else, confirm the claude-in-chrome
extension is actually connected (`tabs_context_mcp` returns live tabs; a test navigate
succeeds). **The extension failed to connect earlier in this project and the work fell
back to curl-only.** If it is not connected: **stop and report.** Do not silently
degrade to curl. Curl cannot see rendering, layout, console errors, or hydration
warnings — which is the entire reason this phase exists separately from Phase 1. A
curl-only pass here would be a false green.

**Against the live backend:** start → intake (all three question kinds; the progress
bar; "finish early" appearing exactly when `progress.floor_met` flips) → review →
generating → result `succeeded`. Then a deliberately under-budget brief (e.g. ₹20,000 for
a 4K gaming rig) to reach `infeasible` naturally.

**Against the stub:** `cannot_proceed`, and `failed` in both variants — `retryable: true`
and `retryable: false` render different CTAs (`ResultScreen.tsx:125-140`).

Screenshot every screen and sub-state. Check the browser console for React errors and
hydration warnings at each step.

**Cross-check against:** `api_design.md` §8 items 1-4 — notably that `warnings` are
"displayed prominently, they are dead-end notices, not fine print", and that the frontend
performs "zero arithmetic beyond formatting `price_inr`" (invariant 4).

**Fixes in-phase:** defects **2, 3, and 5**, plus whatever the screenshots reveal.
- **Defect 2** — wire `onRetry` through `KarmaAdvisorApp.tsx:200-204` so `ErrorBanner`'s
  `retryable && onRetry` branch can render, and have it re-attempt the action that
  failed rather than merely dismissing the banner.
- **Defect 3** — reschedule the poll after a caught `getBuildStatus` error
  (`KarmaAdvisorApp.tsx:64-66`) with bounded backoff and a finite attempt cap, so a
  transient blip does not strand the user on the spinner.
- **Defect 5** — give `succeeded`-with-null-`build` and `infeasible`-with-null-`verdict`
  a real message and CTA instead of an empty page.

**Checkpoint:** screenshots for all 4 screens and all 4 terminal statuses, the diff, and
**two explicit failure-mode demonstrations:**

1. **Defect 2** — trigger a retryable error live, confirm "Try again" *actually renders*
   (it can render nowhere in the app today), and confirm clicking it re-attempts the
   failed action rather than just clearing the banner.
2. **Defect 3** — kill the backend mid-poll, confirm the UI surfaces a retry path
   instead of leaving the user on the spinner forever, then bring the backend back and
   confirm polling can resume — or that the user can restart — rather than being stuck.

---

## 5. Phase 4 — Error-state audit

**Status: not started.**

Deliberately trigger every code in `frontend_contract_plan.md` §5a and confirm the UI
shows something sane — not a blank screen, not an unhandled exception, not a raw
Pydantic string. Use a real trigger where one is env-tunable; use the Phase 2 stub
otherwise.

**Step 0 (do this before any env edit): capture the current `.env` values** for
`KARMA_RL_SESSION_CREATE_PER_MIN` and `KARMA_MAX_CONCURRENT_BUILDS`, so the closing
revert gate has something to diff against.

| Code | HTTP | How to trigger |
|---|---|---|
| `UNAUTHORIZED` | 401 | wrong `NEXT_PUBLIC_API_KEY` — real |
| `RATE_LIMITED` | 429 | `KARMA_RL_SESSION_CREATE_PER_MIN=1`, click Begin twice — real |
| `BUILD_CAPACITY` | 429 | `KARMA_MAX_CONCURRENT_BUILDS=1`, two parallel sessions — real |
| `BUILD_ALREADY_ACTIVE` | 409 | `POST /builds` twice for one session — real |
| `SESSION_NOT_FOUND` | 404 | `DELETE` the session by curl mid-intake, then answer in the UI — real |
| `SESSION_ALREADY_LOCKED` | 409 | curl-`lock` while the UI sits on a question, then answer — real |
| `VALIDATION_ERROR` | 422 | paste more than 2000 characters — real |
| `TURN_IN_PROGRESS` | 409 | stub — racing two real turns is flaky. Confirm `api.ts:65-68`'s 3× backoff fires *and then* surfaces to the user |
| `BRIEF_FLOOR_NOT_MET`, `BRIEF_NOT_LOCKED` | 409 | stub — unreachable through the UI |
| `LLM_UPSTREAM_ERROR`, `DATABASE_UNAVAILABLE`, `INTERNAL_ERROR` | 502 / 503 / 500 | stub — otherwise requires breaking a dependency |
| network failure | — | stop the backend mid-flow |

**Do not special-case `504`.** It is designed but no code path emits it
(`frontend_contract_plan.md` §8 item 5); treat `502` as the upstream-LLM failure signal
in v1.

**Cross-check against:** `frontend_contract_plan.md` §5a **and** §5b — the two error
classes must not be conflated; a `failed` build arrives as **HTTP 200** with an in-band
`error` body, so the frontend branches on `status`, never on HTTP code, for anything
build-related. Also `api/errors.py`, `tests/test_errors.py`,
`tests/test_rate_limit_wiring.py`, and `hardening_plan.md` §2 for the rate-limit tiers.

**Fixes in-phase:** defects **4, 6, and 8** — cap answer length client-side; give 404
`SESSION_NOT_FOUND` a restart CTA per `api_design.md` §8 item 1; mirror
`handleGenerate`'s `BUILD_ALREADY_ACTIVE` recovery into `handleRetryBuild`. (Defects 2
and 3 belong to Phase 3 and are assumed landed by the time this phase runs.)

### 5.1 Env-mutation hazard — mandatory revert

`KARMA_RL_SESSION_CREATE_PER_MIN=1` and `KARMA_MAX_CONCURRENT_BUILDS=1` are real backend
`.env` edits that make the system behave pathologically. Two ways they outlive the phase:

- **`get_settings()` is `@lru_cache`d** (`api/config.py:37`), so restoring the value is
  *not enough* — **the backend process must be restarted** to pick it up. This has bitten
  the project before: commit `b460482` exists solely to clear cached `Settings` between
  `tests/test_errors.py` cases for exactly this reason.
- **A PowerShell `$env:KARMA_*` override persists for the life of that shell window**, so
  a later run in the same window silently inherits it even after `.env` is clean.

*(No pre-existing note on this hazard was found in `hardening_plan.md`, `api_design.md`,
`context.md`, or `lesson.md` — hence stating it here. Worth promoting to `lesson.md`.)*

**Checkpoint:** a code-by-code table of what the user actually sees, screenshot-backed,
**and a closing revert gate — the phase is not done until all three hold:**

1. `.env` is confirmed restored to its pre-phase values, diffed against the step-0 capture.
2. The backend process has been restarted, and any shell carrying a `$env:KARMA_*`
   override is closed or explicitly cleared.
3. A normal session-create **and** a normal build both succeed at default limits —
   proving the pathological settings are actually gone, not merely edited on disk.

---

## 6. Phase 5 — Test suite (narrow, fixture-driven)

**Status: not started. Decided scope: Vitest only — no jsdom, no React Testing Library,
no component tests.**

The rationale, stated plainly because "add tests" is otherwise unbounded: the backend
suite already covers the pipeline logic this frontend merely displays, and the screens
are thin presentational wrappers over a typed client. Component tests would mostly
re-assert Tailwind class names. The one place a test genuinely earns its keep is the
**untyped-dict boundary** — `BriefSummaryDTO`'s section fields are `dict` on the wire, so
the OpenAPI freeze test **cannot** catch a change to their interiors
(`frontend_contract_plan.md` §3), and `lib/summarize.ts` reads roughly 25 keys out of
them by hand.

Two test files:

1. **`lib/summarize.test.ts`** — run `buildReviewFields()` over the real
   `api/contract/fixtures/review/*.json`; assert the expected labels appear and that no
   section silently yields nothing. This is the only automated guard against fixture ↔
   `summarize.ts` drift.
2. **`lib/api.test.ts`** — feed `api/contract/fixtures/errors/*.json` through the
   `ApiError` mapping; assert `code` / `retryable` / `details` / `Retry-After` land
   correctly, and that the `TURN_IN_PROGRESS` retry path bounds at 3 attempts.

Both import the fixtures **by relative path from `api/contract/`**, so a backend fixture
regen breaks the frontend test — which is the point.

Add `"test": "vitest run"` and a `typecheck` script to `package.json`.

**Checkpoint:** `npm test` green, plus one deliberately mutated fixture demonstrating the
test actually fails — proof the assertions are not vacuous.

---

## 7. Phase 6 — Production build + launch-readiness register

**Status: not started. No deploy in v1** — hosting stays blocked behind constraints the
backend docs already own.

Verify `npm run build` compiles (only `next dev` has ever run), `npx tsc --noEmit`, and
`npm run lint`.

Then write the gap register — documentation, no code:

- **⚠ Launch blocker — auth.** `NEXT_PUBLIC_API_KEY` is a static shared secret, and the
  `NEXT_PUBLIC_*` prefix means it is **inlined into the client bundle and readable by
  anyone who loads the page**. Acceptable only because v1's audience is
  internal/controlled (`frontend_contract_plan.md` §6). When real per-user auth lands,
  the frontend changes are: the key moves out of `NEXT_PUBLIC_*` to server-side only;
  `lib/config.ts` and the `X-API-Key` header at `lib/api.ts:42` are replaced by an
  `Authorization` bearer flow; and calls likely route through a Next Route Handler proxy
  rather than browser-direct. **Scoped here, not implemented now.**
- **Single-process backend** — a restart drops in-flight sessions and builds; the
  frontend re-POSTs (`frontend_contract_plan.md` §6 and §8 item 7).
- **Neo4j is still local Docker** — a deployed frontend needs a deployed backend that can
  reach it. Pre-production blocker already recorded in `CLAUDE.md`.
- **No error monitoring, no analytics**, and no `error.tsx` / `not-found.tsx` boundaries.
- **`expires_at` unused** (defect 7) — a session-TTL warning is its natural home.

**Checkpoint:** a clean production build, and the register reviewed as written.

---

## 8. Explicitly out of scope for v1

- **Refinement / chat-style negotiation UI.** `parse_refinement_request_v2` is dormant
  and has no route; it slots in later as `POST /builds/{build_id}/refinements`
  (`api_design.md` §10). The review screen has no edit-in-place **by design** — a wrong
  summary means restarting intake (`api_design.md` §8 item 2).
- **Purchase / cart / "accept build" flow.** Requires a `routers/catalog.py` that does
  not exist (`api_design.md` §10).
- **Session resume via `GET /intake/sessions/{id}`.** No reload-recovery UX in v1, so the
  route stays unbound (defect 1). Recorded as a deliberate contract-coverage gap.
- **Streaming progress (SSE).** Polling is the v1 contract and upgrades to SSE without
  contract breakage (`api_design.md` §8 item 3, §10).
- **Auth UI, user accounts, build history, multi-language, mobile-native.**
- **Backend changes of any kind.** Every phase adapts the frontend to the frozen v1
  contract; a mismatch is reported, never patched in `api/`.

---

## 9. Open questions

1. Should `.contract-capture/` artifacts be committed as a dated snapshot, or stay
   gitignored scratch? Gitignored is the Phase 1 default.
2. Is `poll_after_ms`'s 2 s default right for a build that takes ~60 s? It is the
   server's value and the frontend honors it; worth revisiting once real build durations
   are measured (`api_design.md` §8 item 3).
3. How many consecutive poll failures should the Phase 3 defect-3 fix tolerate before
   giving up and offering restart? Proposed: 3, with backoff — confirm at implementation.
4. Should the `expires_at` TTL warning (defect 7) become real work, or stay documented?
   It matters most for a locked session left sitting, since there is no read-back path
   for a persisted locked brief (`frontend_contract_plan.md` §8 item 8).
