import logging
import os
from typing import Optional

from dotenv import load_dotenv
from neo4j import GraphDatabase

from agents.schemas.slots import ComponentSlot

logger = logging.getLogger(__name__)

# Load .env so a standalone import has NEO4J_* available. Idempotent: only fills
# vars that are not already set in the process environment.
load_dotenv()

_driver = None

# -- Cypher query constants (no f-strings; all inputs passed as params) --

# Junction-node traversal for socket compatibility (CPU ↔ Motherboard, Cooler ↔ CPU/Mobo).
# Aggregates with count() so .single() is safe when multiple spec paths exist.
_SOCKET_COMPAT_QUERY = """
OPTIONAL MATCH (c:Component {product_id: $candidate_id})
WITH c, c IS NOT NULL AS candidate_exists
OPTIONAL MATCH (locked_node:Component {product_id: $locked_id})
WITH c, candidate_exists, locked_node, locked_node IS NOT NULL AS locked_exists
OPTIONAL MATCH (c)-[:REQUIRES_SOCKET|COMPATIBLE_SOCKET]->(spec:Spec {type: "socket"})
              <-[:REQUIRES_SOCKET|COMPATIBLE_SOCKET]-(locked_node)
WITH candidate_exists, locked_exists, count(spec) AS shared_specs
RETURN candidate_exists, locked_exists, shared_specs > 0 AS compatible
"""

# Junction-node traversal for DDR generation compatibility (Motherboard ↔ RAM).
_DDR_COMPAT_QUERY = """
OPTIONAL MATCH (c:Component {product_id: $candidate_id})
WITH c, c IS NOT NULL AS candidate_exists
OPTIONAL MATCH (locked_node:Component {product_id: $locked_id})
WITH c, candidate_exists, locked_node, locked_node IS NOT NULL AS locked_exists
OPTIONAL MATCH (c)-[:SUPPORTS_DDR]->(spec:Spec {type: "ddr_gen"})
              <-[:SUPPORTS_DDR]-(locked_node)
WITH candidate_exists, locked_exists, count(spec) AS shared_specs
RETURN candidate_exists, locked_exists, shared_specs > 0 AS compatible
"""

# Junction-node traversal for form-factor compatibility (Case ↔ Motherboard).
_FORM_FACTOR_COMPAT_QUERY = """
OPTIONAL MATCH (c:Component {product_id: $candidate_id})
WITH c, c IS NOT NULL AS candidate_exists
OPTIONAL MATCH (locked_node:Component {product_id: $locked_id})
WITH c, candidate_exists, locked_node, locked_node IS NOT NULL AS locked_exists
OPTIONAL MATCH (c)-[:SUPPORTS_FORM_FACTOR|REQUIRES_FORM_FACTOR]->(spec:Spec {type: "form_factor"})
              <-[:SUPPORTS_FORM_FACTOR|REQUIRES_FORM_FACTOR]-(locked_node)
WITH candidate_exists, locked_exists, count(spec) AS shared_specs
RETURN candidate_exists, locked_exists, shared_specs > 0 AS compatible
"""

# Fitness edge lookup — OPTIONAL so components with no GOOD_FOR edge return tier/score=null.
# tier/score are written by data/graph/seed_fitness_benchmarks.py (benchmark-derived),
# superseding the old stub r.weight from seed_graph.py's _GOOD_FOR_WEIGHTS table.
_FITNESS_QUERY = """
OPTIONAL MATCH (c:Component {product_id: $product_id})-[r:GOOD_FOR]->(u:UseCase {name: $use_case})
RETURN r.tier AS tier, r.score AS score
LIMIT 1
"""

# Strict fitness lookup — returns nothing if no edge exists.
_FITNESS_SINGLE_QUERY = """
MATCH (c:Component {product_id: $product_id})-[r:GOOD_FOR]->(u:UseCase {name: $use_case})
RETURN r.tier AS tier, r.score AS score
LIMIT 1
"""

# Number of quintile buckets score_to_tier() (data/graph/seed_fitness_benchmarks.py)
# cuts [0.0, 1.0] scores into. required_tier() below must use the same cut points so
# a threshold of 0.8 means exactly "tier 4 or nothing," not an approximation of it.
_TIER_COUNT = 5

# Empirically (phase4/fitness-query-migration: 63 live derive_fitness_thresholds
# samples across all 7 fixtures), real LLM thresholds land on 0.05 increments —
# either mid-bucket or exactly on a cut point — never within this margin below a
# boundary. That pattern isn't guaranteed, so a threshold that does land here is
# logged loudly rather than silently truncated a tier low.
_TIER_BOUNDARY_WARN_MARGIN = 0.01


