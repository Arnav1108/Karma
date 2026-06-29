"""
Neo4j graph schema — constraint and index definitions for Karma's knowledge graph.

apply_schema(driver) is idempotent: safe to call on every deploy (IF NOT EXISTS
requires Neo4j 4.4+; Neo4j 5.x is assumed).
"""

# ── Node label constants ──────────────────────────────────────────────────────
COMPONENT       = "Component"
SPEC            = "Spec"
USE_CASE        = "UseCase"
PERFORMANCE     = "Performance"
COMPONENT_CLASS = "ComponentClass"

# ── Uniqueness / node-key constraints ─────────────────────────────────────────

CONSTRAINT_COMPONENT_PRODUCT_ID = (
    "CREATE CONSTRAINT component_product_id IF NOT EXISTS "
    "FOR (n:Component) REQUIRE n.product_id IS UNIQUE"
)

# NODE KEY enforces uniqueness on (type, value) together and creates a
# composite index automatically — used by MERGE in the seed script.
CONSTRAINT_SPEC_NODE_KEY = (
    "CREATE CONSTRAINT spec_type_value IF NOT EXISTS "
    "FOR (n:Spec) REQUIRE (n.type, n.value) IS NODE KEY"
)

CONSTRAINT_USE_CASE_NAME = (
    "CREATE CONSTRAINT use_case_name IF NOT EXISTS "
    "FOR (n:UseCase) REQUIRE n.name IS UNIQUE"
)

CONSTRAINT_PERFORMANCE_NODE_KEY = (
    "CREATE CONSTRAINT performance_type_value IF NOT EXISTS "
    "FOR (n:Performance) REQUIRE (n.type, n.value) IS NODE KEY"
)

CONSTRAINT_COMPONENT_CLASS_NAME = (
    "CREATE CONSTRAINT component_class_name IF NOT EXISTS "
    "FOR (n:ComponentClass) REQUIRE n.name IS UNIQUE"
)

_CONSTRAINTS = [
    CONSTRAINT_COMPONENT_PRODUCT_ID,
    CONSTRAINT_SPEC_NODE_KEY,
    CONSTRAINT_USE_CASE_NAME,
    CONSTRAINT_PERFORMANCE_NODE_KEY,
    CONSTRAINT_COMPONENT_CLASS_NAME,
]

# ── Performance indexes ───────────────────────────────────────────────────────
# Speeds up Node 3's catalog-query → graph-filter funnel.

INDEX_COMPONENT_CATEGORY = (
    "CREATE INDEX component_category IF NOT EXISTS "
    "FOR (n:Component) ON (n.category)"
)

INDEX_COMPONENT_PRICE = (
    "CREATE INDEX component_price IF NOT EXISTS "
    "FOR (n:Component) ON (n.price_inr)"
)

INDEX_COMPONENT_IN_STOCK = (
    "CREATE INDEX component_in_stock IF NOT EXISTS "
    "FOR (n:Component) ON (n.in_stock)"
)

# Single-property index on Spec.type for lookups that don't need the full NODE KEY.
INDEX_SPEC_TYPE = (
    "CREATE INDEX spec_type IF NOT EXISTS "
    "FOR (n:Spec) ON (n.type)"
)

_INDEXES = [
    INDEX_COMPONENT_CATEGORY,
    INDEX_COMPONENT_PRICE,
    INDEX_COMPONENT_IN_STOCK,
    INDEX_SPEC_TYPE,
]


def apply_schema(driver) -> None:
    """Apply all constraints and indexes against a live Neo4j instance.

    Idempotent — IF NOT EXISTS means re-running never raises duplicates.
    """
    with driver.session() as session:
        for stmt in _CONSTRAINTS + _INDEXES:
            session.run(stmt)
