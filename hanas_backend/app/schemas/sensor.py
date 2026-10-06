"""Schemas for HANAS sensor readings."""

from datetime import datetime
from typing import Any
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


SUPPORTED_CONTROL_STRATEGIES = {"baseline", "agentic_ai"}


class SensorPayload(BaseModel):
    """Incoming sensor measurements from the ESP32 device."""

    model_config = ConfigDict(
        allow_inf_nan=False,
        json_schema_extra={
            "examples": [
                {
                    "temperature": 25.0,
                    "ph": 6.2,
                    "ec": 1.8,
                    "reservoir_volume_liters": 70.0,
                    "ph_stable_for_seconds": 30,
                    "ec_stable_for_seconds": 30,
                    "ph_stability_threshold": 0.03,
                    "ec_stability_threshold": 0.03,
                    "control_strategy": "agentic_ai",
                }
            ]
        }
    )

    temperature: float = Field(
        ...,
        gt=-10,
        lt=80,
        description="Water or ambient temperature reading.",
        examples=[25.0],
    )
    ph: float = Field(
        ...,
        ge=0,
        le=14,
        description="Potential hydrogen level of the nutrient solution.",
        examples=[6.2],
    )
    ec: float = Field(
        ...,
        ge=0,
        le=20,
        description="Electrical conductivity of the nutrient solution.",
        examples=[1.8],
    )
    reservoir_volume_liters: float = Field(
        ...,
        ge=0,
        description=(
            "Current liquid volume in the reservoir. A value of 0 is accepted "
            "only when the backend is configured to use a fixed experiment volume."
        ),
        examples=[70.0],
    )
    ph_stable_for_seconds: int | None = Field(
        default=None,
        ge=0,
        description="Seconds the pH reading stayed within the device stability threshold.",
        examples=[30],
    )
    ec_stable_for_seconds: int | None = Field(
        default=None,
        ge=0,
        description="Seconds the EC reading stayed within the device stability threshold.",
        examples=[30],
    )
    ph_stability_threshold: float | None = Field(
        default=None,
        gt=0,
        description="pH stability threshold used by the device.",
        examples=[0.03],
    )
    ec_stability_threshold: float | None = Field(
        default=None,
        gt=0,
        description="EC stability threshold used by the device, in mS/cm.",
        examples=[0.03],
    )
    control_strategy: Literal["baseline", "agentic_ai"] | None = Field(
        default=None,
        description="Requested control strategy.",
        examples=["agentic_ai"],
    )


class ReferenceRange(BaseModel):
    """Configured target range for a crop and growth stage."""

    id: int
    crop_type: str
    growth_stage: str
    hydroponic_system_type: str = "DFT"
    ph_target_min: float
    ph_target_max: float
    ec_target_min: float
    ec_target_max: float
    ph_up_dose_ml_per_liter_per_unit: float = Field(..., gt=0)
    ph_down_dose_ml_per_liter_per_unit: float = Field(..., gt=0)
    ec_up_dose_ml_per_liter_per_unit: float = Field(..., gt=0)
    ec_down_dose_ml_per_liter_per_unit: float = Field(..., gt=0)
    ph_pump_flow_ml_per_min: float = Field(..., gt=0)
    ec_pump_flow_ml_per_min: float = Field(..., gt=0)
    max_dose_ml_per_cycle: float = Field(..., gt=0)
    ph_up_max_dose_ml_per_cycle: float | None = Field(default=None, gt=0)
    ph_down_max_dose_ml_per_cycle: float | None = Field(default=None, gt=0)
    ec_up_max_dose_ml_per_cycle: float | None = Field(default=None, gt=0)
    ec_down_max_dose_ml_per_cycle: float | None = Field(default=None, gt=0)
    ph_up_max_duration_ms: int | None = Field(default=None, gt=0)
    ph_down_max_duration_ms: int | None = Field(default=None, gt=0)
    ec_up_max_duration_ms: int | None = Field(default=None, gt=0)
    ec_down_max_duration_ms: int | None = Field(default=None, gt=0)


