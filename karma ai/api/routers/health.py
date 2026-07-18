import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from agents.db.neo4j import Neo4jClient
from agents.db.postgres import PostgresClient
from agents.schemas.slots import ComponentSlot
from api.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/healthz")
def healthz():
    settings = get_settings()
    return {"status": "ok", "version": settings.version}


def _postgres_up() -> bool:
    try:
        # Same connectivity probe tests/conftest.py's db_available fixture uses —
        # there is no dedicated PostgresClient.ping(); a live catalog query is
        # core's existing way of confirming the pool can actually serve Postgres.
        PostgresClient().get_min_catalog_price(ComponentSlot.gpu)
        return True
    except Exception:
        logger.exception("[readyz] Postgres check failed")
        return False


def _neo4j_up() -> bool:
    try:
        return Neo4jClient().ping()
    except Exception:
        logger.exception("[readyz] Neo4j check failed")
        return False


@router.get("/readyz")
def readyz():
    postgres_ok = _postgres_up()
    neo4j_ok = _neo4j_up()

    body = {
        "postgres": "up" if postgres_ok else "down",
        "neo4j": "up" if neo4j_ok else "down",
        "status": "ok" if postgres_ok and neo4j_ok else "degraded",
    }
    status_code = 200 if postgres_ok else 503
    return JSONResponse(content=body, status_code=status_code)
