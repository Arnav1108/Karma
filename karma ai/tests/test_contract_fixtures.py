"""Phase 6 Step 4 — round-trip every example fixture through its real DTO.

Each file under api/contract/fixtures/ is loaded from disk and validated against the
Pydantic model the section-4 table says it represents (docs/frontend_contract_plan.md
section 7). This is a genuine model_validate() — a fixture that drifts from the real
schema (renamed/removed required field, hand-edit) fails here.

The mapping below is maintained independently of the generator script on purpose, so
the test is a real external check on the committed files, not a mirror of how they were
produced.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from api.dtos import (
    AnswerAskingResponse,
    AnswerLockedResponse,
    BuildAcceptedDTO,
    BuildStatusResponse,
    CreateSessionRequest,
    CreateSessionResponse,
    ErrorEnvelope,
    LockResponse,
    SnapshotResponse,
    SubmitAnswerRequest,
)

_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "api" / "contract" / "fixtures"

_TRANSCRIPT_REL = "chat/conversation.transcript.json"

# Explicit path -> DTO map for the single-object fixtures. errors/*.json all map to
# ErrorEnvelope and are handled by prefix below; the transcript is handled separately.
_FIXTURE_DTOS: dict[str, type[BaseModel]] = {
    "chat/create_session.response.json": CreateSessionResponse,
    "chat/submit_answer.request.json": SubmitAnswerRequest,
    "chat/answer_asking.response.json": AnswerAskingResponse,
    "chat/answer_clarification.response.json": AnswerAskingResponse,
    "chat/answer_confirm_default.response.json": AnswerAskingResponse,
    "review/answer_locked.response.json": AnswerLockedResponse,
    "review/lock.response.json": LockResponse,
    "review/snapshot_asking.response.json": SnapshotResponse,
    "review/snapshot_locked.response.json": SnapshotResponse,
    "progress/build_accepted.response.json": BuildAcceptedDTO,
    "progress/build_status_queued.json": BuildStatusResponse,
    "progress/build_status_running.json": BuildStatusResponse,
    "result/build_status_succeeded.json": BuildStatusResponse,
    "result/build_status_infeasible.json": BuildStatusResponse,
    "result/build_status_cannot_proceed.json": BuildStatusResponse,
    "result/build_status_failed.json": BuildStatusResponse,
}


def _rel(path: Path) -> str:
    return path.relative_to(_FIXTURES_DIR).as_posix()


def _all_fixture_files() -> list[Path]:
    return sorted(_FIXTURES_DIR.rglob("*.json"))


def _dto_for(rel: str) -> type[BaseModel]:
    if rel.startswith("errors/"):
        return ErrorEnvelope
    return _FIXTURE_DTOS[rel]


def test_fixtures_directory_is_populated():
    files = _all_fixture_files()
    assert len(files) >= 31, f"expected the full section-4 fixture set, found {len(files)}"


def test_every_fixture_is_mapped_to_a_dto():
    # Guards against a new fixture landing with no DTO mapping — which would otherwise
    # slip through the parametrized round-trip below unexercised.
    unmapped = [
        _rel(p)
        for p in _all_fixture_files()
        if _rel(p) != _TRANSCRIPT_REL and not (_rel(p).startswith("errors/") or _rel(p) in _FIXTURE_DTOS)
    ]
    assert unmapped == [], f"fixtures with no DTO mapping: {unmapped}"


@pytest.mark.parametrize(
    "path",
    [p for p in _all_fixture_files() if _rel(p) != _TRANSCRIPT_REL],
    ids=lambda p: _rel(p),
)
def test_fixture_round_trips_through_its_dto(path):
    rel = _rel(path)
    dto = _dto_for(rel)
    data = json.loads(path.read_text(encoding="utf-8"))

    model = dto.model_validate(data)  # real validation; raises on schema drift

    # Round-trip: the model re-serializes to JSON-compatible data without error.
    assert isinstance(model.model_dump(mode="json"), dict)


def test_build_status_variants_carry_the_right_discriminated_fields():
    """Non-vacuous checks that each result-screen fixture models a distinct outcome."""
    def load(rel: str) -> BuildStatusResponse:
        return BuildStatusResponse.model_validate(
            json.loads((_FIXTURES_DIR / rel).read_text(encoding="utf-8"))
        )

    succeeded = load("result/build_status_succeeded.json")
    assert succeeded.status == "succeeded"
    assert succeeded.build is not None and succeeded.build.parts
    assert succeeded.error is None

    infeasible = load("result/build_status_infeasible.json")
    assert infeasible.status == "infeasible"
    assert infeasible.verdict is not None and infeasible.verdict.verdict == "impossible"
    assert infeasible.build is None

    cannot = load("result/build_status_cannot_proceed.json")
    assert cannot.status == "cannot_proceed" and cannot.reason
    assert cannot.build is None and cannot.error is None

    failed = load("result/build_status_failed.json")
    assert failed.status == "failed"
    assert failed.error is not None and failed.error.code == "BUILD_TIMEOUT"


def test_error_fixtures_cover_the_full_transport_catalog():
    """Every §5a transport error code has an ErrorEnvelope fixture, and each parses."""
    expected_codes = {
        "VALIDATION_ERROR", "UNAUTHORIZED", "SESSION_NOT_FOUND", "SESSION_ALREADY_LOCKED",
        "TURN_IN_PROGRESS", "BRIEF_FLOOR_NOT_MET", "BRIEF_NOT_LOCKED", "BUILD_ALREADY_ACTIVE",
        "BUILD_NOT_FOUND", "BUILD_CAPACITY", "RATE_LIMITED", "LLM_UPSTREAM_ERROR",
        "DATABASE_UNAVAILABLE", "INTERNAL_ERROR",
    }
    seen = set()
    for path in (_FIXTURES_DIR / "errors").glob("*.json"):
        env = ErrorEnvelope.model_validate(json.loads(path.read_text(encoding="utf-8")))
        seen.add(env.error.code)
        assert isinstance(env.error.retryable, bool)
    assert seen == expected_codes, f"missing={expected_codes - seen}, extra={seen - expected_codes}"


def test_conversation_transcript_turns_validate_turn_by_turn():
    turns = json.loads((_FIXTURES_DIR / _TRANSCRIPT_REL).read_text(encoding="utf-8"))
    assert len(turns) >= 2

    # First turn: session creation.
    CreateSessionRequest.model_validate(turns[0]["request"])
    CreateSessionResponse.model_validate(turns[0]["response"])

    # Middle turns: answer -> asking; final turn: answer -> locked.
    for turn in turns[1:]:
        SubmitAnswerRequest.model_validate(turn["request"])

    for turn in turns[1:-1]:
        AnswerAskingResponse.model_validate(turn["response"])

    last = AnswerLockedResponse.model_validate(turns[-1]["response"])
    assert last.status == "locked" and last.brief_summary is not None
