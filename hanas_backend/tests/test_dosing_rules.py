"""Rule-based dosing decision tests."""

from datetime import datetime, timedelta, timezone

from app.schemas.sensor import SensorPayload
from app.schemas.sensor import SensorHistoryEntry
from app.services.baseline.dosing_rules import (
    evaluate_dosing_decision,
    evaluate_persistent_dosing_decision,
)
from tests.factories import build_reference_range


REFERENCE_RANGE = build_reference_range()


def test_ph_low_triggers_ph_up() -> None:
    """Low pH returns a pH-up dosing decision."""
    payload = SensorPayload(temperature=24.5, ph=5.3, ec=1.6, reservoir_volume_liters=20)

    decision = evaluate_dosing_decision(payload, REFERENCE_RANGE)

    assert decision.decision == "ph_low"
    assert decision.pump_activated == "ph_up"
    assert decision.dose_ml == 10.0
    assert decision.duration_ms == 4959
    assert decision.metadata["target_policy"] == "midpoint_single_correction_capped"
    assert decision.metadata["target_ph_midpoint"] == 6.0
    assert decision.metadata["target_ph_midpoint"] == 6.0
    assert decision.metadata["dose_deviation"] == 0.7
    assert decision.metadata["dose_cap_hit"] is True


def test_small_out_of_range_ph_doses_toward_midpoint() -> None:
    """A confirmed pH deviation targets midpoint within actuator safety bounds."""
    payload = SensorPayload(temperature=24.5, ph=5.45, ec=1.6, reservoir_volume_liters=20)

    decision = evaluate_dosing_decision(payload, REFERENCE_RANGE)

    assert decision.decision == "ph_low"
    assert decision.pump_activated == "ph_up"
    assert decision.duration_ms > 0
    assert decision.dose_ml > 0
    assert decision.metadata["target_policy"] == "midpoint_single_correction_capped"
    assert decision.metadata["target_ph_midpoint"] == 6.0
    assert decision.metadata["target_ph_midpoint"] == 6.0


def test_near_boundary_ec_waits_instead_of_dosing_to_midpoint() -> None:
    """Near-boundary EC should not create a large nutrient command from probe noise."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.79,
        ec=1.191,
        reservoir_volume_liters=20,
        ph_stability_threshold=0.03,
        ec_stability_threshold=0.03,
    )

    decision = evaluate_dosing_decision(payload, REFERENCE_RANGE)

    assert decision.decision == "wait_near_boundary"
    assert decision.pump_activated == "none"
    assert decision.duration_ms == 0
    assert decision.dose_ml == 0
    assert decision.ec_within_range is False
    assert decision.ec_deviation == 0.009
    assert decision.metadata["rule"] == "near_boundary_deadband"
    assert decision.metadata["deadband"] == 0.03


def test_confirmed_near_boundary_ec_doses_toward_midpoint() -> None:
    """A stable repeated EC excursion should target midpoint after confirmation."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.79,
        ec=1.191,
        reservoir_volume_liters=20,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        ph_stability_threshold=0.03,
        ec_stability_threshold=0.03,
    )
    history = [
        SensorHistoryEntry(
            timestamp=datetime.now(timezone.utc) - timedelta(minutes=1),
            ph=5.79,
            ec=1.192,
            decision="wait_near_boundary",
            pump_activated="none",
        )
    ]

    decision = evaluate_persistent_dosing_decision(payload, REFERENCE_RANGE, history)

    assert decision.decision == "ec_low"
    assert decision.pump_activated == "ec_up"
    assert decision.dose_ml == 36.4
    assert decision.duration_ms == 17472
    assert decision.metadata["target_ec_midpoint"] == 1.6
    assert decision.metadata["target_ec_midpoint"] == 1.6
    assert decision.metadata["target_policy"] == "midpoint_single_correction_capped"
    assert decision.metadata["deadband_policy"] == "dose_after_stable_confirmation"
    assert decision.metadata["near_boundary_confirmation"]["metric"] == "ec"


