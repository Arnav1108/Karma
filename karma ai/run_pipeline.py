"""
run_pipeline.py  -  CLI harness for Karma AI Phase 1 pipeline.

Wires three stages end to end:
  PHASE 1  Intake        -  conversational Q&A -> UserBuildBrief
  PHASE 2  Feasibility   -  rough gate (comfortable / tight / impossible)
  PHASE 3  Allocation    -  price bands per component

Three modules may not be merged yet; this harness imports them defensively
and falls back to clearly-labelled stubs so the pipeline runs skeletally now.
Stubs drop out automatically once the real modules land.

Usage:
  python run_pipeline.py               # full conversational run
  python run_pipeline.py --fixture     # skip intake, load budget_gamer.json demo
  python run_pipeline.py --help        # print this message and exit
"""
from __future__ import annotations

import datetime
import sys
import uuid
from pathlib import Path

# Bootstrap path so `agents.*` resolves from this script's directory.
sys.path.insert(0, str(Path(__file__).parent))

from agents.schemas import (  # noqa: E402
    ComponentSlot,
    FeasibilityVerdict,
    UserBuildBrief,
)
from agents.schemas.price_bands import PriceBand, PriceBands  # noqa: E402
from agents.state.pipeline_state import PipelineState, new_state  # noqa: E402

# ---------------------------------------------------------------------------
# Defensive imports  -  _HAS_* flags control real vs stub dispatch.
# Only ImportError is caught: runtime errors inside a module must surface.
# ---------------------------------------------------------------------------

try:
    from agents.nodes.node1_intake import (
        QUESTION_SEQUENCE,
        blank_brief,
        extract_turn,
        floor_met,
        newly_filled_sections,
        next_question,
    )
    _HAS_NODE1 = True
except ImportError:
    _HAS_NODE1 = False

try:
    from agents.feasibility.estimate import estimate_feasibility
    _HAS_ESTIMATE = True
except ImportError:
    _HAS_ESTIMATE = False

try:
    from agents.nodes.node2_allocation import allocate
    _HAS_ALLOC = True
except ImportError:
    _HAS_ALLOC = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FIXTURE_PATH = Path(__file__).parent / "data" / "fixtures" / "budget_gamer.json"

_STUB_QUESTIONS = [
    "What is your total budget in INR for this build, and does it cover just the PC "
    "or also a monitor and peripherals?",
    "What will you primarily use this PC for? Name the key software you plan to run "
    "(e.g. game titles, creative tools, dev environment).",
    "Any hard constraints  -  brand preferences, form-factor requirements, "
    "parts you must avoid? (type 'none' to skip)",
]

# Realistic mid-range INR bands; total_mid ~62,700 (within budget_gamer 65 k max).
_STUB_BANDS_INR: dict[str, dict[str, int]] = {
    "gpu":         {"low": 18000, "mid": 22000, "high": 27000},
    "cpu":         {"low": 10000, "mid": 13000, "high": 16000},
    "ram":         {"low":  3500, "mid":  4500, "high":  6000},
    "storage":     {"low":  3000, "mid":  4000, "high":  5500},
    "motherboard": {"low":  5500, "mid":  7000, "high":  9000},
    "psu":         {"low":  3500, "mid":  4500, "high":  6000},
    "case":        {"low":  3000, "mid":  4000, "high":  5500},
    "cooler":      {"low":  1500, "mid":  2500, "high":  3500},
    "fans":        {"low":    800, "mid":  1200, "high":  1800},
}

# ---------------------------------------------------------------------------
# Stub functions
# Each stub has the expected real interface documented so wiring is drop-in.
# ---------------------------------------------------------------------------

def _stub_next_question(brief: UserBuildBrief, asked_so_far: set[str]) -> str | None:
    # TODO: Remove when agents/nodes/node1_intake.py merges.
    # EXPECTED INTERFACE:
    #   next_question(brief: UserBuildBrief, asked_so_far: set[str]) -> str | None
    #   Walks the static QUESTION_SEQUENCE, skips IDs in asked_so_far or already
    #   filled in the brief, calls call_text() to phrase the next question, or
    #   returns None when all questions are exhausted.
    asked_count = len(asked_so_far)
    return _STUB_QUESTIONS[asked_count] if asked_count < len(_STUB_QUESTIONS) else None


