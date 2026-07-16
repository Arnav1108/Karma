"""
run_pipeline.py  -  CLI harness for Karma AI Phase 1 pipeline.

Wires four stages end to end:
  PHASE 1  Intake        -  conversational Q&A -> UserBuildBrief
  PHASE 2  Feasibility   -  rough gate (comfortable / tight / impossible)
  PHASE 3  Allocation    -  price bands per component
  PHASE 4  Selection     -  part finder funnel -> build card

Usage:
  python run_pipeline.py                                    # full conversational run
  python run_pipeline.py --fixture data/fixtures/budget_gamer.json
  python run_pipeline.py --fixture-all                      # run all 3 fixtures, print summary
  python run_pipeline.py --help                             # print this message and exit
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

# Bootstrap path so `agents.*` resolves from this script's directory.
sys.path.insert(0, str(Path(__file__).parent))

from agents.schemas import (  # noqa: E402
    FeasibilityVerdict,
    UserBuildBrief,
)
from agents.schemas.build_card import BuildCard  # noqa: E402
from agents.schemas.price_bands import PriceBands  # noqa: E402
from agents.output.formatter import format_build_card, format_price_bands  # noqa: E402
from agents.state.pipeline_state import PipelineState, new_state  # noqa: E402

from agents.nodes.node1_intake import (  # noqa: E402
    IntakeInterrupted,
    blank_brief,
    drive_intake,
)
from agents.feasibility.estimate import estimate_feasibility  # noqa: E402
from agents.nodes.node2_allocation import allocate_budget  # noqa: E402
from agents.nodes.node3_selector import ThresholdCache, select_build  # noqa: E402
from agents.nodes.node3_refinement import (  # noqa: E402
    MAX_REFINEMENT_ROUNDS,
    dispatch_refinement,
    dispatch_refinement_v2,
    parse_refinement_request,
    parse_refinement_request_v2,
)
from agents.llm.client import StructuredCallError  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).parent / "data" / "fixtures"

_FIXTURE_ALL_PATHS: dict[str, Path] = {
    "budget_gamer":   _FIXTURES_DIR / "budget_gamer.json",
    "video_editor":   _FIXTURES_DIR / "video_editor.json",
    "ml_workstation": _FIXTURES_DIR / "ml_workstation.json",
}

# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

_SEP = "=" * 62


def _print_header(title: str) -> None:
    print(f"\n{_SEP}")
    print(f"  {title}")
    print(_SEP)


def _load_fixture_brief(path: Path) -> UserBuildBrief:
    raw = path.read_text(encoding="utf-8")
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


# ---------------------------------------------------------------------------
# Fixture-all helpers
# ---------------------------------------------------------------------------

def _run_fixture_quiet(
    label: str,
    brief: UserBuildBrief,
) -> tuple[FeasibilityVerdict | None, PriceBands | None]:
    """Run feasibility + allocation for one fixture without sys.exit on impossible."""
    try:
        verdict = estimate_feasibility(brief)
    except Exception as exc:
        print(f"  [{label}] estimate_feasibility raised {type(exc).__name__}: {exc}")
        return None, None

    if verdict.verdict == "impossible":
        return verdict, None

    try:
        bands = allocate_budget(brief)
    except Exception as exc:
        print(f"  [{label}] allocate raised {type(exc).__name__}: {exc}")
        return verdict, None

    return verdict, bands


def _print_fixture_summary(
    results: list[tuple[str, FeasibilityVerdict | None, PriceBands | None]],
) -> None:
    col = (16, 13, 18, 12, 12, 12)
    header = (
        f"  {'Fixture':<{col[0]}}{'Verdict':<{col[1]}}{'Binding':<{col[2]}}"
        f"{'total_low':>{col[3]}}{'total_mid':>{col[4]}}{'total_high':>{col[5]}}"
    )
    rule = "  " + "-" * sum(col)
    print(header)
    print(rule)
    for label, verdict, bands in results:
        v_str  = verdict.verdict if verdict else "error"
        b_str  = verdict.binding_constraint or "—" if verdict else "—"
        if bands:
            lo  = _inr(bands.total_low())
            mid = _inr(bands.total_mid())
            hi  = _inr(bands.total_high())
        else:
            lo = mid = hi = "n/a"
        print(
            f"  {label:<{col[0]}}{v_str:<{col[1]}}{b_str:<{col[2]}}"
            f"{lo:>{col[3]}}{mid:>{col[4]}}{hi:>{col[5]}}"
        )
    print(rule)


# ---------------------------------------------------------------------------
# Phase functions
# ---------------------------------------------------------------------------

def _cli_answer_fn(question_id: str, question_text: str) -> str:
    """answer_fn for drive_intake(): prints the question, reads stdin, reprompts
    on empty input, and raises IntakeInterrupted on Ctrl-C/EOF so drive_intake
    can finalize (force-lock) the brief exactly as it does on natural exhaustion.
    """
    print(f"\n  Karma AI: {question_text}")
    while True:
        try:
            user_input = input("  You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  [Interrupted]")
            raise IntakeInterrupted
        if not user_input:
            print("  (Empty input  -  please answer or type 'done' to finish early.)")
            continue
        return user_input


def run_intake(state: PipelineState) -> PipelineState:
    _print_header("PHASE 1  -  INTAKE")

    # Initialise a blank brief; IDs are session-scoped placeholders.
    initial_brief = blank_brief(
        brief_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        chat_id=uuid.uuid4(),
    )

    # drive_intake() owns the question walk, asked_so_far / opportunistic-fill
    # bookkeeping, and history threading — shared verbatim with any
    # non-interactive caller (e.g. an E2E test). This wrapper supplies only the
    # stdin-bound answer_fn.
    brief, history = drive_intake(
        initial_brief,
        _cli_answer_fn,
        conversation_history=state.get("conversation_history"),
    )
    turn_count = sum(1 for m in history if m.get("role") == "user")

    state = {
        **state,
        "current_brief": brief,
        "conversation_history": history,
        "current_node": "feasibility",
    }
    print(f"\n  Brief status: {brief.status}  |  Turns: {turn_count}")
    return state


def run_feasibility(state: PipelineState) -> PipelineState:
    _print_header("PHASE 2  -  FEASIBILITY CHECK")

    brief = state.get("current_brief")
    if brief is None:
        print("  [ERROR] No brief in state  -  intake must run before feasibility.")
        sys.exit(1)

    try:
        verdict = estimate_feasibility(brief)
    except Exception as exc:
        print(f"  [ERROR] estimate_feasibility raised {type(exc).__name__}: {exc}")
        sys.exit(1)

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

    try:
        bands = allocate_budget(brief)
    except Exception as exc:
        print(f"  [ERROR] allocate_budget raised {type(exc).__name__}: {exc}")
        sys.exit(1)

    print("\n  Price bands per component (INR):\n")
    print(format_price_bands(bands))

    if brief.budget:
        print(f"\n  Budget comfortable_max : {_inr(brief.budget.comfortable_max)}")
        print(f"  Budget ceiling         : {_inr(brief.budget.ceiling)}")
        print(f"  Bands total_mid        : {_inr(bands.total_mid())}")
        print(f"  Bands total_high       : {_inr(bands.total_high())}")

    state = {**state, "price_bands": bands, "current_node": "node3"}
    return state


def run_selection(state: PipelineState) -> PipelineState:
    _print_header("PHASE 4  -  PART SELECTION")

    brief = state.get("current_brief")
    bands = state.get("price_bands")
    verdict = state.get("feasibility_verdict")
    if brief is None or bands is None:
        print("  [ERROR] No brief/price bands in state  -  cannot select parts.")
        sys.exit(1)

    try:
        build_card = select_build(brief, bands, feasibility_verdict=verdict)
    except Exception as exc:
        print(f"  [ERROR] select_build raised {type(exc).__name__}: {exc}")
        sys.exit(1)

    print()
    print(format_build_card(build_card, brief))

    state = {**state, "build_card": build_card, "current_node": "refinement"}
    return state


def _print_build_diff(build_card: BuildCard) -> None:
    """Print only the slots that changed this refinement round, plus the new total."""
    if not build_card.changed_slots:
        print("  No changes to the build.")
        return
    print("\n  Changes this round:")
    for c in build_card.changed_slots:
        old = c.get("old_product_id") or "—"
        new = c.get("new_product_id") or "—"
        reason = c.get("reason") or ""
        print(f"    {c['slot']:<12} {old:>14}  ->  {new:<14}  ({reason})")
    print(f"  New total: {_inr(build_card.total_price_inr)}\n")


def run_refinement(state: PipelineState) -> PipelineState:
    """PHASE 5 — interactive refinement loop.

    Owns the input()/print() loop; all logic lives in pure functions in
    agents/nodes/node3_refinement.py (dispatch_refinement / diff_and_bias). The
    loop-scope `locked_parts` dict (slot_name -> product_id) is threaded through
    dispatch and persisted back into PipelineState["locked_parts"] on exit.
    restart re-enters the graph from inside dispatch via graph_runner, never a
    graph edge — graph.py topology is unchanged.
    """
    _print_header("PHASE 5  -  REFINEMENT")

    brief = state.get("current_brief")
    bands = state.get("price_bands")
    build_card = state.get("build_card")

    if brief is None or bands is None or build_card is None or not build_card.parts:
        print("  No parts to refine  -  skipping refinement loop.")
        return {**state, "locked_parts": {}, "current_node": "done"}

    refinement_mode = os.getenv("KARMA_REFINEMENT_MODE", "legacy")
    print(f"  [Refinement mode: {refinement_mode}]")

    locked_parts: dict[str, str] = {}
    cache = ThresholdCache()
    round_count = 0
    history: list[dict] = []

    print("\n  You can refine the build below. Try: 'pin gpu', 'reject the psu',")
    print("  'set budget to 90k', 'target 1440p', or 'accept' to finalize.")

    while round_count < MAX_REFINEMENT_ROUNDS:
        try:
            user_msg = input(
                f"\n  Refine [{round_count + 1}/{MAX_REFINEMENT_ROUNDS}] "
                f"(or 'accept'): "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  [Interrupted]  -  keeping the current build.")
            break

        if not user_msg:
            print("  (Empty input  -  type 'accept' to finish, or describe a change.)")
            continue

        if refinement_mode == "intent":
            try:
                plan = parse_refinement_request_v2(user_msg, brief, build_card, history)
            except StructuredCallError as exc:
                print(f"  [WARNING] Could not parse that request: {exc}")
                continue

            try:
                result = dispatch_refinement_v2(plan, brief, bands, build_card, locked_parts, cache)
            except Exception as exc:  # noqa: BLE001 — a bad round must not kill the session
                print(f"  [WARNING] Refinement step failed ({type(exc).__name__}: {exc}).")
                round_count += 1
                continue

            history.append({
                "user_msg": user_msg,
                "applied_intents": [i.model_dump(mode="json") for i in plan.intents],
            })
        else:
            try:
                ops = parse_refinement_request(user_msg, brief, build_card)
            except StructuredCallError as exc:
                print(f"  [WARNING] Could not parse that request: {exc}")
                continue

            try:
                result = dispatch_refinement(ops, brief, bands, build_card, locked_parts, cache)
            except Exception as exc:  # noqa: BLE001 — a bad round must not kill the session
                print(f"  [WARNING] Refinement step failed ({type(exc).__name__}: {exc}).")
                round_count += 1
                continue

        brief = result.brief
        bands = result.price_bands
        if result.message:
            print(f"  {result.message}")

        if result.accepted:
            print(f"\n  Build accepted  -  shipping {len(result.product_ids)} product IDs to backend:")
            print("    " + ", ".join(result.product_ids))
            build_card = result.build_card
            break

        prev_card = build_card
        build_card = result.build_card
        if build_card is not prev_card:
            if build_card.changed_slots:
                _print_build_diff(build_card)
            else:
                # A restart (or an unchanged re-solve) returns a fresh card — show it in full.
                print()
                print(format_build_card(build_card, brief))

        round_count += 1
    else:
        print(f"\n  Maximum refinement rounds ({MAX_REFINEMENT_ROUNDS}) reached.")

    return {
        **state,
        "current_brief": brief,
        "price_bands": bands,
        "build_card": build_card,
        "locked_parts": dict(locked_parts),
        "current_node": "done",
    }

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = sys.argv[1:]
    if "--help" in args or "-h" in args:
        print(__doc__)
        sys.exit(0)

    fixture_all = "--fixture-all" in args

    fixture_path: Path | None = None
    if "--fixture" in args:
        idx = args.index("--fixture")
        if idx + 1 >= len(args) or args[idx + 1].startswith("--"):
            print("  [ERROR] --fixture requires a path argument, e.g. --fixture data/fixtures/budget_gamer.json")
            sys.exit(1)
        fixture_path = Path(__file__).parent / args[idx + 1]
        if not fixture_path.exists():
            print(f"  [ERROR] Fixture not found: {fixture_path}")
            sys.exit(1)

    print(f"\n{_SEP}")
    print("  KARMA AI   -   PC Build Recommendation  (Phase 1 Pipeline)")
    print(_SEP)

    # ------------------------------------------------------------------
    # --fixture-all  :  run all three canonical fixtures, print table
    # ------------------------------------------------------------------
    if fixture_all:
        print(f"\n  FIXTURE MODE — skipping intake")
        print(f"  Running all {len(_FIXTURE_ALL_PATHS)} fixtures through feasibility + allocation.\n")
        summary: list[tuple[str, FeasibilityVerdict | None, PriceBands | None]] = []
        for label, path in _FIXTURE_ALL_PATHS.items():
            _print_header(f"FIXTURE: {label}")
            brief = _load_fixture_brief(path)
            verdict, bands = _run_fixture_quiet(label, brief)
            if verdict:
                _print_verdict(verdict)
            if bands:
                print("\n  Price bands per component (INR):\n")
                print(format_price_bands(bands))
            summary.append((label, verdict, bands))

        _print_header("SUMMARY")
        _print_fixture_summary(summary)
        print()
        return

    # ------------------------------------------------------------------
    # --fixture <path>  :  single fixture, full output
    # ------------------------------------------------------------------
    state = new_state()

    if fixture_path is not None:
        print(f"\n  FIXTURE MODE — skipping intake")
        print(f"  Loading: {fixture_path.name}")
        state = {
            **state,
            "current_brief": _load_fixture_brief(fixture_path),
            "current_node": "feasibility",
        }
    else:
        state = run_intake(state)

    state = run_feasibility(state)
    state = run_allocation(state)
    state = run_selection(state)
    state = run_refinement(state)

    verdict = state.get("feasibility_verdict")
    bands   = state.get("price_bands")
    build_card = state.get("build_card")
    band_count = len(bands.root) if bands else 0
    parts_count = len(build_card.parts) if build_card else 0
    verdict_label = verdict.verdict if verdict else "n/a"

    print(f"\n{_SEP}")
    print("  PIPELINE COMPLETE")
    print(
        f"  Feasibility: {verdict_label}  |  Price bands: {band_count} slots  |  "
        f"Build: {parts_count}/{band_count} slots filled"
    )
    print(_SEP + "\n")


if __name__ == "__main__":
    main()
