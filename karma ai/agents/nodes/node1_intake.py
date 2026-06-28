"""Node 1 — per-turn intake logic.

The harness (separate task) owns the conversation loop. This module exposes two
per-turn functions the harness calls plus three supporting helpers.

Public API:
    blank_brief(brief_id, user_id, chat_id, schema_version) -> UserBuildBrief
    floor_met(brief) -> bool
    newly_filled_sections(old_brief, new_brief) -> set[str]
    next_question(brief, asked_so_far) -> str | None
    extract_turn(user_answer, current_brief, conversation_history) -> UserBuildBrief
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from agents.llm import StructuredCallError, call_structured, call_text
from agents.schemas import (
    UserBuildBrief,
)
from agents.schemas.brief import (
    Budget,
    Completeness,
    EcosystemPrefs,
    Existing,
    Extras,
    HardConstraints,
    Longevity,
    Monitor,
    OperatingSystem,
    Performance,
    Peripheral,
    Physical,
    Purpose,
    SoftwareEntry,
    Storage,
)
from agents.schemas.source_flag import SourceFlag

# ---------------------------------------------------------------------------
# Persona prompts (shared across calls)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are Karma AI, a friendly and knowledgeable PC-building advisor for Indian "
    "consumers. Ask one question per turn, keep it concise and conversational. "
    "Do not number your questions or use bullet points — just ask naturally."
)

_EXTRACT_SYSTEM = (
    "You are an extraction assistant for a PC-building advisor. "
    "Given a conversation history and the user's latest answer, extract ONLY what the "
    "user has explicitly stated or directly implied. "
    "Return null for every field the user has not mentioned — do NOT infer or fill in "
    "defaults. "
    "For fields that carry a 'source' key, set source to 'user_stated' whenever you "
    "fill that field."
)

# ---------------------------------------------------------------------------
# Static question sequence (DESIGN.md 2.1)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _QuestionDef:
    id: str
    raw_text: str
    is_final: bool = False


QUESTION_SEQUENCE: list[_QuestionDef] = [
    _QuestionDef(
        id="budget",
        raw_text=(
            "Ask the user about their budget range in INR: what are they comfortable "
            "spending and what is their absolute maximum? Also ask what the budget "
            "covers — just the PC, or monitor and peripherals too?"
        ),
    ),
    _QuestionDef(
        id="primary_use_case",
        raw_text=(
            "Ask the user what they will primarily use this PC for (for example: gaming, "
            "video/content creation, work productivity, home server/storage, or general "
            "use), and what their specific focus is within that."
        ),
    ),
    _QuestionDef(
        id="software",
        raw_text=(
            "Ask the user which specific software, games, or applications they plan to "
            "run regularly on this PC."
        ),
    ),
    _QuestionDef(
        id="performance",
        raw_text=(
            "Ask the user what visual quality targets they have: what display resolution "
            "and frame-rate are they aiming for, and whether they want HDR support?"
        ),
    ),
    _QuestionDef(
        id="monitor",
        raw_text=(
            "Ask the user whether they already own a monitor. If yes, ask for its key "
            "specs (resolution, refresh rate, HDR). If no, ask what they are looking for."
        ),
    ),
    _QuestionDef(
        id="peripherals",
        raw_text=(
            "Ask the user whether they need peripherals such as a keyboard, mouse, "
            "headset, or microphone as part of this build, and whether any are must-haves."
        ),
    ),
    _QuestionDef(
        id="storage",
        raw_text=(
            "Ask the user how much storage capacity they need and what kind of data they "
            "will primarily store — games, video projects, archives, or a mix?"
        ),
    ),
    _QuestionDef(
        id="operating_system",
        raw_text=(
            "Ask the user which operating system they plan to use (Windows, Linux, "
            "dual-boot) and whether they already have a license."
        ),
    ),
    _QuestionDef(
        id="existing",
        raw_text=(
            "Ask the user whether they have any existing PC parts they want to reuse or "
            "keep, and whether they have any strong CPU or GPU brand preferences."
        ),
    ),
    _QuestionDef(
        id="physical",
        raw_text=(
            "Ask the user about physical preferences: any case size or form-factor "
            "preference, noise tolerance, and where they plan to place the PC."
        ),
    ),
    _QuestionDef(
        id="longevity",
        raw_text=(
            "Ask the user how long they want this build to last and whether they prefer "
            "to future-proof it or optimise for value right now."
        ),
    ),
    _QuestionDef(
        id="extras",
        raw_text=(
            "Ask the user about aesthetic preferences (RGB, clean look), any special "
            "connectivity needs (Wi-Fi, Bluetooth, Thunderbolt), or whether they have "
            "any specific parts already in mind."
        ),
    ),
    _QuestionDef(
        id="hard_constraints",
        raw_text=(
            "Ask the user if they have any absolute must-haves or must-nots for this "
            "build — anything they would refuse to compromise on, no matter what."
        ),
        is_final=True,
    ),
]

# Set of question IDs that correspond to source-flagged brief sections.
_SOURCE_FLAGGED_IDS = {"performance", "monitor", "storage", "operating_system"}

# ---------------------------------------------------------------------------
# Internal extraction model (_IntakeDelta)
# ---------------------------------------------------------------------------

class _IntakeDelta(BaseModel):
    """LLM extraction target: every extractable section is Optional.

    UUIDs, timestamps, and envelope fields are NOT included here — they live in the
    brief envelope and are never subject to LLM extraction.
    """
    budget: Budget | None = None
    purpose: Purpose | None = None
    software: list[SoftwareEntry] | None = None
    performance: Performance | None = None
    monitor: Monitor | None = None
    peripherals: list[Peripheral] | None = None
    storage: Storage | None = None
    operating_system: OperatingSystem | None = None
    existing: Existing | None = None
    physical: Physical | None = None
    longevity: Longevity | None = None
    extras: Extras | None = None
    hard_constraints: HardConstraints | None = None

# ---------------------------------------------------------------------------
# blank_brief()
# ---------------------------------------------------------------------------

def blank_brief(
    brief_id: UUID,
    user_id: UUID,
    chat_id: UUID,
    schema_version: str = "1.0",
) -> UserBuildBrief:
    """Create a valid UserBuildBrief with sentinel placeholder values.

    Sentinel conventions (checked by _is_field_filled and floor_met):
    - budget.comfortable_max == 0  →  not yet answered
    - purpose.sub_case == ""       →  not yet answered
    - source-flagged sections start as SourceFlag.default_applied
    - existing.has_existing_parts == "no" (no source flag; tracked via asked_so_far)
    """
    now = datetime.now(timezone.utc)
    return UserBuildBrief(
        brief_id=brief_id,
        user_id=user_id,
        chat_id=chat_id,
        build_id=None,
        schema_version=schema_version,
        status="draft",
        completeness=Completeness(
            required_complete=False,
            optional_filled=0,
            optional_skipped=0,
        ),
        open_questions=[],
        created_at=now,
        updated_at=now,
        # Required sections — sentinel values
        budget=Budget(
            currency="INR",
            comfortable_min=0,
            comfortable_max=0,
            ceiling=0,
            scope="pc_only",
            notes=None,
        ),
        purpose=Purpose(
            primary_use_case="general_use",
            sub_case="",
            secondary_use_cases=[],
        ),
        # Source-flagged sections — default_applied until user states them
        performance=Performance(
            target_resolution=None,
            target_framerate=None,
            hdr_wanted=False,
            source=SourceFlag.default_applied,
        ),
        monitor=Monitor(
            owned="no",
            owned_specs=None,
            target_specs=None,
            count=1,
            source=SourceFlag.default_applied,
        ),
        storage=Storage(
            capacity_gb=None,
            speed_tier="nvme",
            data_profile="mixed",
            source=SourceFlag.default_applied,
        ),
        operating_system=OperatingSystem(
            os="windows",
            license="oem",
            source=SourceFlag.default_applied,
        ),
        # Optional sections with defaults
        software=[],
        peripherals=[],
        existing=Existing(
            has_existing_parts="no",
            reuse_parts=[],
            existing_pc_build_id=None,
            ecosystem_prefs=EcosystemPrefs(),
        ),
        physical=Physical(),
        longevity=Longevity(),
        extras=Extras(),
        hard_constraints=HardConstraints(),
    )

# ---------------------------------------------------------------------------
# floor_met()
# ---------------------------------------------------------------------------

def floor_met(brief: UserBuildBrief) -> bool:
    """Return True when budget AND primary use case are filled (the proceed gate)."""
    return brief.budget.comfortable_max > 0 and bool(brief.purpose.sub_case)

# ---------------------------------------------------------------------------
# _is_field_filled() — internal
# ---------------------------------------------------------------------------

def _is_field_filled(brief: UserBuildBrief, question_id: str) -> bool:
    """Return True if the Brief section for question_id is already populated."""
    if question_id == "budget":
        return brief.budget.comfortable_max > 0
    if question_id == "primary_use_case":
        return bool(brief.purpose.sub_case)
    if question_id == "software":
        return len(brief.software) > 0
    if question_id == "performance":
        return brief.performance.source == SourceFlag.user_stated
    if question_id == "monitor":
        return brief.monitor.source == SourceFlag.user_stated
    if question_id == "peripherals":
        return len(brief.peripherals) > 0
    if question_id == "storage":
        return brief.storage.source == SourceFlag.user_stated
    if question_id == "operating_system":
        return brief.operating_system.source == SourceFlag.user_stated
    # Remaining sections (existing, physical, longevity, extras, hard_constraints)
    # have no source flag and use asked_so_far tracking (rule 1 in next_question).
    return False

# ---------------------------------------------------------------------------
# newly_filled_sections()
# ---------------------------------------------------------------------------

_SECTION_TO_DUMP_KEY: dict[str, str] = {
    "budget": "budget",
    "primary_use_case": "purpose",
    "software": "software",
    "performance": "performance",
    "monitor": "monitor",
    "peripherals": "peripherals",
    "storage": "storage",
    "operating_system": "operating_system",
    "existing": "existing",
    "physical": "physical",
    "longevity": "longevity",
    "extras": "extras",
    "hard_constraints": "hard_constraints",
}

def newly_filled_sections(
    old_brief: UserBuildBrief,
    new_brief: UserBuildBrief,
) -> set[str]:
    """Return question IDs whose underlying section changed between two briefs.

    The harness adds these to asked_so_far so next_question skips them (opportunistic
    fill). Pure dict comparison — no LLM call.
    """
    old_data = old_brief.model_dump()
    new_data = new_brief.model_dump()
    filled: set[str] = set()
    for q_id, dump_key in _SECTION_TO_DUMP_KEY.items():
        if old_data.get(dump_key) != new_data.get(dump_key):
            filled.add(q_id)
    return filled

# ---------------------------------------------------------------------------
# next_question()
# ---------------------------------------------------------------------------

def next_question(
    brief: UserBuildBrief,
    asked_so_far: set[str],
) -> str | None:
    """Return the next unanswered question phrased conversationally, or None if done.

    Skips questions that are:
    1. Already in asked_so_far (asked or harness-flagged as opportunistically filled).
    2. Already filled according to the brief's own state (source-flag safety net for
       source-flagged sections and sentinel checks for budget/purpose/software).
    """
    for q in QUESTION_SEQUENCE:
        if q.id in asked_so_far:
            continue
        if _is_field_filled(brief, q.id):
            continue
        return call_text(q.raw_text, system=_SYSTEM_PROMPT)
    return None

# ---------------------------------------------------------------------------
# _is_exit_signal() — internal
# ---------------------------------------------------------------------------

_EXIT_PATTERN = re.compile(r"\b(done|stop)\b", re.IGNORECASE)

def _is_exit_signal(user_answer: str) -> bool:
    return bool(_EXIT_PATTERN.search(user_answer))

# ---------------------------------------------------------------------------
# _compute_completeness() — internal
# ---------------------------------------------------------------------------

def _compute_completeness(brief: UserBuildBrief) -> Completeness:
    required_complete = floor_met(brief)
    # Count optional sections that have been user-stated vs skipped/defaulted.
    optional_ids = [
        q.id for q in QUESTION_SEQUENCE
        if q.id not in ("budget", "primary_use_case")
    ]
    filled = sum(1 for q_id in optional_ids if _is_field_filled(brief, q_id))
    return Completeness(
        required_complete=required_complete,
        optional_filled=filled,
        optional_skipped=len(optional_ids) - filled,
    )

# ---------------------------------------------------------------------------
# _merge_delta() — internal
# ---------------------------------------------------------------------------

def _merge_delta(
    brief: UserBuildBrief,
    delta: _IntakeDelta,
) -> UserBuildBrief:
    """Merge extracted delta into the existing brief and return a new validated instance."""
    data: dict[str, Any] = brief.model_dump()

    # Scalar sections: overwrite if present in delta.
    for attr in ("budget", "purpose", "performance", "monitor", "storage",
                 "operating_system", "existing", "physical", "longevity", "extras"):
        val = getattr(delta, attr)
        if val is not None:
            data[attr] = val.model_dump()

    # List sections: extend, deduplicate.
    if delta.software:
        existing_names = {s["name"] for s in data.get("software", [])}
        new_items = [s.model_dump() for s in delta.software if s.name not in existing_names]
        data["software"] = data.get("software", []) + new_items

    if delta.peripherals:
        existing_types = {p["type"] for p in data.get("peripherals", [])}
        new_items = [p.model_dump() for p in delta.peripherals if p.type not in existing_types]
        data["peripherals"] = data.get("peripherals", []) + new_items

    # hard_constraints: append-only (DESIGN.md — never compacted, never overwritten).
    if delta.hard_constraints is not None:
        hc = data.setdefault("hard_constraints", {
            "must_have": [], "must_not": [], "rejected_parts": []
        })
        existing_must_have_ids = {c["id"] for c in hc.get("must_have", [])}
        for c in delta.hard_constraints.must_have:
            c_data = c.model_dump()
            c_data["id"] = str(c_data["id"])
            if c_data["id"] not in existing_must_have_ids:
                hc["must_have"].append(c_data)

        existing_must_not_ids = {c["id"] for c in hc.get("must_not", [])}
        for c in delta.hard_constraints.must_not:
            c_data = c.model_dump()
            c_data["id"] = str(c_data["id"])
            if c_data["id"] not in existing_must_not_ids:
                hc["must_not"].append(c_data)

        existing_rejected = {r["product_id"] for r in hc.get("rejected_parts", [])}
        for r in delta.hard_constraints.rejected_parts:
            r_data = r.model_dump()
            if r_data["product_id"] not in existing_rejected:
                hc["rejected_parts"].append(r_data)

    data["updated_at"] = datetime.now(timezone.utc).isoformat()

    # Recompute completeness after merge.
    merged = UserBuildBrief.model_validate(data)
    completeness = _compute_completeness(merged)
    merged_data = merged.model_dump()
    merged_data["completeness"] = completeness.model_dump()
    return UserBuildBrief.model_validate(merged_data)

# ---------------------------------------------------------------------------
# extract_turn()
# ---------------------------------------------------------------------------

def extract_turn(
    user_answer: str,
    current_brief: UserBuildBrief,
    conversation_history: list[dict],
) -> UserBuildBrief:
    """Extract and merge whatever the user stated in this turn into the brief.

    Step 0 — early-exit: if the user said "done"/"stop" and the floor is met,
    lock the brief immediately (no LLM extraction needed).

    Step 1 — extraction: call_structured against _IntakeDelta with full conversation
    context. On StructuredCallError, return current_brief unchanged.

    Step 2 — merge: call _merge_delta to produce an updated brief.
    """
    # Step 0 — early-exit
    if _is_exit_signal(user_answer) and floor_met(current_brief):
        data = current_brief.model_dump()
        data["status"] = "locked"
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        completeness = _compute_completeness(current_brief)
        data["completeness"] = completeness.model_dump()
        data["completeness"]["required_complete"] = True
        return UserBuildBrief.model_validate(data)

    # Step 1 — build extraction prompt
    history_text = "\n".join(
        f"{msg['role'].upper()}: {msg['content']}"
        for msg in conversation_history
    )
    already_filled_summary = json.dumps(
        {
            k: v
            for k, v in current_brief.model_dump().items()
            if k not in (
                "brief_id", "user_id", "chat_id", "build_id",
                "schema_version", "created_at", "updated_at",
                "completeness", "open_questions",
            )
        },
        default=str,
        indent=2,
    )
    prompt = (
        f"Conversation so far:\n{history_text}\n\n"
        f"User's latest answer:\n{user_answer}\n\n"
        f"Current brief state (already filled — do NOT re-extract these unless the user "
        f"explicitly contradicts or updates them):\n{already_filled_summary}\n\n"
        "Extract what the user stated in their latest answer. Return null for every "
        "field they did not mention."
    )

    try:
        delta = call_structured(prompt, _IntakeDelta, system=_EXTRACT_SYSTEM)
    except StructuredCallError:
        return current_brief

    # Step 2 — merge
    return _merge_delta(current_brief, delta)
