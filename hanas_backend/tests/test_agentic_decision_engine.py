"""Agentic decision engine tests."""

import pytest

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json

from pydantic import BaseModel

from app.core.config import settings
from app.schemas.sensor import SensorHistoryEntry, SensorPayload
from app.services import pipeline_tracker
from app.services.agentic_ai.graph_engine import (
    _agent_context,
    _consistency_review,
    _ensure_monitoring_consistency,
    _ensure_strategy_consistency,
    _calculate_dose_plan_candidate,
    _safety_gate_node,
    _orchestrator_node,
    _monitoring_node,
    _monitoring_context,
    _natural_recovery_reason,
    evaluate_agentic_decision,
)
from app.services.agentic_ai.schemas import (
    DosePlanResult,
    DiagnosticResult,
    LLMCompletionResult,
    MonitoringResult,
    StrategyResult,
    OrchestratorResult,
)
from app.services.agentic_ai.tools import agentic_tool_results
from app.services.baseline.dosing_rules import evaluate_dosing_decision
from tests.factories import build_reference_range


REFERENCE_RANGE = build_reference_range()


def test_agentic_tool_results_summarize_history_and_bounded_dose() -> None:
    """Backend-supplied tool results should be deterministic and actuator-safe."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    baseline = evaluate_dosing_decision(payload, REFERENCE_RANGE)
    history = [
        SensorHistoryEntry(
            timestamp=datetime.now(timezone.utc) - timedelta(minutes=10),
            ph=5.45,
            ec=1.8,
            temperature=24.2,
            decision="within_range",
            pump_activated="none",
            status="within_range",
        ),
        SensorHistoryEntry(
            timestamp=datetime.now(timezone.utc) - timedelta(minutes=5),
            ph=5.3,
            ec=1.8,
            temperature=24.5,
            decision="ph_low",
            pump_activated="ph_up",
            dose_ml=10,
            status="completed",
        ),
    ]

    results = agentic_tool_results(history, baseline, REFERENCE_RANGE)

    extended_history = results["get_extended_sensor_history"]
    bounded_dose = results["calculate_bounded_dose"]
    assert extended_history["available"] is True
    assert extended_history["sample_count"] == 2
    assert extended_history["dosing_event_count"] == 1
    assert extended_history["metrics"]["ph"]["delta"] == -0.15
    assert extended_history["metrics"]["water_temperature"]["latest"] == 24.5
    assert bounded_dose["pump_activated"] == "ph_up"
    assert bounded_dose["bounded_dose_ml"] == 10
    assert bounded_dose["duration_ms"] == 4959
    assert bounded_dose["reliable_pulse_floor_applied"] is False
    assert bounded_dose["was_capped_by_dose_limit"] is False


def test_agent_context_exposes_agentic_tool_results() -> None:
    """LLM prompts should receive deterministic tool results as context."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    baseline = evaluate_dosing_decision(payload, REFERENCE_RANGE)
    state = {
        "payload": payload,
        "reference_range": REFERENCE_RANGE,
        "history": [_history_entry(minutes_ago=5, ph=5.3, ec=1.8, decision="ph_low")],
        "baseline": baseline,
        "llm_trace": [],
        "crop_lifecycle": {},
    }

    context = _agent_context(state)

    tool_results = context["derived_context"]["tool_results"]
    assert "get_extended_sensor_history" in tool_results
    assert "calculate_bounded_dose" in tool_results
    assert tool_results["calculate_bounded_dose"]["pump_activated"] == "ph_up"
    assert context["agentic_tool_results"] == context["derived_context"]["tool_results"]


def test_monitoring_context_retains_batch_dose_events_outside_sensor_history() -> None:
    """Raw batch trends must not hide a prior command or its execution times."""
    payload = SensorPayload(
        temperature=24.5, ph=5.3, ec=1.8, reservoir_volume_liters=80,
        ph_stable_for_seconds=30, ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    now = datetime.now(timezone.utc)
    dose = SensorHistoryEntry(
        timestamp=now - timedelta(minutes=10), ph=5.1, ec=1.8,
        decision="ph_low", pump_activated="ph_up", dose_ml=10,
        duration_ms=5000, status="completed",
        action_started_at=now - timedelta(minutes=9),
        action_completed_at=now - timedelta(minutes=8),
        decision_metadata={"triggered_by": "batch_scheduler"},
    )
    reading = _history_entry(minutes_ago=1, ph=5.25, ec=1.8, decision="ph_low")
    state = {
        "payload": payload, "reference_range": REFERENCE_RANGE,
        "baseline": evaluate_dosing_decision(payload, REFERENCE_RANGE),
        "history": [reading], "control_history": [dose, reading],
    }

    context = _agent_context(state)

    assert context["recent_history"] == [reading.model_dump()]
    assert len(context["recent_control_events"]) == 1
    event = context["recent_control_events"][0]
    assert event["pump_activated"] == "ph_up"
    assert event["dose_ml"] == 10
    assert event["status"] == "completed"
    assert event["action_started_at"] == dose.action_started_at
    assert event["action_completed_at"] == dose.action_completed_at
    assert "decision_metadata" not in event
    assert context["derived_context"]["recent_dose_mixing_reason"] is None
    assert datetime.fromisoformat(context["derived_context"]["history_evaluated_at"]) >= now
    assert context["derived_context"]["history_freshness_gap_seconds"] == settings.agentic_history_freshness_gap_seconds


def test_orchestrator_feedback_does_not_regress_live_pipeline_stage() -> None:
    """Manager feedback loops should not make the live progress UI jump backward."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    baseline = evaluate_dosing_decision(payload, REFERENCE_RANGE)
    state = {
        "payload": payload,
        "reference_range": REFERENCE_RANGE,
        "history": [],
        "baseline": baseline,
        "orchestration": {
            "route": "monitoring_agent",
            "skipped_agents": [],
            "route_history": [
                {
                    "from": "initial_context",
                    "route": "monitoring_agent",
                    "reason": "Monitoring Agent should classify the cycle.",
                }
            ],
        },
        "monitoring": MonitoringResult(
            status="deviation",
            is_stable=True,
            summary="Stable deviation is present.",
        ),
    }

    pipeline_tracker.clear()
    pipeline_tracker.set_stage("monitoring_agent")

    try:
        updates = _orchestrator_node(state, None)

        assert updates["orchestration"]["route"] == "diagnostic_reasoning_agent"
        assert pipeline_tracker.get_progress()["stage"] == "monitoring_agent"
    finally:
        pipeline_tracker.clear()


def test_agentic_dosing_uses_bounded_reentry_correction() -> None:
    """Confirmed Agentic dosing aims inside range while enforcing safety bounds."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )

    recent_logs = [_history_entry(minutes_ago=5, ph=5.3, ec=1.8, decision="wait_initial_confirmation")]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs)

    assert decision.decision == "ph_low"
    assert decision.pump_activated == "ph_up"
    assert decision.dose_ml == 10
    assert decision.duration_ms == 4959
    assert decision.metadata["dose_adjustment_factor"] == 1.0
    assert decision.metadata["dose_factor_source"] == "dose_planning_agent_bounded_by_safety_gate"
    assert decision.metadata["baseline_shadow"]["dose_ml"] == 10
    assert decision.metadata["orchestration"] == "langgraph"
    assert decision.metadata["reasoning_source"] == "deterministic_fallback"
    assert decision.metadata["llm_enabled"] is False
    assert decision.metadata["llm_models_used"] == []
    assert decision.metadata["safety_gate_source"] == "deterministic_python"
    assert decision.metadata["human_review_gate"]["stage"] == "after_safety_gate"
    assert decision.metadata["human_review_gate"]["eligible_for_review"] is True
    assert decision.metadata["human_review_gate"]["review_applied_by"] == "sensor_ingestion_route"
    assert decision.metadata["agent_communication"]["mode"] == "structured_graph_state"
    assert decision.metadata["agent_communication"]["sequence"].count("orchestrator_agent") > 1
    assert "consistency_review" in decision.metadata["agent_communication"]["sequence"]
    assert "consistency_review" in decision.metadata["agent_communication"]["available_outputs"]
    assert decision.metadata["agent_communication"]["completed_agents"] == [
        "orchestrator_agent",
        "monitoring_agent",
        "diagnostic_reasoning_agent",
        "decision_agent",
        "dose_planning_agent",
    ]
    assert decision.metadata["initial_orchestrator_agent"]["route"] == "monitoring_agent"
    assert decision.metadata["orchestrator_agent"]["route"] == "safety_gate"
    assert decision.metadata["orchestrator_agent"]["route_history"][-1]["from"] == "consistency_review"
    assert "Diagnostic classified ph_low" in decision.metadata["orchestrator_agent"]["reason"]
    assert "Safety Gate issued bounded ph_up dosing" in decision.metadata["orchestrator_agent"]["reason"]
    assert decision.metadata["consistency_review"]["review_status"] == "pass"
    assert decision.metadata["same_pump_response"]["reason"] == (
        "No recent same-pump dose exists inside the fresh history window."
    )


def test_agentic_dosing_uses_crop_lifecycle_as_dose_factor() -> None:
    """Sensitive crop stages reduce final dose factor and extend mixing policy."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [_history_entry(minutes_ago=5, ph=5.3, ec=1.8, decision="wait_initial_confirmation")]

    decision = evaluate_agentic_decision(
        payload,
        REFERENCE_RANGE,
        recent_logs,
        crop_lifecycle={
            "age_days": 3,
            "stage": "establishment",
            "stage_label": "Establishment",
            "harvest_start_day": 30,
            "harvest_end_day": 35,
        },
    )

    assert decision.decision == "ph_low"
    assert decision.pump_activated == "ph_up"
    assert decision.metadata["crop_dosing_policy"]["stage"] == "establishment"
    assert decision.metadata["crop_dosing_policy"]["max_dose_factor"] == 0.65
    assert decision.metadata["dose_adjustment_factor"] == 0.65
    assert decision.dose_ml == 6.5
    assert decision.metadata["mixing_window"]["crop_lifecycle_factor"] == 1.25
    assert "Establishment caps the correction factor" in decision.reason


def test_agentic_waits_for_initial_confirmation_before_first_dose() -> None:
    """The first out-of-range agentic reading waits to avoid startup sensor shock."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, [])

    assert decision.decision == "wait_initial_confirmation"
    assert decision.pump_activated == "none"
    assert decision.metadata["orchestrator_agent"]["initial_route"] == "monitoring_agent"
    assert decision.metadata["monitoring_agent"]["status"] == "confirming"
    assert "initial_confirmation" in decision.metadata["risk_flags"]


def test_agentic_requires_confirmation_gap_before_second_reading_doses() -> None:
    """A rapid second matching reading still waits until the backend gap expires."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [_history_entry(minutes_ago=0, ph=5.3, ec=1.8, decision="wait_initial_confirmation")]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs)

    assert decision.decision == "wait_initial_confirmation"
    assert decision.pump_activated == "none"
    assert "initial_confirmation" in decision.metadata["risk_flags"]


def test_agentic_waits_for_confirmation_after_in_range_history() -> None:
    """A fresh deviation after in-range history still requires one confirmation."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [_history_entry(minutes_ago=1, ph=5.8, ec=1.8, decision="within_range")]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs)

    assert decision.decision == "wait_initial_confirmation"
    assert decision.pump_activated == "none"
    assert "initial_confirmation" in decision.metadata["risk_flags"]


