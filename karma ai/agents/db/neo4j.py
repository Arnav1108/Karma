import os

from neo4j import GraphDatabase

from agents.schemas.slots import ComponentSlot

_driver = None


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
    # Phase 2 implementation pending
    def compatibility_check(
        self,
        candidate_product_id: str,
        locked_parts: dict,
    ) -> list[dict]:
        return []

    def fitness_filter(
        self,
        slot: ComponentSlot,
        use_case: str,
        candidate_product_ids: list[str],
        threshold: float,
    ) -> list[dict]:
        return []
