"""Regression test for graph.py's intake termination bug.

Bug: _route_after_intake routed purely on brief.status == "locked", while
node_intake's own "no more questions to ask" branch never set that status (it only
communicated its decision via the state["current_node"] key, which the router
ignored). Any unlocked brief entering the graph where next_question() returns None
-- i.e. the question sequence is exhausted -- would loop node_intake -> node_intake
forever regardless of whether the floor (budget + primary use case) was ever met,
until LangGraph's recursion limit raised GraphRecursionError.

Fix: node_intake is now the sole owner of the termination decision. When the
question sequence is exhausted it checks floor_met() itself: if met, it locks the
brief and hands off to node_feasibility; if not met, it routes to the new terminal
node_cannot_proceed instead of looping. _route_after_intake just relays
state["current_node"] rather than re-deriving the decision from brief.status.

Hermetic -- next_question is monkeypatched to simulate "no questions left" without
any LLM call, so this isolates the routing/termination logic itself.
"""

from __future__ import annotations

from uuid import uuid4

import agents.graph as graph
from agents.nodes.node1_intake import blank_brief, floor_met


def _unlocked_brief_floor_unmet():
    brief = blank_brief(brief_id=uuid4(), user_id=uuid4(), chat_id=uuid4())
    assert brief.status == "draft"
    assert floor_met(brief) is False  # budget.comfortable_max == 0 (sentinel)
    return brief


def test_node_intake_routes_to_cannot_proceed_when_exhausted_and_floor_unmet(monkeypatch):
    """Unit-level: node_intake itself must not claim "node_feasibility" for a
    brief that fails the floor gate just because there are no more questions."""
    monkeypatch.setattr(graph, "next_question", lambda brief, asked: None)

    brief = _unlocked_brief_floor_unmet()
    result = graph.node_intake({"current_brief": brief, "conversation_history": []})

    assert result["current_node"] == "node_cannot_proceed"
    assert result.get("error_message")
    # Must not have silently locked a brief that fails the floor gate.
    assert brief.status == "draft"


def test_router_relays_node_intake_decision(monkeypatch):
    """_route_after_intake must trust state["current_node"], not re-derive from
    brief.status -- otherwise it can disagree with node_intake and loop forever."""
    state = {
        "current_brief": _unlocked_brief_floor_unmet(),
        "current_node": "node_cannot_proceed",
    }
    assert graph._route_after_intake(state) == "node_cannot_proceed"


def test_full_graph_terminates_when_intake_exhausted_with_floor_unmet(monkeypatch):
    """End-to-end: invoking the compiled graph with an unlocked, floor-unmet brief
    whose question sequence is exhausted must terminate at node_cannot_proceed --
    not loop node_intake -> node_intake until GraphRecursionError."""
    monkeypatch.setattr(graph, "next_question", lambda brief, asked: None)

    initial_state = {
        "current_brief": _unlocked_brief_floor_unmet(),
        "conversation_history": [],
        "current_node": "node_intake",
    }

    # Must not raise GraphRecursionError (the pre-fix behavior).
    final_state = graph.karma_graph.invoke(initial_state)

    assert final_state.get("error_message")
    assert final_state["current_brief"].status != "locked"


def test_node_intake_locks_and_routes_to_feasibility_when_exhausted_with_floor_met(monkeypatch):
    """Negative control: when the floor IS met and questions are exhausted,
    node_intake must auto-lock and route to node_feasibility -- proving the fix
    doesn't dead-end every exhausted brief, only ones that fail the floor gate.

    Unit-level (calls node_intake directly, not the full graph) so it doesn't
    exercise node_allocate/node_select's real LLM/DB/Neo4j dependencies.
    """
    monkeypatch.setattr(graph, "next_question", lambda brief, asked: None)

    brief = _unlocked_brief_floor_unmet()
    data = brief.model_dump()
    data["budget"]["comfortable_max"] = 100000
    from agents.schemas.brief import UserBuildBrief
    brief = UserBuildBrief.model_validate(data)
    assert floor_met(brief) is True

    result = graph.node_intake({"current_brief": brief, "conversation_history": []})

    assert result["current_node"] == "node_feasibility"
    assert result["current_brief"].status == "locked"
