"""
Seed the Neo4j knowledge graph from the Postgres catalog.

Run from karma ai/:
    python -m data.graph.seed_graph

Idempotent — all writes use MERGE so re-running never duplicates nodes or edges.
"""
import json
import sys

from agents.db.neo4j import get_driver
from agents.db.neo4j_schema import apply_schema
from agents.db.postgres import PostgresClient
from agents.schemas.slots import ComponentSlot

# ── Use-case names mirror UserBuildBrief.purpose.primary_use_case ─────────────
_USE_CASES = [
    "gaming",
    "content_creation",
    "work_productivity",
    "general_use",
    "storage_homeserver",
]

# !!! STUB !!! ─────────────────────────────────────────────────────────────────
# Fitness weights: category → use-case → weight (0.0–1.0).
# These are placeholder defaults — replace with benchmark-derived values once
# real performance data is available. PSU / case / cooler / fans are omitted
# intentionally; they have no meaningful fitness score for a use-case.
_GOOD_FOR_WEIGHTS: dict[ComponentSlot, dict[str, float]] = {
    ComponentSlot.gpu: {
        "gaming":             0.9,
        "content_creation":   0.7,
        "work_productivity":  0.4,
        "general_use":        0.5,
        "storage_homeserver": 0.2,
    },
    ComponentSlot.cpu: {
        "gaming":             0.6,
        "content_creation":   0.7,
        "work_productivity":  0.9,
        "general_use":        0.6,
        "storage_homeserver": 0.5,
    },
    ComponentSlot.ram: {
        "gaming":             0.5,
        "content_creation":   0.5,
        "work_productivity":  0.5,
        "general_use":        0.5,
        "storage_homeserver": 0.5,
    },
    ComponentSlot.storage: {
        "gaming":             0.4,
        "content_creation":   0.4,
        "work_productivity":  0.4,
        "general_use":        0.4,
        "storage_homeserver": 0.4,
    },
}
# !!! END STUB !!! ─────────────────────────────────────────────────────────────


def _parse_specs(raw) -> dict:
    """Return the JSONB specs as a plain dict, dropping None-valued fields."""
    if not raw:
        return {}
    if isinstance(raw, str):
        raw = json.loads(raw)
    return {k: v for k, v in raw.items() if v is not None}


# ── Per-session seed logic ────────────────────────────────────────────────────

def _merge_static_nodes(session) -> None:
    """Idempotently create ComponentClass and UseCase nodes."""
    for slot in ComponentSlot:
        session.run(
            "MERGE (:ComponentClass {name: $name})",
            name=slot.value,
        )
    for uc in _USE_CASES:
        session.run(
            "MERGE (:UseCase {name: $name})",
            name=uc,
        )


def _merge_component(session, row: dict) -> None:
    """Create/update the :Component node, flattening specs into properties."""
    specs = _parse_specs(row.get("specs"))
    base_props = {
        "name":      row["name"],
        "brand":     row["brand"],
        "price_inr": row["price_inr"],
        "in_stock":  row["in_stock"],
        "category":  row["category"],
    }
    # SET c += map merges properties without removing existing ones.
    session.run(
        """
        MERGE (c:Component {product_id: $product_id})
        SET c += $base_props
        SET c += $spec_props
        """,
        product_id=row["product_id"],
        base_props=base_props,
        spec_props=specs,
    )


def _merge_belongs_to(session, product_id: str, category: str) -> None:
    session.run(
        """
        MATCH (c:Component {product_id: $product_id})
        MATCH (cc:ComponentClass {name: $category})
        MERGE (c)-[:BELONGS_TO]->(cc)
        """,
        product_id=product_id,
        category=category,
    )


# ── Compatibility family edges ────────────────────────────────────────────────

def _merge_socket_spec(session, product_id: str, rel_type: str, socket: str) -> None:
    """Wire a component to a :Spec{type:'socket'} node via rel_type."""
    session.run(
        f"""
        MATCH (c:Component {{product_id: $product_id}})
        MERGE (s:Spec {{type: 'socket', value: $socket}})
        MERGE (c)-[:{rel_type} {{socket: $socket}}]->(s)
        """,
        product_id=product_id,
        socket=socket,
    )


