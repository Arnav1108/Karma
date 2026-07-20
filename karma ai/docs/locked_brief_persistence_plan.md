# Locked-Brief Postgres Persistence — Implementation Plan

**Planning document only — no implementation in this pass.** Grounded in the real, current code,
all read in full for this plan: `karma ai/agents/db/postgres.py` (connection/query pattern),
`karma ai/agents/schemas/brief.py` (`UserBuildBrief`'s real shape), `karma ai/api/services/
intake_service.py` and `karma ai/api/services/exceptions.py` (current async method bodies and
exception taxonomy), `karma ai/docs/intake_routes_plan.md` §8 item 4 (the decision this plan
implements), and `karma ai/docs/api_design.md` §5 (durability discussion) and §9 (env/deployment).
Repo-wide `grep` was also run for any existing RLS/`CREATE POLICY`/migration-tool usage — findings
below in §4 and §5 are explicit about what was and wasn't found.

This is the follow-up planning task `intake_routes_plan.md` §8 item 4 calls for: "a new Postgres
table and write path ... that is not yet designed anywhere in the reviewed code." That item is
**decided**, not open — `lock_early` and `submit_answer`'s auto-lock branch both synchronously
persist the newly-locked `UserBuildBrief` to Postgres, as part of the same request that flips
`SessionRecord.status` to `"locked"`, not deferred to build time. This plan designs the schema,
write path, failure mode, and RLS policy for that decision. It does not touch `intake_routes_plan.md`
§1's route contracts, which stay exactly as specified there.

---

## 1. Table schema proposal

```sql
CREATE TABLE IF NOT EXISTS locked_briefs (
    brief_id        UUID         PRIMARY KEY,
    session_id      TEXT         NOT NULL,
    user_id         UUID         NOT NULL,
    chat_id         UUID         NOT NULL,
    schema_version  TEXT         NOT NULL,
    brief           JSONB        NOT NULL,
    locked_at       TIMESTAMPTZ  NOT NULL,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_locked_briefs_session_id ON locked_briefs (session_id);
CREATE INDEX IF NOT EXISTS idx_locked_briefs_user_id    ON locked_briefs (user_id);
```

**JSONB for the full brief, not normalized columns — justification.** `UserBuildBrief`
(`agents/schemas/brief.py:197`) has 13 nested sections (`budget`, `purpose`, `software: list[...]`,
`performance`, `monitor`, `peripherals: list[...]`, `storage`, `operating_system`, `existing`,
`physical`, `longevity`, `extras`, `hard_constraints`), several of which are themselves nested
(`Monitor.owned_specs`/`target_specs`, `Existing.reuse_parts`/`ecosystem_prefs`,
`HardConstraints.must_have`/`must_not`/`rejected_parts`) or list-typed. Normalizing this would mean
either ~13 separate JSONB sub-columns (no real gain over one JSONB blob for the whole object — still
opaque to SQL `WHERE` clauses on nested fields, still no relational integrity benefit) or a much
larger set of properly normalized tables (a `locked_brief_software`, `locked_brief_peripherals`,
etc. per list-typed section) that buys relational query power this feature has no stated need for
(§5's open questions confirm there is no read path designed yet at all). The model already carries
its own `schema_version` field specifically to let a JSON payload evolve without a table migration
— storing the brief as one versioned JSONB blob is the intended use of that field, not a workaround.

**Column choices:**
- `brief_id` as primary key: `UserBuildBrief.brief_id` is a UUID generated once per session
  (`blank_brief(uuid4(), uuid4(), uuid4())` in `intake_service.py:44`) and never changes after that
  — it is the natural, already-unique key for "one locked brief."
- `session_id` promoted to its own column (not left buried in the JSONB): it's the key
  `IntakeService`/`SessionStore` already index by, and it's the identifier the frontend actually
  holds after `POST /intake/sessions` (`brief_id` is an internal envelope field never surfaced in
  any DTO per `intake_routes_plan.md` §3's `BriefSummaryDTO`). A lookup path keyed by `session_id`
  is far more likely to be needed first than one keyed by `brief_id`. Typed `TEXT`, matching
  `SessionStore`'s own key type (`session_store.py`'s dict keys are plain strings, not UUID objects
  — `intake_routes_plan.md` §1.2 notes `session_id` is not validated as UUID at the route layer
  either).
- `user_id` promoted: called out explicitly in this plan's scope as "useful for future lookup," and
  `api_design.md` §9 already anticipates it becoming a real auth principal ("the auth principal will
  populate `user_id` with no schema change") rather than today's random placeholder — a future
  "all locked briefs for this user" query is plausible once that lands.
- `chat_id` stored but not indexed: kept for parity/completeness with the brief's envelope, no
  identified query pattern needs it yet.
- `build_id` deliberately **not** promoted to its own column: it's `None` at lock time by
  construction (`UserBuildBrief.build_id: UUID | None = None`, only ever set later when a build is
  generated) — useless as a lookup key for a row written at lock time. It stays inside the JSONB
  blob, already `null` on every row this write path produces.
- `schema_version` promoted: lets a future reader detect and branch on brief-shape drift without
  parsing the JSONB first.
- `brief` — `UserBuildBrief.model_dump(mode="json")` (or equivalent), the full object as JSON.
  `mode="json"` specifically so UUID/datetime fields serialize to strings the way `psycopg2`'s JSONB
  adapter expects, rather than Python objects `json.dumps` can't handle directly.
- `locked_at` vs `created_at`: `locked_at` is the semantic timestamp (when `lock_brief()` ran,
  logically same instant as this write); `created_at` defaults to `now()` at insert time. In the
  normal case these are ~identical; they diverge only if a retry (see §3) re-attempts the insert
  after an earlier failed attempt, in which case `created_at` reflects the successful attempt while
  `locked_at` should still reflect `working_state.brief.updated_at` (or an equivalent lock-moment
  timestamp) from the original `lock_brief()` call. Keeping both avoids conflating "when did the
  domain event happen" with "when did the row land."

**Idempotency:** the actual insert should be `INSERT ... ON CONFLICT (brief_id) DO NOTHING` (or
`DO UPDATE` — see the retry discussion in §3) rather than a bare `INSERT`, since brief_id is
generated once per session and a caller retry after a partial failure must not raise a duplicate-key
error on the second attempt.

---

## 2. Where the write happens inside `IntakeService`

**Confirmed: `agents/db/postgres.py`'s connection pattern is accurate as read** — module-level
`ThreadedConnectionPool(minconn=1, maxconn=10)` lazily constructed in `_get_pool()`, a `_cursor()`
`@contextmanager` that does `pool.getconn()` → yields a cursor → `conn.commit()` on success /
`conn.rollback()` on exception → `pool.putconn(conn)` in `finally`. Every existing `PostgresClient`
method (`get_min_catalog_price`, `get_parts_in_band`, `set_software_spec_cache`, etc.) is a plain
synchronous, blocking call built on that contextmanager. This plan adds one more method in the same
style:

```python
class PostgresClient:
    def persist_locked_brief(self, brief: UserBuildBrief, session_id: str) -> None:
        with _cursor() as cur:
            cur.execute(
                """
                INSERT INTO locked_briefs
                    (brief_id, session_id, user_id, chat_id, schema_version, brief, locked_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (brief_id) DO NOTHING
                """,
                (
                    str(brief.brief_id), session_id, str(brief.user_id), str(brief.chat_id),
                    brief.schema_version, brief.model_dump_json(), brief.updated_at,
                ),
            )
```

**Does this need `run_in_executor` wrapping? Yes — resolved explicitly, per the task's own flag
that this is easy to get wrong.**

Every *existing* synchronous `PostgresClient` call in the codebase is made from code that is itself
already off the event loop by construction: pipeline code (`node2_allocation.py`,
`feasibility/catalog_floor.py`, `node3_selector.py`, etc.) runs either inside the CLI's synchronous
`run_pipeline.py` main, or — per `api_design.md` §3.6/§9 — inside a dedicated `BuildService`
`ThreadPoolExecutor` worker, never directly on a FastAPI request's event loop. So none of those
callers needed to think about this.

`IntakeService` is different in exactly the relevant way: `intake_routes_plan.md` §7 states a hard
rule that every intake route handler is `async def` and calls `await service.<method>(...)`
*directly* — never wrapped in its own thread. That means `lock_early` and `submit_answer` run their
entire bodies **on the request-handling event loop**, and `IntakeService` already had to solve this
exact problem once: its one existing blocking call (`intake_begin`/`intake_step`, sync LLM-calling
core functions) is dispatched via `loop.run_in_executor(None, ...)` (`intake_service.py:49`, `:72-74`)
specifically so it doesn't block that loop for every other concurrent session's requests.

A new `PostgresClient.persist_locked_brief` call is the same class of blocking I/O — a network
round-trip via `psycopg2`, which is not `asyncio`-aware — called from the same `async def` context.
Calling it directly (not through an executor) would block the entire event loop, and therefore every
other in-flight request across every session, for the duration of the DB round-trip. It must be
wrapped in `run_in_executor` exactly like the LLM call is. This is not optional hardening — skipping
it reintroduces, for Postgres, precisely the event-loop-blocking bug the LLM dispatch already exists
to avoid.

**Exact placement**, both call sites, *before* `self._store.update(...)`:

```python
# lock_early — after floor check, after lock_brief(), before the store update
async with record.lock:
    if not floor_met(record.state.brief):
        ...  # unchanged
    working_state = record.state.model_copy(deep=True)
    working_state.brief = lock_brief(working_state.brief)

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None,
            functools.partial(self._postgres.persist_locked_brief, working_state.brief, session_id),
        )
    except Exception as exc:
        raise BriefPersistenceError(exc) from exc

    updated = await self._store.update(session_id, working_state, "locked")
    ...
```

```python
# submit_answer's auto-lock branch — same shape, inserted where the brief is force-locked
if not locked and question is None:
    working_state.brief = lock_brief(working_state.brief)
    locked = True

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None,
            functools.partial(self._postgres.persist_locked_brief, working_state.brief, session_id),
        )
    except Exception as exc:
        raise BriefPersistenceError(exc) from exc

