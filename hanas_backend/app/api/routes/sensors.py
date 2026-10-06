"""Sensor data ingestion routes."""

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from loguru import logger

from app.api.dependencies import get_sensor_repository, verify_device_token
from app.core.config import settings
from app.database.repositories.sensor_repository import SensorReadingRepository
from app.schemas.sensor import (
    DosingDecision,
    ReferenceRange,
    SensorHistoryEntry,
    SensorPayload,
    SensorResponse,
)
from app.services.baseline.dosing_rules import evaluate_dosing_decision
from app.services.agentic_ai.graph_engine import evaluate_agentic_decision
from app.services.agentic_ai.llm import OpenAIJsonLLM
from app.services.crop_lifecycle import build_crop_lifecycle_context
from app.services.decision_engine import evaluate_control_decision, requested_control_strategy
from app.services.hitl import hold_for_human_review
from app.services.notifications import maybe_send_decision_alert
from app.services.runtime_settings import (
    emergency_stop_enabled,
    experiment_preflight_required,
    full_agentic_mode_enabled,
    maintenance_mode_enabled,
    monitoring_mode_enabled,
    runtime_bool_setting,
)

router = APIRouter(tags=["sensors"])


@router.post("/sensor-data", response_model=SensorResponse)
def receive_sensor_data(
    payload: SensorPayload,
    repository: SensorReadingRepository = Depends(get_sensor_repository),
    _: None = Depends(verify_device_token),
) -> SensorResponse:
    """Receive validated readings from the HANAS sensor device."""
    control_payload = _control_payload(payload)
    full_agentic = full_agentic_mode_enabled(repository)
    if full_agentic:
        control_payload = control_payload.model_copy(
            update={"control_strategy": "agentic_ai"}
        )
    strategy = requested_control_strategy(control_payload)
    logger.info(
        "Received sensor data from ESP32: temperature={} ph={} ec={} received_water_volume_liters={} "
        "control_water_volume_liters={} strategy={}",
        payload.temperature,
        payload.ph,
        payload.ec,
        payload.reservoir_volume_liters,
        control_payload.reservoir_volume_liters,
        strategy,
    )
    reference_range = repository.get_active_reference_range(strategy)
    crop_lifecycle = build_crop_lifecycle_context(repository, reference_range)
    if emergency_stop_enabled(repository):
        decision = _emergency_stop_decision(control_payload, reference_range)
        decision.metadata.setdefault("crop_lifecycle", crop_lifecycle)
        _annotate_volume_metadata(decision, payload, control_payload)
        sensor_log = repository.create_sensor_log(control_payload, decision)
        repository.emergency_stop_active_commands(strategy)
        logger.warning(
            "Stored emergency-stop reading id={} control_cycle_id={}; all dosing disabled.",
            sensor_log.log_id,
            sensor_log.control_cycle_id,
        )
        return SensorResponse(
            status="success",
            received=control_payload,
            decision=decision,
            pump_activated=decision.pump_activated,
            duration_ms=decision.duration_ms,
            mixing_time_ms=0,
            log_id=sensor_log.log_id,
            control_cycle_id=sensor_log.control_cycle_id,
        )

    if maintenance_mode_enabled(repository):
        decision = _maintenance_decision(control_payload, reference_range)
        decision.metadata.setdefault("crop_lifecycle", crop_lifecycle)
        _annotate_volume_metadata(decision, payload, control_payload)
        sensor_log = repository.create_sensor_log(control_payload, decision)
        logger.info(
            "Stored maintenance reading id={} control_cycle_id={}; dosing disabled.",
            sensor_log.log_id,
            sensor_log.control_cycle_id,
        )
        return SensorResponse(
            status="success",
            received=control_payload,
            decision=decision,
            pump_activated=decision.pump_activated,
            duration_ms=decision.duration_ms,
            mixing_time_ms=0,
            log_id=sensor_log.log_id,
            control_cycle_id=sensor_log.control_cycle_id,
        )

    if monitoring_mode_enabled(repository):
        decision = _monitoring_decision(control_payload, reference_range)
        decision.metadata.setdefault("crop_lifecycle", crop_lifecycle)
        _annotate_volume_metadata(decision, payload, control_payload)
        sensor_log = repository.create_sensor_log(control_payload, decision)
        logger.info(
            "Stored monitoring-only reading id={} control_cycle_id={}; AI analysis and dosing disabled.",
            sensor_log.log_id,
            sensor_log.control_cycle_id,
        )
        return SensorResponse(
            status="success",
            received=control_payload,
            decision=decision,
            pump_activated=decision.pump_activated,
            duration_ms=decision.duration_ms,
            mixing_time_ms=0,
            log_id=sensor_log.log_id,
            control_cycle_id=sensor_log.control_cycle_id,
        )

    if experiment_preflight_required(repository, strategy):
        decision = _experiment_preflight_decision(control_payload, reference_range)
        logger.warning(
            "Experiment preflight required; stored reading without AI analysis or dosing."
        )
        return _persist_sensor_decision(
            payload,
            control_payload,
            decision,
            crop_lifecycle,
            repository,
        )

    if full_agentic and strategy == "agentic_ai":
        decision, release_agentic_lock = _full_agentic_decision(
            control_payload,
            reference_range,
            repository,
            crop_lifecycle=crop_lifecycle,
        )
        try:
            decision = _apply_human_review_gate(control_payload, decision, strategy, repository)
            return _persist_sensor_decision(
                payload,
                control_payload,
                decision,
                crop_lifecycle,
                repository,
            )
        finally:
            _release_full_agentic_lock(release_agentic_lock)
    elif settings.batch_analysis_enabled and strategy == "agentic_ai":
        # Phase 3: LLM batch is the primary actuator on the configured interval.
        # Per-reading deterministic dosing only fires for moderate-or-worse deviations.
        # baseline strategy always uses the normal deterministic pipeline.
        recent_logs = repository.get_recent_sensor_logs(
            limit=10,
            control_strategy=strategy,
        )
        active_pump_command = repository.get_recent_or_active_pump_command(
            settings.default_mixing_time_seconds,
        )
        decision = _phase3_decision(
            control_payload,
            reference_range,
            recent_logs,
            active_pump_command=active_pump_command,
        )
        decision = _apply_human_review_gate(control_payload, decision, strategy, repository)
    else:
        history_limit = (
            settings.agentic_control_history_limit
            if strategy == "agentic_ai"
            else settings.agentic_history_limit
        )
        control_history = repository.get_recent_sensor_logs(
            limit=history_limit,
            control_strategy=strategy,
        )
        recent_logs = control_history[: settings.agentic_history_limit]
        decision = evaluate_control_decision(
            control_payload,
            reference_range,
            recent_logs,
            crop_lifecycle=crop_lifecycle,
            control_history=control_history if strategy == "agentic_ai" else None,
        )
        if decision.pump_activated != "none":
            active_pump_command = repository.get_recent_or_active_pump_command(
                settings.default_mixing_time_seconds,
            )
            if active_pump_command is not None:
                decision = _physical_command_hold(decision, active_pump_command, strategy)
        decision = _apply_human_review_gate(control_payload, decision, strategy, repository)

    return _persist_sensor_decision(
        payload,
        control_payload,
        decision,
        crop_lifecycle,
        repository,
    )