def _seed_compatibility(session, product_id: str, slot: ComponentSlot, specs: dict) -> None:
    if slot == ComponentSlot.cpu:
        socket = specs.get("socket")
        if socket:
            _merge_socket_spec(session, product_id, "REQUIRES_SOCKET", socket)

    elif slot == ComponentSlot.motherboard:
        socket = specs.get("socket")
        if socket:
            _merge_socket_spec(session, product_id, "REQUIRES_SOCKET", socket)

        # ddr_type is an int (4 or 5) in catalog specs
        ddr_type = specs.get("ddr_type")
        if ddr_type is not None:
            gen_value = f"DDR{ddr_type}"
            session.run(
                """
                MATCH (c:Component {product_id: $product_id})
                MERGE (s:Spec {type: 'ddr_gen', value: $gen_value})
                MERGE (c)-[:SUPPORTS_DDR {gen: $gen_value}]->(s)
                """,
                product_id=product_id,
                gen_value=gen_value,
            )

    elif slot == ComponentSlot.ram:
        # RAM connects to the same :Spec{type:'ddr_gen'} nodes as motherboards,
        # enabling compatibility traversal between the two.
        ddr_gen = specs.get("ddr_gen")
        if ddr_gen is not None:
            gen_value = f"DDR{ddr_gen}"
            session.run(
                """
                MATCH (c:Component {product_id: $product_id})
                MERGE (s:Spec {type: 'ddr_gen', value: $gen_value})
                MERGE (c)-[:SUPPORTS_DDR {gen: $gen_value}]->(s)
                """,
                product_id=product_id,
                gen_value=gen_value,
            )

    elif slot == ComponentSlot.cooler:
        # socket_compat is a list: ["LGA1700","LGA1851","AM4","AM5"]
        for socket in specs.get("socket_compat") or []:
            _merge_socket_spec(session, product_id, "COMPATIBLE_SOCKET", socket)

    elif slot == ComponentSlot.case:
        # form_factor_support is a list: ["ATX","mATX","ITX"]
        for ff in specs.get("form_factor_support") or []:
            session.run(
                """
                MATCH (c:Component {product_id: $product_id})
                MERGE (s:Spec {type: 'form_factor', value: $ff})
                MERGE (c)-[:SUPPORTS_FORM_FACTOR {ff: $ff}]->(s)
                """,
                product_id=product_id,
                ff=ff,
            )


# ── Fitness family edges ──────────────────────────────────────────────────────

def _seed_fitness(session, product_id: str, slot: ComponentSlot) -> None:
    # !!! STUB !!! weights — see _GOOD_FOR_WEIGHTS table above
    weights = _GOOD_FOR_WEIGHTS.get(slot)
    if not weights:
        return
    for uc, weight in weights.items():
        session.run(
            """
            MATCH (c:Component {product_id: $product_id})
            MATCH (u:UseCase {name: $uc})
            MERGE (c)-[r:GOOD_FOR]->(u)
            SET r.weight = $weight
            """,
            product_id=product_id,
            uc=uc,
            weight=weight,
        )


# ── Performance edges ─────────────────────────────────────────────────────────

def _seed_performance(session, product_id: str, slot: ComponentSlot, specs: dict) -> None:
    if slot == ComponentSlot.gpu:
        vram_gb = specs.get("vram_gb")
        if vram_gb is not None:
            session.run(
                """
                MATCH (c:Component {product_id: $product_id})
                MERGE (p:Performance {type: 'vram_gb', value: $vram_gb})
                MERGE (c)-[:HAS_VRAM {gb: $vram_gb}]->(p)
                """,
                product_id=product_id,
                vram_gb=vram_gb,
            )


# ── Main seed loop ────────────────────────────────────────────────────────────

def seed(session, products: list[dict]) -> None:
    """Populate the graph from a list of catalog rows. Idempotent."""
    _merge_static_nodes(session)

    for row in products:
        product_id = row["product_id"]
        category   = row["category"]
        specs      = _parse_specs(row.get("specs"))

        try:
            slot = ComponentSlot(category)
        except ValueError:
            print(f"[seed_graph] Unknown category '{category}' for {product_id}, skipping.", file=sys.stderr)
            continue

        _merge_component(session, row)
        _merge_belongs_to(session, product_id, category)
        _seed_compatibility(session, product_id, slot, specs)
        _seed_fitness(session, product_id, slot)
        _seed_performance(session, product_id, slot, specs)


def main() -> None:
    # ── Postgres ──────────────────────────────────────────────────────────────
    pg = PostgresClient()
    try:
        products = pg.get_all_products()
    except Exception as e:
        print(f"[seed_graph] Postgres unavailable: {e}", file=sys.stderr)
        sys.exit(1)

    # ── Neo4j ─────────────────────────────────────────────────────────────────
    try:
        driver = get_driver()
        driver.verify_connectivity()
    except KeyError as e:
        print(
            f"[seed_graph] Missing env var: {e}. "
            "Set NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD.",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as e:
        print(
            f"[seed_graph] Neo4j not available — "
            "check NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD.\n"
            f"  Error: {e}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"[seed_graph] Applying schema …")
    apply_schema(driver)

    print(f"[seed_graph] Seeding {len(products)} products …")
    with driver.session() as session:
        seed(session, products)

    print("[seed_graph] Done.")


if __name__ == "__main__":
    main()