def _required_tier(fitness_threshold: float) -> int:
    """Convert a continuous fitness_threshold (0.0-1.0) into the tier ordinal
    (0-4) that GOOD_FOR.tier buckets components into.

    Raises ValueError for out-of-range input (fail loud on a contract violation).
    Degrades safely — logs a warning but still returns the floored tier — when
    the threshold sits within _TIER_BOUNDARY_WARN_MARGIN of a cut point, since
    that pattern has never been observed in practice and could indicate the
    LLM threshold and the tier scale have drifted apart.
    """
    if not isinstance(fitness_threshold, (int, float)) or not 0.0 <= fitness_threshold <= 1.0:
        raise ValueError(
            f"fitness_threshold must be a float in [0.0, 1.0], got {fitness_threshold!r}"
        )

    raw = fitness_threshold * _TIER_COUNT
    tier = min(_TIER_COUNT - 1, int(raw))

    # Margin is compared in original threshold units (0.0-1.0), not raw (threshold*5)
    # units — comparing in raw space would make the margin 5x tighter than intended.
    # The 1e-9 fudge absorbs float noise (e.g. 0.8 - 0.79 lands on 0.010000000000000009,
    # not 0.01) so it doesn't defeat the margin check it's meant to satisfy.
    next_boundary_tier = tier + 1
    if next_boundary_tier <= _TIER_COUNT:
        next_boundary_threshold = next_boundary_tier / _TIER_COUNT
        gap = next_boundary_threshold - fitness_threshold
        if gap <= _TIER_BOUNDARY_WARN_MARGIN + 1e-9:
            logger.warning(
                "[Neo4j] fitness_threshold %.4f is within %.4f of tier boundary %d "
                "(threshold %.2f) but truncates to tier %d — outside the observed "
                "0.05-increment pattern from derive_fitness_thresholds; verify this "
                "threshold was intentional.",
                fitness_threshold, gap, next_boundary_tier, next_boundary_threshold, tier,
            )

    return tier

# Maps (candidate_slot, locked_slot) → Cypher that checks their shared-spec constraint.
# Slot pairs absent from this map have no compatibility rule and pass through unchanged.
_CONSTRAINT_MAP: dict[tuple, str] = {
    (ComponentSlot.cpu, ComponentSlot.motherboard): _SOCKET_COMPAT_QUERY,
    (ComponentSlot.motherboard, ComponentSlot.cpu): _SOCKET_COMPAT_QUERY,
    (ComponentSlot.motherboard, ComponentSlot.ram): _DDR_COMPAT_QUERY,
    (ComponentSlot.ram, ComponentSlot.motherboard): _DDR_COMPAT_QUERY,
    (ComponentSlot.cooler, ComponentSlot.cpu): _SOCKET_COMPAT_QUERY,
    (ComponentSlot.cooler, ComponentSlot.motherboard): _SOCKET_COMPAT_QUERY,
    (ComponentSlot.case, ComponentSlot.motherboard): _FORM_FACTOR_COMPAT_QUERY,
}


def get_driver():
    """Public accessor for the singleton Neo4j driver."""
    return _get_driver()


def get_driver():
    """Public accessor for the singleton Neo4j driver."""
    return _get_driver()


def _get_driver():
    global _driver
    if _driver is None:
        uri = os.environ["NEO4J_URI"]
        username = os.environ["NEO4J_USERNAME"]
        password = os.environ["NEO4J_PASSWORD"]
        _driver = GraphDatabase.driver(uri, auth=(username, password))
    return _driver


