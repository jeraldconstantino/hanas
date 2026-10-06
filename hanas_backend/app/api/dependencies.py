"""FastAPI dependency providers."""

from collections.abc import Iterator
from secrets import compare_digest

from fastapi import Header, HTTPException, status

from app.core.config import settings
from app.database.connection import get_db_connection
from app.database.repositories.sensor_repository import (
    PostgresSensorReadingRepository,
    SensorReadingRepository,
)


def get_sensor_repository() -> Iterator[SensorReadingRepository]:
    """Yield a sensor reading repository for the current request."""
    with get_db_connection() as db_connection:
        yield PostgresSensorReadingRepository(db_connection)


def verify_device_token(x_device_token: str | None = Header(default=None)) -> None:
    """Require a matching ESP32 token when DEVICE_API_TOKEN is configured."""
    if not settings.device_api_token:
        if settings.app_env == "prod":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="DEVICE_API_TOKEN is not configured for production.",
            )
        return

    if x_device_token is not None and compare_digest(x_device_token, settings.device_api_token):
        return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid device token.",
    )


def verify_operator_token(x_operator_token: str | None = Header(default=None)) -> None:
    """Require a matching dashboard operator token when OPERATOR_API_TOKEN is configured."""
    if not settings.operator_api_token:
        if settings.app_env == "prod":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="OPERATOR_API_TOKEN is not configured for production.",
            )
        return

    if x_operator_token is not None and compare_digest(
        x_operator_token,
        settings.operator_api_token,
    ):
        return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid operator token.",
    )
