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


class BriefPersistenceError(IntakeServiceError):
    """The synchronous Postgres write of the newly-locked brief failed inside
    lock_early / submit_answer's auto-lock branch. In-memory session state is
    left unchanged (still "asking") — the caller may safely retry."""
    def __init__(self, cause: Exception) -> None:
        self.cause = cause
        super().__init__(f"Failed to persist locked brief: {type(cause).__name__}: {cause}")


class BuildServiceError(Exception):
    """Base for all BuildService-raised exceptions."""


class BriefNotLockedError(BuildServiceError):
    """Session exists but record.status != "locked" — the brief isn't ready to build."""


class BuildNotFoundError(BuildServiceError):
    """build_id is unknown or has been evicted (TTL/LRU) from the JobRegistry."""


class BuildCapacityError(BuildServiceError):
    """Active build count is already at max_concurrent; retryable — the caller
    should back off and retry rather than the request queueing invisibly."""


class BuildAlreadyActiveError(BuildServiceError):
    """A build for this session is already queued/running. Non-retryable — a
    blind re-POST would fail identically every time. Carries build_id of the
    already-active build so the client can switch to polling it instead."""
    def __init__(self, build_id: str) -> None:
        self.build_id = build_id
        super().__init__(f"A build is already active for this session: {build_id}")
