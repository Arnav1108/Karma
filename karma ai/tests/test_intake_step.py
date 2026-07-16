"""Unit coverage for the resumable per-turn intake primitives.

intake_begin()/intake_step() carry the loop logic drive_intake() used to hold in
loop locals (asked_so_far / opportunistic-fill bookkeeping, the open-question
attempts machine, history ordering, current_question_id across the ask→answer
gap). These tests drive them per-turn — including a full JSON round-trip of
IntakeSessionState mid-conversation, the property an HTTP API session store
depends on — and assert the state transitions and the locked flag directly.

Hermetic — no LLM, Postgres, or Neo4j involved. call_structured is monkeypatched
per turn (same pattern as tests/test_node1_intake.py); sequence-question phrasing
is stubbed via phrase_fn so call_text is never reached.
"""

from __future__ import annotations

import uuid

import pytest

from agents.nodes.node1_intake import (
    IntakeSessionState,
    _IntakeDelta,
    blank_brief,
    floor_met,
    intake_begin,
    intake_step,
)
from agents.schemas.brief import Budget, Purpose, SoftwareEntry, UserBuildBrief
from agents.schemas.source_flag import SourceFlag


def _blank() -> UserBuildBrief:
    return blank_brief(brief_id=uuid.uuid4(), user_id=uuid.uuid4(), chat_id=uuid.uuid4())


def _phrase(question_id: str) -> str:
    """Deterministic offline stand-in for the phrasing LLM call."""
    return f"Q[{question_id}]"


def _set_delta(monkeypatch, delta: _IntakeDelta) -> None:
    monkeypatch.setattr(
        "agents.nodes.node1_intake.call_structured",
        lambda prompt, model, system=None: delta,
    )


def _forbid_llm(monkeypatch) -> None:
    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("call_structured must not be reached on this turn")
    monkeypatch.setattr("agents.nodes.node1_intake.call_structured", _boom)


# ---------------------------------------------------------------------------
# Sequence walk: merge, bookkeeping, opportunistic fill, JSON resume, done-lock
# ---------------------------------------------------------------------------

def test_intake_step_multi_turn_merge_bookkeeping_and_lock(monkeypatch) -> None:
    state = IntakeSessionState(brief=_blank())

    # Begin: first sequence question is budget; served + recorded before any answer.
    state, q1 = intake_begin(state, phrase_fn=_phrase)
    assert q1 is not None
    assert q1.question_id == "budget"
    assert q1.kind == "sequence"
    assert q1.text == "Q[budget]"
    assert state.current_question_id == "budget"
    assert state.history == [{"role": "assistant", "content": "Q[budget]"}]

    # Turn 1 — budget answer merges into the brief; bookkeeping advances.
    _set_delta(
        monkeypatch,
        _IntakeDelta(
            budget=Budget(
                comfortable_min=60000, comfortable_max=90000,
                ceiling=100000, scope="pc_only",
            )
        ),
    )
    state, q2, locked = intake_step(state, "60 to 90k, ceiling 1 lakh", phrase_fn=_phrase)
    assert locked is False
    assert state.brief.budget.comfortable_max == 90000        # merge applied
    assert state.asked_so_far == ["budget"]                   # answered id recorded
    assert state.current_question_id == "primary_use_case"    # consumed then re-set
    assert q2 is not None and q2.question_id == "primary_use_case"
    # History ordering: question, answer, next question — nothing else.
    assert state.history == [
        {"role": "assistant", "content": "Q[budget]"},
        {"role": "user", "content": "60 to 90k, ceiling 1 lakh"},
        {"role": "assistant", "content": "Q[primary_use_case]"},
    ]

    # Mid-conversation JSON round-trip — the resume property an API session
    # store relies on. The revived state must continue identically.
    state = IntakeSessionState.model_validate_json(state.model_dump_json())
    assert state.current_question_id == "primary_use_case"
    assert state.asked_so_far == ["budget"]

    # Turn 2 — purpose answer that also volunteers software (opportunistic fill):
    # both sections must land in asked_so_far, so the software question is skipped
    # and the next question served is performance.
    _set_delta(
        monkeypatch,
        _IntakeDelta(
            purpose=Purpose(primary_use_case="gaming", sub_case="1080p esports"),
            software=[
                SoftwareEntry(
                    name="Valorant", category="game",
                    frequency="primary", intensity="moderate",
                )
            ],
        ),
    )
    state, q3, locked = intake_step(
        state, "gaming — mostly Valorant at 1080p", phrase_fn=_phrase
    )
    assert locked is False
    assert state.brief.purpose.sub_case == "1080p esports"
    assert [s.name for s in state.brief.software] == ["Valorant"]
    assert state.asked_so_far == ["budget", "primary_use_case", "software"]
    assert q3 is not None and q3.question_id == "performance"
    assert floor_met(state.brief) is True

    # Turn 3 — "done" with the floor met: extract_turn's early-exit locks with
    # NO LLM call (call_structured is rigged to fail the test if reached) and
    # no further question is phrased.
    _forbid_llm(monkeypatch)
    state, q4, locked = intake_step(state, "done", phrase_fn=_phrase)
    assert locked is True
    assert q4 is None
    assert state.brief.status == "locked"
    assert state.current_question_id is None
    # The final user answer is still recorded after extraction.
    assert state.history[-1] == {"role": "user", "content": "done"}


