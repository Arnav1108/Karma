"""Unit coverage for Node 1's core per-turn helpers.

Targets functions that had zero test coverage before this file: blank_brief,
newly_filled_sections, _merge_delta, _compute_completeness, next_question /
_is_field_filled (happy-path walk), and floor_met.

Hermetic — no LLM, Postgres, or Neo4j involved. next_question's single LLM call
(call_text) is monkeypatched to an identity function so the walk can assert on
question ordering without a network call.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from agents.nodes.node1_intake import (
    QUESTION_SEQUENCE,
    _compute_completeness,
    _IntakeDelta,
    _merge_delta,
    blank_brief,
    floor_met,
    newly_filled_sections,
    next_question,
)
from agents.schemas.brief import (
    Budget,
    Constraint,
    HardConstraints,
    Peripheral,
    SoftwareEntry,
)
from agents.schemas.source_flag import SourceFlag

BRIEF_ID = uuid4()
USER_ID = uuid4()
CHAT_ID = uuid4()


def _blank():
    return blank_brief(BRIEF_ID, USER_ID, CHAT_ID)


# ---------------------------------------------------------------------------
# blank_brief()
# ---------------------------------------------------------------------------

def test_blank_brief_sentinel_defaults() -> None:
    brief = _blank()

    assert brief.status == "draft"
    assert brief.open_questions == []
    assert brief.brief_id == BRIEF_ID
    assert brief.user_id == USER_ID
    assert brief.chat_id == CHAT_ID

    # Required-section sentinels: not yet answered.
    assert brief.budget.comfortable_max == 0
    assert brief.purpose.sub_case == ""

    # Source-flagged sections start as default_applied, not user_stated.
    assert brief.performance.source == SourceFlag.default_applied
    assert brief.monitor.source == SourceFlag.default_applied
    assert brief.storage.source == SourceFlag.default_applied
    assert brief.operating_system.source == SourceFlag.default_applied

    # Completeness computed correctly for a fully blank brief.
    assert brief.completeness.required_complete is False
    assert brief.completeness.optional_filled == 0
    assert brief.completeness.optional_skipped == 0


# ---------------------------------------------------------------------------
# newly_filled_sections()
# ---------------------------------------------------------------------------

def test_newly_filled_sections_no_changes_returns_empty() -> None:
    old = _blank()
    new = old.model_copy(deep=True)
    assert newly_filled_sections(old, new) == set()


def test_newly_filled_sections_budget() -> None:
    old = _blank()
    new = old.model_copy(
        update={"budget": old.budget.model_copy(update={"comfortable_max": 80000})}
    )
    assert newly_filled_sections(old, new) == {"budget"}


def test_newly_filled_sections_purpose() -> None:
    old = _blank()
    new = old.model_copy(
        update={"purpose": old.purpose.model_copy(update={"sub_case": "1440p esports"})}
    )
    assert newly_filled_sections(old, new) == {"primary_use_case"}


def test_newly_filled_sections_software() -> None:
    old = _blank()
    new = old.model_copy(
        update={
            "software": [
                SoftwareEntry(
                    name="Valorant", category="game", frequency="primary", intensity="moderate"
                )
            ]
        }
    )
    assert newly_filled_sections(old, new) == {"software"}


def test_newly_filled_sections_performance() -> None:
    old = _blank()
    new = old.model_copy(
        update={
            "performance": old.performance.model_copy(
                update={"target_resolution": "1440p", "source": SourceFlag.user_stated}
            )
        }
    )
    assert newly_filled_sections(old, new) == {"performance"}


def test_newly_filled_sections_monitor() -> None:
    old = _blank()
    new = old.model_copy(
        update={"monitor": old.monitor.model_copy(update={"owned": "yes"})}
    )
    assert newly_filled_sections(old, new) == {"monitor"}


def test_newly_filled_sections_peripherals() -> None:
    old = _blank()
    new = old.model_copy(
        update={"peripherals": [Peripheral(type="keyboard", requirements=None, priority="must_have")]}
    )
    assert newly_filled_sections(old, new) == {"peripherals"}


def test_newly_filled_sections_storage() -> None:
    old = _blank()
    new = old.model_copy(
        update={"storage": old.storage.model_copy(update={"capacity_gb": 2000})}
    )
    assert newly_filled_sections(old, new) == {"storage"}


def test_newly_filled_sections_operating_system() -> None:
    old = _blank()
    new = old.model_copy(
        update={"operating_system": old.operating_system.model_copy(update={"os": "linux"})}
    )
    assert newly_filled_sections(old, new) == {"operating_system"}


def test_newly_filled_sections_hard_constraints() -> None:
    old = _blank()
    constraint = Constraint(
        id=uuid4(),
        type="brand",
        value="must be AMD GPU",
        source="user_stated",
        locked_at=datetime.now(timezone.utc),
    )
    new = old.model_copy(
        update={
            "hard_constraints": old.hard_constraints.model_copy(
                update={"must_have": [constraint]}
            )
        }
    )
    assert newly_filled_sections(old, new) == {"hard_constraints"}


# ---------------------------------------------------------------------------
# _merge_delta()
# ---------------------------------------------------------------------------

def test_merge_delta_scalar_overwrite() -> None:
    brief = _blank()
    delta = _IntakeDelta(
        budget=Budget(
            comfortable_min=60000,
            comfortable_max=90000,
            ceiling=100000,
            scope="pc_only",
        )
    )
    merged = _merge_delta(brief, delta)
    assert merged.budget.comfortable_max == 90000
    assert merged.budget.comfortable_min == 60000
    assert merged.budget.ceiling == 100000


def test_merge_delta_software_extend_dedupe_by_name() -> None:
    brief = _blank()
    delta1 = _IntakeDelta(
        software=[
            SoftwareEntry(name="Photoshop", category="video", frequency="primary", intensity="heavy")
        ]
    )
    merged1 = _merge_delta(brief, delta1)
    assert [s.name for s in merged1.software] == ["Photoshop"]

    delta2 = _IntakeDelta(
        software=[
            SoftwareEntry(name="Photoshop", category="video", frequency="primary", intensity="heavy"),
            SoftwareEntry(name="Blender", category="3d", frequency="secondary", intensity="moderate"),
        ]
    )
    merged2 = _merge_delta(merged1, delta2)
    names = {s.name for s in merged2.software}
    assert names == {"Photoshop", "Blender"}
    assert len(merged2.software) == 2


def test_merge_delta_peripherals_extend_dedupe_by_type() -> None:
    brief = _blank()
    delta1 = _IntakeDelta(
        peripherals=[Peripheral(type="mouse", requirements=None, priority="must_have")]
    )
    merged1 = _merge_delta(brief, delta1)
    assert [p.type for p in merged1.peripherals] == ["mouse"]

    delta2 = _IntakeDelta(
        peripherals=[
            Peripheral(type="mouse", requirements="wireless", priority="nice_to_have"),
            Peripheral(type="keyboard", requirements=None, priority="must_have"),
        ]
    )
    merged2 = _merge_delta(merged1, delta2)
    types = {p.type for p in merged2.peripherals}
    assert types == {"mouse", "keyboard"}
    assert len(merged2.peripherals) == 2


def test_merge_delta_hard_constraints_append_only_across_turns() -> None:
    brief = _blank()
    first = Constraint(
        id=uuid4(), type="brand", value="AMD only", source="user_stated",
        locked_at=datetime.now(timezone.utc),
    )
    delta1 = _IntakeDelta(hard_constraints=HardConstraints(must_have=[first]))
    merged1 = _merge_delta(brief, delta1)
    assert len(merged1.hard_constraints.must_have) == 1

    # A turn that doesn't mention hard_constraints must never drop existing ones.
    delta_unrelated = _IntakeDelta(
        budget=Budget(comfortable_min=1, comfortable_max=2, ceiling=3, scope="pc_only")
    )
    merged2 = _merge_delta(merged1, delta_unrelated)
    assert len(merged2.hard_constraints.must_have) == 1
    assert merged2.hard_constraints.must_have[0].value == "AMD only"

    # A later turn adds a second constraint — it accumulates, doesn't replace.
    second = Constraint(
        id=uuid4(), type="budget", value="never exceed ceiling", source="user_stated",
        locked_at=datetime.now(timezone.utc),
    )
    delta3 = _IntakeDelta(hard_constraints=HardConstraints(must_have=[second]))
    merged3 = _merge_delta(merged2, delta3)
    assert len(merged3.hard_constraints.must_have) == 2
    values = {c.value for c in merged3.hard_constraints.must_have}
    assert values == {"AMD only", "never exceed ceiling"}


# ---------------------------------------------------------------------------
# _compute_completeness()
# ---------------------------------------------------------------------------

def test_compute_completeness_required_complete_tracks_floor_met() -> None:
    brief = _blank()
    assert _compute_completeness(brief).required_complete is False

    filled = brief.model_copy(
        update={
            "budget": brief.budget.model_copy(update={"comfortable_max": 50000}),
            "purpose": brief.purpose.model_copy(update={"sub_case": "1080p gaming"}),
        }
    )
    assert _compute_completeness(filled).required_complete is True
    assert _compute_completeness(filled).required_complete == floor_met(filled)


def test_compute_completeness_optional_filled_and_skipped_counts() -> None:
    brief = _blank()
    brief = brief.model_copy(
        update={
            "software": [
                SoftwareEntry(name="Blender", category="3d", frequency="primary", intensity="heavy")
            ],
            "performance": brief.performance.model_copy(update={"source": SourceFlag.user_stated}),
            # monitor left at default_applied -> not filled
            "storage": brief.storage.model_copy(update={"source": SourceFlag.user_stated}),
            # operating_system left at default_applied -> not filled
            # peripherals left empty -> not filled
        }
    )
    completeness = _compute_completeness(brief)
    # 11 optional ids total (13 questions minus budget, primary_use_case).
    # Filled: software, performance, storage = 3. The rest (8) are skipped.
    assert completeness.optional_filled == 3
    assert completeness.optional_skipped == 8
    assert completeness.optional_filled + completeness.optional_skipped == 11


# ---------------------------------------------------------------------------
# next_question() / _is_field_filled() — happy-path walk
# ---------------------------------------------------------------------------

def test_next_question_serves_full_sequence_exactly_once(monkeypatch) -> None:
    monkeypatch.setattr(
        "agents.nodes.node1_intake.call_text",
        lambda prompt, system=None: prompt,
    )

    brief = _blank()
    asked: set[str] = set()

    for expected in QUESTION_SEQUENCE:
        got = next_question(brief, asked)
        assert got == expected.raw_text
        asked.add(expected.id)

    assert next_question(brief, asked) is None


# ---------------------------------------------------------------------------
# floor_met()
# ---------------------------------------------------------------------------

def test_floor_met_false_when_both_unanswered() -> None:
    assert floor_met(_blank()) is False


def test_floor_met_false_when_only_budget_answered() -> None:
    brief = _blank()
    brief = brief.model_copy(
        update={"budget": brief.budget.model_copy(update={"comfortable_max": 70000})}
    )
    assert floor_met(brief) is False


def test_floor_met_false_when_only_primary_use_case_answered() -> None:
    brief = _blank()
    brief = brief.model_copy(
        update={"purpose": brief.purpose.model_copy(update={"sub_case": "video editing rig"})}
    )
    assert floor_met(brief) is False


def test_floor_met_true_when_both_answered() -> None:
    brief = _blank()
    brief = brief.model_copy(
        update={
            "budget": brief.budget.model_copy(update={"comfortable_max": 70000}),
            "purpose": brief.purpose.model_copy(update={"sub_case": "video editing rig"}),
        }
    )
    assert floor_met(brief) is True
