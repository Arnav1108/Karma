"""
run_pipeline.py  -  CLI harness for Karma AI Phase 1 pipeline.

Wires four stages end to end:
  PHASE 1  Intake        -  conversational Q&A -> UserBuildBrief
  PHASE 2  Feasibility   -  rough gate (comfortable / tight / impossible)
  PHASE 3  Allocation    -  price bands per component
  PHASE 4  Selection     -  part finder funnel -> build card

Modules may not be merged yet; this harness imports them defensively
and falls back to clearly-labelled stubs so the pipeline runs skeletally now.
Stubs drop out automatically once the real modules land.

Usage:
  python run_pipeline.py                                    # full conversational run
  python run_pipeline.py --fixture data/fixtures/budget_gamer.json
  python run_pipeline.py --fixture-all                      # run all 3 fixtures, print summary
  python run_pipeline.py --help                             # print this message and exit
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
from agents.schemas.build_card import BuildCard  # noqa: E402
from agents.schemas.price_bands import PriceBand, PriceBands  # noqa: E402
from agents.output.formatter import format_build_card  # noqa: E402
from agents.state.pipeline_state import PipelineState, new_state  # noqa: E402

# ---------------------------------------------------------------------------
# Defensive imports  -  _HAS_* flags control real vs stub dispatch.
# Only ImportError is caught: runtime errors inside a module must surface.
# ---------------------------------------------------------------------------

try:
    from agents.nodes.node1_intake import (
        IntakeInterrupted,
        blank_brief,
        drive_intake,
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
    from agents.nodes.node2_allocation import allocate_budget
    _HAS_ALLOC = True
except ImportError:
    _HAS_ALLOC = False

try:
    from agents.nodes.node3_selector import select_build
    _HAS_SELECT = True
except ImportError:
    _HAS_SELECT = False

try:
    from agents.nodes.node3_selector import ThresholdCache
    from agents.nodes.node3_refinement import (
        MAX_REFINEMENT_ROUNDS,
        dispatch_refinement,
        parse_refinement_request,
    )
    from agents.llm.client import StructuredCallError
    _HAS_REFINE = True
except ImportError:
    _HAS_REFINE = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).parent / "data" / "fixtures"

_FIXTURE_ALL_PATHS: dict[str, Path] = {
    "budget_gamer":   _FIXTURES_DIR / "budget_gamer.json",
    "video_editor":   _FIXTURES_DIR / "video_editor.json",
    "ml_workstation": _FIXTURES_DIR / "ml_workstation.json",
}

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
        basis="stub",
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


def _stub_select_build(brief: UserBuildBrief, price_bands: PriceBands) -> BuildCard:
    # TODO: Remove when agents/nodes/node3_selector.py merges.
    # EXPECTED INTERFACE:
    #   select_build(brief, price_bands, feasibility_verdict=None, cache=None) -> BuildCard
    #   Three-step funnel per slot (Postgres catalog -> Neo4j graph filter -> LLM
    #   pick), walked in SELECTION_ORDER. Returns a BuildCard with parts, total
    #   price, summary, and any dead-end warnings.
    print("  [STUB] select_build not available  -  returning empty build card.")
    return BuildCard(parts=[], total_price_inr=0, summary="[STUB] no parts selected", warnings=[])

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
    print(f"  node3_selector    {tag(_HAS_SELECT)}")


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
# Fixture-all helpers
# ---------------------------------------------------------------------------

def _run_fixture_quiet(
    label: str,
    brief: UserBuildBrief,
) -> tuple[FeasibilityVerdict | None, PriceBands | None]:
    """Run feasibility + allocation for one fixture without sys.exit on impossible."""
    if _HAS_ESTIMATE:
        try:
            verdict = estimate_feasibility(brief)
        except Exception as exc:
            print(f"  [{label}] estimate_feasibility raised {type(exc).__name__}: {exc}")
            verdict = _stub_estimate_feasibility(brief)
    else:
        verdict = _stub_estimate_feasibility(brief)

    if verdict.verdict == "impossible":
        return verdict, None

    if _HAS_ALLOC:
        try:
            bands = allocate_budget(brief)
        except Exception as exc:
            print(f"  [{label}] allocate raised {type(exc).__name__}: {exc}")
            bands = _stub_allocate(brief)
    else:
        bands = _stub_allocate(brief)

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

    if not _HAS_NODE1:
        print("  [STUB] node1_intake not available  -  loading fixture brief (budget_gamer.json).")
        state = {**state, "current_brief": _load_fixture_brief(_FIXTURE_ALL_PATHS["budget_gamer"])}
        return _run_stub_intake_loop(state)

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


def _run_stub_intake_loop(state: PipelineState) -> PipelineState:
    """Legacy stub-mode intake loop, used only when node1_intake fails to import.

    Kept as its own inline loop (not routed through drive_intake, which is a
    real-node1_intake-only driver) since the stub's question/extraction/exit
    logic is entirely separate stand-in behavior for early scaffolding.
    """
    asked_so_far: set[str] = set()
    turn_count = 0

    while True:
        brief = state["current_brief"]

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

        history = list(state.get("conversation_history") or [])
        history.append({"role": "assistant", "content": question})
        history.append({"role": "user", "content": user_input})
        brief = _stub_extract_turn(user_input, brief, history)
        state = {**state, "current_brief": brief, "conversation_history": history}

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

    # Lock brief if the question set exhausted naturally.
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
            bands = allocate_budget(brief)
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

    if _HAS_SELECT:
        try:
            build_card = select_build(brief, bands, feasibility_verdict=verdict)
        except Exception as exc:
            print(f"  [WARNING] select_build raised {type(exc).__name__}: {exc}")
            print("  [FALLBACK] Using stub build card.")
            build_card = _stub_select_build(brief, bands)
    else:
        build_card = _stub_select_build(brief, bands)

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

    if not _HAS_REFINE:
        print("  [STUB] refinement not available  -  skipping refinement loop.")
        return {**state, "locked_parts": {}, "current_node": "done"}
    if brief is None or bands is None or build_card is None or not build_card.parts:
        print("  No parts to refine  -  skipping refinement loop.")
        return {**state, "locked_parts": {}, "current_node": "done"}

    locked_parts: dict[str, str] = {}
    cache = ThresholdCache()
    round_count = 0

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

    _print_module_status()

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
                _print_price_bands(bands)
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
