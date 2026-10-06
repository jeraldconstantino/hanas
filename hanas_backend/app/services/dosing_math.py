"""Shared dosing math helpers."""

from app.core.config import settings


def calculate_pump_duration_ms(
    dose_ml: float,
    flow_ml_per_min: float,
    max_duration_ms: int | None = None,
    minimum_duration_ms: int | None = None,
) -> int:
    """Calculate a bounded pump duration from dose volume and pump flow rate."""
    if dose_ml <= 0:
        return 0

    max_duration_ms = max_duration_ms or settings.maximum_pump_duration_ms
    flow_ml_per_second = flow_ml_per_min / 60
    duration_ms = round((dose_ml / flow_ml_per_second) * 1000)
    minimum_duration_ms = max(
        settings.minimum_pump_duration_ms,
        minimum_duration_ms or settings.minimum_pump_duration_ms,
    )
    duration_ms = max(minimum_duration_ms, duration_ms)
    return min(max_duration_ms, duration_ms)


def estimate_dose_ml_for_duration(duration_ms: int, flow_ml_per_min: float) -> float:
    """Estimate the dispensed dose for a bounded pump runtime."""
    if duration_ms <= 0 or flow_ml_per_min <= 0:
        return 0

    return round(flow_ml_per_min * duration_ms / 60_000, 2)


def calculate_bounded_dose_and_duration_ms(
    dose_ml: float,
    flow_ml_per_min: float,
    max_duration_ms: int | None = None,
    minimum_duration_ms: int | None = None,
) -> tuple[float, int]:
    """Return a dose/runtime pair that describes the same pump actuation."""
    duration_ms = calculate_pump_duration_ms(
        dose_ml,
        flow_ml_per_min,
        max_duration_ms,
        minimum_duration_ms,
    )
    return estimate_dose_ml_for_duration(duration_ms, flow_ml_per_min), duration_ms


def estimated_duration_ms_for_dose(dose_ml: float, flow_ml_per_min: float) -> int:
    """Return the raw runtime estimate before minimum/maximum duration bounds."""
    if dose_ml <= 0 or flow_ml_per_min <= 0:
        return 0

    flow_ml_per_second = flow_ml_per_min / 60
    return round((dose_ml / flow_ml_per_second) * 1000)


def minimum_reliable_dose_ml(flow_ml_per_min: float) -> float:
    """Return the dose volume represented by the configured reliable runtime."""
    return estimate_dose_ml_for_duration(settings.minimum_reliable_pump_duration_ms, flow_ml_per_min)


def max_deliverable_dose_ml(flow_ml_per_min: float, max_duration_ms: int | None = None) -> float:
    """Return the largest dose the pump can deliver within the max runtime."""
    if flow_ml_per_min <= 0:
        return 0

    max_duration_ms = max_duration_ms or settings.maximum_pump_duration_ms
    return round(flow_ml_per_min * max_duration_ms / 60_000, 2)
