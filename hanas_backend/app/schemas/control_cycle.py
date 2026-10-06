"""Schemas for control-cycle lifecycle updates."""

from typing import Literal

from pydantic import BaseModel, Field


class ControlCycleCompletionPayload(BaseModel):
    """Payload sent when the ESP32 finishes a control cycle."""

    status: Literal["completed", "error", "emergency_stopped"] = Field(default="completed", description="Final control-cycle status.")


class ControlCycleActionCompletedPayload(BaseModel):
    """Pump shutdown callback; final completion has a separate contract."""

    status: Literal["mixing"] = "mixing"


class ControlCycleActionStartedPayload(BaseModel):
    """Payload sent when the ESP32 begins the dosing action."""

    status: Literal["dosing", "inter_dose_mixing", "ec_up_b_dosing"] = Field(default="dosing", description="Control-cycle status while actuation is running.")


class ControlCycleCompletionResponse(BaseModel):
    """Response returned after a control cycle is marked complete."""

    status: str
    control_cycle_id: int


class HumanReviewPayload(BaseModel):
    """Operator review payload for a pending agentic AI decision."""

    action: Literal["approve", "reject", "override"]
    reviewer: str | None = Field(default=None, max_length=120)
    reason: str = Field(default="", max_length=500)
    decision: str | None = Field(default=None, max_length=80)
    pump_activated: Literal["ph_up", "ph_down", "ec_up", "ec_down", "none"] | None = None
    dose_ml: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    duration_ms: int | None = Field(default=None, ge=0)
    mixing_time_ms: int | None = Field(default=None, ge=0)


class HumanReviewResponse(BaseModel):
    """Decision command returned after human review."""

    status: str
    control_cycle_id: int
    decision: str
    pump_activated: str
    dose_ml: float
    duration_ms: int
    mixing_time_ms: int
    message: str


class PendingCommandResponse(BaseModel):
    """Queued ESP32 pump command from HITL approval or batch scheduling."""

    status: str
    has_command: bool
    control_cycle_id: int | None = None
    decision: str = "none"
    pump_activated: str = "none"
    dose_ml: float = 0
    duration_ms: int = 0
    mixing_time_ms: int = 0
    agentic_mode: str = "none"
    actuation_source: str = "none"
    message: str = "No pending command."


class LatestControlCycle(BaseModel):
    """Latest control-cycle shape consumed by the frontend dashboard."""

    id: int
    status: str
    pump_activated: str
    dose_ml: float
    duration_ms: int
    mixing_duration_seconds: int
    mixing_elapsed_seconds: int
    action_started_at: str
    action_completed_at: str | None = None