status = "locked" if locked else "asking"
updated = await self._store.update(session_id, working_state, status)
```

Why *before* `self._store.update`, not after: see §3 — this ordering is what makes the failure mode
atomic for free, using the same structural pattern the existing `LlmUpstreamError` rollback already
relies on (the only mutation of shared state, `self._store.update`, happens last).

**New constructor dependency:** `IntakeService.__init__(self, store: SessionStore)` gains a second
parameter, `postgres: PostgresClient`. Composes with the existing `app.state` singleton DI pattern
(`api_design.md` §5 / `intake_routes_plan.md` §5) unchanged — `app.state.intake_service =
IntakeService(InMemorySessionStore(), PostgresClient())` at `create_app()` time, same one-singleton-
per-process reasoning already established for `SessionStore`.

---

## 3. `BriefPersistenceError` and the rollback question

**New exception**, added to `api/services/exceptions.py` alongside the existing five:

```python
class BriefPersistenceError(IntakeServiceError):
    """The synchronous Postgres write of the newly-locked brief failed inside
    lock_early / submit_answer's auto-lock branch. In-memory session state is
    left unchanged (still "asking") — the caller may safely retry."""
    def __init__(self, cause: Exception) -> None:
        self.cause = cause
        super().__init__(f"Failed to persist locked brief: {type(cause).__name__}: {cause}")
