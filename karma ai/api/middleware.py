import logging

from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader

from api.config import Settings, get_settings

logger = logging.getLogger(__name__)

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

_auth_disabled_warned = False


def require_api_key(
    api_key: str | None = Depends(_api_key_header),
    settings: Settings = Depends(get_settings),
) -> None:
    global _auth_disabled_warned
    if not settings.api_keys:
        if not _auth_disabled_warned:
            _auth_disabled_warned = True
            logger.warning(
                "KARMA_API_KEYS is not set — API key auth is DISABLED. "
                "All requests are allowed through."
            )
        return
    if api_key is None or api_key not in settings.api_keys:
        raise HTTPException(
            status_code=401,
            detail={"error": {"code": "UNAUTHORIZED", "message": "Invalid or missing API key."}},
        )