def test_agentic_in_range_reason_includes_current_readings_and_recent_dose() -> None:
    """In-range logs should explain the current reading and correction context."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.62,
        ec=1.41,
        reservoir_volume_liters=20,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [
        _history_entry(
            minutes_ago=4,
            ph=5.2,
            ec=1.41,
            decision="ph_low",
            pump_activated="ph_up",
            status="completed",
        )
    ]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs)

    assert decision.decision == "within_range"
    assert "Current pH 5.62 and EC 1.41 are within target range" in decision.reason
    assert "recent pH-up correction" in decision.reason
    assert "post-dose remeasurement" in decision.metadata["monitoring_agent"]["summary"]


def test_agentic_preventively_doses_projected_phase3_ph_rise() -> None:
    """A strong in-range pH rise near the boundary should reach monitoring and dose."""
    payload = SensorPayload(
        temperature=24.5,
        ph=6.5,
        ec=1.8,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        ph_stability_threshold=0.03,
        ec_stability_threshold=0.03,
        control_strategy="agentic_ai",
    )
    recent_logs = [
        _history_entry(minutes_ago=3, ph=6.3, ec=1.8, decision="within_range"),
        _history_entry(minutes_ago=2, ph=6.37, ec=1.8, decision="within_range"),
        _history_entry(minutes_ago=1, ph=6.44, ec=1.8, decision="within_range"),
    ]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs)

    assert decision.decision == "ph_high"
    assert decision.pump_activated == "ph_down"
    assert decision.dose_ml == 3.41
    assert decision.duration_ms == 1691
    assert decision.metadata["orchestrator_agent"]["initial_route"] == "monitoring_agent"
    assert decision.metadata["monitoring_agent"]["status"] == "projected_deviation"
    assert "projected_out_of_range" in decision.metadata["risk_flags"]
    assert decision.metadata["projected_deviation"]["projected_value"] == 6.56


def test_agentic_honors_monitoring_agent_projection_without_deterministic_projection() -> None:
    """Monitoring Agent can be the SME that escalates an in-range projected deviation."""
    payload = SensorPayload(
        temperature=24.5,
        ph=6.42,
        ec=1.8,
        reservoir_volume_liters=20,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        ph_stability_threshold=0.03,
        ec_stability_threshold=0.03,
        control_strategy="agentic_ai",
    )
    recent_logs = [
        _history_entry(minutes_ago=3, ph=6.38, ec=1.8, decision="within_range"),
        _history_entry(minutes_ago=2, ph=6.41, ec=1.8, decision="within_range"),
        _history_entry(minutes_ago=1, ph=6.39, ec=1.8, decision="within_range"),
    ]

    decision = evaluate_agentic_decision(
        payload,
        REFERENCE_RANGE,
        recent_logs,
        llm=MonitoringProjectedDeviationLLM(),
    )

    assert decision.decision == "ph_high"
    assert decision.pump_activated == "ph_down"
    assert decision.duration_ms > 0
    assert decision.metadata["monitoring_agent"]["status"] == "projected_deviation"
    assert decision.metadata["projected_deviation"]["source"] == "monitoring_agent"
    assert decision.metadata["projected_deviation"]["projected_value"] == 6.56
    assert decision.metadata["agent_communication"]["completed_agents"] == [
        "orchestrator_agent",
        "monitoring_agent",
        "diagnostic_reasoning_agent",
        "decision_agent",
        "dose_planning_agent",
    ]


def test_agentic_rejects_monitoring_projection_with_invalid_boundary() -> None:
    """Monitoring projections must still match configured target boundaries."""
    payload = SensorPayload(
        temperature=24.5,
        ph=6.42,
        ec=1.8,
        reservoir_volume_liters=20,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        ph_stability_threshold=0.03,
        ec_stability_threshold=0.03,
        control_strategy="agentic_ai",
    )

    decision = evaluate_agentic_decision(
        payload,
        REFERENCE_RANGE,
        [],
        llm=InvalidMonitoringProjectionLLM(),
    )

    assert decision.decision == "wait_consistency_review"
    assert decision.pump_activated == "none"
    assert decision.metadata["projected_deviation"] is None
    assert "consistency_review_block" in decision.metadata["risk_flags"]


def test_agentic_does_not_dose_stable_in_range_near_boundary() -> None:
    """A near-boundary value without an outward trend remains normal monitoring."""
    payload = SensorPayload(
        temperature=24.5,
        ph=6.5,
        ec=1.8,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        ph_stability_threshold=0.03,
        ec_stability_threshold=0.03,
        control_strategy="agentic_ai",
    )
    recent_logs = [
        _history_entry(minutes_ago=3, ph=6.48, ec=1.8, decision="within_range"),
        _history_entry(minutes_ago=2, ph=6.47, ec=1.8, decision="within_range"),
        _history_entry(minutes_ago=1, ph=6.49, ec=1.8, decision="within_range"),
    ]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs)

    assert decision.decision == "within_range"
    assert decision.pump_activated == "none"
    assert decision.metadata["orchestrator_agent"]["initial_route"] == "monitoring_agent"
    assert decision.metadata["monitoring_agent"]["status"] == "in_range"


def test_agentic_stops_after_monitoring_llm_for_in_range_no_issue() -> None:
    """A plain in-range reading should stop after Orchestrator and Monitoring."""
    payload = SensorPayload(
        temperature=24.5,
        ph=6.1,
        ec=1.7,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [
        _history_entry(minutes_ago=3, ph=6.08, ec=1.7, decision="within_range"),
        _history_entry(minutes_ago=2, ph=6.09, ec=1.7, decision="within_range"),
        _history_entry(minutes_ago=1, ph=6.1, ec=1.7, decision="within_range"),
    ]
    llm = CountingAgenticLLM()

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs, llm=llm)

    assert decision.decision == "within_range"
    assert decision.pump_activated == "none"
    assert llm.calls == ["Orchestrator Agent", "Monitoring Agent"]
    assert decision.metadata["orchestrator_agent"]["initial_route"] == "monitoring_agent"
    assert decision.metadata["monitoring_agent"]["status"] == "in_range"
    assert decision.metadata["orchestrator_agent"]["source"] == "llm"
    assert decision.metadata["orchestrator_agent"]["routing_reason"] == (
        "Monitoring agent should classify the current range, trend, and projection state."
    )
    assert decision.metadata["orchestrator_agent"]["reason"] == (
        "Orchestrator routed to Monitoring; Monitoring reported in_range, so downstream dosing "
        "agents were skipped and Safety Gate returned within_range."
    )
    assert decision.metadata["initial_orchestrator_agent"]["reason"] == (
        "Monitoring agent should classify the current range, trend, and projection state."
    )
    assert decision.metadata["orchestrator_agent"]["route"] == "safety_gate"
    assert decision.metadata["orchestrator_agent"]["route_history"][-1]["from"] == "monitoring_agent"


def test_agentic_doses_when_projected_phase2_ph_correction_is_small() -> None:
    """Phase 2 proactive pH corrections should actuate even for small doses."""
    payload = SensorPayload(
        temperature=24.5,
        ph=6.5,
        ec=1.8,
        reservoir_volume_liters=20,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        ph_stability_threshold=0.03,
        ec_stability_threshold=0.03,
        control_strategy="agentic_ai",
    )
    recent_logs = [
        _history_entry(minutes_ago=3, ph=6.3, ec=1.8, decision="within_range"),
        _history_entry(minutes_ago=2, ph=6.37, ec=1.8, decision="within_range"),
        _history_entry(minutes_ago=1, ph=6.44, ec=1.8, decision="within_range"),
    ]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs)

    assert decision.decision == "ph_high"
    assert decision.pump_activated == "ph_down"
    assert decision.duration_ms > 0
    assert "projected_out_of_range" in decision.metadata["risk_flags"]


def test_agentic_waits_for_first_tiny_near_boundary_ph_deviation() -> None:
    """A first tiny near-boundary pH drift still uses the confirmation gate."""
    payload = SensorPayload(
        temperature=24.5,
        ph=6.51,
        ec=1.8,
        reservoir_volume_liters=20,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        ph_stability_threshold=0.03,
        ec_stability_threshold=0.03,
        control_strategy="agentic_ai",
    )
    recent_logs = [
        _history_entry(minutes_ago=1, ph=6.5, ec=1.8, decision="within_range"),
        _history_entry(minutes_ago=2, ph=6.5, ec=1.8, decision="within_range"),
        _history_entry(
            minutes_ago=5,
            ph=6.55,
            ec=1.8,
            decision="ph_high",
            pump_activated="ph_down",
            status="completed",
        ),
    ]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs)

    assert decision.decision == "wait_near_boundary"
    assert decision.pump_activated == "none"
    assert decision.dose_ml == 0
    assert "near_boundary" in decision.metadata["risk_flags"]


def test_agentic_confirmed_tiny_deviation_doses_toward_midpoint() -> None:
    """A repeated stable pH excursion should receive a bounded midpoint correction."""
    payload = SensorPayload(
        temperature=24.5,
        ph=6.51,
        ec=1.8,
        reservoir_volume_liters=20,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        ph_stability_threshold=0.03,
        ec_stability_threshold=0.03,
        control_strategy="agentic_ai",
    )
    recent_logs = [
        _history_entry(minutes_ago=1, ph=6.51, ec=1.8, decision="wait_near_boundary"),
        _history_entry(minutes_ago=2, ph=6.5, ec=1.8, decision="within_range"),
    ]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs)

    assert decision.decision == "ph_high"
    assert decision.pump_activated == "ph_down"
    assert decision.dose_ml == 7.24
    assert decision.duration_ms == 3590
    assert decision.metadata["agentic_target_value"] == 6.0
    assert decision.metadata["agentic_target_policy"] == "midpoint_single_correction_capped"
    assert decision.duration_ms > 0
    assert decision.metadata["deadband_policy"] == "dose_after_stable_confirmation"
    assert decision.metadata["near_boundary_confirmation"]["metric"] == "ph"


def test_agentic_confirmation_gap_uses_first_matching_wait_in_streak() -> None:
    """Rapid repeated wait rows do not keep resetting the initial confirmation window."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [
        _history_entry(minutes_ago=0, ph=5.3, ec=1.8, decision="wait_initial_confirmation"),
        _history_entry(minutes_ago=1, ph=5.3, ec=1.8, decision="wait_initial_confirmation"),
    ]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs)

    assert decision.decision == "ph_low"
    assert decision.pump_activated == "ph_up"


def test_agentic_treats_old_continuity_history_as_fresh_deviation() -> None:
    """A long quiet gap should force confirmation while retaining adaptive history."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [_history_entry(minutes_ago=60, ph=5.3, ec=1.8, decision="wait_initial_confirmation")]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs)

    assert decision.decision == "wait_initial_confirmation"
    assert decision.pump_activated == "none"
    assert decision.metadata["recent_log_count"] == 1
    assert "initial_confirmation" in decision.metadata["risk_flags"]


def test_agentic_keeps_context_across_short_operational_gap() -> None:
    """A normal multi-minute operational gap can still use matching history."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [_history_entry(minutes_ago=5, ph=5.3, ec=1.8, decision="wait_initial_confirmation")]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs)

    assert decision.decision == "ph_low"
    assert decision.pump_activated == "ph_up"
    assert decision.metadata["recent_log_count"] == 1


def test_agentic_combined_disturbance_can_prioritize_ec_first() -> None:
    """Agentic AI can consider pH and EC together while still executing one pump."""
    payload = SensorPayload(
        temperature=24.5,
        ph=6.7,
        ec=2.5,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [_history_entry(minutes_ago=5, ph=6.7, ec=2.5, decision="wait_initial_confirmation")]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs)

    assert decision.decision == "ec_high"
    assert decision.pump_activated == "ec_down"
    assert decision.metadata["combined_disturbance"] is True
    assert decision.metadata["baseline_shadow"]["pump_activated"] == "ph_down"
    assert decision.metadata["agentic_primary_shadow"]["pump_activated"] == "ec_down"
    assert decision.metadata["one_action_per_cycle"] is True
    assert "Combined pH/EC disturbance detected" in decision.reason
    assert "defer the other out-of-range metric" in decision.reason


def test_agentic_combined_disturbance_fallback_prioritizes_severe_ph() -> None:
    """Severe pH disturbance can still be corrected before EC."""
    payload = SensorPayload(
        temperature=24.5,
        ph=7.1,
        ec=2.5,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [_history_entry(minutes_ago=5, ph=7.1, ec=2.5, decision="wait_initial_confirmation")]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs)

    assert decision.decision == "ph_high"
    assert decision.pump_activated == "ph_down"
    assert decision.metadata["combined_disturbance"] is True
    assert "pH is the selected primary correction" in decision.reason
    assert "defer the other out-of-range metric" in decision.reason


def test_agentic_combined_disturbance_accepts_agent_selected_ph_primary() -> None:
    """The Diagnostic Agent may choose pH first for a non-severe mixed disturbance."""

    class PhPrimaryCombinedLLM(FakeAgenticLLM):
        def complete_json(
            self,
            agent_name: str,
            system_prompt: str,
            context: dict,
            output_model: type[BaseModel],
        ) -> LLMCompletionResult:
            if output_model is DiagnosticResult:
                return LLMCompletionResult(
                    output=DiagnosticResult(
                        classification="combined_disturbance",
                        primary_metric="ph",
                        summary="Both pH and EC are outside target range; pH should be corrected first.",
                    ),
                    model="gpt-4.1-nano",
                )
            if output_model is DosePlanResult:
                return LLMCompletionResult(
                    output=DosePlanResult(
                        pump_activated="ph_down",
                        dose_adjustment_factor=1.0,
                        mixing_adjustment_factor=1.25,
                        reason="Combined disturbance detected; pH-down is the selected one-pump correction.",
                    ),
                    model="gpt-4.1-nano",
                )
            return super().complete_json(agent_name, system_prompt, context, output_model)

    payload = SensorPayload(
        temperature=24.5,
        ph=6.7,
        ec=2.5,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [_history_entry(minutes_ago=5, ph=6.7, ec=2.5, decision="wait_initial_confirmation")]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs, llm=PhPrimaryCombinedLLM())

    assert decision.decision == "ph_high"
    assert decision.pump_activated == "ph_down"
    assert decision.metadata["diagnostic_reasoning_agent"]["primary_metric"] == "ph"
    assert decision.metadata["combined_disturbance"] is True
    assert "pH is the selected primary correction" in decision.reason


def test_agentic_combined_disturbance_updates_downstream_primary_context() -> None:
    """Decision and Dose Planning see the Diagnostic Agent's validated primary metric."""

    class CapturingPhPrimaryCombinedLLM(FakeAgenticLLM):
        def __init__(self) -> None:
            self.contexts_by_agent: dict[str, dict] = {}

        def complete_json(
            self,
            agent_name: str,
            system_prompt: str,
            context: dict,
            output_model: type[BaseModel],
        ) -> LLMCompletionResult:
            self.contexts_by_agent[agent_name] = context
            if output_model is DiagnosticResult:
                return LLMCompletionResult(
                    output=DiagnosticResult(
                        classification="combined_disturbance",
                        primary_metric="ph",
                        summary="Both pH and EC are outside target range; pH should be corrected first.",
                    ),
                    model="gpt-4.1-nano",
                )
            if output_model is DosePlanResult:
                return LLMCompletionResult(
                    output=DosePlanResult(
                        pump_activated="ph_down",
                        dose_adjustment_factor=1.0,
                        mixing_adjustment_factor=1.25,
                        reason="Combined disturbance detected; pH-down is the selected one-pump correction.",
                    ),
                    model="gpt-4.1-nano",
                )
            return super().complete_json(agent_name, system_prompt, context, output_model)

    llm = CapturingPhPrimaryCombinedLLM()
    payload = SensorPayload(
        temperature=24.5,
        ph=6.7,
        ec=2.5,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [_history_entry(minutes_ago=5, ph=6.7, ec=2.5, decision="wait_initial_confirmation")]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs, llm=llm)

    diagnostic_context = llm.contexts_by_agent["Diagnostic Reasoning Agent"]
    decision_context = llm.contexts_by_agent["Decision Agent"]
    dose_context = llm.contexts_by_agent["Dose Planning Agent"]

    assert diagnostic_context["agentic_primary_shadow"]["pump_activated"] == "ec_down"
    assert decision_context["diagnostic_reasoning_agent"]["primary_metric"] == "ph"
    assert decision_context["agentic_primary_shadow"]["pump_activated"] == "ph_down"
    assert dose_context["diagnostic_reasoning_agent"]["primary_metric"] == "ph"
    assert dose_context["agentic_primary_shadow"]["pump_activated"] == "ph_down"
    assert decision.pump_activated == "ph_down"


