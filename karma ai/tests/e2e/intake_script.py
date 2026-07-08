"""Canned answer script for the E2E pipeline test (tests/e2e/test_full_pipeline.py).

A fresh, purpose-built, comfortably-feasible mid-range gaming brief — deliberately
NOT derived from any existing data/fixtures/*.json:
  - budget_gamer sits at a calibrated ~1.04 min/target ratio (see
    feasibility/estimate.py's _TIGHT_RATIO commentary) — i.e. deliberately *tight*,
    which also activates the DDR4-bias selection path. Extra moving parts, and a
    verdict that sits close enough to the threshold that ordinary stock drift
    could flip it.
  - ml_workstation reuses a part (only 8 of 9 slots get selected); video_editor
    carries monitor-scope fixed-cost deductions. Both are real scenarios, but
    edge behavior belongs in the cheaper stage-level tests against those fixtures,
    not in the one expensive E2E spine.

Comfortable headroom (well above the calibrated tight ratio) keeps the
feasibility verdict stable under live catalog stock drift and makes a full 9/9
slot-fill a fair, low-flake assertion.

CANNED_ANSWERS is keyed by QUESTION_SEQUENCE question IDs (node1_intake.py) —
every field below is produced by the REAL extraction LLM call via
drive_intake()/extract_turn(), never hand-assembled into a UserBuildBrief
directly. Answers are supplied through "storage" explicitly (not relying on
next_question_id() to skip anything) so the script's control flow does not
depend on which field, if any, the LLM's opportunistic-fill (newly_filled_sections)
picks up early — if it fills a later field ahead of its own turn, drive_intake
naturally asks fewer questions, which is exactly the behavior
test_brief_gate's turn-count assertion checks for. Observed in a live run:
the "performance" question was the one skipped, opportunistically filled
from the primary_use_case answer's "high, stable frame rates" aside — not
the monitor/peripherals aside in the budget answer, which were still asked
individually. Either outcome is valid; the assertions don't depend on which
specific field gets skipped, only that at least one turn-count is saved.
Any question beyond "storage" gets "done", which extract_turn's early-exit path
(floor_met() is already True by then) locks immediately with no LLM call.
"""
from __future__ import annotations

from typing import Callable

CANNED_ANSWERS: dict[str, str] = {
    "budget": (
        "My comfortable budget is 85,000 to 95,000 rupees, with an absolute "
        "ceiling of 100,000 rupees. This is just for the PC — I already own a "
        "1080p 144Hz monitor, plus a keyboard and mouse, so none of those are "
        "needed."
    ),
    "primary_use_case": (
        "This is primarily for gaming — mostly competitive shooters, and I "
        "want high, stable frame rates."
    ),
    "software": "I mainly play Valorant and CS2, and sometimes GTA V.",
    "performance": (
        "I'm targeting 1080p at 144fps or higher. HDR isn't important to me."
    ),
    "monitor": (
        "I already have a 1080p 144Hz monitor with no HDR — I don't need a new one."
    ),
    "peripherals": (
        "I already have a keyboard and mouse I'm happy with — nothing needed there."
    ),
    "storage": "Around 512GB of fast NVMe storage is enough, mostly for games.",
}

# Any question not covered above (asked after "storage", or earlier if
# opportunistic-fill skips one of the entries below) gets this — floor_met()
# is guaranteed True by the time "budget" is answered, so extract_turn's
# "done"/"stop" early-exit locks the brief immediately, with no LLM call.
_EXIT_ANSWER = "done"


def make_answer_fn() -> Callable[[str, str], str]:
    """Return an answer_fn(question_id, question_text) -> str for drive_intake().

    Keys strictly by question ID, never by phrased wording, so the script is
    stable regardless of how a question is phrased — irrelevant when paired
    with a phrase_fn that skips the phrasing LLM call entirely (as
    test_full_pipeline.py does), but keeps this script reusable if a future
    caller wants the real phrased questions too.
    """
    def answer_fn(question_id: str, question_text: str) -> str:
        return CANNED_ANSWERS.get(question_id, _EXIT_ANSWER)
    return answer_fn
