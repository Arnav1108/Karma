import os
from contextlib import contextmanager

from psycopg2.pool import ThreadedConnectionPool

from agents.schemas.slots import ComponentSlot

_pool: ThreadedConnectionPool | None = None


def _get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is None:
        url = os.environ["POSTGRES_URL"]
        _pool = ThreadedConnectionPool(minconn=1, maxconn=10, dsn=url)
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
                WHERE slot = %s AND in_stock = TRUE
                """,
                (component_slot.value,),
            )
            row = cur.fetchone()
            return int(row[0]) if row and row[0] is not None else 0

    def get_parts_in_band(
        self,
        slot: ComponentSlot,
        low: int,
        high: int,
        in_stock: bool = True,
    ) -> list[dict]:
        with _cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM catalog
                WHERE slot = %s
                  AND price_inr >= %s
                  AND price_inr <= %s
                  AND in_stock = %s
                """,
                (slot.value, low, high, in_stock),
            )
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]