def test_agentic_combined_disturbance_preserves_agent_ec_choice_despite_severe_ph() -> None:
    """A valid Diagnostic EC priority survives downstream dose and safety checks."""

    class EcPrimaryForSeverePhLLM(FakeAgenticLLM):
        def complete_json(
            self,
            agent_name: str,
            system_prompt: str,
            context: dict,
            output_model: type[BaseModel],
        ) -> LLMCompletionResult:
            if output_model is DiagnosticResult:
                return LLMCompletionResult(
                    output=DiagnosticResult(
                        classification="combined_disturbance",
                        primary_metric="ec",
                        summary="Both pH and EC are outside target range; EC should be corrected first.",
                    ),
                    model="gpt-4.1-nano",
                )
            if output_model is DosePlanResult:
                return LLMCompletionResult(
                    output=DosePlanResult(
                        pump_activated="ec_down",
                        dose_adjustment_factor=1.0,
                        mixing_adjustment_factor=1.5,
                        reason="Combined disturbance detected; EC-down is the selected correction.",
                    ),
                    model="gpt-4.1-nano",
                )
            return super().complete_json(agent_name, system_prompt, context, output_model)

    for ph in (7.1, 5.0):
        for ec in (2.5, 4.0):
            payload = SensorPayload(
                temperature=24.5, ph=ph, ec=ec, reservoir_volume_liters=80,
                ph_stable_for_seconds=30, ec_stable_for_seconds=30,
                control_strategy="agentic_ai",
            )
            recent_logs = [_history_entry(minutes_ago=5, ph=ph, ec=ec,
                                          decision="wait_initial_confirmation")]
            decision = evaluate_agentic_decision(
                payload, REFERENCE_RANGE, recent_logs, llm=EcPrimaryForSeverePhLLM(),
            )
            assert decision.decision == "ec_high"
            assert decision.pump_activated == "ec_down"
            assert decision.metadata["diagnostic_reasoning_agent"]["primary_metric"] == "ec"
            assert 0 < decision.dose_ml <= REFERENCE_RANGE.ec_down_max_dose_ml_per_cycle
            assert decision.duration_ms > 0
            assert "EC" in decision.reason


def test_agentic_waits_when_reading_is_unstable() -> None:
    """Agentic AI refuses to dose while stability duration is below the required window."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=10,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, [])

    assert decision.decision == "wait_for_stability"
    assert decision.pump_activated == "none"
    assert decision.metadata["baseline_shadow"]["pump_activated"] == "ph_up"
    assert "unstable_reading" in decision.metadata["risk_flags"]


def test_agentic_waits_during_mixing_window_after_recent_dose() -> None:
    """Agentic AI avoids repeated overcorrection before the latest dose has mixed."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [
        _history_entry(
            minutes_ago=1,
            ph=5.2,
            ec=1.8,
            decision="ph_low",
            pump_activated="ph_up",
            status="completed",
        )
    ]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs)

    assert decision.decision == "wait_for_mixing"
    assert decision.pump_activated == "none"
    assert "recent_dose_mixing" in decision.metadata["risk_flags"]
    assert decision.metadata["orchestrator_agent"]["route"] == "safety_gate"
    assert "dose_planning_agent" in decision.metadata["orchestrator_agent"]["skipped_agents"]


def test_agentic_waits_during_mixing_window_even_if_current_reading_is_in_range() -> None:
    """A recent dose must finish mixing before an in-range follow-up is accepted."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.8,
        ec=1.8,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [
        _history_entry(
            minutes_ago=1,
            ph=5.2,
            ec=1.8,
            decision="ph_low",
            pump_activated="ph_up",
            status="completed",
        )
    ]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs)

    assert decision.decision == "wait_for_mixing"
    assert decision.pump_activated == "none"
    assert "recent_dose_mixing" in decision.metadata["risk_flags"]


def test_agentic_ignores_stale_dosing_status_after_estimated_mixing_window() -> None:
    """A missed completion callback should not block future dosing forever."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [
        _history_entry(
            minutes_ago=5,
            ph=5.2,
            ec=1.8,
            decision="ph_low",
            pump_activated="ph_up",
            status="dosing",
            duration_ms=1_000,
        )
    ]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs)

    assert decision.decision == "ph_low"
    assert decision.pump_activated == "ph_up"
    assert decision.metadata["agentic_action"] == "dose"
    assert "recent_dose_mixing" not in decision.metadata["monitoring_agent"]["risk_flags"]


def test_agentic_still_waits_for_active_dosing_status() -> None:
    """A plausible in-progress dosing row still blocks repeat dosing."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [
        _history_entry(
            minutes_ago=0,
            ph=5.2,
            ec=1.8,
            decision="ph_low",
            pump_activated="ph_up",
            status="dosing",
            duration_ms=60_000,
        )
    ]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs)

    assert decision.decision == "wait_for_mixing"
    assert decision.pump_activated == "none"
    assert "still active" in decision.reason


def test_orchestrator_agent_uses_no_llm_tokens_during_mixing_window() -> None:
    """The no-token monitoring precheck lets orchestrator stop unsafe dosing early."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [
        _history_entry(
            minutes_ago=1,
            ph=5.2,
            ec=1.8,
            decision="ph_low",
            pump_activated="ph_up",
            status="completed",
        )
    ]
    llm = CountingAgenticLLM()

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs, llm=llm)

    assert decision.decision == "wait_for_mixing"
    assert decision.pump_activated == "none"
    assert llm.calls == []
    assert len(decision.metadata["llm_trace"]) == 1
    assert decision.metadata["llm_trace"][0]["agent"] == "orchestrator_agent"
    assert decision.metadata["orchestrator_agent"]["consulted_agent"] is None
    assert decision.metadata["orchestrator_agent"]["monitoring_precheck"]["status"] == "mixing"


def test_agentic_dose_records_bounded_adaptive_mixing_window(monkeypatch) -> None:
    """Dosing metadata stores the bounded mixing window used by later decisions."""
    monkeypatch.setattr(settings, "agentic_base_mixing_time_seconds", 180)
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )

    recent_logs = [_history_entry(minutes_ago=5, ph=5.3, ec=1.8, decision="wait_initial_confirmation")]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs, llm=HighMixingFactorLLM())

    assert decision.pump_activated == "ph_up"
    assert decision.metadata["mixing_window"]["base_seconds"] == 180
    assert decision.metadata["mixing_window"]["effective_seconds"] == 450
    assert decision.metadata["mixing_window"]["max_seconds"] == 450
    assert decision.metadata["mixing_window"]["llm_recommended_factor"] == 2.5


def test_agentic_phase3_base_allows_adaptive_mixing_window(monkeypatch) -> None:
    """A 180-second base leaves headroom for a bounded adaptive window."""
    monkeypatch.setattr(settings, "agentic_base_mixing_time_seconds", 180)
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=70,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [
        _history_entry(minutes_ago=5, ph=5.3, ec=1.8, decision="wait_initial_confirmation")
    ]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs)

    assert decision.pump_activated == "ph_up"
    assert decision.metadata["mixing_window"]["base_seconds"] == 180
    assert decision.metadata["mixing_window"]["effective_seconds"] == 225


def test_agentic_uses_previous_effective_mixing_window() -> None:
    """A prior dose can extend the no-redose window beyond the default 120 seconds."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [
        _history_entry(
            minutes_ago=2,
            ph=5.2,
            ec=1.8,
            decision="ph_low",
            pump_activated="ph_up",
            status="completed",
            decision_metadata={"mixing_window": {"effective_seconds": 180}},
        )
    ]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs)

    assert decision.decision == "wait_for_mixing"
    assert decision.pump_activated == "none"


def test_agentic_mixing_window_starts_after_dose_duration() -> None:
    """Post-dose mixing starts after actuation, not at the decision log timestamp."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [
        SensorHistoryEntry(
            timestamp=datetime.now(timezone.utc) - timedelta(seconds=130),
            ph=5.2,
            ec=1.8,
            decision="ph_low",
            pump_activated="ph_up",
            duration_ms=20_000,
            status="completed",
            decision_metadata={"mixing_window": {"effective_seconds": 120}},
        )
    ]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs)

    assert decision.decision == "wait_for_mixing"
    assert decision.pump_activated == "none"
    assert decision.metadata["mixing_window_check"]["elapsed_seconds"] < 120


def test_agentic_waits_when_metric_is_recovering_naturally() -> None:
    """Agentic AI can observe an out-of-range value that is already moving toward target."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [
        _history_entry(minutes_ago=4, ph=5.1, ec=1.8, decision="ph_low"),
        _history_entry(minutes_ago=3, ph=5.15, ec=1.8, decision="ph_low"),
        _history_entry(minutes_ago=2, ph=5.2, ec=1.8, decision="ph_low"),
        _history_entry(minutes_ago=1, ph=5.25, ec=1.8, decision="ph_low"),
    ]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs)

    assert decision.decision == "wait_natural_recovery"
    assert decision.pump_activated == "none"
    assert decision.metadata["trend"] == "moving_toward_target"


def test_llm_monitoring_owns_natural_recovery_assessment() -> None:
    """LLM recovery can disagree with the deterministic trend in either direction."""
    class RecoveryLLM(FakeAgenticLLM):
        def complete_json(self, agent_name, system_prompt, context, output_model):
            assert "natural_recovery_reason" not in context["derived_context"]
            if output_model is MonitoringResult:
                return LLMCompletionResult(
                    output=MonitoringResult(
                        status="deviation", is_stable=True, is_recovering=True,
                        recovery_assessment={"ph_trend": "toward_range", "ec_trend": "in_range", "history_is_fresh": True, "sufficient_observations": True, "executed_dose_in_window": False},
                        summary="pH improved from 5.1 to 5.3 over four minutes despite a small reversal, with no dose.",
                    ),
                    model="test-model",
                )
            return super().complete_json(agent_name, system_prompt, context, output_model)

    payload = SensorPayload(
        temperature=24.5, ph=5.3, ec=1.8, reservoir_volume_liters=80,
        ph_stable_for_seconds=30, ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    for values, llm, expected in [
        ([5.1, 5.2, 5.19, 5.25], RecoveryLLM(), "wait_natural_recovery"),
        ([5.1, 5.15, 5.2, 5.25], FakeAgenticLLM(), "ph_low"),
    ]:
        history = [
            _history_entry(minutes_ago=4 - index, ph=ph, ec=1.8, decision="ph_low")
            for index, ph in enumerate(values)
        ]
        decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, history, llm=llm)
        assert decision.decision == expected
        assert decision.pump_activated == ("none" if expected == "wait_natural_recovery" else "ph_up")
        if expected == "wait_natural_recovery":
            assert decision.dose_ml == 0
            assert decision.duration_ms == 0
            assert "small reversal" in decision.reason
        else:
            assert decision.metadata["monitoring_agent"]["summary"] == "Stable pH deviation remains."


def test_combined_recovery_passes_terminal_consistency_review() -> None:
    """Recovery of both metrics must retain a combined diagnosis and no command."""
    class CombinedRecoveryLLM(FakeAgenticLLM):
        def complete_json(self, agent_name, system_prompt, context, output_model):
            if output_model is MonitoringResult:
                return LLMCompletionResult(
                    output=MonitoringResult(
                        status="deviation", is_stable=True, is_recovering=True,
                        recovery_assessment={"ph_trend": "toward_range", "ec_trend": "toward_range", "history_is_fresh": True, "sufficient_observations": True, "executed_dose_in_window": False},
                        summary="pH and EC both moved toward range over four minutes without dosing.",
                    ),
                    model="test-model",
                )
            return super().complete_json(agent_name, system_prompt, context, output_model)

    payload = SensorPayload(
        temperature=24.5, ph=5.3, ec=2.3, reservoir_volume_liters=70,
        ph_stable_for_seconds=30, ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    history = [
        _history_entry(minutes_ago=4 - i, ph=ph, ec=ec, decision="ph_low")
        for i, (ph, ec) in enumerate([(5.1, 2.5), (5.15, 2.45), (5.2, 2.4), (5.25, 2.35)])
    ]
    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, history, llm=CombinedRecoveryLLM())

    assert decision.decision == "wait_natural_recovery"
    assert decision.pump_activated == "none"
    assert decision.duration_ms == 0
    assert decision.metadata["diagnostic_reasoning_agent"]["classification"] == "combined_disturbance"
    assert decision.metadata["diagnostic_reasoning_agent"]["primary_metric"] in {"ph", "ec"}
    assert decision.metadata["consistency_review"]["review_status"] == "pass"


def test_recovery_assessment_cannot_override_safety_or_confirmation() -> None:
    """Recovery remains subordinate to range, confirmation, and hard safety."""
    dose = _history_entry(
        minutes_ago=1, ph=5.2, ec=1.8, decision="ph_low",
        pump_activated="ph_up", status="completed",
    )
    anomaly = _history_entry(minutes_ago=1, ph=6.4, ec=1.8, decision="within_range")
    for ph, stable_seconds, history, expected_status in [
        (5.3, 0, [], "unstable"),
        (6.0, 30, [], "in_range"),
        (5.3, 30, [dose], "mixing"),
        (5.3, 30, [anomaly], "sensor_anomaly"),
        (5.3, 30, [], "confirming"),
        (5.49, 30, [], "deviation"),
    ]:
        payload = SensorPayload(
            temperature=24.5, ph=ph, ec=1.8, reservoir_volume_liters=80,
            ph_stable_for_seconds=stable_seconds, ec_stable_for_seconds=30,
            control_strategy="agentic_ai",
        )
        state = {
            "payload": payload, "reference_range": REFERENCE_RANGE,
            "baseline": evaluate_dosing_decision(payload, REFERENCE_RANGE),
            "history": history,
        }
        result = _ensure_monitoring_consistency(
            state,
            MonitoringResult(
                status="deviation", is_stable=True, is_recovering=True,
                        recovery_assessment={"ph_trend": "toward_range", "ec_trend": "in_range", "history_is_fresh": True, "sufficient_observations": True, "executed_dose_in_window": False},
                summary="Claimed recovery.",
            ),
        )
        assert result.status == expected_status
        assert result.is_recovering is False


def test_monitoring_reconciles_conflicting_recovery_without_forcing_a_flag() -> None:
    """One correction is allowed; failure preserves a no-pump hold."""
    payload = SensorPayload(temperature=24.5, ph=5.3, ec=1.8, reservoir_volume_liters=70,
        ph_stable_for_seconds=30, ec_stable_for_seconds=30, control_strategy="agentic_ai")
    history = [_history_entry(minutes_ago=2, ph=5.2, ec=1.8, decision="ph_low")]
    state = {"payload":payload,"reference_range":REFERENCE_RANGE,"history":history,
        "baseline":evaluate_dosing_decision(payload,REFERENCE_RANGE),"llm_trace":[]}
    class ConflictingLLM:
        calls = 0
        def __init__(self, correct):
            self.correct = correct
        def complete_json(self, agent_name, prompt, context, model):
            self.calls += 1
            if self.calls == 2:
                assert "monitoring_review" in context
            return LLMCompletionResult(output=MonitoringResult(status="deviation", is_stable=True,
                is_recovering=self.correct and self.calls == 2,
                recovery_assessment={"ph_trend":"toward_range","ec_trend":"in_range",
                    "history_is_fresh":True,"sufficient_observations":True,"executed_dose_in_window":False},
                summary="pH moves toward range."), model="test-model")
    for correct in (True, False):
        llm = ConflictingLLM(correct)
        updates = _monitoring_node(state, llm)
        assert llm.calls == 2
        assert updates["monitoring"].is_recovering is correct
        assert updates["strategy"].action == "wait"
        assert updates["dose_plan"].pump_activated == "none"
        if not correct:
            assert "recovery_assessment_conflict" in updates["monitoring"].risk_flags


def test_monitoring_context_exposes_startup_hold_and_chronological_observations() -> None:
    payload = SensorPayload(temperature=24.5, ph=5.3, ec=1.8, reservoir_volume_liters=70,
        ph_stable_for_seconds=30, ec_stable_for_seconds=30, control_strategy="agentic_ai")
    history = [_history_entry(minutes_ago=30, ph=5.2, ec=1.8, decision="ph_low")]
    state = {"payload":payload,"reference_range":REFERENCE_RANGE,"history":history,
        "baseline":evaluate_dosing_decision(payload,REFERENCE_RANGE)}
    context = _monitoring_context(state)
    assert context["history_order"] == "oldest_to_newest"
    assert context["history_timing"]["newest_age_seconds"] >= 1800
    assert context["derived_context"]["initial_confirmation_reason"] is not None
    assert "baseline_shadow" not in context
    assert "calculate_bounded_dose" not in str(context)


def test_monitoring_llm_failure_keeps_deterministic_recovery_fallback() -> None:
    """Removing the LLM recovery flag must not break recovery on adapter failure."""
    class FailedMonitoringLLM(FakeAgenticLLM):
        def complete_json(self, agent_name, system_prompt, context, output_model):
            if output_model is MonitoringResult:
                raise TimeoutError("Monitoring unavailable")
            return super().complete_json(agent_name, system_prompt, context, output_model)

    payload = SensorPayload(
        temperature=24.5, ph=5.3, ec=1.8, reservoir_volume_liters=80,
        ph_stable_for_seconds=30, ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    history = [
        _history_entry(minutes_ago=4 - index, ph=ph, ec=1.8, decision="ph_low")
        for index, ph in enumerate([5.1, 5.15, 5.2, 5.25])
    ]
    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, history, llm=FailedMonitoringLLM())

    assert decision.decision == "wait_natural_recovery"
    assert decision.pump_activated == "none"
    trace = next(entry for entry in decision.metadata["llm_trace"] if entry["agent"] == "monitoring_agent")
    assert trace["source"] == "deterministic_fallback"


def test_agentic_does_not_wait_for_natural_recovery_with_short_trend() -> None:
    """Two historical points are not enough to classify natural recovery."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [
        _history_entry(minutes_ago=2, ph=5.1, ec=1.8, decision="ph_low"),
        _history_entry(minutes_ago=1, ph=5.2, ec=1.8, decision="ph_low"),
    ]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs)

    assert decision.decision == "ph_low"
    assert decision.pump_activated == "ph_up"
    assert decision.metadata["agentic_action"] == "dose"


