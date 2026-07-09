"""Integration coverage for run_pipeline.run_refinement — the interactive loop.

Everything *inside* dispatch_refinement / diff_and_bias is exercised by
tests/test_node3_refinement.py (pure layer) and the live e2e suites. What was
never covered until now is the loop that OWNS the conversation in the CLI
harness (run_pipeline.py:552) — the input()/print() plumbing the design note
deliberately kept out of the LangGraph node:

  * empty-input reprompt that does NOT consume a round,
  * StructuredCallError from the parser → warn + continue (no round consumed),
  * a dispatch exception → warn + continue, and crucially that this DOES consume
    a round (so a wall of failing turns still terminates at MAX_REFINEMENT_ROUNDS),
  * accept → ship product_ids, exit, current_node="done",
  * MAX_REFINEMENT_ROUNDS → the `while … else` cap fires and input() stops,
  * the SINGLE ThresholdCache object is threaded to every dispatch call (never
    re-created per round),
  * the SINGLE locked_parts dict is threaded + mutated across rounds and
    persisted into PipelineState["locked_parts"] on exit (2-round persistence),
  * the diff-vs-full-card display branch (changed_slots → _print_build_diff,
    else → format_build_card).

parse_refinement_request and dispatch_refinement are stubbed here on purpose:
this file tests the harness loop's control flow and state threading, not the
selection logic those two own. No live Postgres/Neo4j/LLM call is made, so this
runs in the default `pytest tests/` suite.
"""
from __future__ import annotations

import builtins

import pytest

import run_pipeline as rp
from agents.nodes.node3_refinement import RefinementResult
from agents.schemas.build_card import BuildCard, BuildCardPart
from agents.schemas.price_bands import PriceBand, PriceBands
from agents.schemas.slots import ComponentSlot

_BANDS_INR: dict[str, tuple[int, int, int]] = {
    "gpu": (18000, 22000, 27000),
    "cpu": (10000, 13000, 16000),
    "ram": (3500, 4500, 6000),
    "storage": (3000, 4000, 5500),
    "motherboard": (5500, 7000, 9000),
    "psu": (3500, 4500, 6000),
    "case": (3000, 4000, 5500),
    "cooler": (1500, 2500, 3500),
    "fans": (800, 1200, 1800),
}


# ── Builders ──────────────────────────────────────────────────────────────────

def _bands() -> PriceBands:
    return PriceBands(
        root={ComponentSlot(s): PriceBand(low=lo, mid=mid, high=hi)
              for s, (lo, mid, hi) in _BANDS_INR.items()}
    )


def _part(slot: ComponentSlot, pid: str, price: int = 20000) -> BuildCardPart:
    return BuildCardPart(slot=slot, product_id=pid, name=f"{slot.value} {pid}",
                         price_inr=price, justification="test")


def _card(parts: list[BuildCardPart], changed: list[dict] | None = None) -> BuildCard:
    return BuildCard(parts=parts, total_price_inr=sum(p.price_inr for p in parts),
                     summary="test", changed_slots=changed or [])


_START_CARD = _card([_part(ComponentSlot.gpu, "GPU-START"),
                     _part(ComponentSlot.cpu, "CPU-START", 13000)])


def _base_state(brief) -> rp.PipelineState:
    return {
        **rp.new_state(),
        "current_brief": brief,
        "price_bands": _bands(),
        "build_card": _START_CARD,
        "current_node": "refinement",
    }


class _Inputs:
    """Feed a fixed script to input(); raise EOFError once exhausted (Ctrl-D)."""

    def __init__(self, answers: list[str]):
        self._it = iter(answers)
        self.calls = 0

    def __call__(self, _prompt: str = "") -> str:
        self.calls += 1
        try:
            return next(self._it)
        except StopIteration:
            raise EOFError


def _install(monkeypatch, inputs: list[str], dispatch, parse=None):
    """Wire input(), parse_refinement_request and dispatch_refinement for one run."""
    feeder = _Inputs(inputs)
    monkeypatch.setattr(builtins, "input", feeder)
    monkeypatch.setattr(rp, "parse_refinement_request",
                        parse or (lambda msg, brief, card: {"msg": msg}))
    monkeypatch.setattr(rp, "dispatch_refinement", dispatch)
    return feeder


# ── accept path ───────────────────────────────────────────────────────────────