```
Mirrors `LlmUpstreamError`'s shape exactly (cause-wrapping constructor, same "state unchanged, safe
to retry" contract stated in the docstring) rather than inventing a new convention.

**Decision: fail atomically — the whole `lock_early`/auto-lock call fails, the session must not show
`status="locked"` in memory if the Postgres write didn't succeed.** Not "proceed with a warning."

Reasoning:
- The entire reason this table exists (`intake_routes_plan.md` §8 item 4) is that a locked brief
  must survive a process restart **the moment it locks**, closing `api_design.md` §5's durability
  gap for the locked-brief case specifically. If the write fails but the service still reports
  `status: "locked"` to the caller, a process restart between that response and any later retry
  would silently lose the brief while the client believes it is durably locked — reintroducing
  exactly the bug this feature exists to close, just with an extra log line attached. A
  proceed-with-warning design defeats the feature's own purpose.
- It costs nothing extra structurally. `submit_answer` already achieves this same atomicity for LLM
  failures: `working_state` is a local deep-copy, and `self._store.update(...)` — the only line that
  mutates shared state — runs last, after the risky operation has already succeeded. A raised
  exception before that point leaves `record.state` untouched. Placing the new Postgres write in the
  same position (§2's placement, before `self._store.update`) extends that exact guarantee to the
  new failure mode for free — no new rollback logic needed, just the same ordering already proven
  correct for `LlmUpstreamError`.
- Retry safety on the client side falls out naturally: `lock_early` re-raising leaves `record.status`
  at `"asking"`, so a client retry of `POST .../lock` is not blocked by `SessionAlreadyLockedError`
  (that only fires once status has actually flipped) — it's a clean retry of the same operation.
  Same for `submit_answer`'s auto-lock branch: the turn as a whole failed, so re-submitting the same
  terminal answer is safe and expected, matching the existing `LlmUpstreamError` retry contract
  already documented for this method.
- This is also why §1's schema uses `ON CONFLICT (brief_id) DO NOTHING`: if the Postgres write itself
  had already committed on a prior attempt but a *later* step in that same request somehow still
  failed before the caller found out, the retried write must not raise a duplicate-key error.

**HTTP-layer mapping is explicitly not decided here** — flagged for `intake_routes_plan.md` §2's
exception table as a follow-up, per that document's own framing that route-facing concerns belong
there, not in this persistence-focused document. Candidate mapping to record when that table is
revisited: `503` with a code like `BRIEF_PERSISTENCE_FAILED` or reuse of `DATABASE_UNAVAILABLE`
(already used elsewhere in `api_design.md` §7's table for Postgres pool failures), `retryable: true`
either way — not `502`, since this isn't an upstream LLM-provider failure.

---

## 4. RLS policy

**Grounding check, explicitly against the task's own framing:** the task states "catalog RLS is
already on." A repo-wide search (`grep -ri "RLS\|POLICY\|row.level"` across `karma ai/`) found **no**
`CREATE POLICY`, `ENABLE ROW LEVEL SECURITY`, or RLS discussion in any tracked SQL file
(`data/catalog/seed.sql`, `seed_expansion.sql`, `software_specs_cache.sql`,
`scripts/verify_catalog_sync.sql`) or doc file. `seed.sql`'s `catalog` table definition
(`CREATE TABLE IF NOT EXISTS catalog (...)`) has no RLS statements anywhere near it. This strongly
suggests catalog's RLS, if it exists, was configured directly in the Supabase dashboard / SQL editor
and was never checked into this repo — **this plan cannot confirm catalog's actual policy shape from
the codebase alone**, and the proposal below should be treated as a reasoned default to be verified
against whatever Supabase actually has configured for `catalog`, not a confirmed match to it.

**A gotcha worth flagging explicitly, since it's the kind of thing that silently makes RLS a no-op:**
`POSTGRES_URL` connects as `postgres.<ref>` via the Supabase Session Pooler. If that role is (or maps
to) the owner of the tables it creates — which is the common case for the default Supabase connection
role — then **RLS policies do not restrict it by default**: Postgres exempts table owners (and
superusers) from RLS enforcement unless `ALTER TABLE ... FORCE ROW LEVEL SECURITY` is also set. In
that case, enabling RLS on `locked_briefs` would add no actual protection against the API's own
writes — it would only matter for *other* Postgres roles (e.g. an `anon`/`authenticated` role from a
future Supabase-client-side surface) attempting to touch the table directly. Before assuming the same
"RLS is on" pattern used for `catalog` provides meaningful protection here, confirm (a) what role
`POSTGRES_URL` actually authenticates as, and (b) whether `catalog`'s policy uses `FORCE ROW LEVEL
SECURITY` or relies on a non-owner role — otherwise this section's policy is enabled-but-inert in the
same way, if that's in fact catalog's situation too.

**Proposed policy, contingent on that being resolved:**

```sql
ALTER TABLE locked_briefs ENABLE ROW LEVEL SECURITY;