def test_agentic_rejects_suspicious_sensor_jump() -> None:
    """Agentic AI rejects abrupt sensor jumps instead of commanding a pump."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [_history_entry(minutes_ago=10, ph=6.4, ec=1.8, decision="within_range")]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs)

    assert decision.decision == "sensor_anomaly"
    assert decision.pump_activated == "none"
    assert "sensor_anomaly" in decision.metadata["risk_flags"]


def test_agentic_accepts_expected_post_dose_ph_jump() -> None:
    """A large pH rise after pH-up is an actuator response, not a sensor anomaly."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.12,
        ec=1.8,
        reservoir_volume_liters=20,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [
        _history_entry(
            minutes_ago=4,
            ph=3.0,
            ec=1.8,
            decision="ph_low",
            pump_activated="ph_up",
            status="completed",
            duration_ms=4215,
        )
    ]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs)

    assert decision.decision == "ph_low"
    assert decision.pump_activated == "ph_up"
    assert decision.metadata["same_pump_response"]["interpretation"] == "previous_same_pump_still_unresolved"
    assert decision.metadata["same_pump_response"]["unresolved_same_pump_dose_count"] == 1
    assert decision.metadata["dose_adjustment_factor"] == 1.0
    assert decision.dose_ml == 10.0
    assert decision.duration_ms == 4959
    assert decision.metadata["mixing_window"]["effective_seconds"] == 150
    assert decision.metadata["agentic_action"] == "dose"
    assert "sensor_anomaly" not in decision.metadata.get("risk_flags", [])


def test_agentic_warns_repeat_dose_after_no_same_pump_response() -> None:
    """Little or no movement after a completed dose should log a delivery warning."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.0,
        ec=1.8,
        reservoir_volume_liters=20,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        ph_stability_threshold=0.03,
        ec_stability_threshold=0.03,
        control_strategy="agentic_ai",
    )
    recent_logs = [
        _history_entry(
            minutes_ago=4,
            ph=5.0,
            ec=1.8,
            decision="ph_low",
            pump_activated="ph_up",
            status="completed",
            dose_ml=10,
            duration_ms=4959,
        )
    ]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs)

    assert decision.decision == "ph_low"
    assert decision.pump_activated == "ph_up"
    assert decision.duration_ms > 0
    assert "possible_delivery_issue" in decision.metadata["risk_flags"]
    assert decision.metadata["delivery_issue"]["issue_detected"] is True
    assert decision.metadata["delivery_issue_policy"] == "warn_only"
    assert decision.metadata["delivery_issue"]["current_pump"] == "ph_up"
    assert decision.metadata["delivery_issue"]["metric"] == "ph"
    assert decision.metadata["delivery_issue"]["expected_direction_delta"] == 0.0


def test_unresolved_same_pump_factor_is_not_a_retry_count_ladder() -> None:
    """Fallback context keeps unresolved history without deciding factor by count."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.12,
        ec=1.8,
        reservoir_volume_liters=20,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )

    def unresolved_history(dose_count: int) -> list[SensorHistoryEntry]:
        return [
            _history_entry(
                minutes_ago=4 + (index * 4),
                ph=5.0,
                ec=1.8,
                decision="ph_low",
                pump_activated="ph_up",
                status="completed",
            )
            for index in range(dose_count)
        ]

    first = evaluate_agentic_decision(payload, REFERENCE_RANGE, unresolved_history(1))
    second = evaluate_agentic_decision(payload, REFERENCE_RANGE, unresolved_history(2))
    third = evaluate_agentic_decision(payload, REFERENCE_RANGE, unresolved_history(3))

    assert first.metadata["dose_adjustment_factor"] == 1.0
    assert second.metadata["dose_adjustment_factor"] == 1.0
    assert third.metadata["dose_adjustment_factor"] == 1.0
    assert first.dose_ml == second.dose_ml == third.dose_ml
    assert third.metadata["same_pump_response"]["unresolved_same_pump_dose_count"] == 3


def test_batch_control_history_strengthens_weak_ph_down_response_during_sizing() -> None:
    """A confirmed correction may adapt above the sizing preventive-dose cap."""
    payload = SensorPayload(
        temperature=27.0,
        ph=6.54,
        ec=1.45,
        reservoir_volume_liters=35,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [
        _history_entry(
            minutes_ago=1,
            ph=6.54,
            ec=1.45,
            decision="wait_initial_confirmation",
        )
    ]
    control_history = [
        _history_entry(
            minutes_ago=30,
            ph=6.54,
            ec=1.45,
            decision="ph_high",
            pump_activated="ph_down",
            status="completed",
            dose_ml=2.37,
            duration_ms=1175,
            decision_metadata={"agentic_dose_deviation": 0.09},
        ),
        _history_entry(minutes_ago=20, ph=6.50, ec=1.45, decision="within_range"),
        _history_entry(minutes_ago=10, ph=6.48, ec=1.45, decision="within_range"),
        _history_entry(minutes_ago=5, ph=6.49, ec=1.45, decision="within_range"),
        recent_logs[0],
    ]

    decision = evaluate_agentic_decision(
        payload,
        REFERENCE_RANGE,
        recent_logs,
        control_history=control_history,
        crop_lifecycle={
            "age_days": 21,
            "stage": "sizing",
            "stage_label": "Sizing / harvest prep",
        },
    )

    assert decision.decision == "ph_high"
    assert decision.pump_activated == "ph_down"
    assert decision.metadata["same_pump_response"]["available"] is True
    assert decision.metadata["same_pump_response"]["recommended_dose_factor"] == 1.25
    assert decision.metadata["crop_dosing_policy"]["max_dose_factor"] == 0.9
    assert decision.metadata["crop_dosing_policy"]["max_corrective_dose_factor"] == 1.25
    assert decision.metadata["dose_adjustment_factor"] == 1.25
    assert decision.dose_ml == 10.0
    assert decision.duration_ms == 4959
    assert decision.metadata["dose_before_factor_ml"] == 10.0
    assert decision.metadata["requested_dose_ml_before_cap"] == 13.42
    assert decision.metadata["reliable_pulse_floor_applied"] is False


def test_control_history_blocks_dosing_while_prior_batch_dose_is_mixing() -> None:
    """A synthetic batch dose must remain visible to deterministic mixing safety."""
    payload = SensorPayload(
        temperature=27.0,
        ph=6.54,
        ec=1.45,
        reservoir_volume_liters=35,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [
        _history_entry(
            minutes_ago=1,
            ph=6.54,
            ec=1.45,
            decision="wait_initial_confirmation",
        )
    ]
    control_history = [
        _history_entry(
            minutes_ago=1,
            ph=6.54,
            ec=1.45,
            decision="ph_high",
            pump_activated="ph_down",
            status="completed",
            dose_ml=2.37,
            duration_ms=1175,
            decision_metadata={"mixing_window": {"effective_seconds": 225}},
        )
    ]

    decision = evaluate_agentic_decision(
        payload,
        REFERENCE_RANGE,
        recent_logs,
        control_history=control_history,
    )

    assert decision.decision == "wait_for_mixing"
    assert decision.pump_activated == "none"
    assert "recent_dose_mixing" in decision.metadata["risk_flags"]


def test_agent_context_summarizes_control_history_without_replacing_sensor_history() -> None:
    """Agents get consistent dose flags while trends remain based on physical readings."""
    payload = SensorPayload(
        temperature=27.0,
        ph=6.54,
        ec=1.45,
        reservoir_volume_liters=35,
        control_strategy="agentic_ai",
    )
    baseline = evaluate_dosing_decision(payload, REFERENCE_RANGE)
    sensor_history = [
        _history_entry(minutes_ago=1, ph=6.54, ec=1.45, decision="ph_high")
    ]
    control_history = [
        _history_entry(
            minutes_ago=2,
            ph=6.54,
            ec=1.45,
            decision="ph_high",
            pump_activated="ph_down",
            status="completed",
            dose_ml=2.37,
            duration_ms=1175,
        ),
        *sensor_history,
    ]
    state = {
        "payload": payload,
        "reference_range": REFERENCE_RANGE,
        "history": sensor_history,
        "control_history": control_history,
        "baseline": baseline,
        "llm_trace": [],
        "crop_lifecycle": {},
    }

    context = _agent_context(state)

    assert context["derived_context"]["has_recent_dose"] is True
    assert context["derived_context"]["recent_dose_mixing_reason"] is not None
    assert context["recent_history"][0]["pump_activated"] == "none"
    assert "recent_control_history" not in context


def test_agentic_unresolved_history_can_apply_factor_above_previous_ladder_cap() -> None:
    """The dose planner may strengthen an unresolved follow-up above 1.25."""

    class StrongUnresolvedDoseLLM(FakeAgenticLLM):
        def complete_json(
            self,
            agent_name: str,
            system_prompt: str,
            context: dict,
            output_model: type[BaseModel],
        ) -> LLMCompletionResult:
            if output_model is DosePlanResult:
                return LLMCompletionResult(
                    output=DosePlanResult(
                        pump_activated="ph_up",
                        mixing_adjustment_factor=1.5,
                        dose_adjustment_factor=1.6,
                        reason=(
                            "Use a stronger adaptive pH-up follow-up because the previous "
                            "same-pump response remains unresolved."
                        ),
                    ),
                    model="gpt-4.1-mini",
                )
            return super().complete_json(agent_name, system_prompt, context, output_model)

    payload = SensorPayload(
        temperature=24.5,
        ph=5.12,
        ec=1.8,
        reservoir_volume_liters=20,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [
        _history_entry(
            minutes_ago=4,
            ph=5.0,
            ec=1.8,
            decision="ph_low",
            pump_activated="ph_up",
            status="completed",
        )
    ]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs, llm=StrongUnresolvedDoseLLM())

    assert decision.metadata["dose_adjustment_factor"] == 1.5682
    assert decision.metadata["dose_planning_agent"]["dose_adjustment_factor"] == 1.6
    assert decision.metadata["same_pump_response"]["unresolved_same_pump_dose_count"] == 1
    assert decision.dose_ml == REFERENCE_RANGE.ph_up_max_dose_ml_per_cycle
    assert "stronger adaptive pH-up follow-up" in decision.reason
    assert "severe" not in decision.reason.lower()


def test_agentic_factor_still_obeys_pump_dose_cap() -> None:
    """A stronger planned factor still cannot exceed final pump safety bounds."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.12,
        ec=1.8,
        reservoir_volume_liters=20,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        ph_stability_threshold=0.03,
        ec_stability_threshold=0.03,
        control_strategy="agentic_ai",
    )
    recent_logs = [
        _history_entry(
            minutes_ago=4,
            ph=5.0,
            ec=1.8,
            decision="ph_low",
            pump_activated="ph_up",
            status="completed",
        )
    ]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs)

    assert decision.dose_ml <= REFERENCE_RANGE.ph_up_max_dose_ml_per_cycle
    assert decision.metadata["agentic_target_inward_margin"] == 0.5
    assert decision.metadata["agentic_target_value"] == 6.0
    assert decision.metadata["same_pump_response"]["target_range_headroom_factor"] >= 1.0


def test_same_pump_context_marks_previous_cap_as_unresolved() -> None:
    """The planner sees when a capped previous dose still did not resolve deviation."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.12,
        ec=1.8,
        reservoir_volume_liters=20,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [
        _history_entry(
            minutes_ago=4,
            ph=4.8,
            ec=1.8,
            decision="ph_low",
            pump_activated="ph_up",
            status="completed",
            dose_ml=10,
        )
    ]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs)

    assert decision.metadata["same_pump_response"]["latest_same_pump_hit_pump_cap"] is True
    assert decision.metadata["same_pump_response"]["latest_same_pump_hit_cap_and_unresolved"] is True
    assert decision.metadata["same_pump_response"]["recommended_dose_factor"] == 1.0
    assert decision.metadata["same_pump_response"]["fallback_reference_factor"] == 1.0
    assert "hit its cycle cap and remains unresolved" in decision.reason


def test_agentic_factor_is_bounded_by_target_range_headroom() -> None:
    """A strong LLM follow-up factor cannot exceed calibrated range headroom."""

    class StrongEcUpDoseLLM(FakeAgenticLLM):
        def complete_json(
            self,
            agent_name: str,
            system_prompt: str,
            context: dict,
            output_model: type[BaseModel],
        ) -> LLMCompletionResult:
            if output_model is DosePlanResult:
                return LLMCompletionResult(
                    output=DosePlanResult(
                        pump_activated="ec_up",
                        mixing_adjustment_factor=1.5,
                        dose_adjustment_factor=2.0,
                        reason="Use a strong unresolved EC-up correction.",
                    ),
                    model="gpt-4.1-mini",
                )
            return super().complete_json(agent_name, system_prompt, context, output_model)

    payload = SensorPayload(
        temperature=24.5,
        ph=5.8,
        ec=0.4,
        reservoir_volume_liters=5,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        ph_stability_threshold=0.03,
        ec_stability_threshold=0.03,
        control_strategy="agentic_ai",
    )
    recent_logs = [_history_entry(minutes_ago=1, ph=5.8, ec=0.4, decision="wait_initial_confirmation")]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs, llm=StrongEcUpDoseLLM())

    assert decision.decision == "ec_low"
    assert decision.pump_activated == "ec_up"
    assert decision.metadata["requested_dose_adjustment_factor"] == 2.0
    assert decision.metadata["dose_adjustment_factor"] == decision.metadata["target_range_factor_cap"]
    assert decision.metadata["dose_adjustment_factor"] < 2.0
    assert decision.dose_ml <= round(
        decision.metadata["dose_before_factor_ml"] * decision.metadata["target_range_factor_cap"],
        2,
    )


