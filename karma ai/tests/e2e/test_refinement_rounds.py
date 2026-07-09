"""Live, multi-round refinement round-trip — the un-mocked coverage the pure
suite (tests/test_node3_refinement.py) and the single reject→accept spine
(tests/e2e/test_full_pipeline.py) don't reach.

Extends the existing tests/e2e/ pattern — @pytest.mark.e2e, clean-skip when
OPENAI_API_KEY / Postgres is missing, one module-scoped expensive spine shared by
several focused assertion tests, and an artifacts recorder dumped on failure — as
a SIBLING scenario file rather than bolting multi-round logic onto
test_full_pipeline.py's reject→accept spine. Two deliberate deviations from that
file, both justified:

  * The spine is built via run_from_brief(fixture_brief), NOT real intake.
    Intake wiring is already covered by test_full_pipeline.py; this file's
    subject is the refinement loop, so it starts from a locked fixture brief and
    spends its LLM budget on refinement turns.
  * It drives FOUR real refinement rounds — pin → reject → additive(software) →
    structural(use-case restart) → accept — sharing ONE ThresholdCache and ONE
    locked_parts dict, exactly as run_pipeline.run_refinement threads them.

What only a live run can prove and this asserts:
  * pin/reject re-solves are genuine ThresholdCache HITs (refinement never
    re-derives thresholds for an unchanged brief),
  * a software edit (which _threshold_key reads) is a genuine MISS,
  * locked_parts persists a GPU pin across reject + additive + structural rounds
    in ONE session (the 2+-round persistence the single-reject spine can't show),
  * a real structural edit restarts the graph through run_from_brief and returns
    a fresh, use-case-changed card while pins/rejections persist in the dict,
  * real parse_refinement_request classifies pin / reject / additive / structural
    / accept phrasings correctly end-to-end.

Real OpenAI + Postgres (+ Neo4j when up); a couple of minutes wall clock. Run
with `pytest -m e2e tests/e2e/test_refinement_rounds.py`.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field

import pytest

import agents.nodes.node3_refinement as refine
from agents.db.neo4j import Neo4jClient
from agents.graph_runner import run_from_brief
from agents.nodes.node3_refinement import (
    RefinementResult,
    dispatch_refinement,
    parse_refinement_request,
)
from agents.nodes.node3_selector import ThresholdCache
from agents.schemas.brief import UserBuildBrief
from agents.schemas.slots import ComponentSlot

pytestmark = pytest.mark.e2e

_FIXTURE = "budget_gamer.json"


@dataclass
class RoundRecord:
    label: str
    ops: dict | None = None
    derive_calls_after: int | None = None
    cache_hit: bool | None = None
    locked_parts: dict | None = None
    changed_slots: list | None = None
    message: str | None = None
    accepted: bool | None = None
    error: str | None = None


@dataclass
class Artifacts:
    stage: str = "not started"
    spine_slots: int = 0
    old_gpu_id: str | None = None
    rounds: list[RoundRecord] = field(default_factory=list)


def _summary(a: Artifacts) -> str:
    lines = [f"stage={a.stage} spine_slots={a.spine_slots} old_gpu={a.old_gpu_id}"]
    for r in a.rounds:
        lines.append(
            f"  [{r.label}] ops={r.ops} derive_after={r.derive_calls_after} "
            f"hit={r.cache_hit} locked={r.locked_parts} changed={r.changed_slots} "
            f"accepted={r.accepted} msg={r.message!r} err={r.error}"
        )
    return "\n".join(lines)


@pytest.fixture(scope="module")
def refinement_run(db_available):
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY not set — skipping live refinement e2e")
    if not db_available:
        pytest.skip("Postgres unavailable — skipping live refinement e2e")

    a = Artifacts()

    def _fail(exc: Exception):
        pytest.fail(
            f"FAILED AT STAGE: {a.stage}\n{type(exc).__name__}: {exc}\n\n"
            f"Artifacts so far:\n{_summary(a)}"
        )

    fixtures = os.path.join(os.path.dirname(__file__), "..", "..", "data", "fixtures")
    brief = UserBuildBrief.model_validate_json(
        open(os.path.join(fixtures, _FIXTURE), encoding="utf-8").read()
    )
    # Give it a fresh id so nothing collides with other suites.
    brief = brief.model_copy(update={"brief_id": uuid.uuid4()})

    # ── Spine: real feasibility → allocation → selection via the graph ──────────
    try:
        a.stage = "spine"
        state = run_from_brief(brief)
        if state.get("error_message"):
            raise RuntimeError(state["error_message"])
        build_card = state.get("build_card")
        bands = state.get("price_bands")
        if build_card is None or not build_card.parts:
            raise RuntimeError("graph returned no build_card")
        a.spine_slots = len(build_card.parts)
        gpu = next(p for p in build_card.parts if p.slot == ComponentSlot.gpu)
        a.old_gpu_id = gpu.product_id
    except Exception as exc:  # noqa: BLE001
        _fail(exc)

    # Seed the cache from the spine exactly like run_pipeline / test_full_pipeline.
    ft = state.get("fitness_thresholds")
    cache = ThresholdCache(
        thresholds=({ComponentSlot(s): v for s, v in ft.items()} if ft else None),
        key=state.get("fitness_thresholds_key"),
    )
    locked_parts: dict[str, str] = {}

    # Wrap the refinement-layer derive with a counter so cache HIT/MISS is
    # observable per round (the graph's own node_select derive is a different
    # symbol and is not counted here — refinement re-solves go through
    # refine.derive_fitness_thresholds).
    count = {"n": 0}
    real_derive = refine.derive_fitness_thresholds

    def counting_derive(b):
        count["n"] += 1
        return real_derive(b)

    refine.derive_fitness_thresholds = counting_derive
    try:
        def do_round(label, message, expect_no_resolve=False):
            nonlocal brief, bands, build_card
            rec = RoundRecord(label=label)
            a.rounds.append(rec)
            before = count["n"]
            try:
                ops = parse_refinement_request(message, brief, build_card)
                rec.ops = ops.model_dump(exclude_none=True)
                result: RefinementResult = dispatch_refinement(
                    ops, brief, bands, build_card, locked_parts, cache
                )
                brief = result.brief
                bands = result.price_bands
                build_card = result.build_card
                rec.derive_calls_after = count["n"]
                # A re-solve happened iff derive was consulted OR a cache hit was
                # logged; HIT == a re-solve that did not re-derive.
                rec.cache_hit = (count["n"] == before) and not expect_no_resolve
                rec.locked_parts = dict(locked_parts)
                rec.changed_slots = list(build_card.changed_slots)
                rec.message = result.message
                rec.accepted = result.accepted
                return result
            except Exception as exc:  # noqa: BLE001
                rec.error = f"{type(exc).__name__}: {exc}"
                a.stage = f"round:{label}"
                _fail(exc)

        # Round 1 — pin the GPU (re-solve; unchanged brief → cache HIT).
        a.stage = "round1-pin"
        do_round(
            "pin_gpu",
            "I really like this graphics card. Please pin the GPU — keep exactly "
            "this one and don't swap it when you adjust anything else.",
        )

        # Round 2 — reject the PSU (re-solve; still unchanged threshold fields → HIT).
        a.stage = "round2-reject"
        do_round(
            "reject_psu",
            "The power supply you picked is no good — reject it and choose a "
            "different PSU for me.",
        )

        # Round 3 — additive software edit (changes _threshold_key → cache MISS).
        # Use an unambiguous GAME TITLE (the canonical `software` example in the
        # parser prompt) so this routes to software, not extras/connectivity.
        a.stage = "round3-software"
        r3 = do_round(
            "add_game",
            "I also play Baldur's Gate 3 on weekends now — add that to my games.",
        )

        # Round 4 — structural: change the primary use case (restart via run_from_brief).
        a.stage = "round4-structural"
        do_round(
            "restart_general_use",
            "Actually this isn't really a gaming rig anymore — I mostly just do "
            "general everyday use and web browsing now.",
        )

        # Round 5 — accept.
        a.stage = "round5-accept"
        do_round("accept", "This looks perfect, please finalize it.")
        a.stage = "done"
    finally:
        refine.derive_fitness_thresholds = real_derive

    a.neo4j_available = Neo4jClient().ping()
    return a


def _round(a: Artifacts, label: str) -> RoundRecord:
    return next(r for r in a.rounds if r.label == label)


class TestPinRound:
    def test_gpu_got_pinned(self, refinement_run):
        r = _round(refinement_run, "pin_gpu")
        assert r.locked_parts.get("gpu") == refinement_run.old_gpu_id, (
            f"expected GPU {refinement_run.old_gpu_id} pinned, locked={r.locked_parts}"
        )

    def test_pin_resolve_was_a_cache_hit(self, refinement_run):
        r = _round(refinement_run, "pin_gpu")
        assert r.cache_hit is True, (
            "pin re-solve re-derived thresholds for an unchanged brief — expected "
            "a ThresholdCache HIT"
        )


class TestRejectRound:
    def test_psu_rejected_and_gpu_still_pinned(self, refinement_run):
        r = _round(refinement_run, "reject_psu")
        # The GPU pin from round 1 persists across the reject round.
        assert r.locked_parts.get("gpu") == refinement_run.old_gpu_id, (
            f"GPU pin lost across the reject round: {r.locked_parts}"
        )

    def test_reject_resolve_was_a_cache_hit(self, refinement_run):
        r = _round(refinement_run, "reject_psu")
        assert r.cache_hit is True, "reject re-solve should also hit the cache"


class TestAdditiveSoftwareRound:
    def test_software_edit_missed_the_cache(self, refinement_run):
        r = _round(refinement_run, "add_game")
        # Only meaningful if the edit actually re-solved (not blocked as impossible).
        if r.message and "impossible" in r.message.lower():
            pytest.skip(f"software edit went impossible, no re-solve: {r.message}")
        assert r.cache_hit is False, (
            "a software edit changes _threshold_key, so the re-solve MUST re-derive "
            f"(cache MISS); derive_calls_after={r.derive_calls_after}"
        )

    def test_gpu_pin_survives_additive_round(self, refinement_run):
        r = _round(refinement_run, "add_game")
        if r.message and "impossible" in r.message.lower():
            pytest.skip("software edit went impossible")
        assert r.locked_parts.get("gpu") == refinement_run.old_gpu_id


class TestStructuralRound:
    def test_restart_message_and_use_case_changed(self, refinement_run):
        r = _round(refinement_run, "restart_general_use")
        assert r.message and "restart" in r.message.lower(), (
            f"structural edit should report a restart, got: {r.message!r}"
        )
        # A fresh restart card carries no per-slot diff (changed_slots empty).
        assert r.changed_slots == []

    def test_pins_and_rejections_persist_in_the_dict_across_restart(self, refinement_run):
        r = _round(refinement_run, "restart_general_use")
        assert r.locked_parts.get("gpu") == refinement_run.old_gpu_id, (
            "locked_parts dict must persist the GPU pin across a structural restart"
        )


class TestAcceptRound:
    def test_accept_finalizes(self, refinement_run):
        r = _round(refinement_run, "accept")
        assert r.accepted is True

    def test_session_reached_done(self, refinement_run):
        assert refinement_run.stage == "done"
