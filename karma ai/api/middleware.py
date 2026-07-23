import logging

from fastapi import Depends
from fastapi.security import APIKeyHeader

from api.config import Settings, get_settings

logger = logging.getLogger(__name__)

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

_auth_disabled_warned = False


class UnauthorizedError(Exception):
    """Raised by require_api_key when the X-API-Key header is missing or unknown.

    Deliberately NOT an IntakeServiceError/BuildServiceError subclass — auth is a
    cross-cutting concern scoped to neither service family, mirroring RateLimitError
    (api/rate_limit.py). Handled centrally in api/errors.py so its 401 response goes
    through the same _envelope helper as every other error — i.e. a
    {"error": {"code": "UNAUTHORIZED", ..., "retryable": false}} body — instead of
    FastAPI's default {"detail": ...} HTTPException shape, which omitted retryable and
    double-wrapped the envelope.
    """


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
        raise UnauthorizedError()