def test_agentic_graph_accepts_llm_agent_outputs_before_safety_gate() -> None:
    """LLM agent output can steer dose planning while Python still finalizes safely."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )

    recent_logs = [_history_entry(minutes_ago=5, ph=5.3, ec=1.8, decision="wait_initial_confirmation")]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs, llm=FakeAgenticLLM())

    assert decision.pump_activated == "ph_up"
    assert decision.dose_ml == 4.0
    assert decision.duration_ms == 1983
    assert decision.metadata["dose_adjustment_factor"] == 0.4
    assert decision.metadata["dose_factor_source"] == "dose_planning_agent_bounded_by_safety_gate"
    assert decision.metadata["decision_agent"]["action"] == "dose"
    assert decision.metadata["dose_planning_agent"]["dose_adjustment_factor"] == 0.4
    assert decision.metadata["reasoning_source"] == "llm_agents"
    assert decision.metadata["llm_enabled"] is True
    assert decision.metadata["llm_models_used"] == ["gpt-4.1-mini"]
    assert len(decision.metadata["llm_trace"]) == 5
    assert {entry["source"] for entry in decision.metadata["llm_trace"]} == {"llm"}
    assert {
        entry["model"]
        for entry in decision.metadata["llm_trace"]
        if entry["source"] == "llm"
    } == {"gpt-4.1-mini"}


def test_agentic_graph_rejects_unjustified_llm_wait_on_stable_deviation() -> None:
    """A stable non-recovering deviation should not wait just because the LLM says so."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )

    recent_logs = [_history_entry(minutes_ago=5, ph=5.3, ec=1.8, decision="wait_initial_confirmation")]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs, llm=UnjustifiedWaitLLM())

    assert decision.decision == "ph_low"
    assert decision.pump_activated == "ph_up"
    assert decision.metadata["decision_agent"]["action"] == "dose"


def test_agentic_skips_dose_planning_when_decision_agent_waits() -> None:
    """Dose Planning Agent should only run when Decision Agent selects dosing."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [_history_entry(minutes_ago=5, ph=5.3, ec=1.8, decision="wait_initial_confirmation")]
    llm = CautiousWaitLLM()

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs, llm=llm)

    assert decision.pump_activated == "none"
    assert decision.metadata["decision_agent"]["action"] == "wait"
    assert decision.metadata["dose_planning_agent"]["pump_activated"] == "none"
    assert "dose_planning_agent" in decision.metadata["orchestrator_agent"]["skipped_agents"]
    assert "Dose Planning Agent" not in llm.calls


def test_agentic_graph_removes_unconfirmed_hard_risk_flags_from_llm_output() -> None:
    """LLM monitoring risk flags should match deterministic safety checks."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [_history_entry(minutes_ago=5, ph=5.3, ec=1.8, decision="wait_initial_confirmation")]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, llm=MisleadingMonitoringLLM(), recent_logs=recent_logs)

    assert decision.decision == "ph_low"
    assert decision.pump_activated == "ph_up"
    assert decision.metadata["monitoring_agent"]["status"] == "deviation"
    assert decision.metadata["monitoring_agent"]["is_stable"] is True
    assert "unstable_reading" not in decision.metadata["monitoring_agent"]["risk_flags"]
    assert decision.metadata["monitoring_agent"]["summary"] == (
        "pH is below target; EC is within range; readings are stable and not yet recovering."
    )


def test_agentic_graph_forces_llm_outputs_to_match_in_range_payload() -> None:
    """Contradictory LLM outputs are normalized before they reach stored metadata."""
    payload = SensorPayload(
        temperature=25,
        ph=6.2,
        ec=1.8,
        reservoir_volume_liters=80,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, [], llm=ContradictoryInRangeLLM())

    assert decision.decision == "within_range"
    assert decision.pump_activated == "none"
    assert decision.duration_ms == 0
    assert decision.metadata["monitoring_agent"]["status"] == "in_range"
    assert decision.metadata["diagnostic_reasoning_agent"]["classification"] == "within_range"
    assert decision.metadata["diagnostic_reasoning_agent"]["primary_metric"] == "none"
    assert decision.metadata["decision_agent"]["action"] == "no_action"
    assert decision.metadata["dose_planning_agent"]["pump_activated"] == "none"


def test_agentic_graph_rejects_false_combined_diagnosis_for_single_metric_deviation() -> None:
    """A hallucinated combined diagnosis is normalized when only EC is out of range."""
    payload = SensorPayload(
        temperature=25,
        ph=6.0,
        ec=2.3,
        reservoir_volume_liters=20,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [_history_entry(minutes_ago=5, ph=6.0, ec=2.3, decision="wait_initial_confirmation")]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs, llm=FalseCombinedLLM())

    assert decision.decision == "ec_high"
    assert decision.pump_activated == "ec_down"
    assert decision.metadata["combined_disturbance"] is False
    assert decision.metadata["diagnostic_reasoning_agent"]["classification"] == "ec_high"
    assert decision.metadata["diagnostic_reasoning_agent"]["primary_metric"] == "ec"


def test_agentic_ec_high_targets_midpoint_without_bypassing_ec_down_cap() -> None:
    """Mild EC-high should target midpoint while respecting the dilution cap."""
    payload = SensorPayload(
        temperature=25,
        ph=6.0,
        ec=2.1,
        reservoir_volume_liters=20,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [_history_entry(minutes_ago=1, ph=6.0, ec=2.1, decision="wait_initial_confirmation")]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs)

    assert decision.decision == "ec_high"
    assert decision.pump_activated == "ec_down"
    assert decision.dose_ml == 2000.0
    assert decision.duration_ms == 960000
    assert decision.dose_ml == REFERENCE_RANGE.ec_down_max_dose_ml_per_cycle
    assert decision.metadata["agentic_primary_shadow"]["dose_ml"] == 2000.0
    assert decision.metadata["agentic_target_value"] == 1.6
    assert decision.metadata["agentic_target_policy"] == "midpoint_single_correction_capped"
    assert decision.metadata["safety_bounds"]["pump_max_dose_ml_per_cycle"] == (
        REFERENCE_RANGE.ec_down_max_dose_ml_per_cycle
    )
    assert decision.metadata["safety_bounds"]["pump_max_duration_ms"] == REFERENCE_RANGE.ec_down_max_duration_ms
    assert decision.metadata["mixing_window"]["effective_seconds"] == 180
    assert decision.metadata["dose_adjustment_factor"] == 1.0


def test_long_ec_down_completion_keeps_followup_history_fresh() -> None:
    """Long EC-down runtime should not make its post-dose follow-up look like startup."""
    payload = SensorPayload(
        temperature=30.12,
        ph=6.15,
        ec=2.297,
        reservoir_volume_liters=20,
        ph_stable_for_seconds=31,
        ec_stable_for_seconds=31,
        control_strategy="agentic_ai",
    )
    recent_logs = [
        SensorHistoryEntry(
            timestamp=datetime.now(timezone.utc) - timedelta(minutes=17),
            action_completed_at=datetime.now(timezone.utc) - timedelta(minutes=4),
            ph=6.15,
            ec=2.35,
            decision="ec_high",
            pump_activated="ec_down",
            dose_ml=1700,
            duration_ms=816_000,
            status="completed",
            decision_metadata={"mixing_window": {"effective_seconds": 180}},
        )
    ]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs)

    assert decision.decision == "ec_high"
    assert decision.pump_activated == "ec_down"
    assert decision.metadata["same_pump_response"]["interpretation"] == "previous_same_pump_still_unresolved"
    assert decision.metadata["same_pump_response"]["unresolved_same_pump_dose_count"] == 1
    assert "initial_confirmation" not in decision.metadata.get("risk_flags", [])


def test_unresolved_ec_down_history_preserves_agent_factor_inside_safety_bounds() -> None:
    """Unresolved EC-down history informs the agent without forcing a 1.0 floor."""

    class CautiousEcDownLLM(FakeAgenticLLM):
        def complete_json(
            self,
            agent_name: str,
            system_prompt: str,
            context: dict,
            output_model: type[BaseModel],
        ) -> LLMCompletionResult:
            if output_model is DiagnosticResult:
                return LLMCompletionResult(
                    output=DiagnosticResult(
                        classification="ec_high",
                        primary_metric="ec",
                        summary="EC remains above target after EC-down correction.",
                    ),
                    model="gpt-4.1-nano",
                )
            if output_model is DosePlanResult:
                return LLMCompletionResult(
                    output=DosePlanResult(
                        pump_activated="ec_down",
                        mixing_adjustment_factor=1.5,
                        dose_adjustment_factor=0.85,
                        reason="Use a cautious EC dilution correction.",
                    ),
                    model="gpt-4.1-nano",
                )
            return super().complete_json(agent_name, system_prompt, context, output_model)

    payload = SensorPayload(
        temperature=30.12,
        ph=6.16,
        ec=2.299,
        reservoir_volume_liters=20,
        ph_stable_for_seconds=31,
        ec_stable_for_seconds=31,
        control_strategy="agentic_ai",
    )
    recent_logs = [
        SensorHistoryEntry(
            timestamp=datetime.now(timezone.utc) - timedelta(minutes=17),
            action_completed_at=datetime.now(timezone.utc) - timedelta(minutes=4),
            ph=6.15,
            ec=2.35,
            decision="ec_high",
            pump_activated="ec_down",
            dose_ml=1700,
            duration_ms=816_000,
            status="completed",
            decision_metadata={"mixing_window": {"effective_seconds": 180}},
        )
    ]

    decision = evaluate_agentic_decision(
        payload,
        REFERENCE_RANGE,
        recent_logs,
        llm=CautiousEcDownLLM(),
    )

    assert decision.decision == "ec_high"
    assert decision.pump_activated == "ec_down"
    assert decision.metadata["same_pump_response"]["interpretation"] == (
        "previous_same_pump_still_unresolved"
    )
    assert decision.metadata["same_pump_response"]["recommended_dose_factor"] == 1.0
    assert decision.metadata["dose_planning_agent"]["dose_adjustment_factor"] == 0.85
    assert decision.metadata["dose_adjustment_factor"] == 0.85
    assert decision.dose_ml == 1700
    assert decision.duration_ms == 816_000


