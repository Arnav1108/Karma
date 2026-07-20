import os
from contextlib import contextmanager
from urllib.parse import urlparse

from dotenv import load_dotenv
from psycopg2.pool import ThreadedConnectionPool

from agents.schemas.brief import UserBuildBrief
from agents.schemas.slots import ComponentSlot

# Load .env so a standalone import has POSTGRES_URL available. Idempotent: only fills
# vars that are not already set in the process environment.
load_dotenv()

_pool: ThreadedConnectionPool | None = None


def _get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is None:
        url = os.environ["POSTGRES_URL"]
        try:
            _pool = ThreadedConnectionPool(minconn=1, maxconn=10, dsn=url)
        except Exception as e:
            host = urlparse(url).hostname or "<unknown>"
            raise RuntimeError(
                f"[Karma DB] Cannot connect to Postgres. Your POSTGRES_URL may be using the "
                f"legacy Supabase direct host (db.<ref>.supabase.co) which has been retired. "
                f"Update it to the Session Pooler URL from: "
                f"Supabase Dashboard → Connect → Session pooler.\n"
                f"Current host: {host}\n"
                f"Original error: {e}"
            ) from e
    return _pool


@contextmanager
def _cursor():
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


class PostgresClient:
    def get_min_catalog_price(self, component_slot: ComponentSlot) -> int:
        with _cursor() as cur:
            cur.execute(
                """
                SELECT MIN(price_inr)
                FROM catalog
                WHERE category = %s AND in_stock = TRUE
                """,
                (component_slot.value,),
            )
            row = cur.fetchone()
            return int(row[0]) if row and row[0] is not None else 0

    def get_avg_catalog_price(self, component_slot: ComponentSlot) -> float | None:
        with _cursor() as cur:
            cur.execute(
                """
                SELECT AVG(price_inr)
                FROM catalog
                WHERE category = %s AND in_stock = TRUE
                """,
                (component_slot.value,),
            )
            row = cur.fetchone()
            return float(row[0]) if row and row[0] is not None else None

    def get_all_products(self) -> list[dict]:
        with _cursor() as cur:
            cur.execute(
                "SELECT * FROM catalog ORDER BY category, product_id"
            )
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]

    def get_parts_in_band(
        self,
        slot: ComponentSlot,
        low: int,
        high: int,
    ) -> list[dict]:
        with _cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM catalog
                WHERE category = %s
                  AND price_inr >= %s
                  AND price_inr <= %s
                  AND in_stock = TRUE
                """,
                (slot.value, low, high),
            )
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]

    def get_software_spec_cache(self, name: str) -> dict | None:
        with _cursor() as cur:
            cur.execute(
                """
                SELECT category, gpu_tier, cpu_tier, vram_gb, ram_gb, storage_gb
                FROM software_specs_cache
                WHERE name = %s
                """,
                (name,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            columns = [desc[0] for desc in cur.description]
            return dict(zip(columns, row))

    def set_software_spec_cache(
        self,
        name: str,
        category: str,
        *,
        gpu_tier: int,
        cpu_tier: int,
        vram_gb: int,
        ram_gb: int,
        storage_gb: int,
        source: str,
    ) -> None:
        with _cursor() as cur:
            cur.execute(
                """
                INSERT INTO software_specs_cache
                    (name, category, gpu_tier, cpu_tier, vram_gb, ram_gb, storage_gb, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (name) DO UPDATE SET
                    category = EXCLUDED.category,
                    gpu_tier = EXCLUDED.gpu_tier,
                    cpu_tier = EXCLUDED.cpu_tier,
                    vram_gb = EXCLUDED.vram_gb,
                    ram_gb = EXCLUDED.ram_gb,
                    storage_gb = EXCLUDED.storage_gb,
                    source = EXCLUDED.source,
                    created_at = now()
                """,
                (name, category, gpu_tier, cpu_tier, vram_gb, ram_gb, storage_gb, source),
            )

    def persist_locked_brief(self, brief: UserBuildBrief, session_id: str) -> None:
        with _cursor() as cur:
            cur.execute(
                """
                INSERT INTO locked_briefs
                    (brief_id, session_id, user_id, chat_id, schema_version, brief, locked_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (brief_id) DO NOTHING
                """,
                (
                    str(brief.brief_id), session_id, str(brief.user_id), str(brief.chat_id),
                    brief.schema_version, brief.model_dump_json(), brief.updated_at,
                ),
            )