# ---------------------------------------------------------------------------
# Open-question path: clarification → confirm-to-default → accepted default
# ---------------------------------------------------------------------------

_CLARIFICATION = "Which resolution and refresh rate are you targeting?"


def _brief_with_floor_and_software() -> UserBuildBrief:
    brief = _blank()
    data = brief.model_dump()
    data["budget"].update(comfortable_min=50000, comfortable_max=90000, ceiling=100000)
    data["purpose"]["sub_case"] = "AAA gaming"
    data["software"] = [
        {"name": "Cyberpunk 2077", "category": "game",
         "frequency": "primary", "intensity": "heavy"}
    ]
    return UserBuildBrief.model_validate(data)


def test_intake_step_open_question_cycle(monkeypatch) -> None:
    state = IntakeSessionState(brief=_brief_with_floor_and_software())

    # Begin: budget/purpose/software are filled, so performance is served.
    state, q1 = intake_begin(state, phrase_fn=_phrase)
    assert q1 is not None and q1.question_id == "performance" and q1.kind == "sequence"

    # Ambiguous answer → clarification raised; it is served next, tagged with the
    # field it concerns (pending_open_question_field), ahead of the sequence.
    _set_delta(monkeypatch, _IntakeDelta(clarification_needed=_CLARIFICATION))
    state, q2, locked = intake_step(state, "I want it to look good", phrase_fn=_phrase)
    assert locked is False
    assert state.brief.open_questions == [_CLARIFICATION]
    assert state.pending_open_question_field == "performance"
    assert q2 is not None
    assert q2.kind == "clarification"
    assert q2.question_id == "performance"
    assert q2.text == _CLARIFICATION

    # Still ambiguous on the retry → attempts advance to 1 and the NEXT question
    # served is the confirm-to-default prompt.
    _set_delta(monkeypatch, _IntakeDelta(clarification_needed=_CLARIFICATION))
    state, q3, locked = intake_step(state, "just make it look nice", phrase_fn=_phrase)
    assert locked is False
    assert state.open_question_attempts == {_CLARIFICATION: 1}
    assert q3 is not None
    assert q3.kind == "confirm_default"
    assert q3.question_id == "performance"

    # "yes" to the confirm prompt short-circuits BEFORE any LLM extraction:
    # force-resolve applies the default with source=skipped_by_user, clears the
    # open question and its pending field, and the walk moves on to monitor.
    _forbid_llm(monkeypatch)
    state, q4, locked = intake_step(state, "yes", phrase_fn=_phrase)
    assert locked is False
    assert state.brief.open_questions == []
    assert state.pending_open_question_field is None
    assert state.open_question_attempts == {}
    assert state.brief.performance.source == SourceFlag.skipped_by_user
    assert q4 is not None
    assert q4.question_id == "monitor"
    assert q4.kind == "sequence"