def _stub_extract_turn(
    user_answer: str,
    current_brief: UserBuildBrief,
    conversation_history: list[dict],
) -> UserBuildBrief:
    # TODO: Remove when agents/nodes/node1_intake.py merges.
    # EXPECTED INTERFACE:
    #   extract_turn(user_answer: str, current_brief: UserBuildBrief,
    #                conversation_history: list[dict]) -> UserBuildBrief
    #   LLM extracts structured fields from user_answer using conversation_history
    #   as context, merges into current_brief via _merge_delta, recomputes
    #   completeness, and returns the updated UserBuildBrief.
    #   On early-exit signal ("done"/"stop") + floor met: locks the brief immediately.
    #   On StructuredCallError: returns current_brief unchanged.
    return current_brief


def _stub_estimate_feasibility(brief: UserBuildBrief) -> FeasibilityVerdict:
    # TODO: Remove when agents/feasibility/estimate.py merges.
    # EXPECTED INTERFACE:
    #   estimate_feasibility(brief: UserBuildBrief) -> FeasibilityVerdict
    #   Three steps: resolve_requirements() -> aggregate_scope() -> LLM call
    #   with one live Postgres price anchor (cheapest binding slot, usually GPU).
    #   Returns FeasibilityVerdict(verdict in {"comfortable","tight","impossible"}).
    print("  [STUB] estimate_feasibility not available  -  returning comfortable verdict.")
    return FeasibilityVerdict(
        verdict="comfortable",
        reason="[STUB] Cannot estimate without estimate module  -  real call goes here.",
        binding_constraint=None,
        suggested_adjustments=[],
    )


def _stub_allocate(brief: UserBuildBrief) -> PriceBands:
    # TODO: Remove when agents/nodes/node2_allocation.py merges.
    # EXPECTED INTERFACE:
    #   allocate(brief: UserBuildBrief) -> PriceBands
    #   Reads default allocation profile + brief workload + live software specs,
    #   returns PriceBands keyed by ComponentSlot where:
    #     sum(mid values)  == core budget target
    #     sum(high values) == budget ceiling
    #     sum(low values)  == budget floor
    print("  [STUB] allocate not available  -  returning placeholder price bands.")
    bands = {
        ComponentSlot(slot): PriceBand(**values)
        for slot, values in _STUB_BANDS_INR.items()
    }
    return PriceBands(root=bands)

# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

_SEP = "=" * 62


def _print_header(title: str) -> None:
    print(f"\n{_SEP}")
    print(f"  {title}")
    print(_SEP)


def _print_module_status() -> None:
    def tag(flag: bool) -> str:
        return "[REAL]" if flag else "[STUB]"

    print(f"\n  node1_intake      {tag(_HAS_NODE1)}")
    print(f"  estimate          {tag(_HAS_ESTIMATE)}")
    print(f"  node2_allocation  {tag(_HAS_ALLOC)}")


def _load_fixture_brief() -> UserBuildBrief:
    raw = _FIXTURE_PATH.read_text(encoding="utf-8")
    return UserBuildBrief.model_validate_json(raw)


def _print_verdict(verdict: FeasibilityVerdict) -> None:
    labels = {"comfortable": "COMFORTABLE", "tight": "TIGHT", "impossible": "IMPOSSIBLE"}
    print(f"\n  Verdict  : {labels.get(verdict.verdict, verdict.verdict.upper())}")
    print(f"  Reason   : {verdict.reason}")
    if verdict.binding_constraint:
        print(f"  Binding  : {verdict.binding_constraint}")
    if verdict.suggested_adjustments:
        print("\n  Suggested adjustments:")
        for adj in verdict.suggested_adjustments:
            print(f"    - {adj}")


def _inr(amount: int) -> str:
    return f"INR {amount:,}"


