"""Notification-log read routes for the frontend dashboard."""

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_sensor_repository, verify_operator_token
from app.database.repositories.sensor_repository import SensorReadingRepository
from app.schemas.notification import NotificationLogEntry

router = APIRouter(prefix="/notification-logs", tags=["notification-logs"])


@router.get("", response_model=list[NotificationLogEntry])
def get_notification_logs(
    limit: int = Query(default=20, ge=1, le=200),
    repository: SensorReadingRepository = Depends(get_sensor_repository),
    _: None = Depends(verify_operator_token),
) -> list[NotificationLogEntry]:
    """Return recent SMS notification logs to an authenticated operator."""
    return repository.get_recent_notification_logs(limit=limit)
