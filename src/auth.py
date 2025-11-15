import logging
import os
from fastapi import HTTPException, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

logger = logging.getLogger(__name__)
security = HTTPBearer()

VALID_API_KEYS = (
    os.getenv("VALID_API_KEYS", "").split(",") if os.getenv("VALID_API_KEYS") else []
)


async def verify_api_key(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """
    Verify the API key from the Authorization header.
    Expected format: Authorization: Bearer <api_key>
    """
    if not VALID_API_KEYS:
        logger.warning("No API keys configured in environment variables")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication not properly configured",
        )

    if credentials.credentials not in VALID_API_KEYS:
        logger.warning(f"Invalid API key attempted: {credentials.credentials[:10]}...")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    logger.info("Valid API key authenticated")
    return credentials.credentials