def test_agentic_llm_context_includes_crop_and_hydroponic_system() -> None:
    """LLM agent context includes the crop and DFT hydroponic system metadata."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=20,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [_history_entry(minutes_ago=5, ph=5.3, ec=1.8, decision="wait_initial_confirmation")]
    llm = ContextCapturingLLM()

    evaluate_agentic_decision(
        payload,
        REFERENCE_RANGE,
        recent_logs,
        llm=llm,
        crop_lifecycle={
            "age_days": 3,
            "stage": "establishment",
            "stage_label": "Establishment",
            "harvest_start_day": 30,
            "harvest_end_day": 35,
        },
    )

    assert llm.contexts
    first_context = llm.contexts[0]
    reference_context = first_context["reference_range"]
    assert reference_context["crop_type"] == "Lettuce"
    assert reference_context["hydroponic_system_type"] == "DFT"
    assert "Vegetative" in reference_context["growth_stage"]
    assert first_context["crop_lifecycle"]["stage"] == "establishment"
    assert first_context["derived_context"]["crop_dosing_policy"]["stage"] == "establishment"
    assert first_context["derived_context"]["crop_dosing_policy"]["max_dose_factor"] == 0.65
    assert first_context["derived_context"]["crop_dosing_policy"]["preventive_dosing_allowed"] is False


def test_consistency_review_blocks_agent_outputs_that_conflict_with_payload() -> None:
    """The pre-safety review blocks contradictory agent state before actuation."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=20,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    baseline = evaluate_dosing_decision(payload, REFERENCE_RANGE)
    state = {
        "payload": payload,
        "reference_range": REFERENCE_RANGE,
        "history": [],
        "baseline": baseline,
        "orchestration": {
            "route": "monitoring_agent",
            "skipped_agents": [],
        },
        "monitoring": MonitoringResult(
            status="deviation",
            is_stable=True,
            summary="Stable deviation is present.",
        ),
        "diagnosis": DiagnosticResult(
            classification="ec_high",
            primary_metric="ec",
            summary="Incorrectly classifies the pH-low payload as EC high.",
        ),
        "strategy": StrategyResult(
            action="dose",
            confidence=0.82,
            reason="Dose despite contradictory diagnosis.",
        ),
        "dose_plan": DosePlanResult(
            pump_activated="ph_up",
            dose_adjustment_factor=0.85,
            reason="Pump matches baseline, but diagnosis does not match payload.",
        ),
        "llm_trace": [],
    }

    review = _consistency_review(state)

    assert review["review_status"] == "block"
    assert "diagnosis_conflicts_with_payload_range_check" in review["issues"]
    assert review["recommended_action"] == "wait"


def test_agentic_metadata_is_json_serializable_with_same_pump_history() -> None:
    """Decision metadata must be safe for Postgres JSONB storage."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=20,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [
        _history_entry(
            minutes_ago=5,
            ph=5.2,
            ec=1.8,
            decision="ph_low",
            pump_activated="ph_up",
            status="completed",
        )
    ]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs)

    json.dumps(decision.metadata)
    assert isinstance(decision.metadata["same_pump_response"]["latest_same_pump_timestamp"], str)


def test_same_pump_history_followed_by_within_range_is_treated_as_resolved() -> None:
    """A new disturbance after successful recovery should remain cautious."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.19,
        ec=1.8,
        reservoir_volume_liters=20,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [
        _history_entry(minutes_ago=1, ph=5.19, ec=1.8, decision="wait_initial_confirmation"),
        _history_entry(minutes_ago=5, ph=5.6, ec=1.8, decision="within_range"),
        _history_entry(
            minutes_ago=20,
            ph=4.9,
            ec=1.8,
            decision="ph_low",
            pump_activated="ph_up",
            status="completed",
        ),
    ]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs)

    assert decision.decision == "ph_low"
    assert decision.pump_activated == "ph_up"
    assert decision.dose_ml == 10.0
    assert decision.metadata["dose_adjustment_factor"] == 1.0
    assert decision.metadata["same_pump_response"]["interpretation"] == (
        "previous_same_pump_resolved_or_changed_condition"
    )
    assert "treat this as a new confirmed disturbance" in decision.reason


def test_llm_same_pump_reason_is_sanitized_to_match_resolved_history() -> None:
    """Persisted LLM reasons must not contradict deterministic same-pump context."""

    class MisleadingResolvedHistoryLLM(FakeAgenticLLM):
        def complete_json(
            self,
            agent_name: str,
            system_prompt: str,
            context: dict,
            output_model: type[BaseModel],
        ) -> LLMCompletionResult:
            if output_model is MonitoringResult:
                return LLMCompletionResult(
                    output=MonitoringResult(
                        status="deviation",
                        is_stable=True,
                        risk_flags=["initial_confirmation"],
                        summary="pH is below target; stale confirmation flag remained.",
                    ),
                    model="gpt-4.1-nano",
                )
            if output_model is DiagnosticResult:
                return LLMCompletionResult(
                    output=DiagnosticResult(
                        classification="ph_low",
                        primary_metric="ph",
                        summary="Wrongly says pH 4.73 and a dose is underway.",
                    ),
                    model="gpt-4.1-nano",
                )
            if output_model is DosePlanResult:
                return LLMCompletionResult(
                    output=DosePlanResult(
                        pump_activated="ph_up",
                        dose_adjustment_factor=0.85,
                        mixing_adjustment_factor=1.25,
                        reason=(
                            "Use a cautious adaptive pH-up correction because no recent "
                            "same-pump response is available in the fresh agentic history."
                        ),
                    ),
                    model="gpt-4.1-nano",
                )
            return super().complete_json(agent_name, system_prompt, context, output_model)

    payload = SensorPayload(
        temperature=24.5,
        ph=5.42,
        ec=1.8,
        reservoir_volume_liters=20,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [
        _history_entry(minutes_ago=1, ph=5.42, ec=1.8, decision="wait_initial_confirmation"),
        _history_entry(minutes_ago=5, ph=5.5, ec=1.8, decision="within_range"),
        _history_entry(
            minutes_ago=20,
            ph=5.26,
            ec=1.8,
            decision="ph_low",
            pump_activated="ph_up",
            status="completed",
        ),
    ]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs, llm=MisleadingResolvedHistoryLLM())

    assert decision.decision == "ph_low"
    assert decision.pump_activated == "ph_up"
    assert decision.dose_ml > 0
    assert decision.duration_ms > 0
    assert "4.73" not in decision.metadata["diagnostic_reasoning_agent"]["summary"]
    assert "dose" not in decision.metadata["diagnostic_reasoning_agent"]["summary"].lower()


def test_reduced_factor_reason_does_not_claim_full_midpoint_correction() -> None:
    """A reduced factor should be described as reduced, not as a full midpoint dose."""

    class ReducedMidpointReasonLLM(FakeAgenticLLM):
        def complete_json(
            self,
            agent_name: str,
            system_prompt: str,
            context: dict,
            output_model: type[BaseModel],
        ) -> LLMCompletionResult:
            if output_model is DiagnosticResult:
                return LLMCompletionResult(
                    output=DiagnosticResult(
                        classification="ec_high",
                        primary_metric="ec",
                        summary="EC remains above target while pH is within range.",
                    ),
                    model="gpt-4.1-nano",
                )
            if output_model is DosePlanResult:
                return LLMCompletionResult(
                    output=DosePlanResult(
                        pump_activated="ec_down",
                        dose_adjustment_factor=0.85,
                        mixing_adjustment_factor=1.5,
                        reason=(
                            "Use the bounded EC-down midpoint correction because the "
                            "previous same-pump correction returned to range or the "
                            "condition changed; treat this as a new confirmed disturbance."
                        ),
                    ),
                    model="gpt-4.1-nano",
                )
            return super().complete_json(agent_name, system_prompt, context, output_model)

    payload = SensorPayload(
        temperature=30.44,
        ph=5.92,
        ec=2.766,
        reservoir_volume_liters=20,
        ph_stable_for_seconds=31,
        ec_stable_for_seconds=31,
        control_strategy="agentic_ai",
    )
    recent_logs = [
        _history_entry(minutes_ago=1, ph=5.92, ec=2.766, decision="wait_initial_confirmation"),
        _history_entry(minutes_ago=5, ph=5.9, ec=1.8, decision="within_range"),
        SensorHistoryEntry(
            timestamp=datetime.now(timezone.utc) - timedelta(minutes=30),
            action_completed_at=datetime.now(timezone.utc) - timedelta(minutes=10),
            ph=5.9,
            ec=2.4,
            decision="ec_high",
            pump_activated="ec_down",
            dose_ml=2000,
            duration_ms=960_000,
            status="completed",
            decision_metadata={},
        ),
    ]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs, llm=ReducedMidpointReasonLLM())

    assert decision.decision == "ec_high"
    assert decision.pump_activated == "ec_down"
    assert decision.metadata["dose_adjustment_factor"] == 0.85
    assert "reduced dose factor 0.85" in decision.reason
    assert "bounded EC-down midpoint correction" not in decision.reason


def test_mild_ec_high_reason_does_not_claim_severe_deviation() -> None:
    """Reason text should not call a near-boundary EC-high reading severe."""

    class SevereReasonForMildEcLLM(FakeAgenticLLM):
        def complete_json(
            self,
            agent_name: str,
            system_prompt: str,
            context: dict,
            output_model: type[BaseModel],
        ) -> LLMCompletionResult:
            if output_model is DiagnosticResult:
                return LLMCompletionResult(
                    output=DiagnosticResult(
                        classification="ec_high",
                        primary_metric="ec",
                        summary="EC remains slightly above target while pH is within range.",
                    ),
                    model="gpt-4.1-nano",
                )
            if output_model is DosePlanResult:
                return LLMCompletionResult(
                    output=DosePlanResult(
                        pump_activated="ec_down",
                        dose_adjustment_factor=1.0,
                        mixing_adjustment_factor=1.25,
                        reason=(
                            "Use the bounded EC dilution midpoint correction because the "
                            "current deviation remains severe and the previous correction "
                            "hit the pump cap; remeasure after mixing."
                        ),
                    ),
                    model="gpt-4.1-nano",
                )
            return super().complete_json(agent_name, system_prompt, context, output_model)

    payload = SensorPayload(
        temperature=32.69,
        ph=5.66,
        ec=2.09,
        reservoir_volume_liters=20,
        ph_stable_for_seconds=31,
        ec_stable_for_seconds=31,
        control_strategy="agentic_ai",
    )
    recent_logs = [
        _history_entry(minutes_ago=1, ph=5.64, ec=2.087, decision="wait_initial_confirmation"),
        _history_entry(minutes_ago=8, ph=6.11, ec=1.867, decision="within_range"),
        SensorHistoryEntry(
            timestamp=datetime.now(timezone.utc) - timedelta(minutes=35),
            action_completed_at=datetime.now(timezone.utc) - timedelta(minutes=30),
            ph=5.92,
            ec=2.016,
            decision="ec_high",
            pump_activated="ec_down",
            dose_ml=2000,
            duration_ms=960_000,
            status="completed",
            decision_metadata={},
        ),
    ]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs, llm=SevereReasonForMildEcLLM())

    assert decision.decision == "ec_high"
    assert decision.pump_activated == "ec_down"
    assert decision.dose_ml == 2000.0
    assert decision.dose_ml == REFERENCE_RANGE.ec_down_max_dose_ml_per_cycle
    assert decision.metadata["dose_adjustment_factor"] == 1.0
    assert "severe" not in decision.reason.lower()
    assert "new confirmed disturbance" in decision.reason
    assert "midpoint-directed correction" in decision.reason


def test_mild_ph_high_reason_does_not_claim_severe_deviation() -> None:
    """Reason text should not call a mild pH-high reading severe."""

    class SevereReasonForMildPhLLM(FakeAgenticLLM):
        def complete_json(
            self,
            agent_name: str,
            system_prompt: str,
            context: dict,
            output_model: type[BaseModel],
        ) -> LLMCompletionResult:
            if output_model is DiagnosticResult:
                return LLMCompletionResult(
                    output=DiagnosticResult(
                        classification="ph_high",
                        primary_metric="ph",
                        summary="pH is above target while EC remains in range.",
                    ),
                    model="gpt-4.1-nano",
                )
            if output_model is DosePlanResult:
                return LLMCompletionResult(
                    output=DosePlanResult(
                        pump_activated="ph_down",
                        dose_adjustment_factor=1.0,
                        mixing_adjustment_factor=1.25,
                        reason=(
                            "Use the bounded pH-down midpoint correction because the "
                            "current deviation remains severe; remeasure after mixing."
                        ),
                    ),
                    model="gpt-4.1-nano",
                )
            return super().complete_json(agent_name, system_prompt, context, output_model)

    payload = SensorPayload(
        temperature=30.81,
        ph=6.64,
        ec=1.605,
        reservoir_volume_liters=20,
        ph_stable_for_seconds=31,
        ec_stable_for_seconds=31,
        control_strategy="agentic_ai",
    )
    recent_logs = [_history_entry(minutes_ago=1, ph=6.64, ec=1.605, decision="wait_initial_confirmation")]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs, llm=SevereReasonForMildPhLLM())

    assert decision.decision == "ph_high"
    assert decision.pump_activated == "ph_down"
    assert decision.ph_deviation == 0.14
    assert "severe" not in decision.reason.lower()
    assert "pH-down midpoint-directed correction" in decision.reason


