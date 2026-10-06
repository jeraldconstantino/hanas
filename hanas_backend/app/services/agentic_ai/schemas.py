"""Schemas for the HANAS agentic AI graph."""

from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field

from app.schemas.sensor import DosingDecision, ReferenceRange, SensorHistoryEntry, SensorPayload


MIN_AGENTIC_DOSE_FACTOR = 0.4
# The agent may strengthen a calibrated reference dose after reviewing severity
# and unresolved response history. Final milliliters and runtime are still
# bounded by pump-specific safety limits in the deterministic safety gate.
MAX_AGENTIC_DOSE_FACTOR = 2.0
MIN_MIXING_ADJUSTMENT_FACTOR = 0.5
MAX_MIXING_ADJUSTMENT_FACTOR = 2.5


class RecoveryAssessment(BaseModel):
    """Agent-authored evidence checks for a whole-cycle recovery wait."""

    ph_trend: Literal["in_range", "toward_range", "away_from_range", "flat", "uncertain"]
    ec_trend: Literal["in_range", "toward_range", "away_from_range", "flat", "uncertain"]
    history_is_fresh: bool
    sufficient_observations: bool
    executed_dose_in_window: bool


class MonitoringResult(BaseModel):
    """Structured output from the monitoring agent."""

    status: Literal[
        "in_range",
        "deviation",
        "projected_deviation",
        "unstable",
        "sensor_anomaly",
        "mixing",
        "confirming",
    ]
    is_stable: bool
    # Structured output follows schema order: assess evidence before deciding
    # the recovery flag so the flag can agree with the per-metric assessment.
    recovery_assessment: RecoveryAssessment | None = None
    is_recovering: bool = False
    risk_flags: list[str] = Field(default_factory=list)
    summary: str
    projected_decision: Literal["ph_low", "ph_high", "ec_low", "ec_high"] | None = None
    projected_metric: Literal["ph", "ec"] | None = None
    projected_pump_activated: Literal["ph_up", "ph_down", "ec_up", "ec_down"] | None = None
    projected_boundary: float | None = None
    projected_value: float | None = None
    projected_deviation: float | None = Field(default=None, ge=0)


class OrchestratorResult(BaseModel):
    """Structured output from the orchestrator agent."""

    route: Literal["monitoring_agent", "safety_gate"]
    action: Literal["proceed", "wait", "mix_longer", "no_action"]
    confidence: float = Field(..., ge=0, le=1)
    reason: str


class DiagnosticResult(BaseModel):
    """Structured output from the diagnostic reasoning agent."""

    classification: Literal[
        "within_range",
        "ph_low",
        "ph_high",
        "ec_low",
        "ec_high",
        "combined_disturbance",
        "unstable_reading",
        "sensor_anomaly",
    ]
    primary_metric: Literal["ph", "ec", "none"]
    summary: str


class StrategyResult(BaseModel):
    """Structured output from the decision agent."""

    action: Literal["dose", "wait", "mix_longer", "no_action", "fallback_baseline"]
    confidence: float = Field(..., ge=0, le=1)
    reason: str


class DosePlanResult(BaseModel):
    """Structured output from the dose planning agent."""

    pump_activated: Literal["ph_up", "ph_down", "ec_up", "ec_down", "none"]
    mixing_adjustment_factor: float = Field(
        default=1.0,
        ge=MIN_MIXING_ADJUSTMENT_FACTOR,
        le=MAX_MIXING_ADJUSTMENT_FACTOR,
    )
    dose_adjustment_factor: float = Field(
        ...,
        ge=MIN_AGENTIC_DOSE_FACTOR,
        le=MAX_AGENTIC_DOSE_FACTOR,
    )
    reason: str
    # Materialized by the planning stage's calibrated calculation tool.
    candidate_dose_ml: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    candidate_duration_ms: int | None = Field(default=None, ge=0)
    candidate_mixing_time_seconds: int | None = Field(default=None, ge=0)


class LLMCompletionResult(BaseModel):
    """Structured output plus model metadata from an LLM call."""

    output: BaseModel
    model: str

    model_config = {"arbitrary_types_allowed": True}


class AgenticGraphState(TypedDict, total=False):
    """Shared state passed between LangGraph nodes."""

    payload: SensorPayload
    reference_range: ReferenceRange
    history: list[SensorHistoryEntry]
    control_history: list[SensorHistoryEntry]
    baseline: DosingDecision
    orchestration: dict[str, Any]
    monitoring: MonitoringResult
    diagnosis: DiagnosticResult
    strategy: StrategyResult
    dose_plan: DosePlanResult
    consistency_review: dict[str, Any]
    final_decision: DosingDecision
    llm_trace: list[dict[str, Any]]
    crop_lifecycle: dict[str, Any]
