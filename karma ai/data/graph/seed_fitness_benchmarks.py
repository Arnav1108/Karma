"""
Seed benchmark-derived fitness tier/score edges into the Neo4j knowledge graph.

Sibling to seed_graph.py — does not replace or modify it. seed_graph.py no
longer writes r.weight (the _GOOD_FOR_WEIGHTS stub table and its seeding
function were removed); this script sets r.tier / r.score on the
(:Component)-[:GOOD_FOR]->(:UseCase) edge, via MERGE on the relationship
(never on the nodes — see upsert_edges).

Run from karma ai/:
    python -m data.graph.seed_fitness_benchmarks
    python -m data.graph.seed_fitness_benchmarks --gpu path/to/gpu.csv --cpu path/to/cpu.csv
"""
import argparse
import csv
import sys
from dataclasses import dataclass

from agents.db.neo4j import _get_driver

_DEFAULT_GPU_CSV = "data/benchmarks/gpu_benchmarks.csv"
_DEFAULT_CPU_CSV = "data/benchmarks/cpu_benchmarks.csv"


# ── Core primitives ───────────────────────────────────────────────────────────

def percentile_norm(rows: list[dict], score_col: str) -> dict[str, float]:
    """
    {product_id: normalized rank} — fractional (average-rank) percentile
    within the class, on [0, 1].

    Tied raw scores get identical output values (average-rank tie handling,
    equivalent to pandas .rank(method='average') / scipy.stats.rankdata
    (method='average')). A stable sort-by-CSV-order would silently break
    deliberate ties by row order, which is wrong — some rows in the source
    CSVs are scored equal on purpose.
    """
    n = len(rows)
    if n == 0:
        return {}
    if n == 1:
        return {rows[0]["product_id"]: 1.0}

    ordered = sorted(rows, key=lambda r: float(r[score_col]))

    # Average-rank tie handling: scan runs of equal raw score, assign every
    # row in the run the mean of the 0-indexed ranks the run occupies.
    ranks: dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        value = float(ordered[i][score_col])
        while j + 1 < n and float(ordered[j + 1][score_col]) == value:
            j += 1
        avg_rank = (i + j) / 2.0
        for k in range(i, j + 1):
            ranks[ordered[k]["product_id"]] = avg_rank
        i = j + 1

    return {pid: rank / (n - 1) for pid, rank in ranks.items()}


def score_to_tier(score: float) -> int:
    """
    Quintile cuts on [0,1]: 0=budget/weakest, 4=top-class.

    Assumes a genuinely uniform 0-1 percentile input — never feed this a
    capped or scaled composite directly (see general_use edges below).
    """
    return min(4, int(score * 5))


@dataclass
class FitnessEdge:
    product_id: str
    use_case: str     # must match UseCase.name in graph
    tier: int          # 0-4 ordinal, for filtering
    score: float        # [0,1], for tie-break ranking within a tier


# ── CSV loading ────────────────────────────────────────────────────────────────

def _load_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ── Edge recipes ──────────────────────────────────────────────────────────────

def compute_gpu_edges(rows: list[dict]) -> list[FitnessEdge]:
    raster_norm = percentile_norm(rows, "gaming_raster_1080p_ultra_pct")
    creation_norm = percentile_norm(rows, "content_creation_ai_pro_viz_pct")

    # general_use: raw weighted blend first, then re-percentile before tiering
    # (see module docstring / design note — never tier a capped blend directly).
    # Must combine >=2 independent signals: a scalar multiple of a single input
    # re-percentiles back to that input's own rank order regardless of the
    # constant, making general_use structurally identical to gaming.
    #
    # On the current 14-GPU catalog, general_use still re-percentiles to the
    # same tier as gaming for every part. Confirmed intentional, not a
    # leftover defect: raster_norm and creation_norm are Spearman ~0.978
    # correlated here, so no 0.7/0.3 blend of the two produces a rank flip.
    # Rejected alternatives for a third signal to force divergence — VRAM
    # (duplicates the hard-floor filter in catalog_floor.py, a different
    # layer) and price (crosses the layer boundary Node 2/Node 3 already
    # keep separate from fitness scoring). General-use workloads also don't
    # meaningfully stress a GPU differently than gaming does, so manufacturing
    # a rank difference here would encode a distinction that isn't real. Do
    # not "fix" this again without new data that actually diverges.
    blend_rows = [
        {
            "product_id": r["product_id"],
            "_blend": raster_norm[r["product_id"]] * 0.7 + creation_norm[r["product_id"]] * 0.3,
        }
        for r in rows
    ]
    general_norm = percentile_norm(blend_rows, "_blend")

    edges: list[FitnessEdge] = []
    for r in rows:
        pid = r["product_id"]

        edges.append(FitnessEdge(pid, "gaming", score_to_tier(raster_norm[pid]), raster_norm[pid]))
        edges.append(FitnessEdge(pid, "content_creation", score_to_tier(creation_norm[pid]), creation_norm[pid]))
        edges.append(FitnessEdge(pid, "general_use", score_to_tier(general_norm[pid]), general_norm[pid]))
        # work_productivity, storage_homeserver: no GPU edge (per spec)

    return edges