def _persist_sensor_decision(
    received_payload: SensorPayload,
    control_payload: SensorPayload,
    decision: DosingDecision,
    crop_lifecycle: dict[str, object],
    repository: SensorReadingRepository,
) -> SensorResponse:
    """Persist a completed decision and build the ESP32 response."""
    # Analysis can take seconds: re-read operator holds before returning a
    # physical command, rather than relying on the checks before analysis.
    reference = None
    if emergency_stop_enabled(repository):
        reference = repository.get_active_reference_range(requested_control_strategy(control_payload))
        decision = _emergency_stop_decision(control_payload, reference)
        repository.emergency_stop_active_commands(requested_control_strategy(control_payload))
    elif maintenance_mode_enabled(repository):
        reference = repository.get_active_reference_range(requested_control_strategy(control_payload))
        decision = _maintenance_decision(control_payload, reference)
    elif monitoring_mode_enabled(repository):
        reference = repository.get_active_reference_range(requested_control_strategy(control_payload))
        decision = _monitoring_decision(control_payload, reference)
    decision.metadata.setdefault("crop_lifecycle", crop_lifecycle)
    _annotate_volume_metadata(decision, received_payload, control_payload)
    sensor_log = repository.create_sensor_log(control_payload, decision)
    logger.info(
        "Stored sensor data with id={} control_cycle_id={} decision={} pump={} duration_ms={}",
        sensor_log.log_id,
        sensor_log.control_cycle_id,
        decision.decision,
        decision.pump_activated,
        decision.duration_ms,
    )
    maybe_send_decision_alert(
        control_payload,
        decision,
        log_id=sensor_log.log_id,
        control_cycle_id=sensor_log.control_cycle_id,
        notification_log_writer=repository,
    )
    return SensorResponse(
        status="success",
        received=control_payload,
        decision=decision,
        pump_activated=decision.pump_activated,
        duration_ms=decision.duration_ms,
        mixing_time_ms=_mixing_time_ms_for_decision(decision),
        log_id=sensor_log.log_id,
        control_cycle_id=sensor_log.control_cycle_id,
    )


