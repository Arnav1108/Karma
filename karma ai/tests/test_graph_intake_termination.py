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
    monkeypatch.setattr(graph, "next_question", lambda brief, asked, attempts=None: None)

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
    monkeypatch.setattr(graph, "next_question", lambda brief, asked, attempts=None: None)

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
    monkeypatch.setattr(graph, "next_question", lambda brief, asked, attempts=None: None)

    brief = _unlocked_brief_floor_unmet()
    data = brief.model_dump()
    data["budget"]["comfortable_max"] = 100000
    from agents.schemas.brief import UserBuildBrief
    brief = UserBuildBrief.model_validate(data)
    assert floor_met(brief) is True

    result = graph.node_intake({"current_brief": brief, "conversation_history": []})

    assert result["current_node"] == "node_feasibility"
    assert result["current_brief"].status == "locked"


def test_node_intake_open_question_end_to_end(monkeypatch):
    """End-to-end, node_intake-level (not just the extract_turn/next_question unit
    tests in test_node1_intake.py): an ambiguous performance answer raises a real
    follow-up question that node_intake itself serves ahead of the next
    QUESTION_SEQUENCE item, stays ambiguous once (advancing to the confirm-to
    -default prompt), and is accepted -- with open_question_attempts round-tripping
    through PipelineState across all three node_intake invocations, the same way
    conversation_history/fitness_thresholds already do under checkpointer resumption.
    """
    from uuid import uuid4

    from agents.nodes.node1_intake import _IntakeDelta
    from agents.schemas.brief import UserBuildBrief
    from agents.schemas.source_flag import SourceFlag

    brief = blank_brief(brief_id=uuid4(), user_id=uuid4(), chat_id=uuid4())
    data = brief.model_dump()
    data["budget"].update(comfortable_min=50000, comfortable_max=90000, ceiling=100000)
    data["purpose"]["sub_case"] = "AAA gaming"
    data["software"] = [
        {"name": "Cyberpunk 2077", "category": "game", "frequency": "primary", "intensity": "heavy"}
    ]
    brief = UserBuildBrief.model_validate(data)

    state = {"current_brief": brief, "conversation_history": []}
    clarification = "Which resolution and refresh rate are you targeting?"

    def _ask_and_answer(state, answer, delta):
        question = graph.next_question(
            state["current_brief"], set(), state.get("open_question_attempts") or {},
        )
        monkeypatch.setattr("agents.nodes.node1_intake.call_structured", lambda *a, **k: delta)
        state["conversation_history"] = state["conversation_history"] + [
            {"role": "assistant", "content": question},
            {"role": "user", "content": answer},
        ]
        result = graph.node_intake(state)
        return {**state, **result}, question

    # Turn 1 -- ambiguous answer to the real "performance" question (budget,
    # primary_use_case, software are pre-filled so the sequence walk lands there).
    state, q1 = _ask_and_answer(
        state, "I want it to look good", _IntakeDelta(clarification_needed=clarification),
    )
    assert q1 != clarification
    assert state["current_brief"].open_questions == [clarification]
    assert state["current_node"] == "node_intake"

    # Turn 2 -- the open question is served BEFORE any other QUESTION_SEQUENCE
    # item, and is still ambiguous -> advances to the confirm-to-default prompt.
    state, q2 = _ask_and_answer(
        state, "just make it look nice", _IntakeDelta(clarification_needed=clarification),
    )
    assert q2 == clarification
    assert state["open_question_attempts"] == {clarification: 1}
    assert state["current_brief"].open_questions == [clarification]

    # Turn 3 -- the confirm prompt is now what gets served; user accepts the default.
    q3 = graph.next_question(state["current_brief"], set(), state["open_question_attempts"])
    assert q3 == "I still can't pin this down — should I go with a sensible default and move on?"

    state, _ = _ask_and_answer(state, "yes", _IntakeDelta())

    assert state["current_brief"].open_questions == []
    assert state["current_brief"].performance.source == SourceFlag.skipped_by_user
    assert state["open_question_attempts"] == {}


