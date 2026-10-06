"""Background asyncio task that runs the full agentic pipeline on the configured interval."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger
from psycopg2.extensions import connection

from app.core.config import settings
from app.database.connection import get_db_connection
from app.database.repositories.sensor_repository import (
    AGENTIC_CYCLE_LOCK_KEY,
    PostgresSensorReadingRepository,
)
from app.schemas.sensor import SensorHistoryEntry, SensorPayload
from app.services.agentic_ai.graph_engine import evaluate_agentic_decision
from app.services.agentic_ai.llm import OpenAIJsonLLM
from app.services.crop_lifecycle import build_crop_lifecycle_context
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


BATCH_SCHEDULER_LOCK_KEY = AGENTIC_CYCLE_LOCK_KEY
_SCHEDULER_STARTED_AT: datetime | None = None
_SCHEDULER_NEXT_RUN_AT: datetime | None = None
_SCHEDULER_LAST_RUN_STARTED_AT: datetime | None = None
_SCHEDULER_LAST_RUN_FINISHED_AT: datetime | None = None
_SCHEDULER_LAST_STATUS = "not_started"
_SCHEDULER_LAST_MESSAGE = "Batch scheduler has not started in this backend process."
_LAST_BATCH_SKIP_MESSAGE = "Batch cycle has not run yet."


async def run_batch_analysis_loop() -> None:
    """Sleep for the configured interval, then run one agentic pipeline cycle, forever."""
    global _SCHEDULER_STARTED_AT, _SCHEDULER_NEXT_RUN_AT
    _SCHEDULER_STARTED_AT = datetime.now(timezone.utc)
    interval_delta = timedelta(seconds=settings.batch_analysis_interval_seconds)
    _SCHEDULER_NEXT_RUN_AT = _SCHEDULER_STARTED_AT + interval_delta
    logger.info(
        "Batch agentic scheduler started (interval={}s, window={} readings).",
        settings.batch_analysis_interval_seconds,
        settings.batch_analysis_window_readings,
    )
    while True:
        _SCHEDULER_NEXT_RUN_AT = datetime.now(timezone.utc) + interval_delta
        await asyncio.sleep(settings.batch_analysis_interval_seconds)
        await asyncio.to_thread(_execute_batch_cycle)


def _execute_batch_cycle() -> None:
    """Wrapper called by the background loop — logs result but discards return value."""
    global _SCHEDULER_LAST_RUN_STARTED_AT, _SCHEDULER_LAST_RUN_FINISHED_AT
    global _SCHEDULER_LAST_STATUS, _SCHEDULER_LAST_MESSAGE
    _SCHEDULER_LAST_RUN_STARTED_AT = datetime.now(timezone.utc)
    result = run_batch_cycle()
    _SCHEDULER_LAST_RUN_FINISHED_AT = datetime.now(timezone.utc)
    if result:
        _SCHEDULER_LAST_STATUS = "completed"
        _SCHEDULER_LAST_MESSAGE = (
            f"Saved batch cycle #{result['control_cycle_id']} with decision "
            f"{result['decision']}."
        )
        logger.info(
            "Batch agentic cycle saved log_id={} control_cycle_id={} decision={} pump={} duration_ms={} batch_pending={}.",
            result["log_id"],
            result["control_cycle_id"],
            result["decision"],
            result["pump_activated"],
            result["duration_ms"],
            result["batch_pending"],
        )
    else:
        _SCHEDULER_LAST_STATUS = "skipped"
        _SCHEDULER_LAST_MESSAGE = f"Scheduled batch skipped: {_LAST_BATCH_SKIP_MESSAGE}"


def get_batch_scheduler_state() -> dict[str, Any]:
    """Return runtime scheduler timing for dashboard diagnostics."""
    now = datetime.now(timezone.utc)
    next_run_seconds = (
        max(0, int((_SCHEDULER_NEXT_RUN_AT - now).total_seconds()))
        if _SCHEDULER_NEXT_RUN_AT is not None
        else None
    )
    return {
        "batch_scheduler_configured": settings.batch_analysis_enabled,
        "batch_scheduler_running": _SCHEDULER_STARTED_AT is not None,
        "batch_scheduler_started_at": (
            _SCHEDULER_STARTED_AT.isoformat() if _SCHEDULER_STARTED_AT else None
        ),
        "batch_scheduler_next_run_at": (
            _SCHEDULER_NEXT_RUN_AT.isoformat() if _SCHEDULER_NEXT_RUN_AT else None
        ),
        "batch_scheduler_next_run_seconds": next_run_seconds,
        "batch_scheduler_last_run_started_at": (
            _SCHEDULER_LAST_RUN_STARTED_AT.isoformat()
            if _SCHEDULER_LAST_RUN_STARTED_AT
            else None
        ),
        "batch_scheduler_last_run_finished_at": (
            _SCHEDULER_LAST_RUN_FINISHED_AT.isoformat()
            if _SCHEDULER_LAST_RUN_FINISHED_AT
            else None
        ),
        "batch_scheduler_last_status": _SCHEDULER_LAST_STATUS,
        "batch_scheduler_last_message": _SCHEDULER_LAST_MESSAGE,
        "batch_last_skip_message": _LAST_BATCH_SKIP_MESSAGE,
    }


def get_last_batch_skip_message() -> str:
    """Return the most recent reason a batch cycle returned no saved decision."""
    return _LAST_BATCH_SKIP_MESSAGE


def run_batch_cycle(
    *,
    force: bool = False,
    trigger_source: str = "batch_scheduler",
) -> dict[str, Any] | None:
    """Run one full agentic pipeline cycle and return a summary dict.

    Returns None when the cycle is skipped (no API key, no readings, or error).
    The returned dict contains enough information for the manual trigger endpoint
    to report back to the caller.
    """
    logger.info(
        "Batch agentic cycle starting at {}.",
        datetime.now(timezone.utc).isoformat(),
    )

    if not settings.openai_api_key:
        logger.warning("Batch agentic cycle skipped: OPENAI_API_KEY not configured.")
        return _skip_batch("OPENAI_API_KEY is not configured.")

    try:
        with get_db_connection() as conn:
            if not _try_acquire_batch_lock(conn):
                logger.info("Batch agentic cycle skipped: another scheduler worker is already running.")
                return _skip_batch("Another scheduler worker is already running this batch cycle.")

            repo = PostgresSensorReadingRepository(conn)

            try:
                repo.expire_stale_batch_pending_commands(
                    settings.default_control_strategy,
                    settings.batch_pending_command_expiry_seconds,
                )

                if emergency_stop_enabled(repo):
                    repo.emergency_stop_active_commands(settings.default_control_strategy)
                    logger.warning("Batch agentic cycle skipped: emergency stop is enabled.")
                    return _skip_batch("Emergency stop is enabled; batch LLM actuation is disabled.")

                if maintenance_mode_enabled(repo):
                    logger.info("Batch agentic cycle skipped: maintenance mode is enabled.")
                    return _skip_batch("Maintenance mode is enabled; batch LLM actuation is disabled.")

                if monitoring_mode_enabled(repo):
                    logger.info("Batch agentic cycle skipped: Monitoring Only is enabled.")
                    return _skip_batch(
                        "Monitoring Only is enabled; AI analysis and automatic dosing are paused."
                    )

                if experiment_preflight_required(repo, settings.default_control_strategy):
                    logger.warning("Batch agentic cycle skipped: experiment preflight is required.")
                    return _skip_batch(
                        "Experiment preflight is required; choose whether to continue or start a new run."
                    )

                if trigger_source == "batch_scheduler" and full_agentic_mode_enabled(repo):
                    logger.info("Scheduled batch skipped: Full Agentic Mode handles each sensor reading.")
                    return _skip_batch(
                        "Full Agentic Mode is enabled; scheduled batch analysis is paused."
                    )

                if not force and repo.has_recent_batch_analysis(
                    settings.default_control_strategy,
                    settings.batch_analysis_interval_seconds,
                ):
                    logger.info(
                        "Batch agentic cycle skipped: a scheduler decision already exists "
                        "inside the current {}s interval.",
                        settings.batch_analysis_interval_seconds,
                    )
                    return _skip_batch("A scheduled batch decision already exists inside the current interval.")

                recent_or_active_pump = repo.get_recent_or_active_pump_command(
                    settings.default_mixing_time_seconds,
                )
                if recent_or_active_pump is not None:
                    logger.warning(
                        "Batch agentic cycle skipped: pump command is active or still inside "
                        "the mixing window. control_cycle_id={} status={} pump={} dose_ml={} "
                        "age_seconds={}. Waiting for physical mixing before allowing an agentic batch dose.",
                        recent_or_active_pump["control_cycle_id"],
                        recent_or_active_pump["status"],
                        recent_or_active_pump["pump_activated"],
                        recent_or_active_pump["dose_ml"],
                        recent_or_active_pump["age_seconds"],
                    )
                    return _skip_batch(
                        "A pump command is active or still inside the mixing window; waiting before another batch dose."
                    )

                unresolved_batch = repo.get_unresolved_batch_command(settings.default_control_strategy)
                if unresolved_batch is not None:
                    logger.warning(
                        "Batch agentic cycle skipped: unresolved batch pump command exists. "
                        "control_cycle_id={} status={} pump={} dose_ml={} age_seconds={}. "
                        "Waiting for ESP32 completion before allowing another batch dose.",
                        unresolved_batch["control_cycle_id"],
                        unresolved_batch["status"],
                        unresolved_batch["pump_activated"],
                        unresolved_batch["dose_ml"],
                        unresolved_batch["age_seconds"],
                    )
                    return _skip_batch("An unresolved batch pump command is still waiting for ESP32 completion.")

                # Newest-first; fetch extra rows because prior batch decisions
                # are synthetic control rows and must not become source readings
                # for the next agentic batch.
                candidate_readings = repo.get_recent_sensor_logs(
                    limit=max(
                        settings.agentic_control_history_limit,
                        settings.batch_analysis_window_readings * 3,
                        30,
                    ),
                    control_strategy=settings.default_control_strategy,
                )
                raw_readings, control_history = _batch_histories(
                    candidate_readings,
                    source_limit=settings.batch_analysis_window_readings,
                    control_limit=settings.agentic_control_history_limit,
                )

                if not raw_readings:
                    logger.info("Batch agentic cycle: no source readings found, skipping.")
                    return _skip_batch("No source sensor readings were available for batch analysis.")

                reference_range = repo.get_active_reference_range(
                    settings.default_control_strategy
                )
                crop_lifecycle = build_crop_lifecycle_context(repo, reference_range)
                latest = raw_readings[0]
                latest_timestamp = latest.timestamp
                if latest_timestamp.tzinfo is None:
                    latest_timestamp = latest_timestamp.replace(tzinfo=timezone.utc)
                age_seconds = (datetime.now(timezone.utc) - latest_timestamp).total_seconds()
                if age_seconds < 0 or age_seconds > settings.agentic_history_freshness_gap_seconds:
                    return _skip_batch("Latest source sensor reading is stale or future-dated; no dosing is allowed.")
                history = list(reversed(raw_readings))

                if settings.force_fixed_reservoir_volume:
                    reservoir_volume = settings.fixed_reservoir_volume_liters
                else:
                    reservoir_volume = latest.reservoir_volume_liters
                if not settings.force_fixed_reservoir_volume and (
                    reservoir_volume is None or reservoir_volume <= 0
                ):
                    logger.warning(
                        "Batch agentic cycle: latest reservoir volume is missing or not positive; "
                        "skipping control decision."
                    )
                    return _skip_batch(
                        "Latest reservoir volume is missing or not positive."
                    )
                if not settings.force_fixed_reservoir_volume and (
                    reservoir_volume > settings.default_reservoir_max_volume_liters
                ):
                    logger.warning(
                        "Batch agentic cycle: reservoir volume %.2f L exceeds configured "
                        "maximum %.2f L; skipping control decision.",
                        reservoir_volume,
                        settings.default_reservoir_max_volume_liters,
                    )
                    return _skip_batch(
                        "Latest reservoir volume exceeds the configured reservoir maximum."
                    )

                payload = _batch_payload_from_latest(latest, reservoir_volume)

                # Build LLM adapter directly so the pipeline always uses LLM regardless
                # of the agentic_ai_use_llm flag (which is False for per-reading calls).
                llm_adapter = OpenAIJsonLLM(
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
                    history,
                    control_history=control_history,
                    llm=llm_adapter,
                    crop_lifecycle=crop_lifecycle,
                )

                if emergency_stop_enabled(repo):
                    repo.emergency_stop_active_commands(settings.default_control_strategy)
                    return _skip_batch("Emergency stop was enabled during analysis; no command was saved.")
                if maintenance_mode_enabled(repo):
                    return _skip_batch("Maintenance mode was enabled during analysis; no command was saved.")
                if decision.pump_activated != "none" and repo.get_recent_or_active_pump_command(
                    settings.default_mixing_time_seconds
                ) is not None:
                    return _skip_batch("A physical command became active during analysis; no command was saved.")
                if monitoring_mode_enabled(repo):
                    logger.info(
                        "Batch agentic result discarded because Monitoring Only was enabled during analysis."
                    )
                    return _skip_batch(
                        "Monitoring Only was enabled during analysis; no command was saved."
                    )

                decision.metadata["triggered_by"] = trigger_source
                decision.metadata["batch_trigger_mode"] = "manual" if force else "scheduled"
                decision.metadata["agentic_mode"] = "batch_llm"
                decision.metadata["actuation_source"] = trigger_source
                decision.metadata["batch_window_readings"] = len(raw_readings)
                decision.metadata["batch_candidate_rows_read"] = len(candidate_readings)
                decision = hold_for_human_review(
                    payload,
                    decision,
                    settings.default_control_strategy,
                    hitl_enabled=runtime_bool_setting(
                        repo,
                        "hitl_enabled",
                        default=settings.human_in_the_loop_enabled,
                    ),
                )

                sensor_log = repo.create_sensor_log(payload, decision)
                maybe_send_decision_alert(
                    payload,
                    decision,
                    log_id=sensor_log.log_id,
                    control_cycle_id=sensor_log.control_cycle_id,
                    notification_log_writer=repo,
                )

                batch_pending = decision.pump_activated != "none" and decision.duration_ms > 0
                if batch_pending:
                    repo.mark_control_cycle_batch_pending(sensor_log.control_cycle_id)
            finally:
                _release_batch_lock(conn)

        return {
            "log_id": sensor_log.log_id,
            "control_cycle_id": sensor_log.control_cycle_id,
            "decision": decision.decision,
            "pump_activated": decision.pump_activated,
            "duration_ms": decision.duration_ms,
            "batch_pending": batch_pending,
        }

    except Exception as exc:
        logger.error("Batch agentic cycle failed: {}.", exc)
        return _skip_batch(f"Batch agentic cycle failed: {type(exc).__name__}.")


def _skip_batch(message: str) -> None:
    """Record why a batch cycle produced no saved decision."""
    global _LAST_BATCH_SKIP_MESSAGE
    _LAST_BATCH_SKIP_MESSAGE = message
    return None


def _batch_payload_from_latest(
    latest: SensorHistoryEntry,
    reservoir_volume_liters: float,
) -> SensorPayload:
    """Rebuild the batch payload without discarding saved stability evidence.

    Near-boundary corrections require proof that the current metric has stayed
    stable long enough to distinguish a persistent excursion from probe noise.
    The raw sensor row already contains that evidence, so the scheduled batch
    must preserve it instead of replacing it with ``None``.
    """
    return SensorPayload(
        temperature=latest.temperature or 0.0,
        ph=latest.ph,
        ec=latest.ec,
        reservoir_volume_liters=reservoir_volume_liters,
        ph_stable_for_seconds=latest.ph_stable_for_seconds,
        ec_stable_for_seconds=latest.ec_stable_for_seconds,
        # Thresholds are not part of SensorHistoryEntry. SensorPayload falls
        # back to the production profile's configured thresholds here.
        ph_stability_threshold=None,
        ec_stability_threshold=None,
        control_strategy=settings.default_control_strategy,
    )


def _try_acquire_batch_lock(conn: connection) -> bool:
    """Return True only for the one DB session allowed to run a batch cycle."""
    with conn.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s);", (BATCH_SCHEDULER_LOCK_KEY,))
        row = cursor.fetchone()
    return bool(row and row[0])


def _release_batch_lock(conn: connection) -> None:
    """Release the session-level batch lock before closing the DB connection."""
    with conn.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_unlock(%s);", (BATCH_SCHEDULER_LOCK_KEY,))


def _is_batch_source_reading(reading: Any) -> bool:
    """Return True for physical sensor readings usable as batch input history."""
    if _is_maintenance_reading(reading):
        return False
    metadata = getattr(reading, "decision_metadata", None)
    if not isinstance(metadata, dict):
        return True
    return metadata.get("triggered_by") not in {"batch_scheduler", "manual_batch_trigger"}


def _is_maintenance_reading(reading: Any) -> bool:
    """Return True for readings captured while maintenance mode was active."""
    if getattr(reading, "decision", None) == "maintenance_mode":
        return True
    if getattr(reading, "status", None) == "maintenance_mode":
        return True
    metadata = getattr(reading, "decision_metadata", None)
    if not isinstance(metadata, dict):
        return False
    return (
        metadata.get("triggered_by") == "maintenance_mode"
        or metadata.get("maintenance_mode_enabled") is True
    )


def _batch_histories(
    candidate_readings: list[Any],
    *,
    source_limit: int,
    control_limit: int,
) -> tuple[list[Any], list[Any]]:
    """Keep synthetic pump rows for control response, but not sensor trends."""
    raw_readings = [
        reading for reading in candidate_readings if _is_batch_source_reading(reading)
    ][:source_limit]
    control_history = list(
        reversed(
            [reading for reading in candidate_readings if not _is_maintenance_reading(reading)][
                :control_limit
            ]
        )
    )
    return raw_readings, control_history