def _print_price_bands(bands: PriceBands) -> None:
    col = (14, 14, 14, 14)
    header = (
        f"  {'Component':<{col[0]}}{'Low':>{col[1]}}{'Mid':>{col[2]}}{'High':>{col[3]}}"
    )
    rule = "  " + "-" * (sum(col))
    print(header)
    print(rule)
    for slot, band in bands.root.items():
        name = slot.value.capitalize()
        print(
            f"  {name:<{col[0]}}{_inr(band.low):>{col[1]}}"
            f"{_inr(band.mid):>{col[2]}}{_inr(band.high):>{col[3]}}"
        )
    print(rule)
    print(
        f"  {'TOTAL':<{col[0]}}{_inr(bands.total_low()):>{col[1]}}"
        f"{_inr(bands.total_mid()):>{col[2]}}{_inr(bands.total_high()):>{col[3]}}"
    )

# ---------------------------------------------------------------------------
# Phase functions
# ---------------------------------------------------------------------------

def run_intake(state: PipelineState) -> PipelineState:
    _print_header("PHASE 1  -  INTAKE")

    if not _HAS_NODE1:
        print("  [STUB] node1_intake not available  -  loading fixture brief (budget_gamer.json).")
        state = {**state, "current_brief": _load_fixture_brief()}
    else:
        # Initialise a blank brief; IDs are session-scoped placeholders.
        initial_brief = blank_brief(
            brief_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            chat_id=uuid.uuid4(),
        )
        state = {**state, "current_brief": initial_brief}

    # asked_so_far: set of question IDs already put to the user this session.
    # next_question uses this (+ _is_field_filled) to skip already-covered ground.
    asked_so_far: set[str] = set()
    turn_count = 0

    while True:
        brief = state["current_brief"]

        if _HAS_NODE1:
            question = next_question(brief, asked_so_far)
            # Identify which question ID is being asked — first in the ordered
            # sequence not yet in asked_so_far (mirrors next_question's own walk).
            current_q_id: str | None = next(
                (q.id for q in QUESTION_SEQUENCE if q.id not in asked_so_far),
                None,
            )
        else:
            asked_count = len(asked_so_far)
            question = (
                _STUB_QUESTIONS[asked_count]
                if asked_count < len(_STUB_QUESTIONS)
                else None
            )
            current_q_id = f"_stub_{asked_count}"

        if question is None:
            print("\n  All questions answered  -  locking Brief.")
            break

        print(f"\n  Karma AI: {question}")
        try:
            user_input = input("  You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  [Interrupted]")
            break

        if not user_input:
            print("  (Empty input  -  please answer or type 'done' to finish early.)")
            continue

        # Append the assistant question to history BEFORE extract_turn so the
        # LLM receives full context (question + answer) in the same call.
        history = list(state.get("conversation_history") or [])
        history.append({"role": "assistant", "content": question})
        state = {**state, "conversation_history": history}

        if _HAS_NODE1:
            old_brief = brief
            # extract_turn(user_answer, current_brief, conversation_history) -> UserBuildBrief
            # It also handles "done"/"stop" exit: locks the brief when floor is met.
            brief = extract_turn(
                user_input,
                brief,
                state.get("conversation_history") or [],
            )
            # Append user answer to history after extraction (it was context, not input).
            history = list(state.get("conversation_history") or [])
            history.append({"role": "user", "content": user_input})
            state = {**state, "current_brief": brief, "conversation_history": history}

            # Mark this question as asked so next_question doesn't re-ask it.
            if current_q_id:
                asked_so_far.add(current_q_id)

            # Opportunistic fill: if the user volunteered answers to later questions,
            # mark those IDs as covered so they're skipped automatically.
            asked_so_far.update(newly_filled_sections(old_brief, brief))

            # extract_turn locks the brief on "done"/"stop" when floor_met() is True.
            if brief.status == "locked":
                print("  Early exit accepted  -  brief locked.")
                break
        else:
            # Stub path: extract_turn returns brief unchanged; harness owns history.
            history = list(state.get("conversation_history") or [])
            history.append({"role": "user", "content": user_input})
            brief = _stub_extract_turn(
                user_input,
                brief,
                history,
            )
            state = {**state, "current_brief": brief, "conversation_history": history}

            if current_q_id:
                asked_so_far.add(current_q_id)

            # Stub: handle "done"/"stop" manually (extract_turn doesn't do it).
            if user_input.lower() in ("done", "stop"):
                if brief.completeness.required_complete:
                    print("  Early exit accepted  -  required fields complete.")
                    break
                print(
                    "  Budget and primary use case must be filled before stopping. "
                    "Please continue."
                )
                continue

        turn_count += 1

    # Lock brief if the question set exhausted naturally (extract_turn only locks on
    # early exit; the normal end-of-sequence path leaves status as "draft").
    brief = state["current_brief"]
    if brief.status != "locked":
        data = brief.model_dump()
        data["status"] = "locked"
        data["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        brief = UserBuildBrief.model_validate(data)
        state = {**state, "current_brief": brief}

    state = {**state, "current_node": "feasibility"}
    print(f"\n  Brief status: {brief.status}  |  Turns: {turn_count}")
    return state


def run_feasibility(state: PipelineState) -> PipelineState:
    _print_header("PHASE 2  -  FEASIBILITY CHECK")

    brief = state.get("current_brief")
    if brief is None:
        print("  [ERROR] No brief in state  -  intake must run before feasibility.")
        sys.exit(1)

    if _HAS_ESTIMATE:
        try:
            verdict = estimate_feasibility(brief)
        except Exception as exc:
            print(f"  [WARNING] estimate_feasibility raised {type(exc).__name__}: {exc}")
            print("  [FALLBACK] Using stub comfortable verdict.")
            verdict = _stub_estimate_feasibility(brief)
    else:
        verdict = _stub_estimate_feasibility(brief)

    _print_verdict(verdict)

    if verdict.verdict == "impossible":
        print("\n  Build is IMPOSSIBLE within the stated budget.")
        print("  Resolve the binding constraint above and re-enter intake.")
        sys.exit(1)

    proceed_msg = (
        "Budget is tight  -  proceeding to allocation (expect compromises)."
        if verdict.verdict == "tight"
        else "Budget is comfortable  -  proceeding to allocation."
    )
    print(f"\n  {proceed_msg}")

    state = {**state, "feasibility_verdict": verdict, "current_node": "node2"}
    return state


def run_allocation(state: PipelineState) -> PipelineState:
    _print_header("PHASE 3  -  BUDGET ALLOCATION")

    brief = state.get("current_brief")
    if brief is None:
        print("  [ERROR] No brief in state  -  cannot allocate without a brief.")
        sys.exit(1)

    if _HAS_ALLOC:
        try:
            bands = allocate(brief)
        except Exception as exc:
            print(f"  [WARNING] allocate raised {type(exc).__name__}: {exc}")
            print("  [FALLBACK] Using stub price bands.")
            bands = _stub_allocate(brief)
    else:
        bands = _stub_allocate(brief)

    print("\n  Price bands per component (INR):\n")
    _print_price_bands(bands)

    if brief.budget:
        print(f"\n  Budget comfortable_max : {_inr(brief.budget.comfortable_max)}")
        print(f"  Budget ceiling         : {_inr(brief.budget.ceiling)}")
        print(f"  Bands total_mid        : {_inr(bands.total_mid())}")
        print(f"  Bands total_high       : {_inr(bands.total_high())}")

    state = {**state, "price_bands": bands, "current_node": "done"}
    return state

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = sys.argv[1:]
    if "--help" in args or "-h" in args:
        print(__doc__)
        sys.exit(0)

    use_fixture = "--fixture" in args

    print(f"\n{_SEP}")
    print("  KARMA AI   -   PC Build Recommendation  (Phase 1 Pipeline)")
    print(_SEP)

    _print_module_status()

    state = new_state()

    if use_fixture:
        print("\n  [DEMO] --fixture flag  -  loading budget_gamer.json, skipping intake.")
        state = {
            **state,
            "current_brief": _load_fixture_brief(),
            "current_node": "feasibility",
        }
    else:
        state = run_intake(state)

    state = run_feasibility(state)
    state = run_allocation(state)

    verdict = state.get("feasibility_verdict")
    bands   = state.get("price_bands")
    band_count = len(bands.root) if bands else 0
    verdict_label = verdict.verdict if verdict else "n/a"

    print(f"\n{_SEP}")
    print("  PIPELINE COMPLETE")
    print(f"  Feasibility: {verdict_label}  |  Price bands: {band_count} slots")
    print(_SEP + "\n")


if __name__ == "__main__":
    main()
