import hmac
import logging
import os
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from dotenv import load_dotenv

logger = logging.getLogger(__name__)
security = HTTPBearer()

# Local development reads .env; existing process/container variables retain
# precedence because python-dotenv does not override them by default.
load_dotenv()
VALID_API_KEYS = {
    key.strip() for key in os.getenv("VALID_API_KEYS", "").split(",") if key.strip()
}


def valid_api_keys() -> set[str]:
    """The API keys currently configured in ``VALID_API_KEYS``."""
    return VALID_API_KEYS


def is_valid_api_key(api_key: Optional[str]) -> bool:
    """Whether ``api_key`` is a configured key, compared in constant time."""
    if not api_key:
        return False
    candidate = api_key.encode()
    return any(hmac.compare_digest(candidate, key.encode()) for key in VALID_API_KEYS)


class APIKeyError(Exception):
    """An API key was missing, invalid, or cannot be checked."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def check_api_key(api_key: Optional[str]) -> str:
    """Validate a bearer API key shared by the REST API and the MCP server."""
    if not VALID_API_KEYS:
        logger.warning("No API keys configured in environment variables")
        raise APIKeyError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Authentication not properly configured",
        )

    if not is_valid_api_key(api_key):
        logger.warning("Invalid API key attempted")
        raise APIKeyError(status.HTTP_401_UNAUTHORIZED, "Invalid API key")

    logger.info("Valid API key authenticated")
    return api_key


def bearer_token(authorization: Optional[str]) -> Optional[str]:
    """Return the token from an ``Authorization: Bearer <token>`` header."""
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


async def verify_api_key(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """
    Verify the API key from the Authorization header.
    Expected format: Authorization: Bearer <api_key>
    """
    try:
        return check_api_key(credentials.credentials)
    except APIKeyError as error:
        headers = (
            {"WWW-Authenticate": "Bearer"}
            if error.status_code == status.HTTP_401_UNAUTHORIZED
            else None
        )
        raise HTTPException(
            status_code=error.status_code, detail=error.detail, headers=headers
        ) from error
