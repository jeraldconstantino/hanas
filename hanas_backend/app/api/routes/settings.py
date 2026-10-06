"""System settings routes for the frontend dashboard."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from app.api.routes.health import deployment_sha
from app.api.dependencies import get_sensor_repository, verify_operator_token
from app.core.config import settings
from app.database.repositories.sensor_repository import SensorReadingRepository
from app.schemas.notification import (
    ExperimentPreflightRequest,
    SystemSettingsSummary,
    SystemSettingsUpdate,
)
from app.services.crop_lifecycle import (
    CROP_HARVEST_END_DAY_KEY,
    CROP_HARVEST_START_DAY_KEY,
    CROP_TRANSPLANT_DATE_KEY,
    CROP_VARIETY_KEY,
    crop_lifecycle_settings,
)
from app.services.runtime_settings import (
    emergency_stop_enabled,
    EXPERIMENT_PREFLIGHT_BLOCKED_RUN_ID_KEY,
    EXPERIMENT_PREFLIGHT_RUN_ID_KEY,
    experiment_preflight_required,
    full_agentic_mode_enabled,
    maintenance_mode_enabled,
    monitoring_mode_enabled,
    runtime_bool_setting,
)

router = APIRouter(prefix="/settings", tags=["settings"])


def _build_settings_summary(repository: SensorReadingRepository) -> SystemSettingsSummary:
    """Merge DB runtime overrides with env-var defaults."""
    crop_settings = crop_lifecycle_settings(repository)
    reference_range = repository.get_active_reference_range(settings.default_control_strategy)
    active_run = (
        repository.get_active_experiment_run_summary(settings.default_control_strategy)
        if hasattr(repository, "get_active_experiment_run_summary")
        else None
    )
    active_command = repository.get_recent_or_active_pump_command(
        settings.default_mixing_time_seconds,
    )
    return SystemSettingsSummary(
        app_version=settings.app_version,
        app_env=settings.app_env,
        db_schema=settings.db_schema,
        build_sha=deployment_sha(),
        build_label=settings.build_label,
        default_reservoir_max_volume_liters=settings.default_reservoir_max_volume_liters,
        minimum_pumpable_reservoir_volume_liters=(
            settings.minimum_pumpable_reservoir_volume_liters
        ),
        fixed_reservoir_volume_liters=settings.fixed_reservoir_volume_liters,
        force_fixed_reservoir_volume=settings.force_fixed_reservoir_volume,
        hitl_enabled=runtime_bool_setting(
            repository,
            "hitl_enabled",
            default=settings.human_in_the_loop_enabled,
        ),
        sms_enabled=runtime_bool_setting(
            repository,
            "sms_enabled",
            default=settings.sms_notifications_enabled,
        ),
        maintenance_mode_enabled=maintenance_mode_enabled(repository),
        monitoring_mode_enabled=monitoring_mode_enabled(repository),
        emergency_stop_enabled=emergency_stop_enabled(repository),
        full_agentic_mode_enabled=full_agentic_mode_enabled(repository),
        openai_api_key_configured=bool(settings.openai_api_key),
        agentic_ai_model=settings.agentic_ai_model,
        sensor_sampling_interval_seconds=settings.default_sampling_interval_seconds,
        batch_analysis_interval_seconds=settings.batch_analysis_interval_seconds,
        sms_cooldown_seconds=settings.sms_alert_cooldown_seconds,
        control_strategy=settings.default_control_strategy,
        ph_target_min=reference_range.ph_target_min,
        ph_target_max=reference_range.ph_target_max,
        ec_target_min=reference_range.ec_target_min,
        ec_target_max=reference_range.ec_target_max,
        crop_type=settings.default_crop_type,
        crop_variety=crop_settings["crop_variety"],
        crop_transplant_date=crop_settings["crop_transplant_date"],
        crop_harvest_start_day=crop_settings["crop_harvest_start_day"],
        crop_harvest_end_day=crop_settings["crop_harvest_end_day"],
        active_experiment_run=active_run,
        experiment_preflight_required=experiment_preflight_required(
            repository,
            settings.default_control_strategy,
        ),
        experiment_reset_blocked=active_command is not None,
        experiment_reset_blocked_reason=(
            "A pump command or protected mixing window is still active."
            if active_command is not None
            else None
        ),
    )


@router.get("", response_model=SystemSettingsSummary)
def get_system_settings(
    repository: SensorReadingRepository = Depends(get_sensor_repository),
) -> SystemSettingsSummary:
    """Return sanitized system configuration for the frontend dashboard."""
    return _build_settings_summary(repository)


@router.post("/experiment-run/preflight", response_model=SystemSettingsSummary)
def confirm_experiment_preflight(
    payload: ExperimentPreflightRequest,
    repository: SensorReadingRepository = Depends(get_sensor_repository),
    _: None = Depends(verify_operator_token),
) -> SystemSettingsSummary:
    """Choose the current run or create a clean run before agentic control proceeds."""
    strategy = settings.default_control_strategy
    if not hasattr(repository, "get_active_experiment_run_summary"):
        raise HTTPException(status_code=501, detail="Experiment lifecycle is unavailable.")

    active_run = repository.get_active_experiment_run_summary(strategy)
    if active_run is None:
        # Creating the active reference range also creates its configured run.
        repository.get_active_reference_range(strategy)
        active_run = repository.get_active_experiment_run_summary(strategy)
    if active_run is None:
        raise HTTPException(status_code=409, detail="No active experiment run is available.")
    if active_run["id"] != payload.active_run_id:
        raise HTTPException(
            status_code=409,
            detail=(
                "The active experiment changed. Review the latest run before "
                "confirming preflight."
            ),
        )

    if payload.action == "start_new":
        if not repository.try_acquire_agentic_cycle_lock():
            raise HTTPException(
                status_code=409,
                detail=(
                    "An AI control cycle is still running. Wait for it to finish "
                    "before starting a new experiment."
                ),
            )
        try:
            active_command = repository.get_recent_or_active_pump_command(
                settings.default_mixing_time_seconds,
            )
            if active_command is not None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Wait for the active pump command and protected mixing window "
                        "to finish before starting a new experiment."
                    ),
                )
            if not hasattr(repository, "start_new_experiment_run"):
                raise HTTPException(
                    status_code=501,
                    detail="Starting a new experiment is unavailable.",
                )
            active_run = repository.start_new_experiment_run(strategy)
        finally:
            repository.release_agentic_cycle_lock()

    repository.set_system_setting(
        EXPERIMENT_PREFLIGHT_RUN_ID_KEY,
        {
            "run_id": active_run["id"],
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    repository.set_system_setting(EXPERIMENT_PREFLIGHT_BLOCKED_RUN_ID_KEY, None)
    return _build_settings_summary(repository)


@router.patch("", response_model=SystemSettingsSummary)
def update_system_settings(
    payload: SystemSettingsUpdate,
    repository: SensorReadingRepository = Depends(get_sensor_repository),
    _: None = Depends(verify_operator_token),
) -> SystemSettingsSummary:
    """Update runtime system settings without redeployment."""
    full_agentic_was_enabled = full_agentic_mode_enabled(repository)
    if payload.full_agentic_mode_enabled:
        if settings.default_control_strategy != "agentic_ai":
            raise HTTPException(
                status_code=409,
                detail="Full Agentic Mode requires default_control_strategy=agentic_ai.",
            )
        if not settings.openai_api_key:
            raise HTTPException(
                status_code=409,
                detail="Full Agentic Mode requires OPENAI_API_KEY to be configured.",
            )

    should_require_new_preflight = (
        payload.monitoring_mode_enabled is True
        or payload.maintenance_mode_enabled is True
        or payload.emergency_stop_enabled is True
        or (
            payload.full_agentic_mode_enabled is True
            and not full_agentic_was_enabled
        )
    )
    if should_require_new_preflight:
        active_run = (
            repository.get_active_experiment_run_summary(settings.default_control_strategy)
            if hasattr(repository, "get_active_experiment_run_summary")
            else None
        )
        if active_run is not None:
            # Persist the fail-closed gate before enabling or resuming any mode
            # that could issue an automatic command.
            repository.set_system_setting(
                EXPERIMENT_PREFLIGHT_BLOCKED_RUN_ID_KEY,
                active_run["id"],
            )

    if payload.hitl_enabled is not None:
        repository.set_system_setting("hitl_enabled", payload.hitl_enabled)
    if payload.sms_enabled is not None:
        repository.set_system_setting("sms_enabled", payload.sms_enabled)
    enabled_safety_mode = next(
        (
            key
            for key, enabled in (
                ("maintenance_mode_enabled", payload.maintenance_mode_enabled),
                ("monitoring_mode_enabled", payload.monitoring_mode_enabled),
                ("emergency_stop_enabled", payload.emergency_stop_enabled),
            )
            if enabled is True
        ),
        None,
    )
    if enabled_safety_mode is not None:
        # One transaction prevents a short all-disabled window while switching
        # directly from one safety lockout to another.
        repository.set_exclusive_safety_mode(enabled_safety_mode)

    if payload.maintenance_mode_enabled is False:
        repository.set_system_setting("maintenance_mode_enabled", payload.maintenance_mode_enabled)
    if payload.monitoring_mode_enabled is not None:
        if not payload.monitoring_mode_enabled:
            repository.cancel_pending_commands(
                settings.default_control_strategy,
                "monitoring_cancelled",
            )
            repository.set_system_setting("monitoring_mode_enabled", False)
        if payload.monitoring_mode_enabled:
            repository.cancel_pending_commands(
                settings.default_control_strategy,
                "monitoring_cancelled",
            )
    if payload.emergency_stop_enabled is not None:
        if not payload.emergency_stop_enabled:
            repository.set_system_setting("emergency_stop_enabled", False)
        if payload.emergency_stop_enabled:
            repository.emergency_stop_active_commands(settings.default_control_strategy)
    if payload.full_agentic_mode_enabled is not None:
        repository.set_system_setting(
            "full_agentic_mode_enabled",
            payload.full_agentic_mode_enabled,
        )
    if payload.crop_variety is not None:
        repository.set_system_setting(CROP_VARIETY_KEY, payload.crop_variety.strip())
    if "crop_transplant_date" in payload.model_fields_set:
        repository.set_system_setting(
            CROP_TRANSPLANT_DATE_KEY,
            payload.crop_transplant_date.isoformat() if payload.crop_transplant_date else None,
        )
    if payload.crop_harvest_start_day is not None:
        repository.set_system_setting(CROP_HARVEST_START_DAY_KEY, payload.crop_harvest_start_day)
    if payload.crop_harvest_end_day is not None:
        repository.set_system_setting(CROP_HARVEST_END_DAY_KEY, payload.crop_harvest_end_day)
    return _build_settings_summary(repository)
