"""Deterministic context tools for HANAS agentic AI prompts."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from typing import Any, Sequence

from app.schemas.sensor import DosingDecision, ReferenceRange, SensorHistoryEntry
from app.services.baseline.dosing_rules import effective_max_dose_for_pump, max_duration_for_pump
from app.services.dosing_math import (
    calculate_bounded_dose_and_duration_ms,
    estimated_duration_ms_for_dose,
)


def get_extended_sensor_history(
    history: Sequence[SensorHistoryEntry],
) -> dict[str, Any]:
    """Return compact extended history context for LLM reasoning."""
    ordered = sorted(history, key=lambda entry: _as_utc(entry.timestamp))
    if not ordered:
        return {
            "tool": "get_extended_sensor_history",
            "available": False,
            "reason": "No persisted history was supplied for this control cycle.",
            "sample_count": 0,
            "dosing_event_count": 0,
            "latest": None,
            "oldest_timestamp": None,
            "newest_timestamp": None,
            "metrics": {},
            "recent_dosing_events": [],
        }

    ph_values = [entry.ph for entry in ordered]
    ec_values = [entry.ec for entry in ordered]
    temp_values = [entry.temperature for entry in ordered if entry.temperature is not None]
    reservoir_values = [
        entry.reservoir_volume_liters
        for entry in ordered
        if entry.reservoir_volume_liters is not None
    ]
    dosing_events = [
        entry
        for entry in ordered
        if entry.pump_activated not in {None, "none"} and (entry.dose_ml or 0) > 0
    ]

    return {
        "tool": "get_extended_sensor_history",
        "available": True,
        "sample_count": len(ordered),
        "dosing_event_count": len(dosing_events),
        "oldest_timestamp": _iso(ordered[0].timestamp),
        "newest_timestamp": _iso(ordered[-1].timestamp),
        "latest": _history_snapshot(ordered[-1]),
        "metrics": {
            "ph": _series_stats(ph_values),
            "ec": _series_stats(ec_values),
            "water_temperature": _series_stats(temp_values) if temp_values else None,
            "reservoir_volume_liters": (
                _series_stats(reservoir_values) if reservoir_values else None
            ),
        },
        "recent_dosing_events": [
            _dose_event_snapshot(entry) for entry in dosing_events[-5:]
        ],
    }


def calculate_bounded_dose(
    control_decision: DosingDecision,
    reference_range: ReferenceRange,
    *,
    dose_adjustment_factor: float = 1.0,
) -> dict[str, Any]:
    """Return deterministic bounded dose context for a candidate control decision."""
    pump = control_decision.pump_activated
    calibrated_dose_ml = control_decision.metadata.get("requested_actuation_dose_ml")
    if not isinstance(calibrated_dose_ml, int | float):
        calibrated_dose_ml = control_decision.dose_ml
    requested_dose_ml = round(max(float(calibrated_dose_ml) * dose_adjustment_factor, 0), 4)
    if pump == "none" or requested_dose_ml <= 0:
        return {
            "tool": "calculate_bounded_dose",
            "available": True,
            "pump_activated": pump,
            "input_dose_ml": control_decision.dose_ml,
            "dose_adjustment_factor": dose_adjustment_factor,
            "requested_dose_ml": requested_dose_ml,
            "bounded_dose_ml": 0,
            "duration_ms": 0,
            "max_dose_ml_for_pump": 0,
            "max_duration_ms_for_pump": None,
            "reason": "No pump command is currently selected.",
        }

    max_dose_ml = effective_max_dose_for_pump(reference_range, pump)
    capped_dose_ml = round(min(requested_dose_ml, max_dose_ml), 2)
    flow_ml_per_min = (
        reference_range.ph_pump_flow_ml_per_min
        if pump in {"ph_up", "ph_down"}
        else reference_range.ec_pump_flow_ml_per_min
    )
    max_duration_ms = max_duration_for_pump(reference_range, pump)
    reliable_floor_safe = control_decision.metadata.get("reliable_pulse_floor_safe") is True
    configured_reliable_duration = control_decision.metadata.get(
        "minimum_reliable_pump_duration_ms"
    )
    minimum_duration_ms = (
        int(configured_reliable_duration)
        if reliable_floor_safe and isinstance(configured_reliable_duration, int | float)
        else None
    )
    bounded_dose_ml, duration_ms = calculate_bounded_dose_and_duration_ms(
        capped_dose_ml,
        flow_ml_per_min,
        max_duration_ms,
        minimum_duration_ms,
    )
    uncapped_duration_ms = estimated_duration_ms_for_dose(capped_dose_ml, flow_ml_per_min)

    return {
        "tool": "calculate_bounded_dose",
        "available": True,
        "pump_activated": pump,
        "input_dose_ml": control_decision.dose_ml,
        "dose_adjustment_factor": dose_adjustment_factor,
        "requested_dose_ml": requested_dose_ml,
        "bounded_dose_ml": bounded_dose_ml,
        "duration_ms": duration_ms,
        "max_dose_ml_for_pump": max_dose_ml,
        "max_duration_ms_for_pump": max_duration_ms,
        "flow_ml_per_min": flow_ml_per_min,
        "reliable_pulse_floor_safe": reliable_floor_safe,
        "reliable_pulse_floor_applied": (
            minimum_duration_ms is not None
            and uncapped_duration_ms < minimum_duration_ms
            and duration_ms >= minimum_duration_ms
        ),
        "was_capped_by_dose_limit": requested_dose_ml > max_dose_ml,
        "reason": (
            "Candidate dose was bounded by deterministic pump flow, dose cap, "
            "and duration cap."
        ),
    }


def agentic_tool_results(
    history: Sequence[SensorHistoryEntry],
    control_decision: DosingDecision,
    reference_range: ReferenceRange,
    *,
    dose_adjustment_factor: float = 1.0,
) -> dict[str, Any]:
    """Return all deterministic tool outputs made available to LLM agents."""
    extended_history = get_extended_sensor_history(history)
    bounded_dose = calculate_bounded_dose(
        control_decision,
        reference_range,
        dose_adjustment_factor=dose_adjustment_factor,
    )
    return {
        "mode": "backend_supplied_deterministic_tool_results",
        "get_extended_sensor_history": extended_history,
        "calculate_bounded_dose": bounded_dose,
    }


def _series_stats(values: Sequence[float]) -> dict[str, float]:
    first = values[0]
    latest = values[-1]
    return {
        "first": round(first, 4),
        "latest": round(latest, 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
        "delta": round(latest - first, 4),
    }


def _history_snapshot(entry: SensorHistoryEntry) -> dict[str, Any]:
    return {
        "timestamp": _iso(entry.timestamp),
        "ph": entry.ph,
        "ec": entry.ec,
        "water_temperature": entry.temperature,
        "reservoir_volume_liters": entry.reservoir_volume_liters,
        "decision": entry.decision,
        "pump_activated": entry.pump_activated,
        "dose_ml": entry.dose_ml,
        "duration_ms": entry.duration_ms,
        "status": entry.status,
    }


def _dose_event_snapshot(entry: SensorHistoryEntry) -> dict[str, Any]:
    return {
        "timestamp": _iso(entry.timestamp),
        "pump_activated": entry.pump_activated,
        "dose_ml": entry.dose_ml,
        "duration_ms": entry.duration_ms,
        "status": entry.status,
        "ph": entry.ph,
        "ec": entry.ec,
    }


def _iso(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
