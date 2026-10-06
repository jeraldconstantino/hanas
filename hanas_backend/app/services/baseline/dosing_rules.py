"""Rule-based nutrient dosing decisions."""

from datetime import datetime, timezone
from typing import Sequence

from app.core.config import settings
from app.schemas.sensor import DosingDecision, ReferenceRange, SensorHistoryEntry, SensorPayload
from app.services.dosing_math import (
    calculate_bounded_dose_and_duration_ms,
    estimated_duration_ms_for_dose,
    max_deliverable_dose_ml,
    minimum_reliable_dose_ml,
)

PH_DOSING_DEADBAND = 0.03
EC_DOSING_DEADBAND = 0.03
PH_REENTRY_MARGIN = 0.05
EC_REENTRY_MARGIN = 0.05


def calculate_safe_corrective_actuation(
    dose_ml: float,
    payload: SensorPayload,
    reference_range: ReferenceRange,
    decision: str,
    pump_activated: str,
) -> tuple[float, int, dict[str, float | int | bool]]:
    """Apply the reliable pulse floor only when it stays inside the target range."""
    if pump_activated in {"ph_up", "ph_down"}:
        flow_ml_per_min = reference_range.ph_pump_flow_ml_per_min
        coefficient = (
            reference_range.ph_up_dose_ml_per_liter_per_unit
            if pump_activated == "ph_up"
            else reference_range.ph_down_dose_ml_per_liter_per_unit
        )
    else:
        flow_ml_per_min = reference_range.ec_pump_flow_ml_per_min
        coefficient = (
            reference_range.ec_up_dose_ml_per_liter_per_unit
            if pump_activated == "ec_up"
            else reference_range.ec_down_dose_ml_per_liter_per_unit
        )

    safe_deviation = _safe_directional_headroom(payload, reference_range, decision)
    safe_max_dose_ml = round(
        max(safe_deviation, 0) * payload.reservoir_volume_liters * coefficient,
        2,
    )
    pump_max_dose_ml = effective_max_dose_for_pump(reference_range, pump_activated)
    reliable_dose_ml = minimum_reliable_dose_ml(flow_ml_per_min)
    pump_max_duration_ms = max_duration_for_pump(reference_range, pump_activated)
    reliable_floor_safe = (
        pump_activated in {"ph_up", "ph_down"}
        and reliable_dose_ml <= safe_max_dose_ml
        and reliable_dose_ml <= pump_max_dose_ml
        and settings.minimum_reliable_pump_duration_ms <= pump_max_duration_ms
    )
    requested_duration_ms = estimated_duration_ms_for_dose(dose_ml, flow_ml_per_min)
    minimum_duration_ms = (
        settings.minimum_reliable_pump_duration_ms if reliable_floor_safe else None
    )
    bounded_dose_ml, duration_ms = calculate_bounded_dose_and_duration_ms(
        dose_ml,
        flow_ml_per_min,
        pump_max_duration_ms,
        minimum_duration_ms,
    )
    return bounded_dose_ml, duration_ms, {
        "requested_actuation_dose_ml": round(dose_ml, 2),
        "requested_actuation_duration_ms": requested_duration_ms,
        "minimum_reliable_dose_ml": reliable_dose_ml,
        "minimum_reliable_pump_duration_ms": settings.minimum_reliable_pump_duration_ms,
        "safe_max_dose_ml_for_reliable_floor": min(safe_max_dose_ml, pump_max_dose_ml),
        "reliable_pulse_floor_safe": reliable_floor_safe,
        "reliable_pulse_floor_applied": (
            reliable_floor_safe
            and requested_duration_ms < settings.minimum_reliable_pump_duration_ms
            and duration_ms >= settings.minimum_reliable_pump_duration_ms
        ),
    }


def _safe_directional_headroom(
    payload: SensorPayload,
    reference_range: ReferenceRange,
    decision: str,
) -> float:
    """Return correction headroom while retaining an inward opposite-bound margin."""
    if decision == "ph_low":
        return reference_range.ph_target_max - PH_REENTRY_MARGIN - payload.ph
    if decision == "ph_high":
        return payload.ph - (reference_range.ph_target_min + PH_REENTRY_MARGIN)
    if decision == "ec_low":
        return reference_range.ec_target_max - EC_REENTRY_MARGIN - payload.ec
    if decision == "ec_high":
        return payload.ec - (reference_range.ec_target_min + EC_REENTRY_MARGIN)
    return 0


