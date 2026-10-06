"""Control-cycle lifecycle routes."""

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from app.api.dependencies import get_sensor_repository, verify_device_token, verify_operator_token
from app.core.config import settings
from app.database.repositories.sensor_repository import ControlCycleConflict, InvalidHumanReview, SensorReadingRepository
from app.schemas.control_cycle import (
    ControlCycleActionStartedPayload,
    ControlCycleActionCompletedPayload,
    ControlCycleCompletionPayload,
    ControlCycleCompletionResponse,
    HumanReviewPayload,
    HumanReviewResponse,
    LatestControlCycle,
    PendingCommandResponse,
)
from app.services.runtime_settings import (
    emergency_stop_enabled,
    maintenance_mode_enabled,
    monitoring_mode_enabled,
)

router = APIRouter(prefix="/control-cycles", tags=["control-cycles"])


@router.get("/latest", response_model=LatestControlCycle)
def get_latest_control_cycle(
    repository: SensorReadingRepository = Depends(get_sensor_repository),
) -> LatestControlCycle:
    """Return the latest control cycle for the frontend dashboard."""
    latest_cycle = repository.get_latest_control_cycle()
    if latest_cycle is None:
        raise HTTPException(status_code=404, detail="No control cycles found.")

    return latest_cycle


@router.get("/pending-command", response_model=PendingCommandResponse)
def get_pending_command(
    repository: SensorReadingRepository = Depends(get_sensor_repository),
    _: None = Depends(verify_device_token),
) -> PendingCommandResponse:
    """Return the next ESP32 pump command from HITL approval or batch scheduling."""
    if emergency_stop_enabled(repository):
        repository.emergency_stop_active_commands(settings.default_control_strategy)
        return PendingCommandResponse(
            status="emergency_stop",
            has_command=False,
            message="Emergency stop is enabled; no pump command will be dispatched.",
        )

    if maintenance_mode_enabled(repository):
        return PendingCommandResponse(
            status="maintenance_mode",
            has_command=False,
            message="Maintenance mode is enabled; no pump command will be dispatched.",
        )

    if monitoring_mode_enabled(repository):
        return PendingCommandResponse(
            status="monitoring_mode",
            has_command=False,
            message="Monitoring Only is enabled; no pump command will be dispatched.",
        )

    response = repository.get_pending_human_review_command()
    if response.has_command:
        logger.info(
            "Dispatched pending ESP32 command control_cycle_id={} pump={} duration_ms={}",
            response.control_cycle_id,
            response.pump_activated,
            response.duration_ms,
        )
    return response


@router.post("/{control_cycle_id}/action-started", response_model=ControlCycleCompletionResponse)
def mark_control_cycle_action_started(
    control_cycle_id: int,
    payload: ControlCycleActionStartedPayload,
    repository: SensorReadingRepository = Depends(get_sensor_repository),
    _: None = Depends(verify_device_token),
) -> ControlCycleCompletionResponse:
    """Mark the time when the ESP32 starts a dosing action."""
    if emergency_stop_enabled(repository):
        repository.emergency_stop_active_commands(settings.default_control_strategy)
        raise HTTPException(status_code=409, detail="Emergency stop is enabled; pump start rejected.")
    if maintenance_mode_enabled(repository):
        raise HTTPException(status_code=409, detail="Maintenance mode is enabled; pump start rejected.")
    if monitoring_mode_enabled(repository):
        raise HTTPException(status_code=409, detail="Monitoring Only is enabled; pump start rejected.")

    try:
        repository.mark_control_cycle_action_started(control_cycle_id, payload.status)
    except ControlCycleConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    logger.info(
        "Marked control cycle id={} action_started status={}",
        control_cycle_id,
        payload.status,
    )
    return ControlCycleCompletionResponse(
        status=payload.status,
        control_cycle_id=control_cycle_id,
    )


@router.post("/{control_cycle_id}/complete", response_model=ControlCycleCompletionResponse)
def complete_control_cycle(
    control_cycle_id: int,
    payload: ControlCycleCompletionPayload,
    repository: SensorReadingRepository = Depends(get_sensor_repository),
    _: None = Depends(verify_device_token),
) -> ControlCycleCompletionResponse:
    """Mark a control cycle as completed after ESP32 actuation finishes."""
    try:
        repository.complete_control_cycle(control_cycle_id, payload.status)
    except ControlCycleConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    logger.info(
        "Marked control cycle id={} as status={}",
        control_cycle_id,
        payload.status,
    )
    return ControlCycleCompletionResponse(
        status=payload.status,
        control_cycle_id=control_cycle_id,
    )


@router.post("/{control_cycle_id}/action-completed", response_model=ControlCycleCompletionResponse)
def mark_control_cycle_action_completed(
    control_cycle_id: int,
    payload: ControlCycleActionCompletedPayload,
    repository: SensorReadingRepository = Depends(get_sensor_repository),
    _: None = Depends(verify_device_token),
) -> ControlCycleCompletionResponse:
    """Mark pump shutdown while the reservoir remains in its mixing window."""
    try:
        repository.mark_control_cycle_action_completed(control_cycle_id, payload.status)
    except ControlCycleConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    logger.info(
        "Marked control cycle id={} pump action complete status={}",
        control_cycle_id,
        payload.status,
    )
    return ControlCycleCompletionResponse(
        status=payload.status,
        control_cycle_id=control_cycle_id,
    )


@router.post("/{control_cycle_id}/human-review", response_model=HumanReviewResponse)
def apply_human_review(
    control_cycle_id: int,
    payload: HumanReviewPayload,
    repository: SensorReadingRepository = Depends(get_sensor_repository),
    _: None = Depends(verify_operator_token),
) -> HumanReviewResponse:
    """Apply an operator approval, rejection, or override to a pending agentic decision."""
    if emergency_stop_enabled(repository):
        raise HTTPException(status_code=409, detail="Emergency stop is enabled; human review is blocked.")
    if maintenance_mode_enabled(repository):
        raise HTTPException(status_code=409, detail="Maintenance mode is enabled; human review is blocked.")
    if monitoring_mode_enabled(repository):
        raise HTTPException(status_code=409, detail="Monitoring Only is enabled; human review is blocked.")

    try:
        response = repository.apply_human_review(control_cycle_id, payload)
    except InvalidHumanReview as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    logger.info(
        "Applied HITL review for control_cycle_id={} action={} status={} pump={}",
        control_cycle_id,
        payload.action,
        response.status,
        response.pump_activated,
    )
    return response