def _full_agentic_decision(
    payload: SensorPayload,
    reference_range: ReferenceRange,
    repository: SensorReadingRepository,
    *,
    crop_lifecycle: dict[str, object],
) -> tuple[DosingDecision, Callable[[], None] | None]:
    """Run one complete LLM pipeline for an incoming physical sensor reading."""
    if not settings.openai_api_key:
        return (
            _full_agentic_hold(
                payload,
                reference_range,
                reason="OPENAI_API_KEY is unavailable; Full Agentic Mode failed closed.",
                status="full_agentic_unavailable",
            ),
            None,
        )

    acquire_lock = getattr(repository, "try_acquire_agentic_cycle_lock", None)
    release_lock = getattr(repository, "release_agentic_cycle_lock", None)
    lock_acquired = not callable(acquire_lock) or bool(acquire_lock())
    if not lock_acquired:
        return (
            _full_agentic_hold(
                payload,
                reference_range,
                reason="Another full agentic cycle is still running; this reading was saved without dosing.",
                status="full_agentic_busy",
            ),
            None,
        )

    release_callback = release_lock if callable(release_lock) else None
    try:
        control_history = repository.get_recent_sensor_logs(
            limit=settings.agentic_control_history_limit,
            control_strategy="agentic_ai",
        )
        recent_logs = control_history[: settings.agentic_history_limit]
        llm = OpenAIJsonLLM(
            settings.openai_api_key,
            settings.agentic_ai_model,
            settings.agentic_ai_fallback_model,
            settings.agentic_ai_llm_timeout_seconds,
            monitoring_model=settings.agentic_ai_monitoring_model,
            diagnostic_model=settings.agentic_ai_diagnostic_model,
            dose_planning_model=settings.agentic_ai_dose_planning_model,
        )
        decision = evaluate_agentic_decision(
            payload,
            reference_range,
            recent_logs,
            llm=llm,
            control_history=control_history,
            crop_lifecycle=crop_lifecycle,
        )
        decision.metadata.update(
            {
                "triggered_by": "sensor_full_agentic",
                "agentic_mode": "full_agentic_per_reading",
                "actuation_source": "full_agentic_pipeline",
                "full_agentic_mode_enabled": True,
            }
        )

        active_command = repository.get_recent_or_active_pump_command(
            settings.default_mixing_time_seconds,
        )
        if active_command is not None and decision.pump_activated != "none":
            decision = _full_agentic_physical_hold(decision, active_command)
        return decision, release_callback
    except Exception as exc:
        logger.exception("Full agentic sensor cycle failed: {}", exc)
        return (
            _full_agentic_hold(
                payload,
                reference_range,
                reason="The full agentic pipeline failed; this reading was saved without dosing.",
                status="full_agentic_failed",
                error_type=type(exc).__name__,
            ),
            release_callback,
        )


