"""Human-in-the-loop safety helpers for agentic dosing commands."""

from app.schemas.sensor import DosingDecision, SensorPayload


def hold_for_human_review(
    payload: SensorPayload,
    decision: DosingDecision,
    strategy: str,
    *,
    hitl_enabled: bool,
) -> DosingDecision:
    """Return a no-pump review hold when HITL should gate an agentic dose."""
    if not hitl_enabled:
        return decision
    if strategy != "agentic_ai":
        return decision
    if decision.pump_activated == "none" or decision.dose_ml <= 0 or decision.duration_ms <= 0:
        return decision

    metadata = dict(decision.metadata)
    metadata["agentic_mode_before_human_review"] = metadata.get("agentic_mode")
    metadata["agentic_mode"] = "hitl_pending"
    metadata["actuation_source"] = "human_review_gate"
    metadata["human_in_the_loop"] = {
        "status": "pending",
        "review_required": True,
        "trigger": "agentic_ai_dosing_decision",
        "pending_decision": decision.model_dump(mode="json"),
        "sensor_snapshot": payload.model_dump(mode="json"),
    }
    metadata["human_review_gate"] = {
        **(
            metadata.get("human_review_gate")
            if isinstance(metadata.get("human_review_gate"), dict)
            else {}
        ),
        "stage": "after_safety_gate",
        "eligible_for_review": True,
        "held_for_operator": True,
        "runtime_setting": "hitl_enabled",
        "review_required": True,
        "next_stage_if_approved": "esp32_execution",
    }
    risk_flags = metadata.get("risk_flags")
    if not isinstance(risk_flags, list):
        risk_flags = []
    if "human_review_required" not in risk_flags:
        risk_flags.append("human_review_required")
    metadata["risk_flags"] = risk_flags

    return DosingDecision(
        decision="wait_human_review",
        pump_activated="none",
        dose_ml=0,
        duration_ms=0,
        ph_within_range=decision.ph_within_range,
        ec_within_range=decision.ec_within_range,
        ph_deviation=decision.ph_deviation,
        ec_deviation=decision.ec_deviation,
        reason=(
            "Human-in-the-loop review is enabled; agentic AI dosing is held for "
            "operator approval before physical pump activation."
        ),
        metadata=metadata,
    )