def test_wrong_llm_pump_keeps_dose_plan_reason_aligned_with_final_reason() -> None:
    """If the LLM chooses the wrong pump, metadata should still match final decision."""

    class WrongPumpLLM(FakeAgenticLLM):
        def complete_json(
            self,
            agent_name: str,
            system_prompt: str,
            context: dict,
            output_model: type[BaseModel],
        ) -> LLMCompletionResult:
            if output_model is DosePlanResult:
                return LLMCompletionResult(
                    output=DosePlanResult(
                        pump_activated="ec_down",
                        dose_adjustment_factor=1.0,
                        mixing_adjustment_factor=2.5,
                        reason="Wrong pump selected by the LLM.",
                    ),
                    model="gpt-4.1-nano",
                )
            return super().complete_json(agent_name, system_prompt, context, output_model)

    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=20,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [_history_entry(minutes_ago=1, ph=5.3, ec=1.8, decision="wait_initial_confirmation")]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs, llm=WrongPumpLLM())

    assert decision.pump_activated == "ph_up"
    assert decision.metadata["dose_planning_agent"]["pump_activated"] == "ph_up"
    assert decision.metadata["dose_planning_agent"]["reason"] == decision.reason
    assert decision.metadata["mixing_window"]["reason"] == decision.reason


def test_combined_disturbance_reason_uses_final_selected_pump() -> None:
    """Combined pH/EC metadata must describe the pump actually selected for the cycle."""
    payload = SensorPayload(
        temperature=24.5,
        ph=5.2,
        ec=2.3,
        reservoir_volume_liters=20,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )
    recent_logs = [_history_entry(minutes_ago=1, ph=5.2, ec=2.3, decision="wait_initial_confirmation")]

    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, recent_logs, llm=FakeAgenticLLM())

    assert decision.decision == "ec_high"
    assert decision.pump_activated == "ec_down"
    assert "EC-down" in decision.reason
    assert "pH-up" not in decision.reason
    assert decision.metadata["same_pump_response"]["reason"] == (
        "No recent same-pump dose exists inside the fresh history window."
    )
    assert decision.metadata["dose_planning_agent"]["reason"] == decision.reason
    assert decision.metadata["mixing_window"]["reason"] == decision.reason


def test_combined_disturbance_reason_rejects_wrong_priority_story() -> None:
    """A combined-disturbance reason must explain the selected primary correction."""

    class WrongCombinedPriorityReasonLLM(FakeAgenticLLM):
        def complete_json(
            self,
            agent_name: str,
            system_prompt: str,
            context: dict,
            output_model: type[BaseModel],
        ) -> LLMCompletionResult:
            if output_model is DiagnosticResult:
                return LLMCompletionResult(
                    output=DiagnosticResult(
                        classification="combined_disturbance",
                        primary_metric="ec",
                        summary="Both pH and EC are outside target range.",
                    ),
                    model="gpt-4.1-nano",
                )
            if output_model is DosePlanResult:
                return LLMCompletionResult(
                    output=DosePlanResult(
                        pump_activated="ec_up",
                        dose_adjustment_factor=1.0,
                        mixing_adjustment_factor=1.5,
                        reason=(
                            "Combined disturbance detected; pH should be corrected first "
                            "while the EC correction is deferred."
                        ),
                    ),
                    model="gpt-4.1-nano",
                )
            return super().complete_json(agent_name, system_prompt, context, output_model)

    payload = SensorPayload(
        temperature=30.69,
        ph=6.86,
        ec=0.881,
        reservoir_volume_liters=20,
        ph_stable_for_seconds=31,
        ec_stable_for_seconds=54,
        control_strategy="agentic_ai",
    )
    recent_logs = [_history_entry(minutes_ago=1, ph=6.87, ec=0.882, decision="wait_initial_confirmation")]

    decision = evaluate_agentic_decision(
        payload,
        REFERENCE_RANGE,
        recent_logs,
        llm=WrongCombinedPriorityReasonLLM(),
    )

    assert decision.decision == "ec_low"
    assert decision.pump_activated == "ec_up"
    assert "EC is corrected first" in decision.reason
    assert "pH should be corrected first" not in decision.reason
    assert "defer the other out-of-range metric" in decision.reason


def test_combined_disturbance_reason_includes_non_default_dose_factor() -> None:
    """Combined-disturbance reasons should not hide reduced or stronger factors."""

    class ReducedCombinedDoseLLM(FakeAgenticLLM):
        def complete_json(
            self,
            agent_name: str,
            system_prompt: str,
            context: dict,
            output_model: type[BaseModel],
        ) -> LLMCompletionResult:
            if output_model is DiagnosticResult:
                return LLMCompletionResult(
                    output=DiagnosticResult(
                        classification="combined_disturbance",
                        primary_metric="ec",
                        summary="Both pH and EC are outside target range.",
                    ),
                    model="gpt-4.1-nano",
                )
            if output_model is DosePlanResult:
                return LLMCompletionResult(
                    output=DosePlanResult(
                        pump_activated="ec_up",
                        dose_adjustment_factor=0.85,
                        mixing_adjustment_factor=1.5,
                        reason="Use a cautious combined-disturbance EC-up correction.",
                    ),
                    model="gpt-4.1-nano",
                )
            return super().complete_json(agent_name, system_prompt, context, output_model)

    payload = SensorPayload(
        temperature=30.69,
        ph=6.86,
        ec=0.881,
        reservoir_volume_liters=20,
        ph_stable_for_seconds=31,
        ec_stable_for_seconds=54,
        control_strategy="agentic_ai",
    )
    recent_logs = [_history_entry(minutes_ago=1, ph=6.87, ec=0.882, decision="wait_initial_confirmation")]

    decision = evaluate_agentic_decision(
        payload,
        REFERENCE_RANGE,
        recent_logs,
        llm=ReducedCombinedDoseLLM(),
    )

    assert decision.decision == "ec_low"
    assert decision.pump_activated == "ec_up"
    assert decision.metadata["dose_adjustment_factor"] == 0.85
    assert "Use dose factor 0.85" in decision.reason
    assert "EC is corrected first" in decision.reason
    assert "defer the other out-of-range metric" in decision.reason


class FakeAgenticLLM:
    """Fake LLM adapter that returns valid structured outputs for graph nodes."""

    def complete_json(
        self,
        agent_name: str,
        system_prompt: str,
        context: dict,
        output_model: type[BaseModel],
    ) -> LLMCompletionResult:
        """Return deterministic structured agent outputs for tests."""
        if output_model is OrchestratorResult:
            output = OrchestratorResult(
                route="monitoring_agent",
                action="proceed",
                confidence=0.84,
                reason="Stable deviation requires full agentic reasoning.",
            )
        elif output_model is MonitoringResult:
            output = MonitoringResult(
                status="deviation",
                is_stable=True,
                summary="Stable pH deviation remains.",
            )
        elif output_model is StrategyResult:
            output = StrategyResult(
                action="dose",
                confidence=0.86,
                reason="Dose cautiously because pH remains low.",
            )
        elif output_model is DosePlanResult:
            output = DosePlanResult(
                pump_activated="ph_up",
                dose_adjustment_factor=0.4,
                reason="Use the minimum agentic correction to reduce overshoot.",
            )
        else:
            output = output_model.model_validate(
                {
                    "classification": "ph_low",
                    "primary_metric": "ph",
                    "summary": "pH is below target.",
                }
            )

        return LLMCompletionResult(output=output, model="gpt-4.1-mini")


class ContextCapturingLLM(FakeAgenticLLM):
    """Fake LLM that stores every prompt context it receives."""

    def __init__(self) -> None:
        self.contexts: list[dict] = []

    def complete_json(
        self,
        agent_name: str,
        system_prompt: str,
        context: dict,
        output_model: type[BaseModel],
    ) -> LLMCompletionResult:
        self.contexts.append(context)
        return super().complete_json(agent_name, system_prompt, context, output_model)


class ContradictoryInRangeLLM:
    """Fake LLM that hallucinates a high-pH correction for an in-range payload."""

    def complete_json(
        self,
        agent_name: str,
        system_prompt: str,
        context: dict,
        output_model: type[BaseModel],
    ) -> LLMCompletionResult:
        """Return contradictory outputs similar to a weak-model hallucination."""
        if output_model is MonitoringResult:
            output = MonitoringResult(
                status="in_range",
                is_stable=True,
                summary="pH and EC are within target range and stable.",
            )
        elif output_model is DiagnosticResult:
            output = DiagnosticResult(
                classification="ph_high",
                primary_metric="ph",
                summary="The latest pH reading is 6.6 and exceeds target.",
            )
        elif output_model is StrategyResult:
            output = StrategyResult(
                action="wait",
                confidence=0.9,
                reason="No immediate need for dosing.",
            )
        else:
            output = DosePlanResult(
                pump_activated="ph_down",
                dose_adjustment_factor=0.65,
                reason="Use pH down to correct high pH.",
            )

        return LLMCompletionResult(output=output, model="gpt-4.1-nano")


class MonitoringProjectedDeviationLLM(FakeAgenticLLM):
    """Fake LLM where Monitoring is the source of a proactive projected deviation."""

    def complete_json(
        self,
        agent_name: str,
        system_prompt: str,
        context: dict,
        output_model: type[BaseModel],
    ) -> LLMCompletionResult:
        if output_model is MonitoringResult:
            output = MonitoringResult(
                status="projected_deviation",
                is_stable=True,
                is_recovering=False,
                risk_flags=["projected_out_of_range"],
                summary=(
                    "pH remains in range, but recent history suggests an upper-boundary "
                    "crossing before the next cycle."
                ),
                projected_decision="ph_high",
                projected_metric="ph",
                projected_pump_activated="ph_down",
                projected_boundary=6.5,
                projected_value=6.56,
                projected_deviation=0.06,
            )
            return LLMCompletionResult(output=output, model="gpt-4.1-mini")
        if output_model is DiagnosticResult:
            output = DiagnosticResult(
                classification="ph_high",
                primary_metric="ph",
                summary="Monitoring projects pH above the upper boundary.",
            )
            return LLMCompletionResult(output=output, model="gpt-4.1-mini")
        if output_model is StrategyResult:
            output = StrategyResult(
                action="dose",
                confidence=0.78,
                reason="Preventive dosing is warranted by the monitoring projection.",
            )
            return LLMCompletionResult(output=output, model="gpt-4.1-mini")
        if output_model is DosePlanResult:
            output = DosePlanResult(
                pump_activated="ph_down",
                dose_adjustment_factor=1.0,
                reason="Use the bounded pH-down projected correction and remeasure after mixing.",
            )
            return LLMCompletionResult(output=output, model="gpt-4.1-mini")
        return super().complete_json(agent_name, system_prompt, context, output_model)


class InvalidMonitoringProjectionLLM(MonitoringProjectedDeviationLLM):
    """Fake LLM that projects using a boundary outside the configured range."""

    def complete_json(
        self,
        agent_name: str,
        system_prompt: str,
        context: dict,
        output_model: type[BaseModel],
    ) -> LLMCompletionResult:
        if output_model is MonitoringResult:
            output = MonitoringResult(
                status="projected_deviation",
                is_stable=True,
                is_recovering=False,
                risk_flags=["projected_out_of_range"],
                summary="pH is projected above an invented upper boundary.",
                projected_decision="ph_high",
                projected_metric="ph",
                projected_pump_activated="ph_down",
                projected_boundary=7.0,
                projected_value=7.1,
                projected_deviation=0.1,
            )
            return LLMCompletionResult(output=output, model="gpt-4.1-mini")
        return super().complete_json(agent_name, system_prompt, context, output_model)


class FalseCombinedLLM(FakeAgenticLLM):
    """Fake LLM that calls an EC-only deviation a combined disturbance."""

    def complete_json(
        self,
        agent_name: str,
        system_prompt: str,
        context: dict,
        output_model: type[BaseModel],
    ) -> LLMCompletionResult:
        if output_model is DiagnosticResult:
            return LLMCompletionResult(
                output=DiagnosticResult(
                    classification="combined_disturbance",
                    primary_metric="ec",
                    summary="Both pH and EC are outside their target ranges.",
                ),
                model="gpt-4.1-nano",
            )

        return super().complete_json(agent_name, system_prompt, context, output_model)


class UnjustifiedWaitLLM(FakeAgenticLLM):
    """Fake LLM that waits despite no recovery, mixing, instability, or anomaly."""

    def complete_json(
        self,
        agent_name: str,
        system_prompt: str,
        context: dict,
        output_model: type[BaseModel],
    ) -> LLMCompletionResult:
        if output_model is StrategyResult:
            return LLMCompletionResult(
                output=StrategyResult(
                    action="wait",
                    confidence=0.9,
                    reason="Recent history shows natural recovery.",
                ),
                model="gpt-4.1-nano",
            )

        return super().complete_json(agent_name, system_prompt, context, output_model)