def _release_full_agentic_lock(release_lock: Callable[[], None] | None) -> None:
    """Release the agentic lock after the decision has been durably persisted."""
    if release_lock is None:
        return
    try:
        release_lock()
    except Exception as exc:
        logger.error("Failed to release the full agentic cycle lock: {}", exc)


def _full_agentic_hold(
    payload: SensorPayload,
    reference_range: ReferenceRange,
    *,
    reason: str,
    status: str,
    error_type: str | None = None,
) -> DosingDecision:
    """Fail closed without falling back to deterministic pump actuation."""
    baseline = evaluate_dosing_decision(payload, reference_range)
    metadata = _metadata_without_dose_plan(baseline.metadata)
    metadata.update(
        {
            "triggered_by": "sensor_full_agentic",
            "executed_strategy": "agentic_ai",
            "agentic_mode": status,
            "actuation_source": "full_agentic_fail_closed",
            "full_agentic_mode_enabled": True,
            "suppressed_baseline_decision": {
                "decision": baseline.decision,
                "pump_activated": baseline.pump_activated,
                "dose_ml": baseline.dose_ml,
                "duration_ms": baseline.duration_ms,
            },
        }
    )
    if error_type:
        metadata["pipeline_error_type"] = error_type
    return baseline.model_copy(
        update={
            "decision": status,
            "pump_activated": "none",
            "dose_ml": 0.0,
            "duration_ms": 0,
            "reason": reason,
            "metadata": metadata,
        }
    )


def _full_agentic_physical_hold(
    decision: DosingDecision,
    active_command: dict[str, object],
) -> DosingDecision:
    """Block a new agentic pump command during active dosing or post-dose mixing."""
    return _physical_command_hold(decision, active_command, "agentic_ai")


def _physical_command_hold(
    decision: DosingDecision,
    active_command: dict[str, object],
    strategy: str,
) -> DosingDecision:
    """Block any new physical command during active dosing or post-dose mixing."""
    metadata = dict(decision.metadata)
    metadata.update(
        {
            **(
                {"agentic_mode": "full_agentic_physical_hold"}
                if strategy == "agentic_ai"
                else {}
            ),
            "physical_guard": {
                "reason": "A pump command is active or still inside the mixing window.",
                "active_command": active_command,
                "suppressed_command": {
                    "decision": decision.decision,
                    "pump_activated": decision.pump_activated,
                    "dose_ml": decision.dose_ml,
                    "duration_ms": decision.duration_ms,
                },
            },
        }
    )
    return decision.model_copy(
        update={
            "decision": "wait_for_mixing",
            "pump_activated": "none",
            "dose_ml": 0.0,
            "duration_ms": 0,
            "reason": (
                "Dosing is held until the active command and mixing window are complete."
            ),
            "metadata": metadata,
        }
    )


def _control_payload(payload: SensorPayload) -> SensorPayload:
    """Return the payload used for control decisions after experiment-level normalization."""
    if not settings.force_fixed_reservoir_volume:
        if payload.reservoir_volume_liters <= 0:
            raise HTTPException(
                status_code=422,
                detail="reservoir_volume_liters must be greater than 0 when fixed volume is disabled.",
            )
        if payload.reservoir_volume_liters > settings.default_reservoir_max_volume_liters:
            raise HTTPException(
                status_code=422,
                detail=(
                    "reservoir_volume_liters exceeds the configured reservoir maximum "
                    f"of {settings.default_reservoir_max_volume_liters:g} L."
                ),
            )
        return payload

    return payload.model_copy(
        update={"reservoir_volume_liters": settings.fixed_reservoir_volume_liters}
    )


def _annotate_volume_metadata(
    decision: DosingDecision,
    received_payload: SensorPayload,
    control_payload: SensorPayload,
) -> None:
    """Record whether the backend used a fixed experiment volume for dosing."""
    if not settings.force_fixed_reservoir_volume:
        decision.metadata.setdefault("volume_source", "sensor_payload")
        return

    decision.metadata.update(
        {
            "volume_source": "fixed_backend_config",
            "received_reservoir_volume_liters": received_payload.reservoir_volume_liters,
            "control_reservoir_volume_liters": control_payload.reservoir_volume_liters,
        }
    )


