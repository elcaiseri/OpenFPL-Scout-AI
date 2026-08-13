import logging
import os

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from dotenv import load_dotenv

logger = logging.getLogger(__name__)
security = HTTPBearer()

# Local development reads .env; existing process/container variables retain
# precedence because python-dotenv does not override them by default.
load_dotenv()
VALID_API_KEYS = {
    key.strip()
    for key in os.getenv("VALID_API_KEYS", "").split(",")
    if key.strip()
}


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
        logger.warning("Invalid API key attempted")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    logger.info("Valid API key authenticated")
    return credentials.credentials