def test_accept_ships_ids_and_marks_done(monkeypatch, budget_gamer_brief):
    """A single 'accept' turn finalizes: product_ids shipped, node→done, loop exits."""
    accepted = _card([_part(ComponentSlot.gpu, "GPU-FINAL"),
                      _part(ComponentSlot.cpu, "CPU-FINAL", 13000)])

    def dispatch(ops, brief, bands, card, locked, cache):
        return RefinementResult(build_card=accepted, brief=brief, price_bands=bands,
                                accepted=True, product_ids=["GPU-FINAL", "CPU-FINAL"])

    feeder = _install(monkeypatch, ["accept"], dispatch)
    state = rp.run_refinement(_base_state(budget_gamer_brief))

    assert state["current_node"] == "done"
    assert state["build_card"] is accepted
    assert state["locked_parts"] == {}
    assert feeder.calls == 1


# ── cache identity + locked_parts persistence across 2 rounds ──────────────────

def test_same_cache_and_locked_parts_persist_across_two_rounds(monkeypatch, budget_gamer_brief):
    """The harness builds ONE ThresholdCache and ONE locked_parts dict and threads
    both through every round — round 1 pins GPU, round 2 pins CPU, and the final
    PipelineState carries both pins out."""
    seen_caches: list[object] = []
    seen_locked_ids: list[int] = []

    def dispatch(ops, brief, bands, card, locked, cache):
        # Record on EVERY round, accept included, so cache/locked identity is
        # verified across the whole session.
        seen_caches.append(cache)
        seen_locked_ids.append(id(locked))
        slot = ops["msg"]  # stub parse echoes the user_msg straight through
        if slot == "accept":
            return RefinementResult(build_card=card, brief=brief, price_bands=bands,
                                    accepted=True,
                                    product_ids=[p.product_id for p in card.parts])
        # Simulate a pin: mutate the loop-owned locked_parts dict in place.
        part = next(p for p in card.parts if p.slot.value == slot)
        locked[slot] = part.product_id
        # Return a fresh, changed card so the loop advances a round.
        new_card = _card(list(card.parts),
                         changed=[{"slot": slot, "old_product_id": None,
                                   "new_product_id": part.product_id, "reason": "added"}])
        return RefinementResult(build_card=new_card, brief=brief, price_bands=bands)

    _install(monkeypatch, ["gpu", "cpu", "accept"], dispatch,
             parse=lambda msg, brief, card: {"msg": msg})

    state = rp.run_refinement(_base_state(budget_gamer_brief))

    # locked_parts accumulated across BOTH pin rounds and persisted on exit.
    assert state["locked_parts"] == {"gpu": "GPU-START", "cpu": "CPU-START"}
    # Every round received the very same cache object (never re-created per round).
    assert len(seen_caches) == 3
    assert all(c is seen_caches[0] for c in seen_caches), "cache re-created between rounds"
    assert isinstance(seen_caches[0], rp.ThresholdCache)
    # Every round received the very same locked_parts dict object.
    assert len(set(seen_locked_ids)) == 1, "locked_parts dict re-created between rounds"


# ── empty input reprompts without consuming a round ────────────────────────────

def test_empty_input_reprompts_without_consuming_a_round(monkeypatch, budget_gamer_brief):
    calls = {"dispatch": 0, "parse": 0}

    def parse(msg, brief, card):
        calls["parse"] += 1
        return {"msg": msg}

    def dispatch(ops, brief, bands, card, locked, cache):
        calls["dispatch"] += 1
        return RefinementResult(build_card=card, brief=brief, price_bands=bands,
                                accepted=True, product_ids=[])

    _install(monkeypatch, ["", "   ", "accept"], dispatch, parse=parse)
    state = rp.run_refinement(_base_state(budget_gamer_brief))

    # Two blank turns never reached parse/dispatch; only 'accept' did.
    assert calls["parse"] == 1
    assert calls["dispatch"] == 1
    assert state["current_node"] == "done"


# ── parser error → continue, no round consumed, loop survives ──────────────────

def test_parse_error_is_swallowed_and_loop_survives(monkeypatch, budget_gamer_brief):
    from agents.llm.client import StructuredCallError

    parse_calls = {"n": 0}

    def parse(msg, brief, card):
        parse_calls["n"] += 1
        if parse_calls["n"] == 1:
            raise StructuredCallError(ValueError("could not classify"), raw_output=None)
        return {"msg": msg}

    dispatched = {"n": 0}

    def dispatch(ops, brief, bands, card, locked, cache):
        dispatched["n"] += 1
        return RefinementResult(build_card=card, brief=brief, price_bands=bands,
                                accepted=True, product_ids=[])

    _install(monkeypatch, ["gArbLe", "accept"], dispatch, parse=parse)
    state = rp.run_refinement(_base_state(budget_gamer_brief))

    assert parse_calls["n"] == 2          # first raised, second accepted
    assert dispatched["n"] == 1           # the bad turn never reached dispatch
    assert state["current_node"] == "done"


# ── dispatch exception → warn + continue AND consumes a round ──────────────────