def _mixing_time_ms_for_decision(decision: DosingDecision) -> int:
    """Return the post-dose mixing window the ESP32 should observe."""
    if decision.pump_activated == "none" or decision.duration_ms <= 0:
        return 0

    mixing_window = decision.metadata.get("mixing_window") or {}
    effective_seconds = mixing_window.get("effective_seconds")
    if isinstance(effective_seconds, int | float) and effective_seconds > 0:
        return round(effective_seconds * 1000)

    return settings.default_mixing_time_seconds * 1000


def _apply_human_review_gate(
    payload: SensorPayload,
    decision: DosingDecision,
    strategy: str,
    repository: SensorReadingRepository | None = None,
) -> DosingDecision:
    """Hold agentic AI dosing commands for operator review when HITL is enabled."""
    return hold_for_human_review(
        payload,
        decision,
        strategy,
        hitl_enabled=runtime_bool_setting(
            repository,
            "hitl_enabled",
            default=settings.human_in_the_loop_enabled,
        ),
    )


def _maintenance_decision(payload: SensorPayload, reference_range: ReferenceRange) -> DosingDecision:
    """Save the reading but prevent pump commands while the system is being serviced."""
    baseline = evaluate_dosing_decision(payload, reference_range)
    metadata = _metadata_without_dose_plan(baseline.metadata)
    metadata.update(
        {
            "triggered_by": "maintenance_mode",
            "executed_strategy": requested_control_strategy(payload),
            "maintenance_mode_enabled": True,
            "suppressed_baseline_decision": {
                "decision": baseline.decision,
                "pump_activated": baseline.pump_activated,
                "dose_ml": baseline.dose_ml,
                "duration_ms": baseline.duration_ms,
            },
        }
    )
    return baseline.model_copy(
        update={
            "decision": "maintenance_mode",
            "pump_activated": "none",
            "dose_ml": 0.0,
            "duration_ms": 0,
            "reason": (
                "Maintenance mode is enabled. Reading was saved, but automatic dosing is disabled "
                "while sensors, pumps, or aeration are being checked."
            ),
            "metadata": metadata,
        }
    )


def _monitoring_decision(payload: SensorPayload, reference_range: ReferenceRange) -> DosingDecision:
    """Store live readings while suppressing control analysis and new pump commands."""
    baseline = evaluate_dosing_decision(payload, reference_range)
    metadata = _metadata_without_dose_plan(baseline.metadata)
    metadata.update(
        {
            "triggered_by": "monitoring_mode",
            "executed_strategy": requested_control_strategy(payload),
            "monitoring_mode_enabled": True,
            "full_agentic_analysis_suppressed": True,
            "suppressed_baseline_decision": {
                "decision": baseline.decision,
                "pump_activated": baseline.pump_activated,
                "dose_ml": baseline.dose_ml,
                "duration_ms": baseline.duration_ms,
            },
        }
    )
    return baseline.model_copy(
        update={
            "decision": "monitoring_mode",
            "pump_activated": "none",
            "dose_ml": 0.0,
            "duration_ms": 0,
            "reason": (
                "Monitoring Only is enabled. The live reading was saved and is available "
                "to the dashboard, but AI analysis and automatic dosing are paused."
            ),
            "metadata": metadata,
        }
    )


def _experiment_preflight_decision(
    payload: SensorPayload,
    reference_range: ReferenceRange,
) -> DosingDecision:
    """Persist readings while blocking control until the operator chooses a run."""
    baseline = evaluate_dosing_decision(payload, reference_range)
    metadata = _metadata_without_dose_plan(baseline.metadata)
    metadata.update(
        {
            "triggered_by": "experiment_preflight",
            "executed_strategy": requested_control_strategy(payload),
            "experiment_preflight_required": True,
            "full_agentic_analysis_suppressed": True,
            "suppressed_baseline_decision": {
                "decision": baseline.decision,
                "pump_activated": baseline.pump_activated,
                "dose_ml": baseline.dose_ml,
                "duration_ms": baseline.duration_ms,
            },
        }
    )
    return baseline.model_copy(
        update={
            "decision": "experiment_preflight_required",
            "pump_activated": "none",
            "dose_ml": 0.0,
            "duration_ms": 0,
            "reason": (
                "Automatic dosing is paused until an operator continues the active "
                "experiment or starts a new experiment run."
            ),
            "metadata": metadata,
        }
    )


