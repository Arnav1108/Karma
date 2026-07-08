"""Node 1 — per-turn intake logic.

The harness (separate task) owns the conversation loop. This module exposes two
per-turn functions the harness calls plus three supporting helpers.

Public API:
    blank_brief(brief_id, user_id, chat_id, schema_version) -> UserBuildBrief
    floor_met(brief) -> bool
    newly_filled_sections(old_brief, new_brief) -> set[str]
    next_question(brief, asked_so_far) -> str | None
    extract_turn(user_answer, current_brief, conversation_history) -> UserBuildBrief
    lock_brief(brief) -> UserBuildBrief
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
    "fill that field.\n\n"
    "CLARIFICATION RULE: if the user's latest answer is too ambiguous to confidently "
    "fill a required field (budget, primary use case) or an ask-if-ambiguous field "
    "(performance, monitor, storage, operating_system) — e.g. they say 'video editing' "
    "without naming software, or 'a decent monitor' without specs — do NOT guess a "
    "value for it and do NOT leave it silently at a default. Instead set "
    "`clarification_needed` to ONE specific, concrete follow-up question that would "
    "resolve the ambiguity, and leave the underlying field null/unset. Only ever flag "
    "the single most blocking ambiguity per turn; never set it for fields the user "
    "simply hasn't reached yet.\n\n"
    "Intensity MUST follow these rules — do not default to moderate:\n"
    "- heavy: AAA open-world or realistic games (RDR2, GTA, Cyberpunk, Elden Ring etc), "
    "any local ML/LLM inference, PyTorch/CUDA training, 3D rendering, video editing "
    "at high resolution. If the user mentions 1440p/4K or high framerates for a game, "
    "that game is heavy.\n"
    "- moderate: indie games, older/less demanding titles, light productivity, casual gaming.\n"
    "- casual: browsing, office work, media playback.\n\n"
    "FREQUENCY RULE (non-negotiable): The user's primary/secondary USE-CASE declaration "
    "is the sole authority for frequency assignment.\n"
    "- If the user said 'primary X, secondary Y': ALL software belonging to use-case Y "
    "MUST be frequency=secondary, regardless of how many titles they listed.\n"
    "- Do NOT infer frequency from how much software was named per category.\n"
    "- Example: 'primary work, secondary gaming' means all games = secondary even if "
    "3 games were listed and only 1 work app."
)

# ---------------------------------------------------------------------------
# Static question sequence (DESIGN.md 2.1)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _QuestionDef:
    id: str
    raw_text: str


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
    ),
]

# Set of question IDs that correspond to source-flagged brief sections.
_SOURCE_FLAGGED_IDS = {"performance", "monitor", "storage", "operating_system"}

# ---------------------------------------------------------------------------
# Ask-if-ambiguous open-question mechanism (DESIGN.md §2.1 / §9)
# ---------------------------------------------------------------------------

# Max attempts before an open question is force-resolved: attempt 1 asks it plainly,
# attempt 2 asks the confirm-to-default prompt, attempt 2's "no" grants one further
# open-ended attempt (tracked as attempts==2) after which it is force-cleared —
# this constant is the attempts value at which force-clearing kicks in.
_FORCE_CLEAR_AT_ATTEMPTS = 2

_CONFIRM_DEFAULT_TEXT = (
    "I still can't pin this down — should I go with a sensible default and move on?"
)

_AFFIRMATIVE_PATTERN = re.compile(r"^(y|yes|yeah|yep|sure|ok|okay)$", re.IGNORECASE)


def _is_affirmative(answer: str) -> bool:
    return bool(_AFFIRMATIVE_PATTERN.match(answer.strip()))


# Blank-brief-equivalent defaults for the source-flagged sections, applied when an
# open question about one of them is force-resolved without the user ever resolving
# the ambiguity themselves.
_SOURCE_FLAGGED_DEFAULTS: dict[str, dict[str, Any]] = {
    "performance": {"target_resolution": None, "target_framerate": None, "hdr_wanted": False},
    "monitor": {"owned": "no", "owned_specs": None, "target_specs": None, "count": 1},
    "storage": {"capacity_gb": None, "speed_tier": "nvme", "data_profile": "mixed"},
    "operating_system": {"os": "windows", "license": "oem"},
}

# Heuristic resolution-string -> Performance.target_resolution tier mapping, used
# only by the `inferred` heuristic below (never by direct user-stated extraction).
_RESOLUTION_TIER_MAP = {
    "1920x1080": "1080p", "1080p": "1080p", "fhd": "1080p",
    "2560x1440": "1440p", "1440p": "1440p", "qhd": "1440p", "2k": "1440p",
    "3840x2160": "4K", "4k": "4K", "uhd": "4K",
}


def _map_monitor_resolution(resolution: str) -> str | None:
    return _RESOLUTION_TIER_MAP.get(resolution.strip().lower().replace(" ", ""))


def _apply_heuristic_inferences(data: dict[str, Any]) -> dict[str, Any]:
    """Fill source-flagged sections via a computed/heuristic default when a related
    section gives enough signal, without the user ever directly answering that
    section's own question. Marked source=inferred — distinct from user_stated
    (explicit statement) and skipped_by_user (open-question abandonment).
    """
    perf = data.get("performance")
    mon = data.get("monitor")
    if (
        perf is not None
        and mon is not None
        and perf.get("source") == SourceFlag.default_applied.value
        and mon.get("owned") == "yes"
        and mon.get("owned_specs")
    ):
        tier = _map_monitor_resolution(mon["owned_specs"].get("resolution", ""))
        if tier is not None:
            perf["target_resolution"] = tier
            perf["target_framerate"] = mon["owned_specs"].get("refresh_hz")
            perf["source"] = SourceFlag.inferred.value
    return data


def _add_open_question(brief: UserBuildBrief, question: str) -> UserBuildBrief:
    if question in brief.open_questions:
        return brief
    data = brief.model_dump()
    data["open_questions"] = [*data.get("open_questions", []), question]
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    return UserBuildBrief.model_validate(data)


def _remove_open_question(brief: UserBuildBrief, question: str) -> UserBuildBrief:
    if question not in brief.open_questions:
        return brief
    data = brief.model_dump()
    data["open_questions"] = [q for q in data.get("open_questions", []) if q != question]
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    return UserBuildBrief.model_validate(data)


def _force_resolve_open_question(
    brief: UserBuildBrief,
    open_question: str,
    question_id: str | None,
) -> UserBuildBrief:
    """Clear open_question; apply the section's default + skipped_by_user when the
    target section carries a source flag, otherwise just clear (no field mutation).
    """
    data = brief.model_dump()
    data["open_questions"] = [q for q in data.get("open_questions", []) if q != open_question]
    if question_id in _SOURCE_FLAGGED_DEFAULTS:
        section = dict(data[question_id])
        section.update(_SOURCE_FLAGGED_DEFAULTS[question_id])
        section["source"] = SourceFlag.skipped_by_user.value
        data[question_id] = section
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    merged = UserBuildBrief.model_validate(data)
    completeness = _compute_completeness(merged)
    merged_data = merged.model_dump()
    merged_data["completeness"] = completeness.model_dump()
    return UserBuildBrief.model_validate(merged_data)

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
    clarification_needed: str | None = None

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
        return brief.performance.source != SourceFlag.default_applied
    if question_id == "monitor":
        return brief.monitor.source != SourceFlag.default_applied
    if question_id == "peripherals":
        return len(brief.peripherals) > 0
    if question_id == "storage":
        return brief.storage.source != SourceFlag.default_applied
    if question_id == "operating_system":
        return brief.operating_system.source != SourceFlag.default_applied
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
    open_question_attempts: dict[str, int] | None = None,
) -> str | None:
    """Return the next unanswered question phrased conversationally, or None if done.

    0. If brief.open_questions is non-empty, that takes priority over
       QUESTION_SEQUENCE entirely: serve the open question itself (attempts 0 or 2 —
       first ask, or the one further open-ended attempt after a "no"), or the
       confirm-to-default prompt (attempts == 1).
    Skips QUESTION_SEQUENCE entries that are:
    1. Already in asked_so_far (asked or harness-flagged as opportunistically filled).
    2. Already filled according to the brief's own state (source-flag safety net for
       source-flagged sections and sentinel checks for budget/purpose/software).
    """
    if brief.open_questions:
        oq = brief.open_questions[0]
        attempts = (open_question_attempts or {}).get(oq, 0)
        if attempts == 1:
            return _CONFIRM_DEFAULT_TEXT
        return oq

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

_EXIT_PATTERN = re.compile(r"^(done|stop)$", re.IGNORECASE)

def _is_exit_signal(user_answer: str) -> bool:
    return bool(_EXIT_PATTERN.match(user_answer.strip()))

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

    # Computed/heuristic defaults (source=inferred) — never overrides a user_stated
    # or already-inferred value; only fills a still-default_applied section.
    data = _apply_heuristic_inferences(data)

    # Recompute completeness after merge.
    merged = UserBuildBrief.model_validate(data)
    completeness = _compute_completeness(merged)
    merged_data = merged.model_dump()
    merged_data["completeness"] = completeness.model_dump()
    return UserBuildBrief.model_validate(merged_data)

# ---------------------------------------------------------------------------
# lock_brief()
# ---------------------------------------------------------------------------

def lock_brief(brief: UserBuildBrief) -> UserBuildBrief:
    """Transition a brief to status='locked', recomputing completeness.

    Callers must check floor_met(brief) themselves before calling this — it does
    not enforce the floor gate, it only performs the transition.
    """
    data = brief.model_dump()
    data["status"] = "locked"
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    completeness = _compute_completeness(brief)
    data["completeness"] = completeness.model_dump()
    data["completeness"]["required_complete"] = True
    return UserBuildBrief.model_validate(data)

# ---------------------------------------------------------------------------
# extract_turn()
# ---------------------------------------------------------------------------

def extract_turn(
    user_answer: str,
    current_brief: UserBuildBrief,
    conversation_history: list[dict],
    current_question_id: str | None = None,
    open_question_attempts: dict[str, int] | None = None,
) -> UserBuildBrief:
    """Extract and merge whatever the user stated in this turn into the brief.

    Step 0 — early-exit: if the user said "done"/"stop" and the floor is met,
    lock the brief immediately (no LLM extraction needed).

    Step 0.5 — open-question state machine: if current_brief.open_questions is
    non-empty, this turn's answer is a response to that pending clarification (per
    next_question's priority rule), not a fresh QUESTION_SEQUENCE answer. Attempts
    are tracked in open_question_attempts (mutated in place — caller-owned, mirrors
    asked_so_far — so this function's return type stays UserBuildBrief-only):
      - attempts == 0: first ask already happened: fall through to normal
        extraction below to see if this answer resolves it.
      - attempts == 1: this answer responds to the confirm-to-default prompt.
        Yes -> force-resolve (default + skipped_by_user where the section has a
        source flag). No -> grant one further open-ended attempt (attempts -> 2).
      - attempts >= 2: the further open-ended attempt has now been answered —
        force-resolve unconditionally. Bounded to at most 3 asks; never loops.
    current_question_id identifies which brief section the pending open question
    concerns (established by the caller when the clarification was first raised),
    used only to select the right default on a force-resolve.

    Step 1 — extraction: call_structured against _IntakeDelta with full conversation
    context. On StructuredCallError, return current_brief unchanged.

    Step 2 — merge: call _merge_delta to produce an updated brief. If the delta
    still flags clarification_needed for a pending open question, keep it open and
    bump attempts instead of guessing the field; if resolved, drop it.
    """
    # Step 0 — early-exit
    if _is_exit_signal(user_answer) and floor_met(current_brief):
        return lock_brief(current_brief)

    # Step 0.5 — open-question state machine (attempts 1 and 2+ short-circuit
    # before any LLM extraction call; attempt 0 falls through below).
    pending_oq = current_brief.open_questions[0] if current_brief.open_questions else None

    if pending_oq is not None:
        attempts = open_question_attempts.get(pending_oq, 0) if open_question_attempts is not None else 0

        if attempts == 1:
            if _is_affirmative(user_answer):
                if open_question_attempts is not None:
                    open_question_attempts.pop(pending_oq, None)
                return _force_resolve_open_question(current_brief, pending_oq, current_question_id)
            if open_question_attempts is not None:
                open_question_attempts[pending_oq] = _FORCE_CLEAR_AT_ATTEMPTS
            return current_brief

        if attempts >= _FORCE_CLEAR_AT_ATTEMPTS:
            if open_question_attempts is not None:
                open_question_attempts.pop(pending_oq, None)
            return _force_resolve_open_question(current_brief, pending_oq, current_question_id)

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
    merged = _merge_delta(current_brief, delta)

    if delta.clarification_needed:
        if pending_oq is not None:
            # Still ambiguous on retry — keep the ORIGINAL open question active
            # (ignore any reworded text) and move to the confirm-to-default phase.
            if open_question_attempts is not None:
                open_question_attempts[pending_oq] = 1
        else:
            merged = _add_open_question(merged, delta.clarification_needed)
    elif pending_oq is not None:
        # Resolved: the LLM didn't re-flag ambiguity for it this turn.
        merged = _remove_open_question(merged, pending_oq)
        if open_question_attempts is not None:
            open_question_attempts.pop(pending_oq, None)

    return merged
