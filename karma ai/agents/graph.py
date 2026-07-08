"""agents/graph.py — LangGraph StateGraph for the Karma AI pipeline.

Topology (DESIGN.md §1 flowchart):

    START → node_intake ─┬─ (still asking)         → node_intake (loop)
                          ├─ (locked)               → node_feasibility
                          │                             ├─ comfortable/tight → node_allocate → node_select → END
                          │                             └─ impossible        → node_surface_failure        → END
                          └─ (exhausted, floor unmet) → node_cannot_proceed  → END

node_intake is ONE TURN only — the conversation loop lives in run_pipeline.py.
This graph is the engine; run_pipeline.py is the CLI driver.
"""
from __future__ import annotations

from typing import Literal

from langgraph.graph import END, START, StateGraph

from agents.state.pipeline_state import PipelineState

# ---------------------------------------------------------------------------
# Defensive node imports — node3_selector may not be merged yet.
# ---------------------------------------------------------------------------

try:
    from agents.nodes.node1_intake import (
        QUESTION_SEQUENCE,
        _is_field_filled,
        extract_turn,
        floor_met,
        lock_brief,
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
    from agents.nodes.node2_allocation import allocate_budget
    _HAS_ALLOC = True
except ImportError:
    _HAS_ALLOC = False

try:
    from agents.nodes.node3_selector import ThresholdCache, select_build
    _HAS_SELECT = True
except ImportError:
    _HAS_SELECT = False


# ---------------------------------------------------------------------------
# Node functions — each accepts PipelineState, returns a partial PipelineState
# (only the keys that changed; LangGraph merges it into the running state).
# ---------------------------------------------------------------------------

def node_intake(state: PipelineState) -> PipelineState:
    """One turn of intake: ask the next question, get the answer, update brief.

    Designed for checkpointer resumption — the conversation loop in
    run_pipeline.py calls this node repeatedly until brief.status == 'locked'.

    This function is the sole owner of the intake termination decision (via the
    "current_node" key it returns); _route_after_intake just relays that decision.
    It terminates one of three ways: loop back (still asking), hand off to
    node_feasibility (locked, or exhausted with the floor met), or bail out to
    node_cannot_proceed (exhausted with the floor unmet) — it never loops forever.
    """
    brief = state.get("current_brief")
    history = list(state.get("conversation_history") or [])

    if brief is None or (hasattr(brief, "status") and brief.status == "locked"):
        return {"current_node": "node_feasibility"}  # type: ignore[return-value]

    if not _HAS_NODE1:
        return {"current_node": "node_intake"}  # type: ignore[return-value]

    # open_question_attempts persists across repeated node_intake invocations the
    # same way conversation_history/fitness_thresholds already do: read the
    # incoming copy, mutate it (extract_turn/next_question mutate in place), hand
    # the same dict back in the returned partial state.
    open_question_attempts: dict[str, int] = dict(state.get("open_question_attempts") or {})

    question = next_question(brief, set(), open_question_attempts)
    if question is None:
        # Question sequence exhausted. Only proceed if the floor gate (budget +
        # primary use case) is actually met — never silently lock a brief that
        # fails it just to end the loop.
        if floor_met(brief):
            return {  # type: ignore[return-value]
                "current_brief": lock_brief(brief),
                "current_node": "node_feasibility",
            }
        return {  # type: ignore[return-value]
            "error_message": (
                "Cannot proceed: the intake question sequence is exhausted but "
                "required information (budget and/or primary use case) is still "
                "missing."
            ),
            "current_node": "node_cannot_proceed",
        }

    # current_question_id: node_intake has no persisted asked_so_far (each
    # invocation walks the sequence fresh from the brief alone), so this mirrors
    # next_question's own internal walk — with an empty asked_so_far its only
    # filter is _is_field_filled — to identify the SAME field next_question just
    # served, whether that's a fresh QUESTION_SEQUENCE item or an open question
    # still being resolved (its field stays "unfilled" for the whole 3-ask cycle,
    # by construction, so the same walk finds it in both cases).
    current_question_id = next(
        (q.id for q in QUESTION_SEQUENCE if not _is_field_filled(brief, q.id)),
        None,
    )

    # In graph mode, the harness supplies the user answer via state before
    # invoking this node.  Pull it from the last user turn in history.
    user_answer: str = ""
    for turn in reversed(history):
        if turn.get("role") == "user":
            user_answer = turn["content"]
            break

    updated_brief = extract_turn(
        user_answer,
        brief,
        history,
        current_question_id=current_question_id,
        open_question_attempts=open_question_attempts,
    )
    history.append({"role": "user", "content": user_answer})

    return {  # type: ignore[return-value]
        "current_brief": updated_brief,
        "conversation_history": history,
        "open_question_attempts": open_question_attempts,
        "current_node": "node_feasibility" if getattr(updated_brief, "status", None) == "locked" else "node_intake",
    }


def node_feasibility(state: PipelineState) -> PipelineState:
    """Run the feasibility check and store the verdict."""
    brief = state.get("current_brief")
    if brief is None:
        return {  # type: ignore[return-value]
            "error_message": "node_feasibility: no brief in state",
            "current_node": "node_surface_failure",
        }

    if _HAS_ESTIMATE:
        verdict = estimate_feasibility(brief)
    else:
        from agents.schemas import FeasibilityVerdict
        verdict = FeasibilityVerdict(
            verdict="comfortable",
            basis="stub",
            reason="[STUB] estimate_feasibility not available",
            binding_constraint=None,
            suggested_adjustments=[],
        )

    return {  # type: ignore[return-value]
        "feasibility_verdict": verdict,
        "current_node": "node_allocate" if verdict.verdict != "impossible" else "node_surface_failure",
    }


def node_allocate(state: PipelineState) -> PipelineState:
    """Run budget allocation and store price bands."""
    brief = state.get("current_brief")
    if brief is None:
        return {  # type: ignore[return-value]
            "error_message": "node_allocate: no brief in state",
            "current_node": "node_surface_failure",
        }

    if _HAS_ALLOC:
        bands = allocate_budget(brief)
    else:
        from agents.schemas import PriceBands
        from agents.schemas.price_bands import PriceBand
        from agents.schemas import ComponentSlot
        _stub: dict[str, dict[str, int]] = {
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
        bands = PriceBands(root={ComponentSlot(s): PriceBand(**v) for s, v in _stub.items()})

    return {  # type: ignore[return-value]
        "price_bands": bands,
        "current_node": "node_select",
    }


def node_select(state: PipelineState) -> PipelineState:
    """Run Node 3 part selection and store the build card.

    Rehydrates the ThresholdCache from the incoming state's fitness_thresholds /
    fitness_thresholds_key (if present) so select_build's cache-hit check
    (_threshold_key(brief) == cache.key) can actually fire across repeated
    node_select invocations, instead of re-deriving thresholds every call.
    """
    if not _HAS_SELECT:
        return {  # type: ignore[return-value]
            "current_node": "done",
        }

    from agents.schemas import ComponentSlot

    brief = state.get("current_brief")
    bands = state.get("price_bands")
    verdict = state.get("feasibility_verdict")

    stored_thresholds = state.get("fitness_thresholds")
    cache = ThresholdCache(
        thresholds=(
            {ComponentSlot(s): v for s, v in stored_thresholds.items()}
            if stored_thresholds
            else None
        ),
        key=state.get("fitness_thresholds_key"),
    )
    build_card = select_build(brief, bands, feasibility_verdict=verdict, cache=cache)

    return {  # type: ignore[return-value]
        "build_card": build_card,
        "fitness_thresholds": {s.value: v for s, v in (cache.thresholds or {}).items()},
        "fitness_thresholds_key": cache.key,
        "current_node": "done",
    }


def node_cannot_proceed(state: PipelineState) -> PipelineState:
    """Terminal node: intake exhausted its question sequence without meeting the floor.

    Reached when no further questions remain to ask but budget and/or primary use
    case were never provided (e.g. extraction never filled them, or a caller drove
    the graph with an already-partial, never-completing brief). Surfaces a message
    and ends the run instead of looping back to node_intake forever.
    """
    existing_error = state.get("error_message")
    message = existing_error or (
        "Cannot proceed: the intake question sequence is exhausted but the "
        "required information (budget and primary use case) was never provided."
    )
    return {  # type: ignore[return-value]
        "error_message": message,
        "current_node": "done",
    }


def node_surface_failure(state: PipelineState) -> PipelineState:
    """Format an error message for an impossible verdict or upstream failure."""
    verdict = state.get("feasibility_verdict")
    existing_error = state.get("error_message")

    if existing_error:
        message = existing_error
    elif verdict is not None:
        constraints = f"  Binding constraint: {verdict.binding_constraint}" if verdict.binding_constraint else ""
        adjustments = (
            "\n  Suggested adjustments:\n" + "\n".join(f"    - {a}" for a in verdict.suggested_adjustments)
            if verdict.suggested_adjustments
            else ""
        )
        message = (
            f"Build is IMPOSSIBLE within the stated budget.\n"
            f"  Reason: {verdict.reason}\n"
            f"{constraints}{adjustments}"
        )
    else:
        message = "Build cannot proceed: unknown failure."

    return {  # type: ignore[return-value]
        "error_message": message,
        "current_node": "done",
    }


# ---------------------------------------------------------------------------
# Conditional routing
# ---------------------------------------------------------------------------

def _route_after_intake(
    state: PipelineState,
) -> Literal["node_feasibility", "node_intake", "node_cannot_proceed"]:
    """Route based on node_intake's own decision (state["current_node"]).

    node_intake is the sole place that decides whether to keep asking, hand off
    to feasibility, or bail out to node_cannot_proceed — this router must not
    re-derive that decision from brief.status, or it can disagree with node_intake
    and loop back to it forever (e.g. when the question sequence is exhausted but
    node_intake didn't/couldn't lock the brief).
    """
    current_node = state.get("current_node")
    if current_node in ("node_feasibility", "node_cannot_proceed"):
        return current_node
    return "node_intake"


def _route_after_feasibility(
    state: PipelineState,
) -> Literal["node_allocate", "node_surface_failure"]:
    verdict = state.get("feasibility_verdict")
    if verdict is not None and verdict.verdict == "impossible":
        return "node_surface_failure"
    return "node_allocate"


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

builder: StateGraph = StateGraph(PipelineState)

builder.add_node("node_intake", node_intake)
builder.add_node("node_feasibility", node_feasibility)
builder.add_node("node_allocate", node_allocate)
builder.add_node("node_select", node_select)
builder.add_node("node_cannot_proceed", node_cannot_proceed)
builder.add_node("node_surface_failure", node_surface_failure)

builder.add_edge(START, "node_intake")
builder.add_conditional_edges(
    "node_intake",
    _route_after_intake,
    {
        "node_feasibility": "node_feasibility",
        "node_intake": "node_intake",
        "node_cannot_proceed": "node_cannot_proceed",
    },
)
builder.add_conditional_edges(
    "node_feasibility",
    _route_after_feasibility,
    {"node_allocate": "node_allocate", "node_surface_failure": "node_surface_failure"},
)
builder.add_edge("node_allocate", "node_select")
builder.add_edge("node_select", END)
builder.add_edge("node_cannot_proceed", END)
builder.add_edge("node_surface_failure", END)

karma_graph = builder.compile()