class CautiousWaitLLM(FakeAgenticLLM):
    """Fake LLM that chooses a justified wait after diagnosis."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete_json(
        self,
        agent_name: str,
        system_prompt: str,
        context: dict,
        output_model: type[BaseModel],
    ) -> LLMCompletionResult:
        self.calls.append(agent_name)
        if output_model is StrategyResult:
            return LLMCompletionResult(
                output=StrategyResult(
                    action="wait",
                    confidence=0.7,
                    reason="Confidence is low; wait for another stable confirmation.",
                ),
                model="gpt-4.1-mini",
            )

        return FakeAgenticLLM.complete_json(self, agent_name, system_prompt, context, output_model)


class MisleadingMonitoringLLM(FakeAgenticLLM):
    """Fake LLM that reports a hard safety flag without deterministic evidence."""

    def complete_json(
        self,
        agent_name: str,
        system_prompt: str,
        context: dict,
        output_model: type[BaseModel],
    ) -> LLMCompletionResult:
        if output_model is MonitoringResult:
            return LLMCompletionResult(
                output=MonitoringResult(
                    status="deviation",
                    is_stable=False,
                    risk_flags=["unstable_reading"],
                    summary="Stable pH deviation remains, but mistakenly flagged unstable.",
                ),
                model="gpt-4.1-nano",
            )

        return super().complete_json(agent_name, system_prompt, context, output_model)


class CountingAgenticLLM(FakeAgenticLLM):
    """Fake LLM that records calls so routing behavior can be asserted."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete_json(
        self,
        agent_name: str,
        system_prompt: str,
        context: dict,
        output_model: type[BaseModel],
    ) -> LLMCompletionResult:
        """Record and return normal fake LLM output."""
        self.calls.append(agent_name)
        return super().complete_json(agent_name, system_prompt, context, output_model)


class HighMixingFactorLLM(FakeAgenticLLM):
    """Fake LLM that asks for an extended but bounded mixing window."""

    def complete_json(
        self,
        agent_name: str,
        system_prompt: str,
        context: dict,
        output_model: type[BaseModel],
    ) -> LLMCompletionResult:
        if output_model is DosePlanResult:
            return LLMCompletionResult(
                output=DosePlanResult(
                    pump_activated="ph_up",
                    mixing_adjustment_factor=2.5,
                    dose_adjustment_factor=0.4,
                    reason="Use a small dose but wait longer before another correction.",
                ),
                model="gpt-4.1-mini",
            )

        return super().complete_json(agent_name, system_prompt, context, output_model)


def _history_entry(
    minutes_ago: int,
    ph: float,
    ec: float,
    decision: str,
    pump_activated: str = "none",
    status: str = "within_range",
    decision_metadata: dict | None = None,
    dose_ml: float | None = None,
    duration_ms: int | None = None,
) -> SensorHistoryEntry:
    return SensorHistoryEntry(
        timestamp=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        ph=ph,
        ec=ec,
        decision=decision,
        pump_activated=pump_activated,
        dose_ml=dose_ml,
        duration_ms=duration_ms,
        status=status,
        decision_metadata=decision_metadata or {},
    )


def test_fallback_recovery_requires_both_deviating_metrics_to_improve() -> None:
    payload = SensorPayload(temperature=24.5, ph=5.3, ec=2.9,
        reservoir_volume_liters=80, ph_stable_for_seconds=30, ec_stable_for_seconds=30)
    for ec_values, expected in (([2.5, 2.6, 2.7, 2.8], False), ([3.3, 3.2, 3.1, 3.0], True)):
        history = [_history_entry(4-index, ph, ec, "ph_low")
                   for index, (ph, ec) in enumerate(zip([5.1, 5.15, 5.2, 5.25], ec_values))]
        reason = _natural_recovery_reason(payload, evaluate_dosing_decision(payload, REFERENCE_RANGE),
            history, reference_range=REFERENCE_RANGE, control_history=history)
        assert bool(reason) is expected


def test_fallback_recovery_rejects_pump_events_missing_from_sensor_history() -> None:
    payload = SensorPayload(temperature=24.5, ph=5.3, ec=1.8,
        reservoir_volume_liters=80, ph_stable_for_seconds=30, ec_stable_for_seconds=30)
    history = [_history_entry(4-index, ph, 1.8, "ph_low")
               for index, ph in enumerate([5.1, 5.15, 5.2, 5.25])]
    dose = _history_entry(2, 5.2, 1.8, "ph_low", pump_activated="ph_up", status="completed")
    assert _natural_recovery_reason(payload, evaluate_dosing_decision(payload, REFERENCE_RANGE),
        history, reference_range=REFERENCE_RANGE, control_history=history+[dose]) is None


def test_fallback_recovery_rejects_duplicate_snapshots() -> None:
    payload = SensorPayload(temperature=24.5, ph=5.3, ec=1.8,
        reservoir_volume_liters=80, ph_stable_for_seconds=30, ec_stable_for_seconds=30)
    snapshot = _history_entry(1, 5.2, 1.8, "ph_low")
    history = [snapshot] * 4
    assert _natural_recovery_reason(payload, evaluate_dosing_decision(payload, REFERENCE_RANGE),
        history, reference_range=REFERENCE_RANGE, control_history=history) is None


def test_combined_priority_choice_covers_all_deviation_directions() -> None:
    """Either diagnostic priority selects one bounded pump in every combined case."""
    class PriorityLLM(FakeAgenticLLM):
        def __init__(self, primary):
            self.primary = primary

        def complete_json(self, agent_name, system_prompt, context, output_model):
            if output_model is DiagnosticResult:
                return LLMCompletionResult(output=DiagnosticResult(
                    classification="combined_disturbance", primary_metric=self.primary,
                    summary="Both metrics deviate; selected priority follows supplied evidence.",
                ), model="test")
            if output_model is DosePlanResult:
                return LLMCompletionResult(output=DosePlanResult(
                    pump_activated=context["agentic_primary_shadow"]["pump_activated"],
                    dose_adjustment_factor=1.0, mixing_adjustment_factor=1.0,
                    reason="Plan the selected primary correction within backend bounds.",
                ), model="test")
            return super().complete_json(agent_name, system_prompt, context, output_model)

    for ph in (5.0, 7.1):
        for ec in (0.5, 4.0):
            for primary in ("ph", "ec"):
                payload = SensorPayload(temperature=24.5, ph=ph, ec=ec,
                    reservoir_volume_liters=80, ph_stable_for_seconds=30,
                    ec_stable_for_seconds=30, control_strategy="agentic_ai")
                history = [_history_entry(5, ph, ec, "wait_initial_confirmation")]
                decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, history,
                                                     llm=PriorityLLM(primary))
                expected_pump = ("ph_up" if ph < REFERENCE_RANGE.ph_target_min else "ph_down") if primary == "ph" else ("ec_up" if ec < REFERENCE_RANGE.ec_target_min else "ec_down")
                assert decision.pump_activated == expected_pump
                assert decision.metadata["diagnostic_reasoning_agent"]["primary_metric"] == primary
                assert 0 < decision.dose_ml <= getattr(REFERENCE_RANGE, expected_pump+"_max_dose_ml_per_cycle")
                assert decision.duration_ms > 0
                plan = decision.metadata["dose_planning_agent"]
                assert plan["candidate_dose_ml"] == decision.dose_ml
                assert plan["candidate_duration_ms"] == decision.duration_ms
                assert plan["candidate_mixing_time_seconds"] == decision.metadata["mixing_window"]["effective_seconds"]


def test_combined_priority_invalid_choice_and_timeout_use_existing_fallback() -> None:
    """Missing or unusable diagnosis cannot select an arbitrary pump."""
    class InvalidDiagnosticLLM(FakeAgenticLLM):
        def __init__(self, fail):
            self.fail = fail

        def complete_json(self, agent_name, system_prompt, context, output_model):
            if output_model is DiagnosticResult:
                if self.fail:
                    raise TimeoutError("simulated diagnostic timeout")
                return LLMCompletionResult(output=DiagnosticResult(
                    classification="combined_disturbance", primary_metric="none",
                    summary="No primary metric provided.",
                ), model="test")
            return super().complete_json(agent_name, system_prompt, context, output_model)

    payload = SensorPayload(temperature=24.5, ph=7.1, ec=4.0,
        reservoir_volume_liters=80, ph_stable_for_seconds=30,
        ec_stable_for_seconds=30, control_strategy="agentic_ai")
    history = [_history_entry(5, 7.1, 4.0, "wait_initial_confirmation")]
    for fail in (False, True):
        decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, history,
                                             llm=InvalidDiagnosticLLM(fail))
        assert decision.metadata["diagnostic_reasoning_agent"]["primary_metric"] == "ph"
        assert decision.pump_activated == "ph_down"
        assert 0 < decision.dose_ml <= REFERENCE_RANGE.ph_down_max_dose_ml_per_cycle


def test_projected_wait_keeps_action_without_false_natural_recovery_reason() -> None:
    payload = SensorPayload(temperature=24.5, ph=6.49, ec=1.8,
        reservoir_volume_liters=80, ph_stable_for_seconds=30, ec_stable_for_seconds=30)
    monitoring = MonitoringResult(status="projected_deviation", is_stable=True,
        is_recovering=False, risk_flags=["projected_out_of_range"],
        summary="pH projects above the upper boundary.", projected_decision="ph_high",
        projected_metric="ph", projected_pump_activated="ph_down", projected_boundary=6.5,
        projected_value=6.54, projected_deviation=0.04)
    state = {"payload":payload,"reference_range":REFERENCE_RANGE,"history":[],
             "baseline":evaluate_dosing_decision(payload,REFERENCE_RANGE),"monitoring":monitoring}
    result = _ensure_strategy_consistency(state,StrategyResult(action="wait",confidence=0.92,
        reason="Recent readings suggest natural recovery."))
    assert result.action == "wait"
    assert "natural recovery" not in result.reason.lower()
    assert "confirmation" in result.reason.lower()


def test_dose_planning_produces_concrete_candidates_before_safety_gate() -> None:
    payload = SensorPayload(temperature=24.5,ph=5.1,ec=1.8,reservoir_volume_liters=80,
        ph_stable_for_seconds=30,ec_stable_for_seconds=30,control_strategy="agentic_ai")
    history=[_history_entry(5,5.1,1.8,"wait_initial_confirmation")]
    decision=evaluate_agentic_decision(payload,REFERENCE_RANGE,history,llm=FakeAgenticLLM())
    plan=decision.metadata["dose_planning_agent"]
    assert plan["candidate_dose_ml"]==decision.dose_ml
    assert plan["candidate_duration_ms"]==decision.duration_ms
    assert plan["candidate_mixing_time_seconds"]==decision.metadata["mixing_window"]["effective_seconds"]
    assert decision.metadata["dose_plan_validation"]["valid"] is True
    traced_plan=next(row["output"] for row in decision.metadata["llm_trace"] if row["agent"]=="dose_planning_agent")
    assert traced_plan["candidate_dose_ml"]==decision.dose_ml


def test_safety_gate_rejects_missing_or_tampered_candidate_even_with_cached_review() -> None:
    payload=SensorPayload(temperature=24.5,ph=5.1,ec=1.8,reservoir_volume_liters=80,
        ph_stable_for_seconds=30,ec_stable_for_seconds=30)
    plan=DosePlanResult(pump_activated="ph_up",dose_adjustment_factor=1.0,
        mixing_adjustment_factor=1.0,reason="Bounded selected correction.")
    state={"payload":payload,"reference_range":REFERENCE_RANGE,
        "history":[_history_entry(5,5.1,1.8,"wait_initial_confirmation")],
        "baseline":evaluate_dosing_decision(payload,REFERENCE_RANGE),
        "monitoring":MonitoringResult(status="deviation",is_stable=True,is_recovering=False,
            risk_flags=[],summary="Stable pH deviation."),
        "diagnosis":DiagnosticResult(classification="ph_low",primary_metric="ph",summary="pH low."),
        "strategy":StrategyResult(action="dose",confidence=0.9,reason="Stable deviation."),
        "dose_plan":plan,"consistency_review":{"review_status":"pass","reason":"Cached pass."}}
    candidate=_calculate_dose_plan_candidate(state,plan)
    concrete=plan.model_copy(update={"candidate_dose_ml":candidate["dose_ml"],
        "candidate_duration_ms":candidate["duration_ms"],
        "candidate_mixing_time_seconds":candidate["mixing_window"]["effective_seconds"]})
    good=_safety_gate_node({**state,"dose_plan":concrete})["final_decision"]
    assert good.pump_activated=="ph_up"
    for invalid in (plan,
        concrete.model_copy(update={"candidate_dose_ml":candidate["dose_ml"]+1}),
        concrete.model_copy(update={"candidate_duration_ms":candidate["duration_ms"]+1}),
        concrete.model_copy(update={"candidate_mixing_time_seconds":0}),
        concrete.model_copy(update={"pump_activated":"ec_up"})):
        final=_safety_gate_node({**state,"dose_plan":invalid})["final_decision"]
        assert final.pump_activated=="none"
        assert final.dose_ml==0 and final.duration_ms==0
        assert final.metadata["dose_plan_validation"]["valid"] is False


@pytest.mark.parametrize("status", ["inter_dose_mixing", "ec_up_b_dosing"])
def test_unfinished_ec_pair_blocks_new_dose_after_history_window(status):
    payload = SensorPayload(temperature=24, ph=5.1, ec=1.5,
        reservoir_volume_liters=20, ph_stable_for_seconds=30, ec_stable_for_seconds=30,
        control_strategy="agentic_ai")
    previous = _history_entry(minutes_ago=300, ph=6, ec=1, decision="ec_low",
        pump_activated="ec_up", status=status, dose_ml=10, duration_ms=1000)
    decision = evaluate_agentic_decision(payload, REFERENCE_RANGE, [], control_history=[previous])
    assert decision.pump_activated == "none"
    assert decision.duration_ms == 0
    assert "still active" in decision.reason