def evaluate_dosing_decision(
    payload: SensorPayload,
    reference_range: ReferenceRange,
    *,
    dose_inside_deadband: bool = False,
) -> DosingDecision:
    """Choose the pump action needed for one sensor reading."""
    ph_deviation = _range_deviation(
        payload.ph,
        reference_range.ph_target_min,
        reference_range.ph_target_max,
    )
    ec_deviation = _range_deviation(
        payload.ec,
        reference_range.ec_target_min,
        reference_range.ec_target_max,
    )
    ph_within_range = ph_deviation == 0
    ec_within_range = ec_deviation == 0

    if (
        not dose_inside_deadband
        and 0 < ph_deviation <= PH_DOSING_DEADBAND
        and ec_deviation <= EC_DOSING_DEADBAND
    ):
        return _deadband_wait_decision(
            payload,
            reference_range,
            ph_deviation=ph_deviation,
            ec_deviation=ec_deviation,
            metric="pH",
            deadband=PH_DOSING_DEADBAND,
        )

    if (
        not dose_inside_deadband
        and 0 < ec_deviation <= EC_DOSING_DEADBAND
        and ph_deviation <= PH_DOSING_DEADBAND
    ):
        return _deadband_wait_decision(
            payload,
            reference_range,
            ph_deviation=ph_deviation,
            ec_deviation=ec_deviation,
            metric="EC",
            deadband=EC_DOSING_DEADBAND,
        )

    if (
        payload.ph < reference_range.ph_target_min
        and (dose_inside_deadband or ph_deviation > PH_DOSING_DEADBAND)
    ):
        target_ph = reentry_target_value(reference_range, "ph_low")
        dose_deviation = round(target_ph - payload.ph, 4)
        dose_plan = _calculate_dose_plan(
            dose_deviation,
            payload.reservoir_volume_liters,
            reference_range.ph_up_dose_ml_per_liter_per_unit,
            effective_max_dose_for_pump(reference_range, "ph_up"),
        )
        dose_ml = dose_plan["dose_ml"]
        dose_ml, duration_ms, actuation_metadata = calculate_safe_corrective_actuation(
            dose_ml,
            payload,
            reference_range,
            "ph_low",
            "ph_up",
        )
        return DosingDecision(
            decision="ph_low",
            pump_activated="ph_up",
            dose_ml=dose_ml,
            duration_ms=duration_ms,
            ph_within_range=ph_within_range,
            ec_within_range=ec_within_range,
            ph_deviation=ph_deviation,
            ec_deviation=ec_deviation,
            reason="pH is below the configured target range.",
            metadata={
                "strategy": "baseline",
                "rule": "ph_below_minimum",
                "measured_ph": payload.ph,
                "target_ph_min": reference_range.ph_target_min,
                "target_ph_midpoint": target_ph,
                "deviation": ph_deviation,
                "dose_deviation": dose_deviation,
                "target_policy": "midpoint_single_correction_capped",
                "reservoir_volume_liters": payload.reservoir_volume_liters,
                **actuation_metadata,
                **dose_plan,
            },
        )

    if (
        payload.ph > reference_range.ph_target_max
        and (dose_inside_deadband or ph_deviation > PH_DOSING_DEADBAND)
    ):
        target_ph = reentry_target_value(reference_range, "ph_high")
        dose_deviation = round(payload.ph - target_ph, 4)
        dose_plan = _calculate_dose_plan(
            dose_deviation,
            payload.reservoir_volume_liters,
            reference_range.ph_down_dose_ml_per_liter_per_unit,
            effective_max_dose_for_pump(reference_range, "ph_down"),
        )
        dose_ml = dose_plan["dose_ml"]
        dose_ml, duration_ms, actuation_metadata = calculate_safe_corrective_actuation(
            dose_ml,
            payload,
            reference_range,
            "ph_high",
            "ph_down",
        )
        return DosingDecision(
            decision="ph_high",
            pump_activated="ph_down",
            dose_ml=dose_ml,
            duration_ms=duration_ms,
            ph_within_range=ph_within_range,
            ec_within_range=ec_within_range,
            ph_deviation=ph_deviation,
            ec_deviation=ec_deviation,
            reason="pH is above the configured target range.",
            metadata={
                "strategy": "baseline",
                "rule": "ph_above_maximum",
                "measured_ph": payload.ph,
                "target_ph_max": reference_range.ph_target_max,
                "target_ph_midpoint": target_ph,
                "deviation": ph_deviation,
                "dose_deviation": dose_deviation,
                "target_policy": "midpoint_single_correction_capped",
                "reservoir_volume_liters": payload.reservoir_volume_liters,
                **actuation_metadata,
                **dose_plan,
            },
        )

    if (
        payload.ec < reference_range.ec_target_min
        and (dose_inside_deadband or ec_deviation > EC_DOSING_DEADBAND)
    ):
        target_ec = reentry_target_value(reference_range, "ec_low")
        dose_deviation = round(target_ec - payload.ec, 4)
        dose_plan = _calculate_dose_plan(
            dose_deviation,
            payload.reservoir_volume_liters,
            reference_range.ec_up_dose_ml_per_liter_per_unit,
            effective_max_dose_for_pump(reference_range, "ec_up"),
        )
        dose_ml = dose_plan["dose_ml"]
        dose_ml, duration_ms, actuation_metadata = calculate_safe_corrective_actuation(
            dose_ml,
            payload,
            reference_range,
            "ec_low",
            "ec_up",
        )
        return DosingDecision(
            decision="ec_low",
            pump_activated="ec_up",
            dose_ml=dose_ml,
            duration_ms=duration_ms,
            ph_within_range=ph_within_range,
            ec_within_range=ec_within_range,
            ph_deviation=ph_deviation,
            ec_deviation=ec_deviation,
            reason="EC is below the configured target range.",
            metadata={
                "strategy": "baseline",
                "rule": "ec_below_minimum",
                "measured_ec": payload.ec,
                "target_ec_min": reference_range.ec_target_min,
                "deviation": ec_deviation,
                "target_ec_midpoint": target_ec,
                "dose_deviation": dose_deviation,
                "target_policy": "midpoint_single_correction_capped",
                "reservoir_volume_liters": payload.reservoir_volume_liters,
                "nutrient_components": ["ec_up_a", "ec_up_b"],
                "dose_ml_per_component": dose_ml,
                "total_ec_up_dose_ml": round(dose_ml * 2, 2),
                "controller_sequence": "ec_up_a_then_ec_up_b",
                **actuation_metadata,
                **dose_plan,
            },
        )

    if (
        payload.ec > reference_range.ec_target_max
        and (dose_inside_deadband or ec_deviation > EC_DOSING_DEADBAND)
    ):
        target_ec = reentry_target_value(reference_range, "ec_high")
        dose_deviation = round(payload.ec - target_ec, 4)
        dose_plan = _calculate_dose_plan(
            dose_deviation,
            payload.reservoir_volume_liters,
            reference_range.ec_down_dose_ml_per_liter_per_unit,
            effective_max_dose_for_pump(reference_range, "ec_down"),
        )
        dose_ml = dose_plan["dose_ml"]
        dose_ml, duration_ms, actuation_metadata = calculate_safe_corrective_actuation(
            dose_ml,
            payload,
            reference_range,
            "ec_high",
            "ec_down",
        )
        return DosingDecision(
            decision="ec_high",
            pump_activated="ec_down",
            dose_ml=dose_ml,
            duration_ms=duration_ms,
            ph_within_range=ph_within_range,
            ec_within_range=ec_within_range,
            ph_deviation=ph_deviation,
            ec_deviation=ec_deviation,
            reason="EC is above the configured target range.",
            metadata={
                "strategy": "baseline",
                "rule": "ec_above_maximum",
                "measured_ec": payload.ec,
                "target_ec_max": reference_range.ec_target_max,
                "deviation": ec_deviation,
                "target_ec_midpoint": target_ec,
                "dose_deviation": dose_deviation,
                "target_policy": "midpoint_single_correction_capped",
                "reservoir_volume_liters": payload.reservoir_volume_liters,
                **actuation_metadata,
                **dose_plan,
            },
        )

    return DosingDecision(
        decision="within_range",
        pump_activated="none",
        dose_ml=0,
        duration_ms=0,
        ph_within_range=ph_within_range,
        ec_within_range=ec_within_range,
        ph_deviation=ph_deviation,
        ec_deviation=ec_deviation,
        reason="pH and EC are within the configured target ranges.",
        metadata={
            "strategy": "baseline",
            "rule": "within_range",
            "measured_ph": payload.ph,
            "measured_ec": payload.ec,
            "reservoir_volume_liters": payload.reservoir_volume_liters,
        },
    )