-- Resolve <api_write_role> to whichever role POSTGRES_URL actually authenticates
-- as (see gotcha above) before applying.
CREATE POLICY locked_briefs_api_insert ON locked_briefs
    FOR INSERT
    TO <api_write_role>
    WITH CHECK (true);
```

No `SELECT`/`UPDATE`/`DELETE` policy is proposed yet. With RLS enabled and only an `INSERT` policy
present, those commands are denied by default for any role other than the (possibly RLS-exempt)
owner — which is the safe default given §5 below notes no read path is designed yet. Add a `SELECT`
policy only once a concrete reader (e.g. a `GET`-by-brief-id endpoint) is designed and its required
role is known.

---

## 5. Open questions — flagged, not resolved

1. **Retention/cleanup.** No TTL or cleanup job is proposed. Do rows in `locked_briefs` live forever
   (audit trail), get purged after a build is generated, or expire after N days with no build? Not
   decided here.

2. **Read access from elsewhere.** Does a future `GET`-by-brief-id (or by-session-id) endpoint need
   this table? If so, which key is primary in practice — `session_id` (what the frontend already
   holds) or `brief_id` (the internal envelope field)? This also affects whether the `session_id`
   index proposed in §1 is actually load-bearing or premature. Not decided here; would also need its
   own RLS `SELECT` policy per §4.

3. **Migration mechanism — confirmed absent, choice not made.** Repo-wide search for `alembic` or any
   migration tooling found none — the only DB-schema files in the project (`data/catalog/seed.sql`,
   `seed_expansion.sql`, `software_specs_cache.sql`) are hand-written, idempotent
   (`CREATE TABLE IF NOT EXISTS` / `ON CONFLICT ... DO UPDATE`) SQL files with no evidence of a
   tracked runner — `scripts/test_db_connection.py` only pings, and
   `scripts/verify_catalog_sync.sql` only checks sync, neither applies schema. This plan's §1 schema
   would fit that same convention (e.g. a new `data/catalog/locked_briefs_schema.sql`, applied
   manually via the Supabase SQL editor or `psql`, matching how `catalog` itself was presumably
   stood up). Whether the project should introduce real migration tooling (Alembic, or Supabase's own
   migration CLI) before this becomes the second or third hand-managed table with no rollback/version
   story is worth deciding now, not resolved here.

4. **RLS role identity**, restated from §4: which Postgres role does `POSTGRES_URL` actually
   authenticate as, and does `catalog`'s existing "RLS is on" posture meaningfully restrict that role
   or is it exempt as table owner? Needs confirming against the live Supabase project (outside this
   repo's visibility) before §4's policy can be trusted as a real security boundary rather than a
   plausible-looking no-op.

5. **`schema_version` branching on read.** The column is stored for future-proofing but no reader
   exists yet to branch on it (see item 2). Not resolved — just ensuring the column isn't
   write-only-forever by accident once a reader is designed.