def _emergency_stop_decision(payload: SensorPayload, reference_range: ReferenceRange) -> DosingDecision:
    """Save the reading but force every pump command off during an emergency stop."""
    baseline = evaluate_dosing_decision(payload, reference_range)
    metadata = _metadata_without_dose_plan(baseline.metadata)
    metadata.update(
        {
            "triggered_by": "emergency_stop",
            "executed_strategy": requested_control_strategy(payload),
            "emergency_stop_enabled": True,
            "suppressed_baseline_decision": {
                "decision": baseline.decision,
                "pump_activated": baseline.pump_activated,
                "dose_ml": baseline.dose_ml,
                "duration_ms": baseline.duration_ms,
            },
        }
    )
    return baseline.model_copy(
        update={
            "decision": "emergency_stop",
            "pump_activated": "none",
            "dose_ml": 0.0,
            "duration_ms": 0,
            "reason": (
                "Emergency stop is enabled. Reading was saved, all queued or active "
                "commands are being stopped, and automatic dosing remains disabled "
                "until an operator clears the emergency stop."
            ),
            "metadata": metadata,
        }
    )


def _phase3_decision(
    payload: SensorPayload,
    reference_range: ReferenceRange,
    recent_logs: list[SensorHistoryEntry],
    *,
    active_pump_command: dict[str, object] | None = None,
) -> DosingDecision:
    """Phase 3 per-reading decision: deterministic moderate guard only.

    The LLM batch scheduler is the primary actuator. Per-reading dosing only
    fires when values enter the configured moderate-or-worse band, preventing
    plant damage while the next batch cycle is still waiting for its schedule.
    A recent dose within the mixing window suppresses the emergency guard to
    prevent repeated over-dosing while pH is still stabilizing.
    """
    baseline = evaluate_dosing_decision(payload, reference_range)

    if _is_emergency_deviation(payload, reference_range):
        if active_pump_command is not None or _is_within_mixing_window(recent_logs):
            blocker_metadata = {}
            if active_pump_command is not None:
                blocker_metadata = {
                    "active_control_cycle_id": active_pump_command.get("control_cycle_id"),
                    "active_status": active_pump_command.get("status"),
                    "active_pump": active_pump_command.get("pump_activated"),
                    "active_dose_ml": active_pump_command.get("dose_ml"),
                    "active_age_seconds": active_pump_command.get("age_seconds"),
                }
            logger.info(
                "Phase 3 emergency guard suppressed: active or recent pump command exists. "
                "ph={} ec={} blocker={}",
                payload.ph,
                payload.ec,
                blocker_metadata,
            )
            suppressed_metadata = _metadata_without_dose_plan(baseline.metadata)
            suppressed_metadata.update(
                {
                    "triggered_by": "emergency_guard_mixing_wait",
                    "executed_strategy": "agentic_ai",
                    "agentic_mode": "emergency_guard_mixing_wait",
                    "actuation_source": "phase3_deterministic_guard",
                    "mixing_window": _phase3_mixing_window_metadata(),
                    "pump_guard": blocker_metadata or {"source": "recent_sensor_logs"},
                    "suppressed_baseline_decision": {
                        "decision": baseline.decision,
                        "pump_activated": baseline.pump_activated,
                        "dose_ml": baseline.dose_ml,
                        "duration_ms": baseline.duration_ms,
                    },
                }
            )
            return baseline.model_copy(
                update={
                    "pump_activated": "none",
                    "dose_ml": 0.0,
                    "duration_ms": 0,
                    "reason": (
                        "Phase 3 emergency: a pump command is active or still within the "
                        "mixing window. Waiting for physical dosing to finish before another "
                        "correction."
                    ),
                    "metadata": suppressed_metadata,
                }
            )

        baseline.metadata["triggered_by"] = "emergency_guard"
        baseline.metadata["executed_strategy"] = "agentic_ai"
        baseline.metadata["agentic_mode"] = "emergency_guard"
        baseline.metadata["actuation_source"] = "phase3_deterministic_guard"
        baseline.metadata["hitl_policy"] = "runtime_hitl_gate_applied_after_guard_when_enabled"
        baseline.metadata["mixing_window"] = _phase3_mixing_window_metadata()
        logger.warning(
            "Phase 3 emergency guard triggered: ph={} ec={} decision={} pump={}",
            payload.ph,
            payload.ec,
            baseline.decision,
            baseline.pump_activated,
        )
        return baseline

    collection_metadata = _metadata_without_dose_plan(baseline.metadata)
    collection_metadata.update(
        {
            "triggered_by": "batch_mode_collection",
            "executed_strategy": "agentic_ai",
            "agentic_mode": "batch_collection",
            "actuation_source": "phase3_collection_only",
            "baseline_decision": baseline.decision,
            "baseline_pump_activated": baseline.pump_activated,
        }
    )
    return baseline.model_copy(
        update={
            "pump_activated": "none",
            "dose_ml": 0.0,
            "duration_ms": 0,
            "reason": (
                "Phase 3 batch mode: reading saved for LLM batch analysis. "
                "No emergency threshold breached."
            ),
            "metadata": collection_metadata,
        }
    )