def test_unstable_near_boundary_confirmation_keeps_waiting() -> None:
    """A matching history row cannot bypass the current stability requirement."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.79,
        ec=1.191,
        reservoir_volume_liters=20,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=10,
    )
    history = [
        SensorHistoryEntry(
            timestamp=datetime.now(timezone.utc) - timedelta(minutes=1),
            ph=5.79,
            ec=1.192,
            decision="wait_near_boundary",
            pump_activated="none",
        )
    ]

    decision = evaluate_persistent_dosing_decision(payload, REFERENCE_RANGE, history)

    assert decision.decision == "wait_near_boundary"
    assert decision.pump_activated == "none"


def test_near_boundary_confirmation_without_stability_evidence_keeps_waiting() -> None:
    """Missing firmware stability evidence must fail closed for a tiny excursion."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.79,
        ec=1.191,
        reservoir_volume_liters=20,
    )
    history = [
        SensorHistoryEntry(
            timestamp=datetime.now(timezone.utc) - timedelta(minutes=1),
            ph=5.79,
            ec=1.192,
            decision="wait_near_boundary",
            pump_activated="none",
        )
    ]

    decision = evaluate_persistent_dosing_decision(payload, REFERENCE_RANGE, history)

    assert decision.decision == "wait_near_boundary"
    assert decision.pump_activated == "none"


def test_tiny_ph_excursion_does_not_hide_significant_ec_excursion() -> None:
    """A deadband wait on one metric must not suppress a larger correction on the other."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.49,
        ec=1.0,
        reservoir_volume_liters=20,
    )

    decision = evaluate_dosing_decision(payload, REFERENCE_RANGE)

    assert decision.decision == "ec_low"
    assert decision.pump_activated == "ec_up"


def test_tiny_ec_excursion_does_not_hide_significant_ph_excursion() -> None:
    """A small EC boundary excursion must not suppress a meaningful pH correction."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.19,
        reservoir_volume_liters=20,
    )

    decision = evaluate_dosing_decision(payload, REFERENCE_RANGE)

    assert decision.decision == "ph_low"
    assert decision.pump_activated == "ph_up"


