"""Unit tests for api/dtos.py and api/mappers.py.

Pure unit tests: no DB, no network, no LLM calls — briefs are constructed
directly via UserBuildBrief.model_validate rather than through node1_intake's
LLM-backed extraction path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agents.nodes.node1_intake import QUESTION_SEQUENCE, IntakeQuestion, IntakeSessionState
from agents.schemas.brief import UserBuildBrief
from agents.schemas.slots import ComponentSlot
from agents.schemas.source_flag import SourceFlag

from api.dtos import QuestionDTO, SubmitAnswerRequest
from api.mappers import map_brief_summary, map_progress, map_question


# ---------------------------------------------------------------------------
# Brief construction helper
# ---------------------------------------------------------------------------

def _make_brief(**overrides) -> UserBuildBrief:
    now = datetime.now(timezone.utc)
    data = {
        "brief_id": uuid4(),
        "user_id": uuid4(),
        "chat_id": uuid4(),
        "build_id": None,
        "schema_version": "1.0",
        "status": "draft",
        "completeness": {
            "required_complete": True,
            "optional_filled": 5,
            "optional_skipped": 3,
        },
        "open_questions": [],
        "created_at": now,
        "updated_at": now,
        "budget": {
            "currency": "INR",
            "comfortable_min": 60000,
            "comfortable_max": 70000,
            "ceiling": 80000,
            "scope": "pc_only",
            "notes": "flexible for deals",
        },
        "purpose": {
            "primary_use_case": "gaming",
            "sub_case": "competitive_fps",
            "secondary_use_cases": [{"use_case": "streaming", "weight": "low"}],
        },
        "software": [
            {"name": "CS2", "category": "game", "frequency": "primary", "intensity": "casual"},
        ],
        "performance": {
            "target_resolution": "1440p",
            "target_framerate": 144,
            "hdr_wanted": True,
            "source": SourceFlag.user_stated,
        },
        "monitor": {
            "owned": "yes",
            "owned_specs": {
                "resolution": "1440p",
                "refresh_hz": 165,
                "hdr": True,
                "size_inch": 27.0,
            },
            "target_specs": None,
            "count": 1,
            "source": SourceFlag.user_stated,
        },
        "peripherals": [
            {"type": "mouse", "requirements": "lightweight", "priority": "must_have"},
        ],
        "storage": {
            "capacity_gb": 1024,
            "speed_tier": "nvme",
            "data_profile": "hot",
            "source": SourceFlag.inferred,
        },
        "operating_system": {
            "os": "windows",
            "license": "oem",
            "source": SourceFlag.default_applied,
        },
        "existing": {
            "has_existing_parts": "yes",
            "reuse_parts": [
                {"slot": ComponentSlot.gpu, "identifier": "RTX 3070", "action": "keep"},
            ],
            "existing_pc_build_id": None,
            "ecosystem_prefs": {"cpu_brand_pref": "AMD", "gpu_brand_pref": "NVIDIA"},
        },
        "physical": {
            "form_factor_pref": "atx_mid",
            "noise_tolerance": "silent_priority",
            "placement": "hot_room",
            "portability_need": False,
            "size_notes": "must fit under desk",
        },
        "longevity": {
            "reliability_priority": "high_stability_alwayson",
            "upgrade_path": "future_proof",
            "timeline": "flexible_for_deals",
        },
        "extras": {
            "rgb_pref": "want_rgb",
            "visual_style": "showcase_glass",
            "connectivity_needs": ["wifi", "bluetooth"],
            "specific_part_requests": [
                {"slot": ComponentSlot.gpu, "requested": "RTX 4070 Super"},
            ],
        },
        "hard_constraints": {
            "must_have": [
                {
                    "id": uuid4(),
                    "type": "brand",
                    "value": "must be AMD CPU",
                    "source": "user_stated",
                    "locked_at": now,
                },
            ],
            "must_not": [
                {
                    "id": uuid4(),
                    "type": "brand",
                    "value": "no Intel",
                    "source": "user_stated",
                    "locked_at": now,
                },
            ],
            "rejected_parts": [],
        },
    }
    data.update(overrides)
    return UserBuildBrief.model_validate(data)


# ---------------------------------------------------------------------------
# map_question
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["sequence", "clarification", "confirm_default"])
def test_map_question_all_kinds(kind):
    q = IntakeQuestion(question_id="budget", text="What's your budget?", kind=kind)
    dto = map_question(q)
    assert isinstance(dto, QuestionDTO)
    assert dto.question_id == "budget"
    assert dto.text == "What's your budget?"
    assert dto.kind == kind


def test_map_question_none_id():
    q = IntakeQuestion(question_id=None, text="Still can't pin this down?", kind="clarification")
    dto = map_question(q)
    assert dto.question_id is None
    assert dto.kind == "clarification"


# ---------------------------------------------------------------------------
# map_progress
# ---------------------------------------------------------------------------

def test_map_progress_partial_asked_so_far():
    brief = _make_brief(
        budget={
            "currency": "INR", "comfortable_min": 0, "comfortable_max": 0,
            "ceiling": 0, "scope": "pc_only", "notes": None,
        },
        purpose={"primary_use_case": "general_use", "sub_case": "", "secondary_use_cases": []},
    )
    state = IntakeSessionState(
        brief=brief,
        asked_so_far=["budget", "primary_use_case", "software", "performance", "monitor"],
    )
    progress = map_progress(state, brief)
    assert progress.answered == 5
    assert progress.total == len(QUESTION_SEQUENCE) == 13
    assert progress.floor_met is False


def test_map_progress_full_asked_so_far_and_floor_met():
    brief = _make_brief()
    all_ids = [q.id for q in QUESTION_SEQUENCE]
    state = IntakeSessionState(brief=brief, asked_so_far=all_ids)
    progress = map_progress(state, brief)
    assert progress.answered == 13
    assert progress.total == 13
    assert progress.floor_met is True


def test_map_progress_dedupes_asked_so_far():
    brief = _make_brief()
    state = IntakeSessionState(brief=brief, asked_so_far=["budget", "budget", "software"])
    progress = map_progress(state, brief)
    assert progress.answered == 2


# ---------------------------------------------------------------------------
# map_brief_summary
# ---------------------------------------------------------------------------

def test_map_brief_summary_answered_fields_preserves_question_sequence_order():
    brief = _make_brief()
    scrambled = ["hard_constraints", "budget", "monitor", "existing", "primary_use_case"]
    dto = map_brief_summary(brief, scrambled)

    expected_order = [
        q.id for q in QUESTION_SEQUENCE
        if q.id in {"hard_constraints", "budget", "monitor", "existing", "primary_use_case"}
    ]
    assert dto.answered_fields == expected_order
    assert dto.answered_fields == ["budget", "primary_use_case", "monitor", "existing", "hard_constraints"]


def test_map_brief_summary_full_field_mapping():
    brief = _make_brief()
    dto = map_brief_summary(brief, ["budget", "existing"])

    assert dto.completeness == {
        "required_complete": True, "optional_filled": 5, "optional_skipped": 3,
    }

    assert dto.budget == {
        "comfortable_min": 60000, "comfortable_max": 70000, "ceiling": 80000,
        "scope": "pc_only", "currency": "INR", "notes": "flexible for deals",
    }

    assert dto.purpose["primary_use_case"] == "gaming"
    assert dto.purpose["sub_case"] == "competitive_fps"
    assert len(dto.purpose["secondary_use_cases"]) == 1
    assert dto.purpose["secondary_use_cases"][0].use_case == "streaming"
    assert dto.purpose["secondary_use_cases"][0].weight == "low"

    assert len(dto.software) == 1
    assert dto.software[0].name == "CS2"
    assert dto.software[0].category == "game"
    assert dto.software[0].frequency == "primary"
    assert dto.software[0].intensity == "casual"

    assert dto.performance["target_resolution"] == "1440p"
    assert dto.performance["target_framerate"] == 144
    assert dto.performance["hdr_wanted"] is True
    assert dto.performance["source"] == SourceFlag.user_stated

    assert dto.monitor["owned"] == "yes"
    assert dto.monitor["specs"] == "1440p @ 165Hz HDR"
    assert dto.monitor["count"] == 1
    assert dto.monitor["source"] == SourceFlag.user_stated

    assert len(dto.peripherals) == 1
    assert dto.peripherals[0].type == "mouse"
    assert dto.peripherals[0].requirements == "lightweight"
    assert dto.peripherals[0].priority == "must_have"

    assert dto.storage == {
        "capacity_gb": 1024, "speed_tier": "nvme", "data_profile": "hot",
        "source": SourceFlag.inferred,
    }

    assert dto.operating_system == {
        "os": "windows", "license": "oem", "source": SourceFlag.default_applied,
    }

    assert len(dto.reuse_parts) == 1
    assert dto.reuse_parts[0].slot == ComponentSlot.gpu
    assert dto.reuse_parts[0].identifier == "RTX 3070"
    assert dto.reuse_parts[0].action == "keep"
    assert dto.brand_prefs == {"cpu": "AMD", "gpu": "NVIDIA"}

    assert dto.physical == {
        "form_factor_pref": "atx_mid",
        "noise_tolerance": "silent_priority",
        "placement": "hot_room",
        "portability_need": False,
    }
    assert "size_notes" not in dto.physical

    assert dto.longevity == {
        "reliability_priority": "high_stability_alwayson",
        "upgrade_path": "future_proof",
        "timeline": "flexible_for_deals",
    }

    assert dto.extras["rgb_pref"] == "want_rgb"
    assert dto.extras["visual_style"] == "showcase_glass"
    assert dto.extras["connectivity_needs"] == ["wifi", "bluetooth"]
    assert len(dto.extras["specific_part_requests"]) == 1
    assert dto.extras["specific_part_requests"][0].slot == ComponentSlot.gpu
    assert dto.extras["specific_part_requests"][0].requested == "RTX 4070 Super"

    assert dto.hard_constraints == {
        "must_have": ["must be AMD CPU"],
        "must_not": ["no Intel"],
    }
    assert "rejected_parts" not in dto.hard_constraints


def test_map_brief_summary_monitor_specs_owned_with_specs():
    brief = _make_brief(
        monitor={
            "owned": "yes",
            "owned_specs": {"resolution": "1080p", "refresh_hz": 144, "hdr": False, "size_inch": 24.0},
            "target_specs": None,
            "count": 1,
            "source": SourceFlag.user_stated,
        }
    )
    dto = map_brief_summary(brief, [])
    assert dto.monitor["specs"] == "1080p @ 144Hz"


def test_map_brief_summary_monitor_specs_target_specs_only():
    brief = _make_brief(
        monitor={
            "owned": "no",
            "owned_specs": None,
            "target_specs": {"resolution": "4K", "refresh_hz": 60, "hdr": True},
            "count": 1,
            "source": SourceFlag.user_stated,
        }
    )
    dto = map_brief_summary(brief, [])
    assert dto.monitor["specs"] == "4K @ 60Hz HDR"


def test_map_brief_summary_monitor_specs_neither():
    brief = _make_brief(
        monitor={
            "owned": "no",
            "owned_specs": None,
            "target_specs": None,
            "count": 1,
            "source": SourceFlag.default_applied,
        }
    )
    dto = map_brief_summary(brief, [])
    assert dto.monitor["specs"] is None


def test_map_brief_summary_owned_yes_but_no_owned_specs_falls_through_to_none():
    # owned == "yes" but owned_specs missing and target_specs absent too -> None,
    # not a crash (guards the `and brief.monitor.owned_specs` branch condition).
    brief = _make_brief(
        monitor={
            "owned": "yes",
            "owned_specs": None,
            "target_specs": None,
            "count": 1,
            "source": SourceFlag.default_applied,
        }
    )
    dto = map_brief_summary(brief, [])
    assert dto.monitor["specs"] is None


def test_map_brief_summary_existing_flattening_empty():
    brief = _make_brief(
        existing={
            "has_existing_parts": "no",
            "reuse_parts": [],
            "existing_pc_build_id": None,
            "ecosystem_prefs": {"cpu_brand_pref": None, "gpu_brand_pref": None},
        }
    )
    dto = map_brief_summary(brief, [])
    assert dto.reuse_parts == []
    assert dto.brand_prefs == {"cpu": None, "gpu": None}


def test_map_brief_summary_empty_lists_render_as_empty_not_special_cased():
    brief = _make_brief(software=[], peripherals=[])
    dto = map_brief_summary(brief, [])
    assert dto.software == []
    assert dto.peripherals == []


# ---------------------------------------------------------------------------
# SubmitAnswerRequest validation
# ---------------------------------------------------------------------------

def test_submit_answer_request_rejects_empty_string():
    with pytest.raises(ValidationError):
        SubmitAnswerRequest(answer="")


def test_submit_answer_request_rejects_2001_chars():
    with pytest.raises(ValidationError):
        SubmitAnswerRequest(answer="a" * 2001)


def test_submit_answer_request_accepts_1_char():
    req = SubmitAnswerRequest(answer="a")
    assert req.answer == "a"


def test_submit_answer_request_accepts_2000_chars():
    req = SubmitAnswerRequest(answer="a" * 2000)
    assert len(req.answer) == 2000
