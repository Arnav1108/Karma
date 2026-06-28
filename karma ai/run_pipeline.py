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

import sys
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
    from agents.nodes.node1_intake import extract_turn, next_question
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

def _stub_next_question(state: PipelineState) -> str | None:
    # TODO: Remove when agents/nodes/node1_intake.py merges.
    # EXPECTED INTERFACE:
    #   next_question(state: PipelineState) -> str | None
    #   Returns the next question from the pre-prepared set, or None when
    #   the full set is exhausted (Brief locks on None return).
    history = state.get("conversation_history") or []
    asked = sum(1 for m in history if m.get("role") == "assistant")
    return _STUB_QUESTIONS[asked] if asked < len(_STUB_QUESTIONS) else None


def _stub_extract_turn(state: PipelineState, user_input: str) -> PipelineState:
    # TODO: Remove when agents/nodes/node1_intake.py merges.
    # EXPECTED INTERFACE:
    #   extract_turn(state: PipelineState, user_input: str) -> PipelineState
    #   LLM extracts structured fields from user_input, merges them into
    #   state["current_brief"], appends {"role": "user", "content": user_input}
    #   to state["conversation_history"], and returns the updated PipelineState.
    #   Two-stage validation: JSON syntax -> schema + enum conformance.
    history = list(state.get("conversation_history") or [])
    history.append({"role": "user", "content": user_input})
    return {**state, "conversation_history": history}


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

    turn_count = 0
    while True:
        question = next_question(state) if _HAS_NODE1 else _stub_next_question(state)
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

        if user_input.lower() in ("done", "stop", "exit"):
            brief = state.get("current_brief")
            if brief is not None and brief.completeness.required_complete:
                print("  Early exit accepted  -  required fields complete.")
                break
            print(
                "  Budget and primary use case must be filled before stopping. "
                "Please continue."
            )
            continue

        # Append the assistant's question to history BEFORE calling extract_turn
        # so the LLM sees both sides of the exchange.
        history = list(state.get("conversation_history") or [])
        history.append({"role": "assistant", "content": question})
        state = {**state, "conversation_history": history}

        if _HAS_NODE1:
            state = extract_turn(state, user_input)
        else:
            state = _stub_extract_turn(state, user_input)

        turn_count += 1

    state = {**state, "current_node": "feasibility"}
    brief = state.get("current_brief")
    status = brief.status if brief else "unknown"
    print(f"\n  Brief status: {status}  |  Turns: {turn_count}")
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