def test_previous_dose_does_not_reconfirm_near_boundary_excursion() -> None:
    """A post-dose reading must start a fresh confirmation cycle."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.79,
        ec=1.191,
        reservoir_volume_liters=20,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
    )
    history = [
        SensorHistoryEntry(
            timestamp=datetime.now(timezone.utc) - timedelta(minutes=1),
            ph=5.79,
            ec=1.19,
            decision="ec_low",
            pump_activated="ec_up",
            dose_ml=5.34,
        ),
        SensorHistoryEntry(
            timestamp=datetime.now(timezone.utc) - timedelta(minutes=2),
            ph=5.79,
            ec=1.192,
            decision="wait_near_boundary",
            pump_activated="none",
        ),
    ]

    decision = evaluate_persistent_dosing_decision(payload, REFERENCE_RANGE, history)

    assert decision.decision == "wait_near_boundary"
    assert decision.pump_activated == "none"


def test_ph_high_triggers_ph_down() -> None:
    """High pH returns a pH-down dosing decision."""
    payload = SensorPayload(temperature=24.5, ph=6.8, ec=1.6, reservoir_volume_liters=20)

    decision = evaluate_dosing_decision(payload, REFERENCE_RANGE)

    assert decision.decision == "ph_high"
    assert decision.pump_activated == "ph_down"
    assert decision.metadata["dose_cap_hit"] is True
    assert decision.metadata["requested_dose_ml_before_cap"] == 11.36
    assert decision.dose_ml == 10.0
    assert decision.duration_ms == 4959
    assert decision.metadata["target_policy"] == "midpoint_single_correction_capped"
    assert decision.metadata["target_ph_midpoint"] == 6.0
    assert decision.metadata["dose_deviation"] == 0.8


def test_ec_low_triggers_ec_up() -> None:
    """Low EC returns an EC-up dosing decision."""
    payload = SensorPayload(temperature=24.5, ph=6.0, ec=1.0, reservoir_volume_liters=20)

    decision = evaluate_dosing_decision(payload, REFERENCE_RANGE)

    assert decision.decision == "ec_low"
    assert decision.pump_activated == "ec_up"
    assert decision.dose_ml == 53.4
    assert decision.duration_ms == 25632
    assert decision.metadata["target_policy"] == "midpoint_single_correction_capped"
    assert decision.metadata["target_ec_midpoint"] == 1.6
    assert decision.metadata["target_ec_midpoint"] == 1.6
    assert decision.metadata["dose_deviation"] == 0.6


def test_ec_high_triggers_ec_down() -> None:
    """High EC returns an EC-down dosing decision."""
    payload = SensorPayload(temperature=24.5, ph=6.0, ec=2.3, reservoir_volume_liters=20)

    decision = evaluate_dosing_decision(payload, REFERENCE_RANGE)

    assert decision.decision == "ec_high"
    assert decision.pump_activated == "ec_down"
    assert decision.dose_ml == 2000.0
    assert decision.duration_ms == 960000
    assert decision.metadata["target_policy"] == "midpoint_single_correction_capped"
    assert decision.metadata["target_ec_midpoint"] == 1.6
    assert decision.metadata["target_ec_midpoint"] == 1.6
    assert decision.metadata["dose_deviation"] == 0.7
    assert decision.metadata["requested_dose_ml_before_cap"] == 8289.68
    assert decision.metadata["dose_cap_hit"] is True


def test_dosing_uses_direction_specific_calibration_values() -> None:
    """Each low/high branch uses its matching up/down dose calibration."""
    reference_range = REFERENCE_RANGE.model_copy(
        update={
            "ph_up_dose_ml_per_liter_per_unit": 0.2,
            "ph_down_dose_ml_per_liter_per_unit": 0.05,
            "ec_up_dose_ml_per_liter_per_unit": 0.1,
            "ec_down_dose_ml_per_liter_per_unit": 0.06,
        }
    )

    ph_low = evaluate_dosing_decision(
        SensorPayload(temperature=24.5, ph=5.3, ec=1.6, reservoir_volume_liters=80),
        reference_range,
    )
    ph_high = evaluate_dosing_decision(
        SensorPayload(temperature=24.5, ph=6.8, ec=1.6, reservoir_volume_liters=80),
        reference_range,
    )
    ec_low = evaluate_dosing_decision(
        SensorPayload(temperature=24.5, ph=6.0, ec=1.0, reservoir_volume_liters=80),
        reference_range,
    )
    ec_high = evaluate_dosing_decision(
        SensorPayload(temperature=24.5, ph=6.0, ec=2.3, reservoir_volume_liters=80),
        reference_range,
    )

    assert ph_low.dose_ml == 10.0
    assert ph_high.dose_ml == 3.2
    assert ph_high.duration_ms == 1587
    assert ph_high.metadata["requested_actuation_dose_ml"] == 3.2
    assert ph_high.metadata["reliable_pulse_floor_applied"] is False
    assert ec_low.dose_ml == 4.8
    assert ec_high.dose_ml == 3.36


def test_production_ph_down_correction_targets_midpoint_with_cycle_cap() -> None:
    """A production-like pH correction aims midpoint without exceeding its cycle cap."""
    decision = evaluate_dosing_decision(
        SensorPayload(temperature=26.5, ph=6.54, ec=1.42, reservoir_volume_liters=31),
        REFERENCE_RANGE,
        dose_inside_deadband=True,
    )

    assert decision.decision == "ph_high"
    assert decision.pump_activated == "ph_down"
    assert decision.metadata["requested_dose_ml_before_cap"] == 11.89
    assert decision.metadata["requested_actuation_dose_ml"] == 10.0
    assert decision.dose_ml == 10.0
    assert decision.duration_ms == 4959
    assert decision.metadata["reliable_pulse_floor_safe"] is True
    assert decision.metadata["reliable_pulse_floor_applied"] is False


def test_large_reservoir_midpoint_request_is_capped_to_single_safe_cycle() -> None:
    """The observed 51.8 L case requests midpoint but keeps the 10 mL safety cap."""
    decision = evaluate_dosing_decision(
        SensorPayload(temperature=26.5, ph=6.56, ec=1.42, reservoir_volume_liters=51.8),
        REFERENCE_RANGE,
        dose_inside_deadband=True,
    )

    assert decision.metadata["target_ph_midpoint"] == 6.0
    assert decision.metadata["target_ph_midpoint"] == 6.0
    assert decision.metadata["requested_dose_ml_before_cap"] == 20.6
    assert decision.metadata["dose_cap_hit"] is True
    assert decision.dose_ml == 10.0
    assert decision.duration_ms == 4959


def test_dosing_uses_pump_specific_safety_caps() -> None:
    """Each actuator can have a different maximum dose per control cycle."""
    reference_range = REFERENCE_RANGE.model_copy(
        update={
            "ph_down_dose_ml_per_liter_per_unit": 10.0,
            "ec_down_dose_ml_per_liter_per_unit": 660.0,
            "ph_down_max_dose_ml_per_cycle": 10.0,
            "ec_down_max_dose_ml_per_cycle": 1000.0,
        }
    )

    ph_high = evaluate_dosing_decision(
        SensorPayload(temperature=24.5, ph=6.8, ec=1.6, reservoir_volume_liters=20),
        reference_range,
    )
    ec_high = evaluate_dosing_decision(
        SensorPayload(temperature=24.5, ph=6.0, ec=2.3, reservoir_volume_liters=20),
        reference_range,
    )

    assert ph_high.dose_ml == 10
    assert ec_high.dose_ml == 1000
    assert ec_high.duration_ms == 480000


def test_missing_pump_duration_limits_fall_back_without_crashing() -> None:
    """Missing per-pump runtime limits use the global safety limit."""
    reference_range = REFERENCE_RANGE.model_copy(
        update={
            "ph_up_max_duration_ms": None,
            "ph_down_max_duration_ms": None,
            "ec_up_max_duration_ms": None,
            "ec_down_max_duration_ms": None,
        }
    )

    ph_high = evaluate_dosing_decision(
        SensorPayload(temperature=24.5, ph=6.51, ec=1.6, reservoir_volume_liters=20),
        reference_range,
        dose_inside_deadband=True,
    )
    ec_high = evaluate_dosing_decision(
        SensorPayload(temperature=24.5, ph=6.0, ec=2.3, reservoir_volume_liters=20),
        reference_range,
    )

    assert ph_high.duration_ms <= 10_000
    assert 0 < ec_high.duration_ms <= 10_000
    assert ec_high.dose_ml == 20.83


def test_ec_down_can_deliver_one_cycle_phase2_dilution() -> None:
    """The Phase 2 EC-down cap allows the calibrated 20L dilution dose."""
    reference_range = REFERENCE_RANGE.model_copy(
        update={
            "ec_down_dose_ml_per_liter_per_unit": 592.12,
            "ec_down_max_dose_ml_per_cycle": 2000.0,
            "ec_down_max_duration_ms": 960_000,
        }
    )

    decision = evaluate_dosing_decision(
        SensorPayload(temperature=24.5, ph=6.0, ec=2.2, reservoir_volume_liters=20),
        reference_range,
    )

    assert decision.decision == "ec_high"
    assert decision.pump_activated == "ec_down"
    assert decision.dose_ml == 2000.0
    assert decision.duration_ms == 960000