def evaluate_persistent_dosing_decision(
    payload: SensorPayload,
    reference_range: ReferenceRange,
    recent_logs: Sequence[SensorHistoryEntry] | None = None,
) -> DosingDecision:
    """Dose a tiny excursion only after a fresh, matching confirmation reading."""
    initial = evaluate_dosing_decision(payload, reference_range)
    if initial.decision != "wait_near_boundary":
        return initial

    confirmation = _near_boundary_confirmation(
        payload,
        reference_range,
        list(recent_logs or []),
    )
    if confirmation is None:
        return initial

    confirmed = evaluate_dosing_decision(
        payload,
        reference_range,
        dose_inside_deadband=True,
    )
    return confirmed.model_copy(
        update={
            "reason": (
                f"{confirmed.reason} The small boundary excursion persisted across "
                "a stable confirmation reading, so a bounded midpoint-directed dose was created."
            ),
            "metadata": {
                **confirmed.metadata,
                "near_boundary_confirmation": confirmation,
                "deadband_policy": "dose_after_stable_confirmation",
            },
        }
    )


def _near_boundary_confirmation(
    payload: SensorPayload,
    reference_range: ReferenceRange,
    recent_logs: Sequence[SensorHistoryEntry],
) -> dict[str, float | int | str] | None:
    """Return confirmation evidence for the current deadband-side excursion."""
    required_stability = settings.default_stability_required_seconds
    metric, side = _out_of_range_side(payload, reference_range)
    if metric is None or side is None:
        return None

    stable_for_seconds = (
        payload.ph_stable_for_seconds if metric == "ph" else payload.ec_stable_for_seconds
    )
    if stable_for_seconds is None or stable_for_seconds < required_stability:
        return None

    now = datetime.now(timezone.utc)
    minimum_gap = settings.default_initial_confirmation_gap_seconds
    maximum_age = settings.agentic_history_freshness_gap_seconds
    for entry in sorted(recent_logs, key=lambda item: item.timestamp, reverse=True):
        timestamp = _as_utc(entry.timestamp)
        age_seconds = (now - timestamp).total_seconds()
        if age_seconds < minimum_gap:
            continue
        if age_seconds > maximum_age:
            break
        if entry.decision == "within_range":
            return None
        entry_metric, entry_side = _history_out_of_range_side(entry, reference_range)
        if (entry_metric, entry_side) != (metric, side):
            continue
        if entry.pump_activated not in {None, "none"}:
            return None
        if entry.decision not in {"wait_near_boundary", "wait_initial_confirmation"}:
            continue
        return {
            "metric": metric,
            "side": side,
            "confirmation_gap_seconds": round(age_seconds),
            "required_gap_seconds": minimum_gap,
            "required_stability_seconds": required_stability,
            "prior_decision": entry.decision or "unknown",
        }
    return None


