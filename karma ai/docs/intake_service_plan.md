# IntakeService — Implementation Plan (Phase 3)

**Planning document only — no implementation in this pass.** Grounded in the real, current code:
`karma ai/agents/nodes/node1_intake.py`, `karma ai/api/services/session_store.py`, and
`karma ai/agents/llm/client.py`, all read in full for this plan. Where this document disagrees
with `karma ai/docs/api_design.md`'s §2 sketch of the intake contract, that sketch is treated as
**intent, not accurate current signatures** (as `api_design.md` itself instructs) — every
disagreement is called out explicitly rather than silently reconciled.

---

## 1. Method-by-method contract

`IntakeService` wraps one `SessionStore` instance and exposes five async methods. Every method's
inputs/outputs/exceptions are expressed directly in terms of the real `intake_begin`/`intake_step`
signatures from `node1_intake.py` — not invented ones.

```python
class IntakeService:
    def __init__(self, store: SessionStore) -> None:
        self._store = store
```

### `create_session`

```python
async def create_session(
    self, client_ref: str | None = None,
) -> tuple[SessionRecord, IntakeQuestion | None]:
```

1. `brief = blank_brief(uuid4(), uuid4(), uuid4())` — no schema_version override, no IO. The real
   signature is `blank_brief(brief_id: UUID, user_id: UUID, chat_id: UUID, schema_version="1.0")`;
   all three UUIDs are caller-supplied, not generated internally, so `IntakeService` mints fresh
   `uuid4()` values per session (see §8, item 6).
2. `state = IntakeSessionState(brief=brief, history=[])` — the other four fields
   (`asked_so_far`, `open_question_attempts`, `pending_open_question_field`,
   `current_question_id`) take their declared defaults (`[]`, `{}`, `None`, `None`).
