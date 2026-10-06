"""Decision strategy selection for HANAS control engines."""

from fastapi import HTTPException

from app.core.config import settings
from app.schemas.sensor import (
    DosingDecision,
    ReferenceRange,
    SensorHistoryEntry,
    SensorPayload,
    SUPPORTED_CONTROL_STRATEGIES,
)
from app.services.agentic_ai.graph_engine import evaluate_agentic_decision
from app.services.baseline.dosing_rules import evaluate_persistent_dosing_decision


BRANCH_SUPPORTED_STRATEGY = "baseline"
AGENTIC_SUPPORTED_STRATEGY = "agentic_ai"


def requested_control_strategy(payload: SensorPayload) -> str:
    """Return the requested control strategy, falling back to app defaults."""
    strategy = payload.control_strategy or settings.default_control_strategy
    return strategy.strip().lower()


def evaluate_control_decision(
    payload: SensorPayload,
    reference_range: ReferenceRange,
    recent_logs: list[SensorHistoryEntry] | None = None,
    crop_lifecycle: dict[str, object] | None = None,
    control_history: list[SensorHistoryEntry] | None = None,
) -> DosingDecision:
    """Evaluate a dosing decision using the requested strategy.

    Both branches use recent history to confirm tiny boundary excursions. The
    agentic branch adds its other bounded checks while returning the same ESP32
    contract.
    """
    strategy = requested_control_strategy(payload)

    if strategy not in SUPPORTED_CONTROL_STRATEGIES:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported control strategy '{strategy}'.",
        )

    if strategy == AGENTIC_SUPPORTED_STRATEGY:
        return evaluate_agentic_decision(
            payload,
            reference_range,
            recent_logs,
            crop_lifecycle=crop_lifecycle,
            control_history=control_history,
        )

    decision = evaluate_persistent_dosing_decision(payload, reference_range, recent_logs)
    decision.metadata["requested_strategy"] = strategy
    decision.metadata["executed_strategy"] = BRANCH_SUPPORTED_STRATEGY
    return _apply_delivery_safety(decision, payload, recent_logs or [])


def _apply_delivery_safety(
    decision: DosingDecision,
    payload: SensorPayload,
    recent_logs: list[SensorHistoryEntry],
) -> DosingDecision:
    """Warn baseline repeat dosing when a prior same-pump dose had no effect."""
    if decision.pump_activated == "none":
        return decision

    latest_same_pump = next(
        (
            entry
            for entry in recent_logs
            if entry.pump_activated == decision.pump_activated and entry.status != "dosing"
        ),
        None,
    )
    if latest_same_pump is None or latest_same_pump.decision != decision.decision:
        return decision

    if not _is_reliable_delivery_evidence(latest_same_pump):
        return decision

    response = _same_pump_metric_response(payload, latest_same_pump, decision.pump_activated)
    if response is None:
        return decision

    if response["expected_direction_delta"] >= response["minimum_expected_delta"]:
        return decision

    delivery_issue = {
        "issue_detected": True,
        "reason": "Previous same-pump dose produced little or no expected movement.",
        "latest_same_pump_decision": latest_same_pump.decision,
        "latest_same_pump_status": latest_same_pump.status,
        "latest_same_pump_dose_ml": latest_same_pump.dose_ml,
        "latest_same_pump_duration_ms": latest_same_pump.duration_ms,
        "current_decision": decision.decision,
        "current_pump": decision.pump_activated,
        **response,
        "potential_causes": [
            "empty nutrient or pH adjustment container",
            "air lock or clogged dosing tube",
            "pump, relay, wiring, or check-valve failure",
            "dose outlet not reaching circulating water",
            "insufficient mixing before remeasurement",
            "sensor drift or calibration issue",
        ],
    }
    return decision.model_copy(
        update={
            "metadata": {
                **decision.metadata,
                "risk_flags": [*decision.metadata.get("risk_flags", []), "possible_delivery_issue"],
                "delivery_issue": delivery_issue,
                "delivery_issue_policy": "warn_only",
            },
        }
    )


def _is_reliable_delivery_evidence(entry: SensorHistoryEntry) -> bool:
    """Return true only when a previous dose ran long enough to judge response."""
    return (
        entry.duration_ms is not None
        and entry.duration_ms >= settings.minimum_reliable_pump_duration_ms
    )


def _same_pump_metric_response(
    payload: SensorPayload,
    entry: SensorHistoryEntry,
    pump_activated: str,
) -> dict[str, float | str] | None:
    if pump_activated == "ph_up":
        return _metric_response("ph", entry.ph, payload.ph, payload.ph - entry.ph, payload.ph_stability_threshold)
    if pump_activated == "ph_down":
        return _metric_response("ph", entry.ph, payload.ph, entry.ph - payload.ph, payload.ph_stability_threshold)
    if pump_activated == "ec_up":
        return _metric_response("ec", entry.ec, payload.ec, payload.ec - entry.ec, payload.ec_stability_threshold)
    if pump_activated == "ec_down":
        return _metric_response("ec", entry.ec, payload.ec, entry.ec - payload.ec, payload.ec_stability_threshold)
    return None


def _metric_response(
    metric: str,
    previous_value: float,
    current_value: float,
    expected_delta: float,
    payload_threshold: float | None,
) -> dict[str, float | str]:
    default_threshold = 0.03
    threshold = max(payload_threshold or default_threshold, default_threshold)
    return {
        "metric": metric,
        "previous_value": round(previous_value, 4),
        "current_value": round(current_value, 4),
        "expected_direction_delta": round(expected_delta, 4),
        "minimum_expected_delta": round(threshold, 4),
    }
