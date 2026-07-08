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

import pytest

from agents.nodes.node1_intake import _is_exit_signal


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
