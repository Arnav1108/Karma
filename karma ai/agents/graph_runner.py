"""agents/graph_runner.py — Non-interactive runner for the Karma AI graph.

Used by the future API layer and fixture tests to drive the pipeline when
the intake brief is already known (fixture mode, API calls, integration tests).
Skips node_intake by pre-loading brief + price_bands into the initial state and
invoking the graph starting at node_feasibility.
"""
from __future__ import annotations

from agents.graph import karma_graph
from agents.schemas import PriceBands, UserBuildBrief
from agents.state.pipeline_state import PipelineState, new_state


def run_from_brief(brief: UserBuildBrief, price_bands: PriceBands) -> PipelineState:
    """Run the pipeline from node_feasibility onward with a pre-built brief.

    Args:
        brief: A locked UserBuildBrief (from a fixture, API payload, or intake run).
        price_bands: Pre-computed PriceBands (optional — node_allocate will
            recompute if these are placeholder values).

    Returns:
        Final PipelineState after the graph reaches END.
    """
    initial: PipelineState = {
        **new_state(),
        "current_brief": brief,
        "price_bands": price_bands,
        "current_node": "node_feasibility",
    }

    # Invoke the graph starting from node_feasibility by supplying the brief as
    # already-locked, which causes _route_after_intake to jump to node_feasibility.
    # We pass the state directly to the graph's invoke method; LangGraph runs all
    # nodes from START but node_intake will immediately route to node_feasibility
    # because brief.status == "locked".
    final: PipelineState = karma_graph.invoke(initial)  # type: ignore[assignment]
    return final