class Neo4jClient:
    def ping(self) -> bool:
        """Return True if Neo4j is reachable; False on any connection failure."""
        try:
            driver = _get_driver()
            driver.verify_connectivity()
            return True
        except Exception:
            return False

    def compatibility_check(
        self,
        candidate_product_ids: list[str],
        locked_parts: dict[ComponentSlot, str],
        candidate_slot: ComponentSlot,
    ) -> list[str]:
        """
        Return the subset of candidate_product_ids compatible with all locked_parts.

        Checks junction-node constraints (socket, ddr_gen, form_factor) only for
        slot pairs that have a defined rule in _CONSTRAINT_MAP. Pairs with no rule
        pass through unchanged. Candidates absent from the graph also pass through
        (fail open — do not penalise components because the graph is unpopulated).

        Args:
            candidate_product_ids: Product IDs to filter.
            locked_parts: Already-locked {slot: product_id} mapping from build state.
            candidate_slot: The component slot these candidates occupy.

        Returns:
            Compatible subset of candidate_product_ids, in original order.
        """
        if not locked_parts or not candidate_product_ids:
            return list(candidate_product_ids)

        relevant_checks = [
            (locked_id, _CONSTRAINT_MAP[(candidate_slot, locked_slot)])
            for locked_slot, locked_id in locked_parts.items()
            if (candidate_slot, locked_slot) in _CONSTRAINT_MAP
        ]

        if not relevant_checks:
            return list(candidate_product_ids)

        try:
            driver = _get_driver()
        except Exception:
            return list(candidate_product_ids)

        compatible: list[str] = []
        with driver.session() as session:
            for candidate_id in candidate_product_ids:
                passes = True
                for locked_id, cypher in relevant_checks:
                    record = session.run(
                        cypher,
                        candidate_id=candidate_id,
                        locked_id=locked_id,
                    ).single()
                    if record is None:
                        continue  # No data → fail open
                    # Exclude only when both nodes are in the graph and share no required spec.
                    if (
                        record["candidate_exists"]
                        and record["locked_exists"]
                        and not record["compatible"]
                    ):
                        passes = False
                        break
                if passes:
                    compatible.append(candidate_id)

        return compatible

    def fitness_filter(
        self,
        candidate_product_ids: list[str],
        use_case: str,
        fitness_threshold: float,
    ) -> list[str]:
        """
        Rank candidates by GOOD_FOR fitness for use_case — never excludes one.

        fitness_threshold is a continuous 0.0-1.0 value (from derive_fitness_thresholds).
        _required_tier() maps it to the coarse 0-4 ordinal GOOD_FOR.tier uses, but that
        tier is now a SOFT tie-break only, not a hard cutoff: candidates are ranked by
        the continuous GOOD_FOR.score (descending) as the primary signal, with
        "meets required_tier" breaking ties. This is deliberate — required_tier is a
        budget-blind, catalog-wide bar (e.g. gaming's GPU threshold is uniformly tier 4,
        the catalog's flagship band) while the candidates here are already restricted to
        one price band; hard-excluding by tier silently emptied the shortlist for most
        gaming builds and fell through to the unranked fail-open below, so fitness never
        actually influenced GPU/CPU selection except at the top of the market. See
        docs/context.md open item 4.

        Components with no GOOD_FOR edge for use_case are included by default
        (fail open) — sparse graph coverage must not exclude valid candidates.
        Results are ordered: all scored candidates (score descending, tier as a
        tie-break) then unranked fail-opens.

        Args:
            candidate_product_ids: Product IDs to filter.
            use_case: Target use-case name matching UseCase.name in the graph.
            fitness_threshold: Fitness in [0.0, 1.0], mapped to a required tier used
                               only to break score ties, never to drop a candidate.

        Returns:
            Ranked list: every candidate with a GOOD_FOR edge (score descending, tier
            tie-break), then unranked fail-opens. Never shorter than the input
            (barring driver failure, which falls back to the input order unchanged).
        """
        if not candidate_product_ids:
            return []

        required_tier = _required_tier(fitness_threshold)

        try:
            driver = _get_driver()
        except Exception:
            return list(candidate_product_ids)

        scored: list[tuple[str, int, float]] = []
        unranked: list[str] = []

        with driver.session() as session:
            for product_id in candidate_product_ids:
                record = session.run(
                    _FITNESS_QUERY,
                    product_id=product_id,
                    use_case=use_case,
                ).single()
                tier = record["tier"] if record is not None else None
                if tier is None:
                    # Component absent from graph or has no GOOD_FOR edge → fail open.
                    unranked.append(product_id)
                else:
                    scored.append((product_id, int(tier), float(record["score"])))

        # Primary: score descending. Tie-break only: meeting required_tier, then tier
        # itself — never a reason to drop a candidate, only to order equal scores.
        scored.sort(key=lambda x: (x[2], x[1] >= required_tier, x[1]), reverse=True)
        return [pid for pid, _, _ in scored] + unranked

    def get_component_fitness(
        self,
        product_id: str,
        use_case: str,
    ) -> Optional[float]:
        """
        Return the GOOD_FOR edge score between a component and a use case.

        Returns the continuous score (not the coarse tier) since this is the
        finer-grained value external callers would want.

        Args:
            product_id: Component's product ID.
            use_case: Target use-case name matching UseCase.name in the graph.

        Returns:
            Edge score as float, or None if no edge exists or component is absent.
        """
        try:
            driver = _get_driver()
            with driver.session() as session:
                record = session.run(
                    _FITNESS_SINGLE_QUERY,
                    product_id=product_id,
                    use_case=use_case,
                ).single()
                if record is None or record["score"] is None:
                    return None
                return float(record["score"])
        except Exception:
            return None