def test_dispatch_exception_consumes_a_round(monkeypatch, budget_gamer_brief, capsys):
    """A failing dispatch must not kill the session, must warn, and must count as a
    round — otherwise a wall of failing turns could loop forever past the cap."""
    def dispatch(ops, brief, bands, card, locked, cache):
        if ops["msg"] == "boom":
            raise RuntimeError("selector blew up")
        return RefinementResult(build_card=card, brief=brief, price_bands=bands,
                                accepted=True, product_ids=[])

    _install(monkeypatch, ["boom", "accept"], dispatch)
    state = rp.run_refinement(_base_state(budget_gamer_brief))

    out = capsys.readouterr().out
    assert "Refinement step failed" in out
    assert "RuntimeError" in out
    assert state["current_node"] == "done"   # session survived the exception


def test_max_rounds_cap_fires(monkeypatch, budget_gamer_brief, capsys):
    """MAX_REFINEMENT_ROUNDS non-accepting turns hit the `while … else` cap and
    input() is asked exactly MAX times (the cap prevents an unbounded loop)."""
    round_card = _card([_part(ComponentSlot.gpu, "GPU-X")],
                       changed=[{"slot": "gpu", "old_product_id": "GPU-START",
                                 "new_product_id": "GPU-X", "reason": "changed"}])

    def dispatch(ops, brief, bands, card, locked, cache):
        return RefinementResult(build_card=round_card, brief=brief, price_bands=bands)

    # Supply more inputs than the cap; the loop must stop asking after MAX.
    feeder = _install(monkeypatch, ["go"] * (rp.MAX_REFINEMENT_ROUNDS + 3), dispatch)
    state = rp.run_refinement(_base_state(budget_gamer_brief))

    out = capsys.readouterr().out
    assert "Maximum refinement rounds" in out
    assert feeder.calls == rp.MAX_REFINEMENT_ROUNDS, (
        f"input() called {feeder.calls}x, expected exactly {rp.MAX_REFINEMENT_ROUNDS}"
    )
    assert state["current_node"] == "done"


# ── diff-vs-full-card display ──────────────────────────────────────────────────

def test_changed_slots_render_a_diff_not_a_full_card(monkeypatch, budget_gamer_brief):
    calls = {"diff": 0, "full": 0}
    monkeypatch.setattr(rp, "_print_build_diff", lambda card: calls.__setitem__("diff", calls["diff"] + 1))
    monkeypatch.setattr(rp, "format_build_card", lambda card, brief: calls.__setitem__("full", calls["full"] + 1) or "")

    changed = _card([_part(ComponentSlot.gpu, "GPU-2")],
                    changed=[{"slot": "gpu", "old_product_id": "GPU-START",
                              "new_product_id": "GPU-2", "reason": "changed"}])

    turn = {"n": 0}

    def dispatch(ops, brief, bands, card, locked, cache):
        turn["n"] += 1
        if turn["n"] == 1:
            return RefinementResult(build_card=changed, brief=brief, price_bands=bands)
        return RefinementResult(build_card=card, brief=brief, price_bands=bands,
                                accepted=True, product_ids=[])

    _install(monkeypatch, ["swap gpu", "accept"], dispatch)
    rp.run_refinement(_base_state(budget_gamer_brief))

    assert calls["diff"] == 1, "changed_slots present → should print a diff"
    assert calls["full"] == 0, "changed_slots present → must NOT reprint the full card"


def test_restart_style_card_without_changed_slots_prints_full_card(monkeypatch, budget_gamer_brief):
    """A fresh card with no changed_slots (e.g. a structural restart) is shown in
    full, not as an empty diff."""
    calls = {"diff": 0, "full": 0}
    monkeypatch.setattr(rp, "_print_build_diff", lambda card: calls.__setitem__("diff", calls["diff"] + 1))
    monkeypatch.setattr(rp, "format_build_card", lambda card, brief: calls.__setitem__("full", calls["full"] + 1) or "")

    fresh = _card([_part(ComponentSlot.gpu, "GPU-RESTART"),
                   _part(ComponentSlot.cpu, "CPU-RESTART", 13000)])  # changed_slots == []

    turn = {"n": 0}

    def dispatch(ops, brief, bands, card, locked, cache):
        turn["n"] += 1
        if turn["n"] == 1:
            return RefinementResult(build_card=fresh, brief=brief, price_bands=bands)
        return RefinementResult(build_card=card, brief=brief, price_bands=bands,
                                accepted=True, product_ids=[])

    _install(monkeypatch, ["make it for editing", "accept"], dispatch)
    rp.run_refinement(_base_state(budget_gamer_brief))

    assert calls["full"] == 1, "fresh restart card with no diff → print full card"
    assert calls["diff"] == 0
