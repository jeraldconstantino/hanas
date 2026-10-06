"""Manual batch cycle trigger and diagnostic routes."""

import asyncio

from fastapi import APIRouter, Depends

from app.api.dependencies import get_sensor_repository, verify_operator_token
from app.core.config import settings
from app.database.repositories.sensor_repository import SensorReadingRepository
from app.schemas.sensor import BatchTriggerResponse
from app.services.batch_scheduler import get_batch_scheduler_state, get_last_batch_skip_message
from app.services.runtime_settings import full_agentic_mode_enabled

router = APIRouter(prefix="/batch", tags=["batch"])


@router.get("/status")
def get_batch_status(
    repository: SensorReadingRepository = Depends(get_sensor_repository),
) -> dict:
    """Return batch scheduler settings and recent command status."""
    unresolved_batch = repository.get_unresolved_batch_command(settings.default_control_strategy)
    recent_or_active_pump = repository.get_recent_or_active_pump_command(
        settings.default_mixing_time_seconds,
    )
    latest_batch = repository.get_latest_batch_analysis(settings.default_control_strategy)
    return {
        **get_batch_scheduler_state(),
        "batch_analysis_enabled": settings.batch_analysis_enabled,
        "full_agentic_mode_enabled": full_agentic_mode_enabled(repository),
        "batch_analysis_interval_seconds": settings.batch_analysis_interval_seconds,
        "batch_analysis_window_readings": settings.batch_analysis_window_readings,
        "batch_pending_command_expiry_seconds": settings.batch_pending_command_expiry_seconds,
        "batch_physical_guard_window_seconds": settings.default_mixing_time_seconds,
        "batch_has_recent_or_active_pump_command": recent_or_active_pump is not None,
        "batch_recent_or_active_pump_command": recent_or_active_pump,
        "batch_has_unresolved_command": unresolved_batch is not None,
        "batch_unresolved_command": unresolved_batch,
        "batch_latest_analysis": latest_batch,
        "openai_api_key_configured": bool(settings.openai_api_key),
        "agentic_ai_use_llm": settings.agentic_ai_use_llm,
        "agentic_ai_model": settings.agentic_ai_model,
        "app_env": settings.app_env,
        "db_schema": settings.db_schema,
    }


@router.post("/trigger", response_model=BatchTriggerResponse)
async def trigger_batch_cycle(
    _: None = Depends(verify_operator_token),
) -> BatchTriggerResponse:
    """Run one agentic batch cycle immediately, requiring X-Operator-Token when OPERATOR_API_TOKEN is set."""
    from app.services.batch_scheduler import run_batch_cycle

    result = await asyncio.to_thread(
        run_batch_cycle,
        force=True,
        trigger_source="manual_batch_trigger",
    )

    if result is None:
        return BatchTriggerResponse(
            status="skipped",
            message=f"Batch cycle skipped: {get_last_batch_skip_message()}",
        )

    return BatchTriggerResponse(
        status="triggered",
        log_id=result["log_id"],
        control_cycle_id=result["control_cycle_id"],
        decision=result["decision"],
        pump_activated=result["pump_activated"],
        duration_ms=result["duration_ms"],
        batch_pending=result["batch_pending"],
        message=(
            f"Batch cycle completed. LLM decision: {result['decision']}. "
            + (
                f"Pump command queued: {result['pump_activated']} for {result['duration_ms']}ms."
                if result["batch_pending"]
                else "No dosing needed."
            )
        ),
    )