def _out_of_range_side(
    payload: SensorPayload,
    reference_range: ReferenceRange,
) -> tuple[str | None, str | None]:
    if payload.ph < reference_range.ph_target_min:
        return "ph", "low"
    if payload.ph > reference_range.ph_target_max:
        return "ph", "high"
    if payload.ec < reference_range.ec_target_min:
        return "ec", "low"
    if payload.ec > reference_range.ec_target_max:
        return "ec", "high"
    return None, None


def _history_out_of_range_side(
    entry: SensorHistoryEntry,
    reference_range: ReferenceRange,
) -> tuple[str | None, str | None]:
    if entry.ph < reference_range.ph_target_min:
        return "ph", "low"
    if entry.ph > reference_range.ph_target_max:
        return "ph", "high"
    if entry.ec < reference_range.ec_target_min:
        return "ec", "low"
    if entry.ec > reference_range.ec_target_max:
        return "ec", "high"
    return None, None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _range_deviation(value: float, target_min: float, target_max: float) -> float:
    """Return distance outside a target range, or zero when within range."""
    if value < target_min:
        return round(target_min - value, 4)

    if value > target_max:
        return round(value - target_max, 4)

    return 0


def _midpoint(target_min: float, target_max: float) -> float:
    """Return the configured target-range midpoint."""
    return round((target_min + target_max) / 2, 4)


