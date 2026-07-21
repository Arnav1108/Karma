"""Manual load-test script for KARMA_MAX_CONCURRENT_BUILDS tuning.

Implements docs/hardening_plan.md section 5 ("Real concurrency tuning under
load") exactly: the four-step approach described there against a REAL,
already-running API instance -- this script does not start a server itself.

WHAT THIS PROVES
-----------------
build_service_plan.md sized KARMA_MAX_CONCURRENT_BUILDS=2 against the shared
Postgres pool (ThreadedConnectionPool(minconn=1, maxconn=10)) by inspection
only. This script empirically confirms (or refutes) that headroom by driving
the real HTTP API with concurrent build + intake traffic and watching for
pool-exhaustion-shaped errors (currently indistinguishable from other bugs --
they surface as a bare 500 INTERNAL_ERROR from an intake turn, or a build
that finishes failed/INTERNAL_ERROR -- see hardening_plan.md section 5,
"Make exhaustion observable first"). This script does not fix that
distinguishability; it only detects and reports the shape.

PREREQUISITES
--------------
- A real, already-running instance of this API (e.g. `uvicorn api.main:app
  --port 8010` from `karma ai/`), wired to real Postgres/Neo4j/OpenAI
  credentials -- this script drives it over plain HTTP, no mocks anywhere.
- Run with KARMA_RATE_LIMIT_ENABLED=false on the server. This is required,
  not optional: step 2 asserts that rejections beyond max_concurrent are
  429 BUILD_CAPACITY specifically (a concurrency-cap rejection). With rate
  limiting on, POST /builds is also gated at 3/hour/key
  (KARMA_RL_BUILD_CREATE_PER_HOUR), so a run of more than ~3 build starts
  would start producing 429 RATE_LIMITED instead -- a different 429 for a
  different reason -- and the capacity assertion in step 2 would be
  contaminated by quota exhaustion instead of isolating concurrency.
- If the server has KARMA_API_KEYS configured, pass --api-key (or set
  KARMA_API_KEY) so requests carry X-API-Key.

HOW TO RUN
-----------
    python tests/manual/load_build_concurrency.py \\
        --base-url http://127.0.0.1:8010 \\
        --max-concurrent 2

--max-concurrent MUST match the KARMA_MAX_CONCURRENT_BUILDS value the target
server was actually started with -- this script cannot introspect it, and a
mismatch invalidates the step-2 assertion.

THE CEILING SWEEP (step 4) IS MULTI-RUN AND MANUAL
----------------------------------------------------
This script cannot restart the server, so it cannot sweep
KARMA_MAX_CONCURRENT_BUILDS values itself. Step 4 is implemented as: this
script runs steps 1-3 ONCE against whatever value the server currently has
(passed in via --max-concurrent), and prints a summary tagged with that
value. To complete the sweep described in the plan, the operator must:

    1. Stop the server.
    2. Restart it with KARMA_MAX_CONCURRENT_BUILDS=3 (then 4, then 5, ...).
    3. Re-run this script with --max-concurrent 3 (then 4, then 5, ...).
    4. Compare the printed summaries across runs by hand -- the ceiling is
       the value at which pool-exhaustion-shaped errors first appear.

Each invocation is one point in that sweep, not the sweep itself.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass, field

import httpx

DEFAULT_BASE_URL = os.environ.get("KARMA_LOAD_TEST_BASE_URL", "http://127.0.0.1:8010")
API_PREFIX = "/api/v1"

# The two QUESTION_SEQUENCE entries that gate floor_met() (node1_intake.py) --
# budget and primary_use_case, in that fixed order. Answering exactly these
# two and then calling POST .../lock (IntakeService.lock_early) is the
# "faster seeding path" explored for step 1: it is still a fully real,
# LLM-driven conversation (no fakes, no shortcuts into the store), just
# shorter than running the full ~9-question QUESTION_SEQUENCE to a natural
# auto-lock. tests/test_intake_routes.py and tests/test_build_routes.py both
# monkeypatch intake_begin/intake_step/run_from_brief to seed sessions
# in-process -- that path is unavailable here since this script only speaks
# real HTTP to a real server. No other real-HTTP shortcut exists in the
# codebase today; this explicit-lock-after-two-answers path is it.
SEED_ANSWERS: dict[str, str] = {
    "budget": (
        "My comfortable budget is around INR 90,000 and I can stretch to "
        "INR 100,000 at most. That's just for the PC itself, not the "
        "monitor or peripherals."
    ),
    "primary_use_case": (
        "Mainly gaming -- competitive shooters at high frame rates, with "
        "some general use on the side."
    ),
}
FALLBACK_ANSWER = "No particular preference -- please use a sensible default for this."
MAX_SEED_TURNS = 8  # safety cap if floor_met somehow isn't reached after the two floor answers

TERMINAL_BUILD_STATUSES = {"succeeded", "infeasible", "cannot_proceed", "failed"}

# Per hardening_plan.md section 5's own note: pool exhaustion currently has
# no dedicated error code. It surfaces today as a bare 500 INTERNAL_ERROR
# (intake turn) or a build settling into failed/INTERNAL_ERROR (build
# worker), and possibly 503 DATABASE_UNAVAILABLE (persist path, e.g. locking
# during stress). Flag all three shapes as "suspected" -- this script does
# not attempt to disambiguate further, per the plan's own scope note.
POOL_EXHAUSTION_STATUS_CODES = {500, 503}
POOL_EXHAUSTION_ERROR_CODES = {"INTERNAL_ERROR", "DATABASE_UNAVAILABLE"}


# ---------------------------------------------------------------------------
# Outcome records
# ---------------------------------------------------------------------------

@dataclass
class SeedResult:
    label: str
    ok: bool
    session_id: str | None
    turns: int
    elapsed_s: float
    error: str | None = None


@dataclass
class BuildStartOutcome:
    session_id: str
    status_code: int
    build_id: str | None
    error_code: str | None
    elapsed_s: float


@dataclass
class PollOutcome:
    build_id: str
    final_status: str | None
    error_code: str | None
    polls: int
    elapsed_s: float
    timed_out: bool
    pool_exhaustion_suspected: bool


@dataclass
class StressRequestOutcome:
    label: str
    status_code: int | None
    error_code: str | None
    elapsed_s: float
    pool_exhaustion_suspected: bool
    exception: str | None = None


@dataclass
class RunSummary:
    max_concurrent: int
    capacity_k: int
    stress_sessions: int
    seed_results: list[SeedResult] = field(default_factory=list)
    start_outcomes: list[BuildStartOutcome] = field(default_factory=list)
    # poll_outcomes covers the step-2 builds; polling for them runs
    # concurrently with step 3's stress intake (see run_once), so a
    # pool-exhaustion-shaped error here is just as much a step-3 finding as
    # one on stress_intake_outcomes -- both are reported.
    poll_outcomes: list[PollOutcome] = field(default_factory=list)
    stress_intake_outcomes: list[StressRequestOutcome] = field(default_factory=list)
    step2_started_at: float = 0.0
    step2_finished_at: float = 0.0
    step3_started_at: float = 0.0
    step3_finished_at: float = 0.0


def _error_code(body: dict | None) -> str | None:
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        return None
    return error.get("code")


def _is_pool_exhaustion_shaped(status_code: int | None, error_code: str | None) -> bool:
    if status_code is None:
        return False
    if status_code not in POOL_EXHAUSTION_STATUS_CODES:
        return False
    # A bare 500 with no parseable envelope (e.g. a non-JSON error page) is
    # still exactly the "indistinguishable" shape the plan describes.
    if error_code is None:
        return True
    return error_code in POOL_EXHAUSTION_ERROR_CODES


# ---------------------------------------------------------------------------
# Step 1: seed locked sessions
# ---------------------------------------------------------------------------

async def seed_locked_session(client: httpx.AsyncClient, label: str) -> SeedResult:
    started = time.monotonic()
    try:
        resp = await client.post(f"{API_PREFIX}/intake/sessions", json={})
        resp.raise_for_status()
        body = resp.json()
        session_id = body["session_id"]
        question = body["question"]

        turns = 0
        floor_met = False
        while turns < MAX_SEED_TURNS:
            question_id = (question or {}).get("question_id")
            answer = SEED_ANSWERS.get(question_id, FALLBACK_ANSWER)

            resp = await client.post(
                f"{API_PREFIX}/intake/sessions/{session_id}/answers", json={"answer": answer}
            )
            resp.raise_for_status()
            body = resp.json()
            turns += 1

            if body.get("status") == "locked":
                elapsed = time.monotonic() - started
                return SeedResult(label, True, session_id, turns, elapsed)

            floor_met = bool(body.get("progress", {}).get("floor_met"))
            question = body.get("question")
            if floor_met:
                break

        if not floor_met:
            elapsed = time.monotonic() - started
            return SeedResult(
                label, False, session_id, turns, elapsed,
                error=f"floor_met not reached after {turns} turns",
            )

        resp = await client.post(f"{API_PREFIX}/intake/sessions/{session_id}/lock")
        resp.raise_for_status()
        elapsed = time.monotonic() - started
        return SeedResult(label, True, session_id, turns, elapsed)

    except httpx.HTTPStatusError as exc:
        elapsed = time.monotonic() - started
        try:
            code = _error_code(exc.response.json())
        except Exception:
            code = None
        return SeedResult(
            label, False, None, 0, elapsed,
            error=f"HTTP {exc.response.status_code} {code or exc.response.text[:200]}",
        )
    except Exception as exc:  # noqa: BLE001 - report, don't crash the whole run
        elapsed = time.monotonic() - started
        return SeedResult(label, False, None, 0, elapsed, error=f"{type(exc).__name__}: {exc}")


async def seed_locked_sessions(
    client: httpx.AsyncClient, n: int, label_prefix: str
) -> list[SeedResult]:
    results = await asyncio.gather(
        *(seed_locked_session(client, f"{label_prefix}-{i}") for i in range(n))
    )
    return list(results)


# ---------------------------------------------------------------------------
# Step 2: capacity check
# ---------------------------------------------------------------------------

async def start_build(client: httpx.AsyncClient, session_id: str) -> BuildStartOutcome:
    started = time.monotonic()
    resp = await client.post(f"{API_PREFIX}/builds", json={"session_id": session_id})
    elapsed = time.monotonic() - started
    body = None
    try:
        body = resp.json()
    except Exception:
        pass
    if resp.status_code == 202:
        return BuildStartOutcome(session_id, resp.status_code, body["build_id"], None, elapsed)
    return BuildStartOutcome(session_id, resp.status_code, None, _error_code(body), elapsed)


async def poll_build_to_terminal(
    client: httpx.AsyncClient, build_id: str, poll_interval_s: float, timeout_s: float
) -> PollOutcome:
    started = time.monotonic()
    polls = 0
    last_status: str | None = None
    last_error_code: str | None = None
    while time.monotonic() - started < timeout_s:
        resp = await client.get(f"{API_PREFIX}/builds/{build_id}")
        polls += 1
        body = None
        try:
            body = resp.json()
        except Exception:
            pass

        if resp.status_code != 200:
            error_code = _error_code(body)
            elapsed = time.monotonic() - started
            return PollOutcome(
                build_id, None, error_code, polls, elapsed, False,
                _is_pool_exhaustion_shaped(resp.status_code, error_code),
            )

        last_status = body.get("status")
        if last_status in TERMINAL_BUILD_STATUSES:
            error = body.get("error") or {}
            last_error_code = error.get("code")
            elapsed = time.monotonic() - started
            suspected = last_status == "failed" and _is_pool_exhaustion_shaped(500, last_error_code)
            return PollOutcome(build_id, last_status, last_error_code, polls, elapsed, False, suspected)

        wait_s = (body.get("poll_after_ms") or int(poll_interval_s * 1000)) / 1000
        await asyncio.sleep(max(wait_s, 0.1))

    elapsed = time.monotonic() - started
    return PollOutcome(build_id, last_status, last_error_code, polls, elapsed, True, False)


async def start_builds_concurrently(
    client: httpx.AsyncClient, session_ids: list[str]
) -> list[BuildStartOutcome]:
    return list(await asyncio.gather(*(start_build(client, sid) for sid in session_ids)))


# ---------------------------------------------------------------------------
# Step 3: shared-pool stress
# ---------------------------------------------------------------------------

async def run_stress_intake(client: httpx.AsyncClient, label: str) -> StressRequestOutcome:
    started = time.monotonic()
    try:
        seed = await seed_locked_session(client, label)
        elapsed = time.monotonic() - started
        if seed.ok:
            return StressRequestOutcome(label, 200, None, elapsed, False)
        status_code = None
        error_code = None
        if seed.error and seed.error.startswith("HTTP "):
            try:
                status_code = int(seed.error.split()[1])
            except (IndexError, ValueError):
                pass
            parts = seed.error.split(" ", 2)
            error_code = parts[2] if len(parts) > 2 else None
        return StressRequestOutcome(
            label, status_code, error_code, elapsed,
            _is_pool_exhaustion_shaped(status_code, error_code),
            exception=seed.error,
        )
    except Exception as exc:  # noqa: BLE001
        elapsed = time.monotonic() - started
        return StressRequestOutcome(label, None, None, elapsed, False, exception=str(exc))


# ---------------------------------------------------------------------------
# Orchestration (step 4: one parameterized run of steps 1-3)
# ---------------------------------------------------------------------------

async def run_once(
    base_url: str,
    api_key: str | None,
    max_concurrent: int,
    capacity_extra: int,
    stress_sessions: int,
    poll_interval_s: float,
    poll_timeout_s: float,
    request_timeout_s: float,
) -> RunSummary:
    capacity_k = max_concurrent + capacity_extra
    summary = RunSummary(max_concurrent, capacity_k, stress_sessions)

    headers = {"X-API-Key": api_key} if api_key else {}
    timeout = httpx.Timeout(request_timeout_s)
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout) as client:
        health = await client.get("/healthz")
        health.raise_for_status()
        print(f"[preflight] {base_url}/healthz -> {health.status_code} {health.json()}")

        print(f"\n[step 1] seeding {capacity_k} locked sessions for the capacity check...")
        step1_started = time.monotonic()
        summary.seed_results = await seed_locked_sessions(client, capacity_k, "cap")
        print(f"[step 1] done in {time.monotonic() - step1_started:.1f}s")

        ok_sessions = [r.session_id for r in summary.seed_results if r.ok and r.session_id]
        failed_seeds = [r for r in summary.seed_results if not r.ok]
        if failed_seeds:
            print(f"[step 1] WARNING: {len(failed_seeds)}/{capacity_k} seed sessions failed:")
            for r in failed_seeds:
                print(f"    {r.label}: {r.error}")
        if len(ok_sessions) <= max_concurrent:
            print(
                "[step 1] ABORTING step 2/3: not enough successfully-seeded sessions "
                f"({len(ok_sessions)}) to exceed max_concurrent ({max_concurrent})."
            )
            return summary

        print(
            f"\n[step 2] capacity check: firing {len(ok_sessions)} concurrent POST /builds "
            f"(max_concurrent={max_concurrent}, expecting {max_concurrent} accepted, "
            f"{len(ok_sessions) - max_concurrent} rejected 429 BUILD_CAPACITY)..."
        )
        summary.step2_started_at = time.monotonic()
        summary.start_outcomes = await start_builds_concurrently(client, ok_sessions)
        accepted = [o for o in summary.start_outcomes if o.status_code == 202]
        print(
            f"[step 2] {len(accepted)}/{len(summary.start_outcomes)} accepted (202); "
            "polling accepted builds to terminal now, overlapped with step 3 below"
        )

        # Kick these off as tasks (not awaited yet) so they keep running while
        # step 3 fires -- this is what makes "the builds from step 2 are still
        # running" (hardening_plan.md section 5 step 3) literally true, rather
        # than step 3 starting only after step 2's builds have already
        # finished. asyncio.create_task schedules eagerly; awaiting the tasks
        # later (after step 3 is also under way) does not change when they run.
        step2_poll_tasks = [
            asyncio.create_task(poll_build_to_terminal(client, o.build_id, poll_interval_s, poll_timeout_s))
            for o in accepted
        ]

        print(
            f"\n[step 3] shared-pool stress: running {stress_sessions} concurrent real intake "
            f"conversations while {len(step2_poll_tasks)} build(s) from step 2 are still in flight..."
        )
        summary.step3_started_at = time.monotonic()
        stress_task = asyncio.create_task(
            asyncio.gather(*(run_stress_intake(client, f"stress-{i}") for i in range(stress_sessions)))
        )

        summary.poll_outcomes = list(await asyncio.gather(*step2_poll_tasks)) if step2_poll_tasks else []
        summary.step2_finished_at = time.monotonic()
        print(f"[step 2] all accepted builds reached a terminal state in {summary.step2_finished_at - summary.step2_started_at:.1f}s")

        summary.stress_intake_outcomes = list(await stress_task)
        summary.step3_finished_at = time.monotonic()
        print(f"[step 3] done in {summary.step3_finished_at - summary.step3_started_at:.1f}s")

    return summary


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_summary(summary: RunSummary) -> None:
    print("\n" + "=" * 78)
    print(f"LOAD TEST SUMMARY -- max_concurrent={summary.max_concurrent}")
    print("=" * 78)

    total_seeds = len(summary.seed_results)
    ok_seeds = sum(1 for r in summary.seed_results if r.ok)
    print(f"\n[Step 1: seeding] {ok_seeds}/{total_seeds} sessions locked successfully")
    if summary.seed_results:
        seed_times = [r.elapsed_s for r in summary.seed_results if r.ok]
        if seed_times:
            print(
                f"    seed time per session: min={min(seed_times):.1f}s "
                f"max={max(seed_times):.1f}s avg={sum(seed_times) / len(seed_times):.1f}s"
            )

    accepted = [o for o in summary.start_outcomes if o.status_code == 202]
    capacity_rejected = [
        o for o in summary.start_outcomes if o.status_code == 429 and o.error_code == "BUILD_CAPACITY"
    ]
    other_rejected = [
        o for o in summary.start_outcomes
        if o.status_code != 202 and not (o.status_code == 429 and o.error_code == "BUILD_CAPACITY")
    ]
    capacity_pass = (
        len(summary.start_outcomes) > 0
        and len(accepted) == summary.max_concurrent
        and len(capacity_rejected) == len(summary.start_outcomes) - summary.max_concurrent
        and not other_rejected
    )
    print(f"\n[Step 2: capacity check] {'PASS' if capacity_pass else 'FAIL / INCONCLUSIVE'}")
    print(f"    fired: {len(summary.start_outcomes)}  accepted (202): {len(accepted)}"
          f"  rejected 429 BUILD_CAPACITY: {len(capacity_rejected)}"
          f"  other: {len(other_rejected)}")
    print(f"    expected accepted == max_concurrent ({summary.max_concurrent})")
    if other_rejected:
        print("    UNEXPECTED outcomes (not 202, not 429 BUILD_CAPACITY):")
        for o in other_rejected:
            print(f"        session={o.session_id} status={o.status_code} error_code={o.error_code}")

    terminal = [p for p in summary.poll_outcomes if not p.timed_out]
    timed_out = [p for p in summary.poll_outcomes if p.timed_out]
    succeeded = [p for p in terminal if p.final_status == "succeeded"]
    print(
        f"    polled {len(summary.poll_outcomes)} accepted build(s) to completion: "
        f"{len(succeeded)} succeeded, {len(terminal) - len(succeeded)} other terminal, "
        f"{len(timed_out)} timed out waiting"
    )
    # Polling for these builds ran concurrently with step 3's stress intake
    # (see run_once) -- so a pool-exhaustion-shaped error here IS a step-3
    # finding, not just a step-2 one. Reported here since it's tied to a
    # specific build_id; step 3's section below reports the intake side.
    step2_suspected = [p for p in summary.poll_outcomes if p.pool_exhaustion_suspected]
    if step2_suspected:
        print(f"    POOL-EXHAUSTION-SHAPED errors while polling (overlapped with step 3): {len(step2_suspected)}")
        for p in step2_suspected:
            print(f"        build={p.build_id} status={p.final_status} error_code={p.error_code}")

    intake_ok = sum(1 for o in summary.stress_intake_outcomes if o.exception is None)
    intake_total = len(summary.stress_intake_outcomes)
    stress_suspected = [o for o in summary.stress_intake_outcomes if o.pool_exhaustion_suspected]
    print(
        f"\n[Step 3: shared-pool stress] {intake_ok}/{intake_total} stress intake conversations "
        f"locked, run concurrently with the {len(summary.poll_outcomes)} step-2 build poll(s) above"
    )
    if stress_suspected or step2_suspected:
        total_suspected = len(stress_suspected) + len(step2_suspected)
        print(f"    POOL-EXHAUSTION-SHAPED errors observed: {total_suspected}  <-- investigate")
        for o in summary.stress_intake_outcomes:
            if o.pool_exhaustion_suspected:
                print(f"        intake {o.label}: status={o.status_code} error_code={o.error_code}")
        for p in step2_suspected:
            print(f"        build {p.build_id}: status={p.final_status} error_code={p.error_code}")
    else:
        print("    no pool-exhaustion-shaped errors observed (500/503 with "
              "INTERNAL_ERROR/DATABASE_UNAVAILABLE, or a build settling failed/INTERNAL_ERROR)")

    total_requests = (
        len(summary.seed_results)
        + len(summary.start_outcomes)
        + sum(p.polls for p in summary.poll_outcomes)
        + len(summary.stress_intake_outcomes)
    )
    print(f"\n[Totals] approx. {total_requests} HTTP requests issued this run")
    print(
        f"    step 2 wall time: {summary.step2_finished_at - summary.step2_started_at:.1f}s  "
        f"step 3 wall time: {summary.step3_finished_at - summary.step3_started_at:.1f}s"
    )

    print("\n" + "-" * 78)
    print(
        "This is ONE point in the ceiling sweep (hardening_plan.md section 5 step 4). "
        "To find the empirical ceiling, restart the server with a different "
        "KARMA_MAX_CONCURRENT_BUILDS (e.g. 3, 4, 5) and re-run this script with a "
        "matching --max-concurrent, then compare the pool-exhaustion counts above "
        "across runs."
    )
    print("-" * 78)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Manual concurrency/pool-headroom load test for the Karma Advisor API. "
            "Requires a real, already-running server -- see module docstring."
        )
    )
    parser.add_argument(
        "--base-url", default=DEFAULT_BASE_URL,
        help=f"Base URL of the running API (default: {DEFAULT_BASE_URL}, env KARMA_LOAD_TEST_BASE_URL)",
    )
    parser.add_argument(
        "--api-key", default=os.environ.get("KARMA_API_KEY"),
        help="X-API-Key value, if the target server has KARMA_API_KEYS configured (env KARMA_API_KEY)",
    )
    parser.add_argument(
        "--max-concurrent", type=int, required=True,
        help="Must match the target server's actual KARMA_MAX_CONCURRENT_BUILDS value",
    )
    parser.add_argument(
        "--capacity-extra", type=int, default=3,
        help="Extra build starts fired beyond max_concurrent in step 2 (K = max_concurrent + this)",
    )
    parser.add_argument(
        "--stress-sessions", type=int, default=5,
        help="Number (J) of concurrent intake conversations run in step 3",
    )
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Fallback poll interval, seconds")
    parser.add_argument("--poll-timeout", type=float, default=240.0, help="Per-build poll timeout, seconds")
    parser.add_argument(
        "--request-timeout", type=float, default=60.0,
        help="Per-HTTP-request timeout, seconds (individual intake turns/lock calls involve LLM latency)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    try:
        summary = asyncio.run(
            run_once(
                base_url=args.base_url,
                api_key=args.api_key,
                max_concurrent=args.max_concurrent,
                capacity_extra=args.capacity_extra,
                stress_sessions=args.stress_sessions,
                poll_interval_s=args.poll_interval,
                poll_timeout_s=args.poll_timeout,
                request_timeout_s=args.request_timeout,
            )
        )
    except httpx.ConnectError as exc:
        print(f"ERROR: could not connect to {args.base_url} -- is the server running? ({exc})")
        return 2
    except httpx.HTTPStatusError as exc:
        print(f"ERROR: preflight failed -- {exc}")
        return 2

    print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
