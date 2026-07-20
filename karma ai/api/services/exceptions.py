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