def reentry_target_value(reference_range: ReferenceRange, decision: str) -> float:
    """Return the configured midpoint target for one confirmed correction."""
    if decision in {"ph_low", "ph_high"}:
        # pH uses the midpoint as the supervisory target. The resulting command
        # is still bounded by the pump-specific dose and runtime caps, followed
        # by the configured mixing lock and a new physical reading.
        return _midpoint(reference_range.ph_target_min, reference_range.ph_target_max)
    if decision in {"ec_low", "ec_high"}:
        # EC follows the same supervisory midpoint policy while retaining its
        # independently configured nutrient/dilution dose and runtime caps.
        return _midpoint(reference_range.ec_target_min, reference_range.ec_target_max)
    return 0.0


def _deadband_wait_decision(
    payload: SensorPayload,
    reference_range: ReferenceRange,
    *,
    ph_deviation: float,
    ec_deviation: float,
    metric: str,
    deadband: float,
) -> DosingDecision:
    """Hold pump output for tiny boundary excursions that can be probe noise."""
    return DosingDecision(
        decision="wait_near_boundary",
        pump_activated="none",
        dose_ml=0,
        duration_ms=0,
        ph_within_range=ph_deviation == 0,
        ec_within_range=ec_deviation == 0,
        ph_deviation=ph_deviation,
        ec_deviation=ec_deviation,
        reason=(
            f"{metric} is only {max(ph_deviation, ec_deviation):.3f} outside the target boundary. "
            "No dosing command was created; wait for the next stable reading."
        ),
        metadata={
            "strategy": "baseline",
            "rule": "near_boundary_deadband",
            "measured_ph": payload.ph,
            "measured_ec": payload.ec,
            "ph_target_min": reference_range.ph_target_min,
            "ph_target_max": reference_range.ph_target_max,
            "ec_target_min": reference_range.ec_target_min,
            "ec_target_max": reference_range.ec_target_max,
            "ph_deviation": ph_deviation,
            "ec_deviation": ec_deviation,
            "deadband": deadband,
            "reservoir_volume_liters": payload.reservoir_volume_liters,
        },
    )


def _calculate_dose_plan(
    deviation: float,
    reservoir_volume_liters: float,
    dose_ml_per_liter_per_unit: float,
    max_dose_ml_per_cycle: float,
) -> dict[str, float | bool]:
    """Return the requested dose and whether final dosing hit a safety cap."""
    requested_dose_ml = round(
        deviation * reservoir_volume_liters * dose_ml_per_liter_per_unit,
        2,
    )
    dose_ml = round(min(requested_dose_ml, max_dose_ml_per_cycle), 2)
    return {
        "requested_dose_ml_before_cap": requested_dose_ml,
        "max_dose_ml_per_cycle": round(max_dose_ml_per_cycle, 2),
        "dose_cap_hit": requested_dose_ml > max_dose_ml_per_cycle,
        "dose_ml": dose_ml,
    }


def max_dose_for_pump(reference_range: ReferenceRange, pump_activated: str) -> float:
    """Return the actuator-specific safety cap, falling back to the shared cap."""
    pump_caps = {
        "ph_up": reference_range.ph_up_max_dose_ml_per_cycle,
        "ph_down": reference_range.ph_down_max_dose_ml_per_cycle,
        "ec_up": reference_range.ec_up_max_dose_ml_per_cycle,
        "ec_down": reference_range.ec_down_max_dose_ml_per_cycle,
    }
    return pump_caps.get(pump_activated) or reference_range.max_dose_ml_per_cycle


def effective_max_dose_for_pump(reference_range: ReferenceRange, pump_activated: str) -> float:
    """Return the configured cap bounded by what the pump can physically deliver."""
    flow_ml_per_min = (
        reference_range.ph_pump_flow_ml_per_min
        if pump_activated.startswith("ph_")
        else reference_range.ec_pump_flow_ml_per_min
    )
    return min(
        max_dose_for_pump(reference_range, pump_activated),
        max_deliverable_dose_ml(flow_ml_per_min, max_duration_for_pump(reference_range, pump_activated)),
    )


def max_duration_for_pump(reference_range: ReferenceRange, pump_activated: str) -> int:
    """Return the actuator-specific max runtime, falling back to global settings."""
    pump_durations = {
        "ph_up": reference_range.ph_up_max_duration_ms,
        "ph_down": reference_range.ph_down_max_duration_ms,
        "ec_up": reference_range.ec_up_max_duration_ms,
        "ec_down": reference_range.ec_down_max_duration_ms,
    }
    return pump_durations.get(pump_activated) or settings.maximum_pump_duration_ms
