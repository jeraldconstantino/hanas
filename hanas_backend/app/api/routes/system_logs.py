"""System-log read routes for the frontend dashboard."""

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.dependencies import get_sensor_repository
from app.database.repositories.sensor_repository import SensorReadingRepository
from app.schemas.sensor import LatestSystemLog, SensorHistoryEntry

router = APIRouter(prefix="/system-logs", tags=["system-logs"])


@router.get("", response_model=list[SensorHistoryEntry])
def get_system_logs(
    limit: int = Query(default=50, ge=1, le=100_000),
    hours: int | None = Query(default=None, ge=1, le=8_784),
    cycle_id: int | None = Query(default=None, ge=1),
    repository: SensorReadingRepository = Depends(get_sensor_repository),
) -> list[SensorHistoryEntry]:
    """Return recent system logs for Trends and Logs & Alerts dashboard views."""
    return repository.get_recent_sensor_logs(limit=limit, since_hours=hours, control_cycle_id=cycle_id)


@router.get("/latest", response_model=LatestSystemLog)
def get_latest_system_log(
    repository: SensorReadingRepository = Depends(get_sensor_repository),
) -> LatestSystemLog:
    """Return the latest persisted system log."""
    latest_log = repository.get_latest_system_log()
    if latest_log is None:
        raise HTTPException(status_code=404, detail="No system logs found.")

    return latest_log