class DosingDecision(BaseModel):
    """Rule-based action returned to the ESP32 controller."""

    decision: str
    pump_activated: str
    dose_ml: float
    duration_ms: int
    ph_within_range: bool
    ec_within_range: bool
    ph_deviation: float
    ec_deviation: float
    reason: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class SensorHistoryEntry(BaseModel):
    """Recent persisted reading used by history-aware control strategies."""

    control_cycle_id: int | None = None
    timestamp: datetime
    action_started_at: datetime | None = None
    action_completed_at: datetime | None = None
    control_strategy: Literal["baseline", "agentic_ai"] | None = None
    ph: float
    ec: float
    temperature: float | None = None
    reservoir_volume_liters: float | None = None
    ph_stable_for_seconds: int | None = None
    ec_stable_for_seconds: int | None = None
    decision: str | None = None
    pump_activated: str | None = None
    dose_ml: float | None = None
    duration_ms: int | None = None
    ph_deviation: float | None = None
    ec_deviation: float | None = None
    status: str | None = None
    decision_metadata: dict[str, Any] = Field(default_factory=dict)


class SensorResponse(BaseModel):
    """Response returned after a sensor payload is accepted."""

    status: str
    received: SensorPayload
    decision: DosingDecision
    pump_activated: str
    duration_ms: int
    mixing_time_ms: int
    log_id: int
    control_cycle_id: int


class BatchTriggerResponse(BaseModel):
    """Response returned after a manual batch cycle trigger."""

    status: Literal["triggered", "skipped"]
    log_id: int | None = None
    control_cycle_id: int | None = None
    decision: str | None = None
    pump_activated: str | None = None
    duration_ms: int | None = None
    batch_pending: bool = False
    message: str


class OverviewSummary(BaseModel):
    """Operator-facing summary shown on the Overview dashboard."""

    data_policy_version: int = 1
    generated_at: datetime
    period_start: datetime
    period_end: datetime
    title: str
    summary: str
    highlights: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    trend_notes: list[str] = Field(default_factory=list)
    anomaly_events: list[str] = Field(default_factory=list)
    dosing_events: list[str] = Field(default_factory=list)
    reading_count: int = 0
    dosing_event_count: int = 0
    total_dose_ml: float = 0.0
    ph_min: float | None = None
    ph_max: float | None = None
    ec_min: float | None = None
    ec_max: float | None = None
    reservoir_min_liters: float | None = None
    reservoir_max_liters: float | None = None
    latest_reservoir_liters: float | None = None
    latest_reservoir_percent: int | None = None
    llm_used: bool = False
    model: str | None = None


class OverviewSummaryStatus(BaseModel):
    """Summary scheduler state and latest generated summary."""

    overview_summary_enabled: bool
    overview_summary_interval_seconds: int
    overview_summary_scheduler_running: bool
    overview_summary_scheduler_next_run_at: datetime | None = None
    overview_summary_scheduler_next_run_seconds: int | None = None
    overview_summary_scheduler_last_run_started_at: datetime | None = None
    overview_summary_scheduler_last_run_finished_at: datetime | None = None
    overview_summary_scheduler_last_status: str
    overview_summary_scheduler_last_message: str
    latest_summary: OverviewSummary | None = None


class LatestSystemLog(BaseModel):
    """Latest system log shape consumed by the frontend dashboard."""

    log_id: int
    control_cycle_id: int
    timestamp: datetime
    ph: float
    ec: float
    temperature: float
    reservoir_volume_liters: float
    ph_stable_for_seconds: int | None = None
    ec_stable_for_seconds: int | None = None
    control_strategy: Literal["baseline", "agentic_ai"]
    decision: str
    pump_activated: str
    dose_ml: float
    duration_ms: int
    mixing_time_ms: int
    ph_deviation: float
    ec_deviation: float
    status: str
    decision_metadata: dict[str, Any] = Field(default_factory=dict)
