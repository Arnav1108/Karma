"""Generate the per-screen example fixtures under api/contract/fixtures/.

Each fixture is a real DTO instance serialized with model_dump_json(indent=2,
exclude_none=True), so it is parse-guaranteed by construction and re-validated by
tests/test_contract_fixtures.py. The frontend team imports these to build the chat /
review / progress / result screens against no live backend
(docs/frontend_contract_plan.md section 4).

Seeded from real data where it exists:
- BriefSummaryDTO comes from the actual api.mappers.map_brief_summary over the
  data/fixtures/budget_gamer.json brief, so it matches what the API really emits.
- The conversation transcript reuses tests/e2e/intake_script.py's CANNED_ANSWERS.

Timestamps are fixed literals (not "now") so regenerating produces no churn.

Usage (from `karma ai/`):
    python -m scripts.generate_contract_fixtures
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from agents.nodes.node1_intake import QUESTION_SEQUENCE
from agents.schemas.brief import UserBuildBrief
from pydantic import BaseModel

from api.dtos import (
    AnswerAskingResponse,
    AnswerLockedResponse,
    BuildAcceptedDTO,
    BuildCardDTO,
    BuildPartDTO,
    BuildStatusResponse,
    CreateSessionRequest,
    CreateSessionResponse,
    ErrorBody,
    ErrorEnvelope,
    LockResponse,
    ProgressDTO,
    QuestionDTO,
    SnapshotResponse,
    SubmitAnswerRequest,
    VerdictDTO,
)
from api.mappers import map_brief_summary

_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = _ROOT / "api" / "contract" / "fixtures"

# Fixed, deterministic sample values.
_SAMPLE_SESSION_ID = "3f9a1c2e-0000-4a00-8000-000000000001"
_SAMPLE_BUILD_ID = "b1d0c0de-0000-4b00-8000-000000000002"
_ASKING_EXPIRES = datetime(2026, 7, 21, 12, 30, 0, tzinfo=timezone.utc)
_LOCKED_EXPIRES = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


def _brief_summary():
    """The real mapper over a real brief fixture — matches production output."""
    brief = UserBuildBrief.model_validate_json(
        (_ROOT / "data" / "fixtures" / "budget_gamer.json").read_text(encoding="utf-8")
    )
    asked = [q.id for q in QUESTION_SEQUENCE]
    return map_brief_summary(brief, asked)


def _full_progress() -> ProgressDTO:
    return ProgressDTO(answered=len(QUESTION_SEQUENCE), total=len(QUESTION_SEQUENCE), floor_met=True)


def _sample_build_card() -> BuildCardDTO:
    return BuildCardDTO(
        parts=[
            BuildPartDTO(
                slot="gpu",
                product_id="GPU-4060-ASUS",
                name="ASUS Dual GeForce RTX 4060 8GB",
                brand="ASUS",
                price_inr=30500,
                justification="Cheapest in-stock 8GB card clearing the 1080p/144fps target.",
            ),
            BuildPartDTO(
                slot="cpu",
                product_id="CPU-7500F",
                name="AMD Ryzen 5 7500F",
                brand="AMD",
                price_inr=17000,
                justification="6c/12t, pairs with the AM5 board without bottlenecking the GPU.",
            ),
            BuildPartDTO(
                slot="motherboard",
                product_id="MB-B650M-PG",
                name="ASRock B650M-HDV/M.2",
                brand="ASRock",
                price_inr=11500,
                justification="Cheapest in-stock AM5 microATX board with DDR5 + NVMe.",
            ),
        ],
        total_price_inr=59000,
        summary="A tight 1080p/144fps competitive-gaming build on AM5 with room to upgrade.",
        warnings=["No compatible in-stock cooler under the ceiling — using the boxed AMD cooler."],
    )


def _sample_verdict() -> VerdictDTO:
    return VerdictDTO(
        verdict="tight",
        reason="Feasible, but the GPU floor consumes most of the budget headroom.",
        binding_constraint="gpu",
        suggested_adjustments=["Raise the ceiling by ~₹5,000 for a 12GB GPU."],
    )


def _infeasible_verdict() -> VerdictDTO:
    return VerdictDTO(
        verdict="impossible",
        reason="The cheapest compatible GPU meeting the VRAM floor already exceeds the ceiling.",
        binding_constraint="gpu",
        suggested_adjustments=[
            "Raise the budget ceiling to at least ₹75,000, or",
            "Drop the 12GB VRAM requirement to 8GB.",
        ],
    )


# ---------------------------------------------------------------------------
# Fixture set — (relative path, DTO instance). One entry per section-4 table row.
# ---------------------------------------------------------------------------

def _fixtures() -> list[tuple[str, BaseModel]]:
    summary = _brief_summary()
    progress = _full_progress()

    q_sequence = QuestionDTO(
        question_id="budget",
        text="Roughly what budget are you working with for this build?",
        kind="sequence",
    )
    q_next = QuestionDTO(
        question_id="performance",
        text="What resolution and frame rate are you aiming for?",
        kind="sequence",
    )
    q_clarify = QuestionDTO(
        question_id="software",
        text="When you say 'editing', do you mean photo, video, or 3D work?",
        kind="clarification",
    )
    q_confirm = QuestionDTO(
        question_id="operating_system",
        text="I'll assume Windows 11 unless you'd prefer Linux — is Windows fine?",
        kind="confirm_default",
    )

    create_resp = CreateSessionResponse(
        session_id=_SAMPLE_SESSION_ID,
        status="asking",
        question=q_sequence,
        progress=ProgressDTO(answered=0, total=len(QUESTION_SEQUENCE), floor_met=False),
        expires_at=_ASKING_EXPIRES,
    )
    answer_asking = AnswerAskingResponse(
        status="asking",
        question=q_next,
        progress=ProgressDTO(answered=1, total=len(QUESTION_SEQUENCE), floor_met=True),
        expires_at=_ASKING_EXPIRES,
    )
    answer_clarify = AnswerAskingResponse(
        status="asking",
        question=q_clarify,
        progress=ProgressDTO(answered=2, total=len(QUESTION_SEQUENCE), floor_met=True),
        expires_at=_ASKING_EXPIRES,
    )
    answer_confirm = AnswerAskingResponse(
        status="asking",
        question=q_confirm,
        progress=ProgressDTO(answered=7, total=len(QUESTION_SEQUENCE), floor_met=True),
        expires_at=_ASKING_EXPIRES,
    )
    answer_locked = AnswerLockedResponse(
        status="locked", brief_summary=summary, progress=progress
    )

    return [
        # Chat screen
        ("chat/create_session.response.json", create_resp),
        ("chat/submit_answer.request.json", SubmitAnswerRequest(answer="Around 90,000 rupees.")),
        ("chat/answer_asking.response.json", answer_asking),
        ("chat/answer_clarification.response.json", answer_clarify),
        ("chat/answer_confirm_default.response.json", answer_confirm),
        # Review screen
        ("review/answer_locked.response.json", answer_locked),
        ("review/lock.response.json", LockResponse(status="locked", brief_summary=summary)),
        (
            "review/snapshot_asking.response.json",
            SnapshotResponse(
                status="asking",
                question=q_next,
                progress=ProgressDTO(answered=1, total=len(QUESTION_SEQUENCE), floor_met=True),
                brief_summary=None,
                expires_at=_ASKING_EXPIRES,
            ),
        ),
        (
            "review/snapshot_locked.response.json",
            SnapshotResponse(
                status="locked",
                question=None,
                progress=progress,
                brief_summary=summary,
                expires_at=_LOCKED_EXPIRES,
            ),
        ),
        # Progress screen
        (
            "progress/build_accepted.response.json",
            BuildAcceptedDTO(build_id=_SAMPLE_BUILD_ID, status="queued", poll_after_ms=2000),
        ),
        (
            "progress/build_status_queued.json",
            BuildStatusResponse(build_id=_SAMPLE_BUILD_ID, status="queued", poll_after_ms=2000),
        ),
        (
            "progress/build_status_running.json",
            BuildStatusResponse(build_id=_SAMPLE_BUILD_ID, status="running", poll_after_ms=2000),
        ),
        # Result screen
        (
            "result/build_status_succeeded.json",
            BuildStatusResponse(
                build_id=_SAMPLE_BUILD_ID,
                status="succeeded",
                build=_sample_build_card(),
                verdict=_sample_verdict(),
            ),
        ),
        (
            "result/build_status_infeasible.json",
            BuildStatusResponse(
                build_id=_SAMPLE_BUILD_ID, status="infeasible", verdict=_infeasible_verdict()
            ),
        ),
        (
            "result/build_status_cannot_proceed.json",
            BuildStatusResponse(
                build_id=_SAMPLE_BUILD_ID,
                status="cannot_proceed",
                reason="Postgres was unreachable mid-run; no parts could be selected.",
            ),
        ),
        (
            "result/build_status_failed.json",
            BuildStatusResponse(
                build_id=_SAMPLE_BUILD_ID,
                status="failed",
                error=ErrorBody(
                    code="BUILD_TIMEOUT",
                    message="The build exceeded its time budget. Please retry.",
                    retryable=True,
                ),
            ),
        ),
    ] + _error_fixtures()


def _error_fixtures() -> list[tuple[str, BaseModel]]:
    """One ErrorEnvelope per transport error code (frontend_contract_plan.md §5a).

    Values mirror the exact code/message/retryable each handler in api/errors.py (and
    the normalized 401) produces, so these document the real body shapes.
    """

    def env(code, message, retryable, details=None):
        return ErrorEnvelope(
            error=ErrorBody(code=code, message=message, retryable=retryable, details=details)
        )

    catalog = [
        ("validation_error", env("VALIDATION_ERROR", "Request validation failed.", False)),
        ("unauthorized", env("UNAUTHORIZED", "Invalid or missing API key.", False)),
        ("session_not_found", env("SESSION_NOT_FOUND", "Session not found or has expired.", False)),
        ("session_already_locked", env("SESSION_ALREADY_LOCKED", "Session is already locked.", False)),
        (
            "turn_in_progress",
            env("TURN_IN_PROGRESS", "A turn is already in progress for this session. Retry shortly.", True),
        ),
        (
            "brief_floor_not_met",
            env(
                "BRIEF_FLOOR_NOT_MET",
                "Budget and/or primary use case must be answered before locking.",
                False,
                details={"missing": ["budget"]},
            ),
        ),
        (
            "brief_not_locked",
            env("BRIEF_NOT_LOCKED", "The session's brief is not locked yet; it cannot be built.", False),
        ),
        (
            "build_already_active",
            env(
                "BUILD_ALREADY_ACTIVE",
                "A build is already active for this session.",
                False,
                details={"build_id": _SAMPLE_BUILD_ID},
            ),
        ),
        ("build_not_found", env("BUILD_NOT_FOUND", "Build not found or has expired.", False)),
        (
            "build_capacity",
            env("BUILD_CAPACITY", "The build service is at capacity. Please retry shortly.", True),
        ),
        ("rate_limited", env("RATE_LIMITED", "Rate limit exceeded. Please retry later.", True)),
        (
            "llm_upstream_error",
            env("LLM_UPSTREAM_ERROR", "The upstream language model call failed. Please retry.", True),
        ),
        (
            "database_unavailable",
            env("DATABASE_UNAVAILABLE", "Failed to persist the locked brief. Please retry.", True),
        ),
        ("internal_error", env("INTERNAL_ERROR", "An internal error occurred.", False)),
    ]
    return [(f"errors/{name}.json", model) for name, model in catalog]


def _write(path: Path, model: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # exclude_none=True is the default (cleaner fixtures), but SnapshotResponse.question
    # and .brief_summary are required-but-nullable with no default — the real API returns
    # them as explicit null, and dropping them here would make the fixture fail to
    # re-validate. Keep nulls for SnapshotResponse only.
    exclude_none = not isinstance(model, SnapshotResponse)
    path.write_text(
        model.model_dump_json(indent=2, exclude_none=exclude_none) + "\n", encoding="utf-8"
    )


def _write_transcript() -> None:
    """A full chat transcript, seeded from intake_script.CANNED_ANSWERS.

    Shape: an ordered list of {request, response} turns — the session-create turn
    followed by one answer turn per canned answer, the last locking the brief. Each
    element's request/response is a real DTO dump, validated turn-by-turn by the
    round-trip test.
    """
    from tests.e2e.intake_script import CANNED_ANSWERS

    turns: list[dict] = [
        {
            "request": CreateSessionRequest(client_ref="web-demo").model_dump(exclude_none=True),
            "response": CreateSessionResponse(
                session_id=_SAMPLE_SESSION_ID,
                status="asking",
                question=QuestionDTO(
                    question_id="budget",
                    text="Roughly what budget are you working with?",
                    kind="sequence",
                ),
                progress=ProgressDTO(answered=0, total=len(QUESTION_SEQUENCE), floor_met=False),
                expires_at=_ASKING_EXPIRES,
            ).model_dump(mode="json", exclude_none=True),
        }
    ]

    answered_ids = list(CANNED_ANSWERS.keys())
    for i, qid in enumerate(answered_ids):
        answer_text = CANNED_ANSWERS[qid]
        is_last = i == len(answered_ids) - 1
        request = SubmitAnswerRequest(answer=answer_text).model_dump(exclude_none=True)
        if is_last:
            response = AnswerLockedResponse(
                status="locked", brief_summary=_brief_summary(), progress=_full_progress()
            ).model_dump(mode="json", exclude_none=True)
        else:
            next_qid = answered_ids[i + 1]
            response = AnswerAskingResponse(
                status="asking",
                question=QuestionDTO(
                    question_id=next_qid,
                    text=f"(next) tell me about your {next_qid.replace('_', ' ')}.",
                    kind="sequence",
                ),
                progress=ProgressDTO(
                    answered=i + 1, total=len(QUESTION_SEQUENCE), floor_met=True
                ),
                expires_at=_ASKING_EXPIRES,
            ).model_dump(mode="json", exclude_none=True)
        turns.append({"request": request, "response": response})

    import json

    path = FIXTURES_DIR / "chat" / "conversation.transcript.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(turns, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    count = 0
    for rel, model in _fixtures():
        _write(FIXTURES_DIR / rel, model)
        count += 1
    _write_transcript()
    count += 1
    print(f"wrote {count} fixtures under {FIXTURES_DIR}")


if __name__ == "__main__":
    main()