def _metadata_without_dose_plan(metadata: dict[str, object]) -> dict[str, object]:
    """Remove actuator fields when Phase 3 stores a reading without pumping.

    Mild readings are intentionally collected for the scheduled batch. Keeping
    the baseline dose fields on a no-action row makes later audits look like a
    hidden dose was planned, even though the top-level command was no pump.
    """
    dose_plan_keys = {
        "dose_ml",
        "duration_ms",
        "requested_dose_ml_before_cap",
        "max_dose_ml_per_cycle",
        "dose_cap_hit",
        "mixing_window",
    }
    return {key: value for key, value in metadata.items() if key not in dose_plan_keys}


def _is_emergency_deviation(
    payload: SensorPayload,
    reference_range: ReferenceRange,
) -> bool:
    """Return True when pH or EC is in the moderate-or-worse deviation band."""
    ph_min = reference_range.ph_target_min - settings.ph_emergency_buffer_below
    ph_max = reference_range.ph_target_max + settings.ph_emergency_buffer_above
    ec_min = reference_range.ec_target_min - settings.ec_emergency_buffer_below
    ec_max = reference_range.ec_target_max + settings.ec_emergency_buffer_above
    return (
        payload.ph <= ph_min
        or payload.ph >= ph_max
        or payload.ec <= ec_min
        or payload.ec >= ec_max
    )


def _is_within_mixing_window(recent_logs: list[SensorHistoryEntry]) -> bool:
    """Return True if a dose was given within the configured mixing window.

    Prevents the emergency guard from firing repeatedly while the previous
    dose is still mixing and pH has not yet stabilized.
    """
    if not recent_logs:
        return False
    now = datetime.now(timezone.utc)
    for entry in recent_logs:
        if entry.pump_activated in {None, "none"}:
            continue
        started_at = entry.action_started_at or entry.timestamp
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        effective_seconds = _effective_mixing_seconds_for_entry(entry)
        if now - started_at < timedelta(seconds=effective_seconds):
            return True
    return False


def _effective_mixing_seconds_for_entry(entry: SensorHistoryEntry) -> int:
    """Return the row-specific mixing guard, falling back to current config."""
    mixing_window = entry.decision_metadata.get("mixing_window") or {}
    effective_seconds = mixing_window.get("effective_seconds")
    if isinstance(effective_seconds, int | float) and effective_seconds > 0:
        return round(effective_seconds)
    return settings.default_mixing_time_seconds


def _phase3_mixing_window_metadata() -> dict[str, int | float | str]:
    """Describe the deterministic Phase 3 post-dose guard used by ESP32 and batch."""
    return {
        "base_seconds": settings.default_mixing_time_seconds,
        "effective_seconds": settings.default_mixing_time_seconds,
        "adjustment_factor": 1.0,
        "source": "phase3_deterministic_guard",
        "reason": "Phase 3 waits for the configured reservoir mixing window before another correction.",
    }