3. `state, question = await loop.run_in_executor(None, intake_begin, state, None)` — the **one**
   LLM call this method makes (phrasing the first `QUESTION_SEQUENCE` entry, `"budget"`, via
   `phrase_fn=None`'s fallback — see §2). Runs on the executor per §7.
4. Only if step 3 returns without raising: `record = await self._store.create(state)`.
5. `return record, question`.

`client_ref` is accepted but not persisted onto `IntakeSessionState` (it has no field for it) — if
correlation is needed later it belongs on `SessionRecord` or a thin wrapper, not in scope here.

**Raises:** `LlmUpstreamError` if step 3 raises a raw `openai.OpenAIError` (phrasing the first
question failed). Nothing is stored in that case — `store.create()` is never reached — so there is
nothing to roll back.

### `submit_answer`

```python
async def submit_answer(
    self, session_id: str, answer: str,
) -> tuple[SessionRecord, IntakeQuestion | None, bool]:
```

1. `record = await self._store.get(session_id)` — the refreshing read is correct here; submitting
   an answer is genuine session activity and should slide the TTL.
   - `record is None` → raise `SessionNotFoundError`.
   - `record.status == "locked"` → raise `SessionAlreadyLockedError`.
   - `record.lock.locked()` → raise `TurnInProgressError` (fail fast; see §3 — never queue).
2. `async with record.lock:`
   a. `working_state = record.state.model_copy(deep=True)` — **the atomicity boundary**; see §4
      for why operating on a copy, not `record.state` itself, is required.
   b. Run the turn:
      ```python
      try:
          working_state, question, locked = await loop.run_in_executor(
              None, functools.partial(intake_step, working_state, answer, None)
          )
      except (openai.OpenAIError, StructuredCallError) as exc:
          raise LlmUpstreamError(exc) from exc
      ```
      At this point `record.state` (the object still sitting in the store) is untouched by either
      the success or failure path, because `working_state` was a deep copy.
   c. **Sequence-exhaustion auto-lock (confirmed product behavior, not open):**
      `intake_step`/`intake_begin` never force-lock on their own — by design, per their
      docstrings ("Never locks or force-locks the brief" / "Never force-locks"). They only lock
      via `extract_turn`'s own "done"/"stop" + `floor_met` early-exit path. If the 13-question
      sequence is exhausted without the user ever saying "done"/"stop", `intake_step` returns
      `(working_state, None, False)` — `working_state.brief.status` is still `"draft"`.
      **Confirmed behavior: answering all 13 sequence questions IS completion.** The moment the
      sequence is exhausted, `IntakeService` auto-locks, exactly mirroring `drive_intake`'s own
      end-of-loop finalization (`if final_brief.status != "locked": final_brief =
      lock_brief(final_brief)`) — just moved into the per-turn method instead of a driving loop:
      ```python
      if not locked and question is None:
          working_state.brief = lock_brief(working_state.brief)
          locked = True
      ```
      No `floor_met` re-check is needed at this point: sequence exhaustion means every
      `QUESTION_SEQUENCE` entry — including `"budget"` and `"primary_use_case"`, the two fields
      `floor_met` itself gates on, and the first two entries in the sequence — has already been
      answered or opportunistically filled. There is no ambiguity left to protect against: 13/13
      answered means nothing is missing.
   d. Commit: `status = "locked" if locked else "asking"`;
      `updated = await self._store.update(session_id, working_state, status)`.
      `updated is None` → raise `SessionNotFoundError` (session expired mid-turn — see §8, item 7
      for this edge case).
   e. `return updated, question, locked`.

**Raises:** `SessionNotFoundError`, `SessionAlreadyLockedError`, `TurnInProgressError`,
`LlmUpstreamError`.

### `get_snapshot`

```python
async def get_snapshot(self, session_id: str) -> SessionRecord:
    record = await self._store.peek(session_id)   # non-refreshing read — see §6
    if record is None:
        raise SessionNotFoundError
    return record
```

No core call, no executor dispatch, no lock check — a pure read. `peek()` does not exist on
`SessionStore` yet; see §6.

### `lock_early`

```python
async def lock_early(self, session_id: str) -> SessionRecord:
```

1. `record = await self._store.get(session_id)` — refreshing read is correct: an explicit
   "finish early" action is genuine activity.
   - `record is None` → `SessionNotFoundError`.
   - `record.status == "locked"` → `SessionAlreadyLockedError`.
   - `record.lock.locked()` → `TurnInProgressError`.
2. `async with record.lock:`
   a. `if not floor_met(record.state.brief): raise BriefFloorNotMetError(missing)` where
      `missing` is derived directly from `floor_met`'s own two conditions
      (`budget.comfortable_max > 0`, `bool(purpose.sub_case)`) — checked **before** calling
      `lock_brief`, since `lock_brief`'s own docstring is explicit: *"Callers must check
      floor_met(brief) themselves before calling this — it does not enforce the floor gate."*
   b. `working_state = record.state.model_copy(deep=True)` (uniform with `submit_answer`; see §4
      — `lock_brief` is pure/no-IO so there's no real rollback scenario here, this is defensive
      consistency, not load-bearing).
   c. `working_state.brief = lock_brief(working_state.brief)` — no executor needed (§7).
   d. `updated = await self._store.update(session_id, working_state, "locked")`;
      `updated is None` → `SessionNotFoundError`.
   e. `return updated`.

**Raises:** `SessionNotFoundError`, `SessionAlreadyLockedError`, `TurnInProgressError`,
`BriefFloorNotMetError`.

### `abandon`

```python
async def abandon(self, session_id: str) -> None:
    await self._store.delete(session_id)
```

No exception on a missing/already-gone session (`SessionStore.delete` is documented idempotent:
`True` only if it existed). Matches the intended `204` either way. No lock check — see §8, item 8
for the accepted in-flight-turn race this implies.

---

## 2. The `phrase_fn` injection

**Decision: `IntakeService` passes `phrase_fn=None` to both `intake_begin` and `intake_step` — it
does not inject a custom one.**

`intake_begin`'s own default (`phrase = phrase_fn or _default_phrase_fn`) already *is* "call
`call_text` with the question's raw persona text" — `_default_phrase_fn` looks up
`_QUESTIONS_BY_ID[question_id].raw_text` and calls `call_text(raw_text, system=_SYSTEM_PROMPT)`.
Reimplementing that inside `IntakeService` would require duplicating `node1_intake._SYSTEM_PROMPT`
and `_QUESTIONS_BY_ID` — both underscore-private module internals, not part of the module's
declared public API (the module docstring's "Public API" list does not include them). Duplicating
private constants risks the API's phrasing drifting from the CLI's tone over time (two copies of
the same persona string, edited independently). Passing `phrase_fn=None` guarantees byte-for-byte
identical phrasing behavior to `run_pipeline.py`'s conversational flow, for free, and matches
`api_design.md`'s own stated goal that "the existing intake unit tests and
`tests/e2e/test_full_pipeline.py` prove parity" (§2).

**Consequence — confirming the "only place" requirement:** with this decision, `IntakeService`
makes **zero direct LLM calls** for phrasing anywhere in its own code. Phrasing is delegated
entirely to `node1_intake._default_phrase_fn` (`call_text` + `_SYSTEM_PROMPT` +
`QUESTION_SEQUENCE` raw text), invoked only from inside the two executor-wrapped calls identified
in §7 (`intake_begin` inside `create_session`, `intake_step` inside `submit_answer` — `intake_step`
internally re-invokes `intake_begin` for the next question, so no separate phrasing call site
exists in `submit_answer` either). `IntakeService` never imports `call_text`/`call_structured`
from `agents.llm` directly, and no other method (`get_snapshot`, `lock_early`, `abandon`) touches
the LLM at all.

**Flag for later (not resolved here):** if a future need arises to wrap phrasing calls with a
timeout (see §8, item 1), the clean seam is adding a `timeout=` parameter to `call_text` itself in
`agents/llm/client.py` — not `IntakeService` inventing a second phrase path that duplicates private
constants from `node1_intake.py`.

---

## 3. Per-session lock enforcement

`SessionRecord.lock` (an `asyncio.Lock`, one per record, created via
`field(default_factory=asyncio.Lock)`) exists today but nothing uses it yet. This plan specifies:

- **Checked in exactly two methods:** `submit_answer` and `lock_early` — both mutate stored state
  via a "turn." **Never checked** in `get_snapshot` (pure read — no mutation to guard) or
  `abandon` (delete, not mutate — see §8, item 8 for the accepted race this implies).
- **Pattern** (identical in both methods):
  ```python
  if record.lock.locked():
      raise TurnInProgressError
  async with record.lock:
      ...
  ```
  This must **fail fast**, never queue: a second concurrent request to the same session while one
  is in flight gets an immediate `TurnInProgressError`, never a blocked `await` on a contended
  lock.
- **Race safety — no `await` between the check and the acquire:** `record.lock.locked()` is a
  synchronous, non-suspending call, immediately followed (same coroutine, no intervening `await`
  statement) by `async with record.lock:`. Two points worth being explicit about:
  - The `.locked()` pre-check is **not** what prevents two requests from mutating the same session
    concurrently — asyncio is single-threaded and cooperative, so no other coroutine can run
    between the `.locked()` check and the `async with` line regardless of the pre-check. The
    pre-check exists purely to deliver **fail-fast** behavior: without it, a second concurrent
    call would silently block on `acquire()` (queueing) instead of rejecting immediately with
    `409 TURN_IN_PROGRESS`.
  - `asyncio.Lock.acquire()`'s fast path — lock currently free — sets `_locked = True` and returns
    without ever suspending back to the event loop. So even the `async with record.lock:`
    statement itself introduces no window where another coroutine could interleave between
    "we decided to proceed" and "we hold the lock." This is safe by construction on a single
    event loop; nothing in this design depends on the pre-check for correctness, only for UX.
- **Why the same lock instance is guaranteed across requests:** `InMemorySessionStore.get()`
  returns the *same* `SessionRecord` object from its internal `_sessions` dict on every call (not
  a copy), and `update()` mutates that same object in place (`record.state = state; record.status
  = status`) rather than replacing it in the dict. `record.lock` is therefore the one persistent
  lock instance for a given session across every request that reaches it — this is what makes the
  check-then-lock pattern actually enforce cross-request mutual exclusion. This is a current
  implementation detail of `InMemorySessionStore`, not something the abstract `SessionStore`
  interface promises in writing — worth the implementer's awareness if a future backend (Redis,
  Postgres) replaces it, since a per-process `asyncio.Lock` cannot cross process boundaries.

---

## 4. Atomicity rule

Per `api_design.md` §5: *"a turn's state mutation is committed to the store only after the full
`intake_step` succeeds; any LLM failure/timeout leaves the previous state intact so the client can
retry the same answer safely."* This section pins down exactly where that boundary sits, grounded
in one critical fact from reading the real code: **`intake_step` mutates its `IntakeSessionState`
argument in place** (its own docstring: *"Mutates state in place and returns it"* — it appends to
`state.history`, reassigns `state.brief`, updates `state.asked_so_far`, etc., all on the object it
was handed).

This means calling `intake_step(record.state, answer, None)` directly — operating on the object
that IS the store's copy — would be wrong for atomicity even though the method's return signature
looks like a pure transform: if it raises partway through (specifically, inside the internal
`intake_begin` call it makes for the *next* question, after this turn's answer has already been
merged into `state.brief`), the store's object would already carry this turn's mutations despite
the method as a whole having failed. There would be nothing to roll back to.

**The fix, applied in §1's `submit_answer` and `lock_early`:** `IntakeService` never hands
`record.state` itself to `intake_step`/`lock_brief`. It first takes
`working_state = record.state.model_copy(deep=True)` (a Pydantic v2 deep copy — cheap, and correct
here since `UserBuildBrief` and `IntakeSessionState` are both Pydantic models with no unpicklable
fields), mutates `working_state`, and only writes back via
`store.update(session_id, working_state, status)` if the mutation succeeded. `record.state` in the
store is never touched by a failed turn, because the failed turn was never operating on it.

**Exceptions that must trigger non-commit** (caught around the `intake_step` executor call in
`submit_answer`, translated to `LlmUpstreamError`, and must never reach `store.update`):

- **Raw `openai.OpenAIError`** (and its subclasses — `APIConnectionError`, `RateLimitError`,
  `APITimeoutError`, `APIStatusError`, etc.) — the realistic failure mode, from either of two call
  sites inside `intake_step`: `call_text` (phrasing the next question — has **no error handling at
  all** in `client.py`), or the inner `client.chat.completions.create(...)` call inside
  `call_structured` (extraction) — that call is **not** wrapped in any try/except in
  `call_structured`; only the subsequent `json.loads`/`response_model.model_validate` steps are,
  so a connection-level failure during extraction propagates raw too, before
  `call_structured`'s own retry loop even has a chance to engage.
- **`StructuredCallError`** — caught defensively even though it should never actually reach this
  boundary in practice: `extract_turn` already swallows it internally
  (`except StructuredCallError: return current_brief` — the brief is returned unchanged, the turn
  effectively becomes "same question again," not a raised exception). Handling it anyway costs
  nothing and guards against a future core change that stops swallowing it.

**Not a non-commit case:** `store.update()` returning `None` (the session expired mid-turn, e.g. a
very slow LLM call outlived the 30-minute `ASKING` TTL). That's handled separately as
`SessionNotFoundError` in §1 step 2d — nothing about the LLM call itself failed, so it doesn't
belong in the `LlmUpstreamError` family.

---

## 5. Exception taxonomy

Plain-Python exceptions only — `IntakeService` and this module never import `fastapi` or reference
HTTP status codes. The route layer (not built yet) maps each to the codes already specified in
`api_design.md` §7.

```python
class IntakeServiceError(Exception):
    """Base for all IntakeService-raised exceptions."""


class SessionNotFoundError(IntakeServiceError):
    """Session id is unknown or has expired.

    Covers BOTH cases — SessionStore.get()/peek() cannot currently distinguish
    "never existed" from "expired" (both return None). Matches api_design.md
    §7's own mapping, which sends both to the same 404 SESSION_NOT_FOUND.
    """


class SessionAlreadyLockedError(IntakeServiceError):
    """The session is already status="locked"; no further turns/lock-attempts accepted."""


class TurnInProgressError(IntakeServiceError):
    """A concurrent request is already mutating this session (record.lock held);
    fail-fast, caller should retry shortly — never silently queued."""


class BriefFloorNotMetError(IntakeServiceError):
    """lock_early called before floor_met(brief) — budget and/or primary use case
    are still unanswered."""
    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(f"Floor not met, missing: {missing}")


class LlmUpstreamError(IntakeServiceError):
    """An LLM call inside intake_begin/intake_step failed: a raw openai.OpenAIError
    or (defensively) a StructuredCallError. Session state was not mutated —
    the caller can safely resubmit the same answer."""
    def __init__(self, cause: Exception) -> None:
        self.cause = cause
        super().__init__(f"LLM call failed: {type(cause).__name__}: {cause}")
```

Mapping for the future route layer (already specified in `api_design.md` §7, restated for
completeness): `SessionNotFoundError` → `404`, `SessionAlreadyLockedError` → `409`,
`TurnInProgressError` → `409` (retryable), `BriefFloorNotMetError` → `409` (with
`details.missing`), `LlmUpstreamError` → `502` (retryable — state unchanged, safe to resubmit).

**Deliberately not included:** a separate `SessionExpiredError`. See §8, item 4.

---

## 6. GET snapshot behavior

**Confirmed problem, not hypothetical.** `SessionStore.get()` unconditionally refreshes
`last_accessed_at` on every call, including reads — verified directly in
`tests/test_session_store.py::test_get_on_live_session_refreshes_last_accessed_at`:
```python
async def test_get_on_live_session_refreshes_last_accessed_at(clock):
    ...
    fetched = await store.get(record.session_id)
    assert fetched.last_accessed_at == clock.now
```
A client polling a snapshot endpoint (`get_snapshot` in §1) using `get()` as-is would keep an
otherwise-abandoned session alive indefinitely, defeating the 30-minute `ASKING` TTL's entire
purpose — the exact risk flagged in earlier project notes.

**Proposed fix — a new `SessionStore` method (interface addition, not implemented in this pass):**

```python
class SessionStore(ABC):
    @abstractmethod
    async def peek(self, session_id: str) -> SessionRecord | None:
        """Same lazy-expiry semantics as get() (evicts and returns None if
        expired), but never updates last_accessed_at on a hit. For read-only
        snapshot access that must not extend the session's life."""
```

`InMemorySessionStore.peek()` would be implemented identically to `get()` except the
`record.last_accessed_at = now` line is omitted on the hit path — expiry detection and eviction of
an already-stale record on a hit stays exactly as `get()` does today (a `peek()` on an expired
session must still evict it, not just report `None` while leaving a dead entry behind).

`IntakeService.get_snapshot()` (§1) must call `peek()`, never `get()`. `submit_answer` and
`lock_early` deliberately keep using `get()` — those represent genuine session activity and
*should* slide the TTL; only the pure-read path needs the non-refreshing variant. This asymmetry
(three methods refresh, one doesn't) is intentional and should be preserved, not "simplified" into
one shared read path later.

This is a required companion change to `session_store.py` before `get_snapshot` can be
implemented correctly — explicitly out of scope for this task (which touches no `.py` files) and
called out again in §8, item 6.

---

## 7. Async execution

`intake_begin`/`intake_step` are synchronous core functions; `IntakeService` methods are called
from async route handlers. Exactly **two** call sites need `run_in_executor` — every other core
call `IntakeService` uses is pure Python with no IO and is safe to call inline on the event loop:

| Call | In method | Needs executor? | Why |
|---|---|---|---|
| `blank_brief(...)` | `create_session` | No | Pydantic model construction only, no IO |
| `intake_begin(state, phrase_fn=None)` | `create_session` | **Yes** | phrases the first question via `call_text` |
| `intake_step(state, answer, phrase_fn=None)` | `submit_answer` | **Yes** | runs `extract_turn` (`call_structured`) and internally re-invokes `intake_begin` (`call_text`) for the next question — both LLM calls happen inside this one synchronous call |
| `floor_met(brief)` | `lock_early`; internally referenced by `submit_answer`'s reasoning in §1 | No | two attribute comparisons, no IO |
| `lock_brief(brief)` | `lock_early`, `submit_answer`'s exhaustion path | No | pure dict transform via `model_dump`/`model_validate`, no IO |

Pattern:
```python
loop = asyncio.get_running_loop()   # not the deprecated get_event_loop()
state, question = await loop.run_in_executor(None, intake_begin, state, None)
# or, where more than 2-3 positional args make functools.partial clearer:
state, question, locked = await loop.run_in_executor(
    None, functools.partial(intake_step, working_state, answer, None)
)
```
`run_in_executor` does not accept keyword arguments directly, hence `functools.partial` (or a
lambda) for the multi-argument `intake_step` call.

**No sync core call is ever awaited directly on the event loop.** `submit_answer` must call
`intake_step` **exactly once** per turn — it already internally chains to `intake_begin` for the
next question's phrasing, so `IntakeService` must never separately re-invoke `intake_begin`
afterward (that would double-phrase and make a redundant LLM call). The default executor (`None` →
the process-wide `ThreadPoolExecutor`) is sufficient for intake traffic; no dedicated pool is
needed here, unlike the build job registry's likely dedicated executor (`api_design.md` §5/§9).

---

## 8. Open questions / risks to flag (not resolved here)

1. **No LLM timeout exists yet.** Verified directly in `agents/llm/client.py`: neither
   `call_structured` nor `call_text` pass a `timeout=` argument to the OpenAI client
   (`client.chat.completions.create(...)` is called with no timeout in either function).
   `IntakeService` inherits this as a hard dependency — a hung phrase or extract call currently
   has no bound at all. Even once the API layer adds its own `KARMA_INTAKE_TURN_TIMEOUT_S` wrapper
   (`asyncio.wait_for` around the executor future, per `api_design.md` §9 — not built yet), that
   can only stop *awaiting* the future on the event-loop side; the underlying blocking OpenAI HTTP
   call keeps running in its thread-pool slot until it eventually resolves or the SDK/OS times it
   out independently — a leaked worker thread, not a leaked request. The correct fix is a
   `timeout=` parameter on `call_structured`/`call_text` in core — explicitly **not** attempted in
   this task.

2. ~~Sequence exhaustion without "done"/"stop"~~ — **resolved, not open.** Confirmed product
   decision, already baked into §1's `submit_answer` contract: answering all 13
   `QUESTION_SEQUENCE` questions **is** completion. `IntakeService` auto-locks the moment
   `intake_step` reports the sequence exhausted (`question is None`, `locked` still `False`),
   mirroring `drive_intake`'s own end-of-loop finalization. No further confirmation step, no
   `floor_met` re-check, and no alternate "awaiting confirmation" status — 13/13 answered means
   nothing is missing.

3. **`api_design.md`'s §7 error table cites the wrong exception for phrasing failures.** It lists
   `StructuredCallError` as the source of `502 LLM_UPSTREAM_ERROR` during an intake turn. Per §4
   above, `StructuredCallError` is swallowed internally by `extract_turn` and should essentially
   never reach `IntakeService`; the real failure mode is a raw `openai.OpenAIError` subclass, from
   either `call_text` (phrasing — no error handling at all) or the inner, unwrapped API call
   inside `call_structured` (extraction, before its own retry logic engages). `LlmUpstreamError`'s
   handling in §4/§5 is built around the real failure mode, not `api_design.md`'s stated one.

4. **`SessionStore` cannot distinguish "never existed" from "expired."** Both `get()` and the
   proposed `peek()` (§6) return `None` either way — there is no reason code. If product later
   wants a different user-facing message ("your session timed out" vs "invalid session link"),
   `SessionStore` needs a richer return type (a tri-state, or an explicit reason). Deliberately
   deferred; §5's `SessionNotFoundError` collapses both cases into one, matching `api_design.md`'s
   own single `404` mapping for both.

5. **`api_design.md`'s §2 `IntakeSessionState` sketch omits real fields.** The actual class also
   carries `pending_open_question_field: str | None` and `current_question_id: str | None` (both
   absent from the doc's sketch), and uses `asked_so_far: list[str]` rather than `set[str]` — the
   real docstring explains this is deliberate, purely for JSON-serializability (`model_dump(mode=
   "json")` round-tripping), with membership semantics preserved by manual dedup on append. Not a
   bug; confirms the doc's §2 sketch predates the real implementation and should not be read as
   the literal contract — this plan's §1 is.

6. **`peek()` does not exist on `SessionStore` yet** (§6) — a required companion interface change
   before `get_snapshot` can be implemented as specified. Not built in this task, per the
   instruction to leave `session_store.py` untouched; flagged here as a hard prerequisite for
   whoever implements §1.

7. **Session can expire mid-turn.** If an LLM call inside `submit_answer` runs long enough that the
   session's `ASKING` TTL (30 min default) lapses before `store.update()` is reached,
   `store.update()` returns `None` and `IntakeService` raises `SessionNotFoundError` per §1 step
   2d — the user's answer is silently lost from the session's perspective (though the LLM work
   itself wasn't wasted from a cost standpoint, just its result). Acceptable given the TTL is 30
   minutes and turns are expected to take single-digit seconds; noted so it isn't mistaken for a
   bug during testing with an artificially short TTL.

8. **`abandon()` does not check `record.lock`.** If `submit_answer` is mid-flight (lock held,
   awaiting an LLM call) when `abandon()` runs concurrently, `store.delete()` succeeds
   immediately; the in-flight turn's later `store.update()` call then returns `None` and correctly
   raises `SessionNotFoundError` (per item 7's same mechanism) rather than crashing or silently
   resurrecting a deleted session. This is arguably the right behavior for an explicit
   user-initiated abandon (their intent was "stop this"), not a bug to design further around —
   flagged for the implementer's awareness, not as something requiring a fix.

9. **UUID minting for `blank_brief`.** The real signature requires `brief_id`, `user_id`,
   `chat_id` as caller-supplied `UUID` values — `blank_brief` does not default or generate them
   itself. §1's `create_session` mints all three as fresh `uuid4()` per session, consistent with
   `api_design.md` §9's own note that these are "currently random placeholders per session" until
   real per-user auth lands and populates `user_id` from an authenticated principal. Not a
   discrepancy, just stated explicitly since it's an easy detail to miss when implementing.