def compute_cpu_edges(rows: list[dict]) -> list[FitnessEdge]:
    gaming_norm = percentile_norm(rows, "gaming_pct")
    single_norm = percentile_norm(rows, "single_thread_pct")
    multi_norm = percentile_norm(rows, "multi_thread_pct")

    # general_use: raw weighted blend first, then re-percentile before tiering.
    blend_rows = [
        {
            "product_id": r["product_id"],
            "_blend": single_norm[r["product_id"]] * 0.5 + gaming_norm[r["product_id"]] * 0.3,
        }
        for r in rows
    ]
    general_norm = percentile_norm(blend_rows, "_blend")

    edges: list[FitnessEdge] = []
    for r in rows:
        pid = r["product_id"]

        edges.append(FitnessEdge(pid, "gaming", score_to_tier(gaming_norm[pid]), gaming_norm[pid]))
        edges.append(FitnessEdge(pid, "content_creation", score_to_tier(multi_norm[pid]), multi_norm[pid]))
        edges.append(FitnessEdge(pid, "work_productivity", score_to_tier(single_norm[pid]), single_norm[pid]))
        edges.append(FitnessEdge(pid, "general_use", score_to_tier(general_norm[pid]), general_norm[pid]))
        # storage_homeserver: no CPU edge (per spec)

    return edges


# ── Write ──────────────────────────────────────────────────────────────────────

def upsert_edges(session, edges: list[FitnessEdge]) -> int:
    """
    MATCH (not MERGE) on both nodes — fail loudly if a product_id from the
    CSV isn't in the graph, rather than silently seeding a partial graph.
    MERGE on the relationship preserves any existing `weight` property the
    seed_graph.py stub already wrote — this script only ever touches
    tier/score, never weight.

    Returns count of edges written.
    """
    count = 0
    for edge in edges:
        result = session.run(
            """
            MATCH (c:Component {product_id: $product_id})
            MATCH (u:UseCase {name: $use_case})
            MERGE (c)-[r:GOOD_FOR]->(u)
            SET r.tier = $tier, r.score = $score
            RETURN c
            """,
            product_id=edge.product_id,
            use_case=edge.use_case,
            tier=edge.tier,
            score=edge.score,
        )
        if result.single() is None:
            raise RuntimeError(
                f"[seed_fitness_benchmarks] No match for product_id={edge.product_id!r} "
                f"use_case={edge.use_case!r} — component or use-case node missing from graph."
            )
        count += 1
    return count


# ── CLI entry ──────────────────────────────────────────────────────────────────

def main(gpu_path: str = _DEFAULT_GPU_CSV, cpu_path: str = _DEFAULT_CPU_CSV) -> None:
    try:
        gpu_rows = _load_csv(gpu_path)
    except FileNotFoundError:
        print(f"[seed_fitness_benchmarks] Missing GPU CSV: {gpu_path}", file=sys.stderr)
        sys.exit(1)

    try:
        cpu_rows = _load_csv(cpu_path)
    except FileNotFoundError:
        print(f"[seed_fitness_benchmarks] Missing CPU CSV: {cpu_path}", file=sys.stderr)
        sys.exit(1)

    edges = compute_gpu_edges(gpu_rows) + compute_cpu_edges(cpu_rows)

    try:
        driver = _get_driver()
        driver.verify_connectivity()
    except KeyError as e:
        print(
            f"[seed_fitness_benchmarks] Missing env var: {e}. "
            "Set NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD.",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as e:
        print(
            f"[seed_fitness_benchmarks] Neo4j not available — "
            "check NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD.\n"
            f"  Error: {e}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"[seed_fitness_benchmarks] Writing {len(edges)} GOOD_FOR tier/score edges …")
    with driver.session() as session:
        written = upsert_edges(session, edges)

    print(f"[seed_fitness_benchmarks] Done. {written} edges written.")


def _cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gpu", default=_DEFAULT_GPU_CSV, help="Path to gpu_benchmarks.csv")
    parser.add_argument("--cpu", default=_DEFAULT_CPU_CSV, help="Path to cpu_benchmarks.csv")
    args = parser.parse_args()
    main(args.gpu, args.cpu)


if __name__ == "__main__":
    _cli()