def test_skipped_by_user_field_not_reasked_on_next_invocation(monkeypatch):
    """Regression for the _is_field_filled gap: a field resolved via
    skipped_by_user must read as "filled" on the very next node_intake
    invocation -- not "unfilled" -- or current_question_id/next_question would
    re-serve the same already-resolved question forever, the same failure
    shape as the pre-fix infinite intake loop (see module docstring).

    Drives node_intake through the full 3-ask cycle to force performance into
    skipped_by_user (same shape as the end-to-end test above), then makes one
    more node_intake invocation and confirms: (a) _is_field_filled now reports
    performance as filled, (b) the next question served moves on to the next
    unfilled section (monitor) instead of re-asking performance, and (c) the
    resolved source flag and empty open_questions are left undisturbed.
    """
    from uuid import uuid4

    from agents.nodes.node1_intake import _IntakeDelta, _is_field_filled
    from agents.schemas.brief import UserBuildBrief
    from agents.schemas.source_flag import SourceFlag

    # next_question's QUESTION_SEQUENCE branch phrases raw_text via call_text --
    # mock it to identity so assertions can inspect which section's raw_text
    # was selected, without hitting a real LLM call.
    monkeypatch.setattr(
        "agents.nodes.node1_intake.call_text", lambda text, system=None: text,
    )

    brief = blank_brief(brief_id=uuid4(), user_id=uuid4(), chat_id=uuid4())
    data = brief.model_dump()
    data["budget"].update(comfortable_min=50000, comfortable_max=90000, ceiling=100000)
    data["purpose"]["sub_case"] = "AAA gaming"
    data["software"] = [
        {"name": "Cyberpunk 2077", "category": "game", "frequency": "primary", "intensity": "heavy"}
    ]
    brief = UserBuildBrief.model_validate(data)

    state = {"current_brief": brief, "conversation_history": []}
    clarification = "Which resolution and refresh rate are you targeting?"

    def _ask_and_answer(state, answer, delta):
        question = graph.next_question(
            state["current_brief"], set(), state.get("open_question_attempts") or {},
        )
        monkeypatch.setattr("agents.nodes.node1_intake.call_structured", lambda *a, **k: delta)
        state["conversation_history"] = state["conversation_history"] + [
            {"role": "assistant", "content": question},
            {"role": "user", "content": answer},
        ]
        result = graph.node_intake(state)
        return {**state, **result}, question

    # Force performance to skipped_by_user via the full 3-ask cycle.
    state, _ = _ask_and_answer(
        state, "I want good graphics", _IntakeDelta(clarification_needed=clarification),
    )
    state, _ = _ask_and_answer(
        state, "just make it look nice", _IntakeDelta(clarification_needed=clarification),
    )
    state, _ = _ask_and_answer(state, "yes", _IntakeDelta())

    assert state["current_brief"].performance.source == SourceFlag.skipped_by_user
    assert _is_field_filled(state["current_brief"], "performance") is True

    # One further node_intake invocation must not re-ask about performance --
    # it should move on to the next unfilled section (monitor) instead.
    next_q = graph.next_question(
        state["current_brief"], set(), state["open_question_attempts"],
    )
    assert next_q != clarification
    assert "monitor" in next_q.lower()
    assert "visual quality" not in next_q.lower()

    monkeypatch.setattr(
        "agents.nodes.node1_intake.call_structured", lambda *a, **k: _IntakeDelta(),
    )
    state["conversation_history"] = state["conversation_history"] + [
        {"role": "assistant", "content": next_q},
        {"role": "user", "content": "no strong preference either way"},
    ]
    result = graph.node_intake({**state})

    # performance's resolution must be undisturbed by the follow-on turn, and
    # open_questions must not have been repopulated for it.
    assert result["current_brief"].performance.source == SourceFlag.skipped_by_user
    assert result["current_brief"].open_questions == []
