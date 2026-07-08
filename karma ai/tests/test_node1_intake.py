"""Regression tests for Node 1's exit-signal detection.

Bug: _EXIT_PATTERN was r"\b(done|stop)\b", a substring search anywhere in the
free-text answer. "I'm done gaming by 10pm" or "please stop asking about GPU"
would match "done"/"stop" as whole words mid-sentence and trigger an early
brief-lock in extract_turn whenever the floor was already met, even though the
user was mid-answer and never intended to end the interview.

Fix: _is_exit_signal now requires the ENTIRE stripped, lowercased answer to be
exactly "done" or "stop" — not a substring match anywhere in a sentence.

Hermetic — no LLM, Postgres, or Neo4j involved; _is_exit_signal is a pure regex
predicate.
"""

from __future__ import annotations

import uuid

import pytest

from agents.nodes.node1_intake import (
    _IntakeDelta,
    _is_exit_signal,
    blank_brief,
    extract_turn,
    next_question,
)
from agents.schemas.brief import Monitor, OwnedSpecs
from agents.schemas.source_flag import SourceFlag


@pytest.mark.parametrize("answer", ["done", "stop", "  Stop  ", "DONE", "  done"])
def test_exit_signal_matches_bare_answer(answer: str) -> None:
    assert _is_exit_signal(answer) is True


@pytest.mark.parametrize(
    "answer",
    [
        "I'm done gaming by 10pm",
        "please stop asking about GPU",
        "not done yet, still deciding on RAM",
        "stop it, I mean the RGB fans not the case",
    ],
)
def test_exit_signal_does_not_match_embedded_word(answer: str) -> None:
    assert _is_exit_signal(answer) is False


# ---------------------------------------------------------------------------
# open_questions / ask-if-ambiguous mechanism
# ---------------------------------------------------------------------------

def _blank():
    return blank_brief(brief_id=uuid.uuid4(), user_id=uuid.uuid4(), chat_id=uuid.uuid4())


_CLARIFICATION_TEXT = "Which resolution and refresh rate are you targeting?"


def test_ambiguous_answer_populates_open_questions_and_is_served_first(monkeypatch) -> None:
    brief = _blank()

    monkeypatch.setattr(
        "agents.nodes.node1_intake.call_structured",
        lambda prompt, model, system=None: _IntakeDelta(clarification_needed=_CLARIFICATION_TEXT),
    )

    brief = extract_turn("I want good graphics", brief, [], current_question_id="performance")

    assert brief.open_questions == [_CLARIFICATION_TEXT]
    # Served before any QUESTION_SEQUENCE item, regardless of asked_so_far state.
    assert next_question(brief, asked_so_far=set()) == _CLARIFICATION_TEXT

    # Now the user resolves it — LLM returns the field filled, no more ambiguity.
    from agents.schemas.brief import Performance

    monkeypatch.setattr(
        "agents.nodes.node1_intake.call_structured",
        lambda prompt, model, system=None: _IntakeDelta(
            performance=Performance(
                target_resolution="1440p",
                target_framerate=144,
                hdr_wanted=False,
                source=SourceFlag.user_stated,
            ),
            clarification_needed=None,
        ),
    )
    attempts: dict[str, int] = {}
    brief = extract_turn(
        "1440p at 144hz",
        brief,
        [],
        current_question_id="performance",
        open_question_attempts=attempts,
    )

    assert brief.open_questions == []
    assert brief.performance.source == SourceFlag.user_stated
    assert brief.performance.target_resolution == "1440p"
    assert attempts == {}


def test_still_ambiguous_then_confirm_yes_sets_skipped_by_user(monkeypatch) -> None:
    brief = _blank()

    monkeypatch.setattr(
        "agents.nodes.node1_intake.call_structured",
        lambda prompt, model, system=None: _IntakeDelta(clarification_needed=_CLARIFICATION_TEXT),
    )
    attempts: dict[str, int] = {}
    brief = extract_turn(
        "I want good graphics", brief, [], current_question_id="performance",
        open_question_attempts=attempts,
    )
    assert brief.open_questions == [_CLARIFICATION_TEXT]

    # Still ambiguous on the retry — LLM flags clarification again.
    brief = extract_turn(
        "just make it look nice", brief, [], current_question_id="performance",
        open_question_attempts=attempts,
    )
    assert brief.open_questions == [_CLARIFICATION_TEXT]
    assert attempts[_CLARIFICATION_TEXT] == 1
    assert next_question(brief, asked_so_far=set(), open_question_attempts=attempts) == (
        "I still can't pin this down — should I go with a sensible default and move on?"
    )

    # User accepts the default.
    brief = extract_turn(
        "yes", brief, [], current_question_id="performance", open_question_attempts=attempts,
    )
    assert brief.open_questions == []
    assert brief.performance.source == SourceFlag.skipped_by_user
    assert _CLARIFICATION_TEXT not in attempts


def test_confirm_no_grants_one_more_attempt_then_force_clears(monkeypatch) -> None:
    brief = _blank()

    monkeypatch.setattr(
        "agents.nodes.node1_intake.call_structured",
        lambda prompt, model, system=None: _IntakeDelta(clarification_needed=_CLARIFICATION_TEXT),
    )
    attempts: dict[str, int] = {}
    brief = extract_turn(
        "I want good graphics", brief, [], current_question_id="performance",
        open_question_attempts=attempts,
    )
    brief = extract_turn(
        "just make it look nice", brief, [], current_question_id="performance",
        open_question_attempts=attempts,
    )
    assert attempts[_CLARIFICATION_TEXT] == 1

    # User declines the default -> one more open-ended attempt granted.
    brief = extract_turn(
        "no", brief, [], current_question_id="performance", open_question_attempts=attempts,
    )
    assert brief.open_questions == [_CLARIFICATION_TEXT]
    assert attempts[_CLARIFICATION_TEXT] == 2
    # next_question re-asks the substantive question one last time, not the confirm.
    assert next_question(brief, asked_so_far=set(), open_question_attempts=attempts) == _CLARIFICATION_TEXT

    # Whatever the user says on this final attempt, it is force-cleared — proves
    # the loop terminates instead of asking indefinitely.
    brief = extract_turn(
        "still not sure honestly", brief, [], current_question_id="performance",
        open_question_attempts=attempts,
    )
    assert brief.open_questions == []
    assert brief.performance.source == SourceFlag.skipped_by_user
    assert attempts == {}


def test_inferred_source_set_via_heuristic_default(monkeypatch) -> None:
    """A field filled by a computed heuristic (not a direct user statement, and not
    the open-question skip path) gets source=inferred."""
    brief = _blank()

    monkeypatch.setattr(
        "agents.nodes.node1_intake.call_structured",
        lambda prompt, model, system=None: _IntakeDelta(
            monitor=Monitor(
                owned="yes",
                owned_specs=OwnedSpecs(
                    resolution="2560x1440", refresh_hz=144, hdr=False, size_inch=27.0,
                ),
                target_specs=None,
                count=1,
                source=SourceFlag.user_stated,
            ),
        ),
    )

    brief = extract_turn(
        "I already have a 1440p 144hz monitor", brief, [], current_question_id="monitor",
    )

    # The user answered the monitor question directly, not the performance one.
    assert brief.monitor.source == SourceFlag.user_stated
    assert brief.performance.source == SourceFlag.inferred
    assert brief.performance.target_resolution == "1440p"
    assert brief.performance.target_framerate == 144
