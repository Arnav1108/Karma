from fastapi import APIRouter

from api.config import get_settings

router = APIRouter()


@router.get("/healthz")
def healthz():
    settings = get_settings()
    return {"status": "ok", "version": settings.version}
