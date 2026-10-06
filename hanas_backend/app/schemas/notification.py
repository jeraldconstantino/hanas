"""Schemas for notification logs and system settings summary."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.config import settings


class NotificationLogEntry(BaseModel):
    """One SMS notification attempt persisted for the dashboard audit trail."""

    id: int
    timestamp: datetime
    provider: str
    recipient_number: str
    sender_id: str | None
    alert_type: str
    message_body: str
    status: str
    provider_response: str | None
    error_message: str | None
    system_log_id: int | None
    control_cycle_id: int | None
    created_at: datetime
    provider_message_id: int | None = None
    latest_provider_response: str | None = None
    provider_status_updated_at: datetime | None = None
    status_checked_at: datetime | None = None
    status_check_count: int = 0


class ExperimentRunSummary(BaseModel):
    """Operator-facing identity and activity for the active experiment run."""

    id: int
    run_name: str
    control_strategy: str
    start_time: datetime
    last_activity_at: datetime | None = None
    cycle_count: int = 0


class SystemSettingsSummary(BaseModel):
    """Sanitized system configuration exposed to the frontend dashboard."""

    app_version: str
    app_env: str
    db_schema: str
    build_sha: str
    build_label: str
    default_reservoir_max_volume_liters: float
    minimum_pumpable_reservoir_volume_liters: float
    fixed_reservoir_volume_liters: float
    force_fixed_reservoir_volume: bool
    hitl_enabled: bool
    sms_enabled: bool
    maintenance_mode_enabled: bool
    monitoring_mode_enabled: bool
    emergency_stop_enabled: bool
    full_agentic_mode_enabled: bool
    openai_api_key_configured: bool
    agentic_ai_model: str
    sensor_sampling_interval_seconds: int
    batch_analysis_interval_seconds: int
    sms_cooldown_seconds: int
    control_strategy: str
    ph_target_min: float
    ph_target_max: float
    ec_target_min: float
    ec_target_max: float
    crop_type: str
    crop_variety: str
    crop_transplant_date: date | None = None
    crop_harvest_start_day: int
    crop_harvest_end_day: int
    active_experiment_run: ExperimentRunSummary | None = None
    experiment_preflight_required: bool = False
    experiment_reset_blocked: bool = False
    experiment_reset_blocked_reason: str | None = None


class ExperimentPreflightRequest(BaseModel):
    """Explicit operator choice for the experiment boundary safety gate."""

    action: str
    active_run_id: int = Field(gt=0)

    @field_validator("action")
    @classmethod
    def validate_action(cls, value: str) -> str:
        if value not in {"continue", "start_new"}:
            raise ValueError("action must be 'continue' or 'start_new'")
        return value


class SystemSettingsUpdate(BaseModel):
    """Partial update payload for runtime system settings."""

    hitl_enabled: bool | None = None
    sms_enabled: bool | None = None
    maintenance_mode_enabled: bool | None = None
    monitoring_mode_enabled: bool | None = None
    emergency_stop_enabled: bool | None = None
    full_agentic_mode_enabled: bool | None = None
    crop_variety: str | None = Field(default=None, max_length=80)
    crop_transplant_date: date | None = None
    crop_harvest_start_day: int | None = Field(default=None, ge=1, le=120)
    crop_harvest_end_day: int | None = Field(default=None, ge=1, le=160)

    @field_validator("crop_transplant_date")
    @classmethod
    def validate_transplant_date_not_future(cls, value: date | None) -> date | None:
        """Crop age is operational context, so future dates are not valid."""
        today = datetime.now(ZoneInfo(settings.app_timezone)).date()
        if value is not None and value > today:
            raise ValueError("crop_transplant_date cannot be in the future")
        return value

    @model_validator(mode="after")
    def validate_harvest_window(self) -> "SystemSettingsUpdate":
        """Reject an inverted harvest window when both sides are supplied."""
        if (
            self.crop_harvest_start_day is not None
            and self.crop_harvest_end_day is not None
            and self.crop_harvest_end_day < self.crop_harvest_start_day
        ):
            raise ValueError("crop_harvest_end_day must be greater than or equal to crop_harvest_start_day")
        return self

    @model_validator(mode="after")
    def validate_exclusive_safety_modes(self) -> "SystemSettingsUpdate":
        """Reject a contradictory request that enables multiple safety modes at once."""
        enabled_modes = sum(
            value is True
            for value in (
                self.emergency_stop_enabled,
                self.maintenance_mode_enabled,
                self.monitoring_mode_enabled,
            )
        )
        if enabled_modes > 1:
            raise ValueError(
                "emergency stop, maintenance mode, and monitoring only are mutually exclusive"
            )
        return self
