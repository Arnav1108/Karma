import os
from typing import Optional

from neo4j import GraphDatabase

from agents.schemas.slots import ComponentSlot

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

# Fitness edge lookup — OPTIONAL so components with no GOOD_FOR edge return weight=null.
_FITNESS_QUERY = """
OPTIONAL MATCH (c:Component {product_id: $product_id})-[r:GOOD_FOR]->(u:UseCase {name: $use_case})
RETURN r.weight AS weight
LIMIT 1
"""

# Strict fitness lookup — returns nothing if no edge exists.
_FITNESS_SINGLE_QUERY = """
MATCH (c:Component {product_id: $product_id})-[r:GOOD_FOR]->(u:UseCase {name: $use_case})
RETURN r.weight AS weight
LIMIT 1
"""

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
        Return candidates whose GOOD_FOR edge weight meets fitness_threshold.

        Components with no GOOD_FOR edge for use_case are included by default
        (fail open) — sparse graph coverage must not exclude valid candidates.
        Results are ordered: weighted passes (highest first) then unweighted.

        Args:
            candidate_product_ids: Product IDs to filter.
            use_case: Target use-case name matching UseCase.name in the graph.
            fitness_threshold: Minimum GOOD_FOR weight in [0.0, 1.0].
                               0.0 returns all candidates that have an edge, plus unweighted.

        Returns:
            Filtered list: weighted passes descending, then unweighted fall-opens.
        """
        if not candidate_product_ids:
            return []

        try:
            driver = _get_driver()
        except Exception:
            return list(candidate_product_ids)

        weighted: list[tuple[str, float]] = []
        unweighted: list[str] = []

        with driver.session() as session:
            for product_id in candidate_product_ids:
                record = session.run(
                    _FITNESS_QUERY,
                    product_id=product_id,
                    use_case=use_case,
                ).single()
                weight = record["weight"] if record is not None else None
                if weight is None:
                    # Component absent from graph or has no GOOD_FOR edge → fail open.
                    unweighted.append(product_id)
                elif float(weight) >= fitness_threshold:
                    weighted.append((product_id, float(weight)))
                # weight < threshold → excluded

        weighted.sort(key=lambda x: x[1], reverse=True)
        return [pid for pid, _ in weighted] + unweighted

    def get_component_fitness(
        self,
        product_id: str,
        use_case: str,
    ) -> Optional[float]:
        """
        Return the GOOD_FOR edge weight between a component and a use case.

        Args:
            product_id: Component's product ID.
            use_case: Target use-case name matching UseCase.name in the graph.

        Returns:
            Edge weight as float, or None if no edge exists or component is absent.
        """
        try:
            driver = _get_driver()
            with driver.session() as session:
                record = session.run(
                    _FITNESS_SINGLE_QUERY,
                    product_id=product_id,
                    use_case=use_case,
                ).single()
                if record is None or record["weight"] is None:
                    return None
                return float(record["weight"])
        except Exception:
            return None
