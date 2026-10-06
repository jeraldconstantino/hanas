"""Scheduled operator summary generation for the Overview dashboard."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import re
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger
from psycopg2.extensions import connection
from pydantic import BaseModel, Field

from app.core.config import settings
from app.database.connection import get_db_connection
from app.database.repositories.sensor_repository import PostgresSensorReadingRepository
from app.schemas.sensor import OverviewSummary, SensorHistoryEntry
from app.services.agentic_ai.llm import OpenAIJsonLLM
from app.services.agentic_ai.prompts import OVERVIEW_SUMMARIZER_AGENT_PROMPT
from app.services.crop_lifecycle import build_crop_lifecycle_context
from app.services.runtime_settings import emergency_stop_enabled, maintenance_mode_enabled


OVERVIEW_SUMMARY_SETTING_KEY = "overview_summary_latest"
OVERVIEW_SUMMARY_LOCK_KEY = 340_240_512
OVERVIEW_SUMMARY_DATA_POLICY_VERSION = 4
AUTOMATIC_SUMMARY_TRIGGER_SOURCES = {
    "summary_scheduler",
    "summary_startup_catchup",
}
RESERVOIR_CONSUMPTION_WINDOW = timedelta(hours=3)
RESERVOIR_MINIMUM_TREND_SECONDS = 10 * 60
RESERVOIR_REFILL_JUMP_MINIMUM_LITERS = 0.5
RESERVOIR_MEANINGFUL_CHANGE_LITERS = 0.05
RESERVOIR_WARNING_USABLE_PERCENT = 30
RESERVOIR_WARNING_HOURS = 24

_SUMMARY_STARTED_AT: datetime | None = None
_SUMMARY_NEXT_RUN_AT: datetime | None = None
_SUMMARY_LAST_RUN_STARTED_AT: datetime | None = None
_SUMMARY_LAST_RUN_FINISHED_AT: datetime | None = None
_SUMMARY_LAST_STATUS = "not_started"
_SUMMARY_LAST_MESSAGE = "Overview summary scheduler has not started in this backend process."


class _SummaryNarrative(BaseModel):
    """LLM-produced narrative fields for the daily operator summary."""

    title: str = Field(max_length=90)
    summary: str = Field(max_length=520)
    highlights: list[str] = Field(default_factory=list, max_length=4)
    risks: list[str] = Field(default_factory=list, max_length=4)
    recommended_actions: list[str] = Field(default_factory=list, max_length=4)
    trend_notes: list[str] = Field(default_factory=list, max_length=4)
    anomaly_events: list[str] = Field(default_factory=list, max_length=4)
    dosing_events: list[str] = Field(default_factory=list, max_length=4)


async def run_overview_summary_loop() -> None:
    """Generate Overview summaries at fixed local 12 AM / 12 PM anchors."""
    global _SUMMARY_STARTED_AT, _SUMMARY_NEXT_RUN_AT

    _SUMMARY_STARTED_AT = datetime.now(timezone.utc)
    logger.info(
        "Overview summary scheduler started (anchored at 12:00 AM and 12:00 PM {}).",
        settings.app_timezone,
    )

    await asyncio.to_thread(_execute_startup_summary_catchup)

    while True:
        _SUMMARY_NEXT_RUN_AT = _next_summary_run_at(datetime.now(timezone.utc))
        sleep_seconds = max(1, (_SUMMARY_NEXT_RUN_AT - datetime.now(timezone.utc)).total_seconds())
        await asyncio.sleep(sleep_seconds)
        await asyncio.to_thread(_execute_summary_cycle)


def get_overview_summary_scheduler_state() -> dict[str, Any]:
    """Return runtime summary scheduler timing for the dashboard."""
    now = datetime.now(timezone.utc)
    next_run_at = _SUMMARY_NEXT_RUN_AT
    if settings.overview_summary_enabled and next_run_at is None:
        next_run_at = _next_summary_run_at(now)
    next_run_seconds = (
        max(0, int((next_run_at - now).total_seconds()))
        if next_run_at is not None
        else None
    )
    return {
        "overview_summary_enabled": settings.overview_summary_enabled,
        "overview_summary_interval_seconds": settings.overview_summary_interval_seconds,
        "overview_summary_scheduler_running": _SUMMARY_STARTED_AT is not None,
        "overview_summary_scheduler_next_run_at": next_run_at,
        "overview_summary_scheduler_next_run_seconds": next_run_seconds,
        "overview_summary_scheduler_last_run_started_at": _SUMMARY_LAST_RUN_STARTED_AT,
        "overview_summary_scheduler_last_run_finished_at": _SUMMARY_LAST_RUN_FINISHED_AT,
        "overview_summary_scheduler_last_status": _SUMMARY_LAST_STATUS,
        "overview_summary_scheduler_last_message": _SUMMARY_LAST_MESSAGE,
    }


def _next_summary_run_at(now_utc: datetime) -> datetime:
    """Return the next fixed 12 AM / 12 PM local run as UTC."""
    app_tz = ZoneInfo(settings.app_timezone)
    now_local = _as_utc(now_utc).astimezone(app_tz)
    noon = now_local.replace(hour=12, minute=0, second=0, microsecond=0)
    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

    if now_local < noon and now_local >= midnight:
        next_local = noon
    else:
        next_day = now_local + timedelta(days=1)
        next_local = next_day.replace(hour=0, minute=0, second=0, microsecond=0)

    return next_local.astimezone(timezone.utc)


def _previous_summary_run_at(now_utc: datetime) -> datetime:
    """Return the most recent fixed 12 AM / 12 PM local run as UTC."""
    app_tz = ZoneInfo(settings.app_timezone)
    now_local = _as_utc(now_utc).astimezone(app_tz)
    noon = now_local.replace(hour=12, minute=0, second=0, microsecond=0)
    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

    if now_local >= noon:
        previous_local = noon
    else:
        previous_local = midnight

    return previous_local.astimezone(timezone.utc)


def get_latest_overview_summary() -> OverviewSummary | None:
    """Return the latest persisted Overview summary, if present."""
    with get_db_connection() as conn:
        repo = PostgresSensorReadingRepository(conn)
        value = repo.get_system_setting(OVERVIEW_SUMMARY_SETTING_KEY)
        if value is None:
            return None
        summary = OverviewSummary.model_validate(value)
        if not _summary_matches_current_logs(repo, summary):
            logger.info("Ignoring stale Overview summary after system log reset or newer reading.")
            return None
        return summary


def _execute_startup_summary_catchup() -> None:
    """Backfill the latest fixed summary anchor after a backend restart."""
    global _SUMMARY_LAST_RUN_STARTED_AT, _SUMMARY_LAST_RUN_FINISHED_AT
    global _SUMMARY_LAST_STATUS, _SUMMARY_LAST_MESSAGE

    now = datetime.now(timezone.utc)
    previous_anchor = _previous_summary_run_at(now)

    try:
        with get_db_connection() as conn:
            repo = PostgresSensorReadingRepository(conn)
            if emergency_stop_enabled(repo):
                logger.info("Overview summary startup catch-up skipped: emergency stop is enabled.")
                return
            if maintenance_mode_enabled(repo):
                logger.info("Overview summary startup catch-up skipped: maintenance mode is enabled.")
                return

            existing_value = repo.get_system_setting(OVERVIEW_SUMMARY_SETTING_KEY)
            if existing_value is not None:
                existing = OverviewSummary.model_validate(existing_value)
                if _as_utc(existing.generated_at) >= previous_anchor and _summary_matches_current_logs(repo, existing):
                    logger.info(
                        "Overview summary startup catch-up skipped: latest fixed anchor already has a summary."
                    )
                    return

            period_start = previous_anchor - timedelta(
                seconds=settings.overview_summary_interval_seconds
            )
            readings = repo.get_sensor_logs_since(
                period_start,
                limit=1,
                control_strategy=settings.default_control_strategy,
            )
            readings = [
                reading for reading in readings
                if _as_utc(reading.timestamp) <= previous_anchor
                and not _is_maintenance_reading(reading)
            ]
            if not readings:
                logger.info("Overview summary startup catch-up skipped: no readings in latest fixed window.")
                return
    except Exception as exc:
        logger.warning("Overview summary startup catch-up check failed: {}.", exc)
        return

    _SUMMARY_LAST_RUN_STARTED_AT = datetime.now(timezone.utc)
    summary = run_overview_summary_cycle(
        trigger_source="summary_startup_catchup",
        period_end=previous_anchor,
    )
    _SUMMARY_LAST_RUN_FINISHED_AT = datetime.now(timezone.utc)

    if summary is None:
        _SUMMARY_LAST_STATUS = "skipped"
        _SUMMARY_LAST_MESSAGE = "Startup summary catch-up skipped: no readings were available."
        return

    _SUMMARY_LAST_STATUS = "completed"
    _SUMMARY_LAST_MESSAGE = (
        f"Startup summary catch-up generated {summary.reading_count} readings "
        f"and {summary.dosing_event_count} dosing events."
    )


def run_overview_summary_cycle(
    *,
    trigger_source: str = "summary_scheduler",
    period_end: datetime | None = None,
) -> OverviewSummary | None:
    """Generate and persist one operator summary for the current local day."""
    logger.info("Overview summary cycle starting at {}.", datetime.now(timezone.utc).isoformat())

    try:
        with get_db_connection() as conn:
            if not _try_acquire_summary_lock(conn):
                logger.info("Overview summary cycle skipped: another worker is already running.")
                return None

            repo = PostgresSensorReadingRepository(conn)
            try:
                if (
                    trigger_source in AUTOMATIC_SUMMARY_TRIGGER_SOURCES
                    and emergency_stop_enabled(repo)
                ):
                    logger.info("Overview summary cycle skipped: emergency stop is enabled.")
                    return None

                if (
                    trigger_source in AUTOMATIC_SUMMARY_TRIGGER_SOURCES
                    and maintenance_mode_enabled(repo)
                ):
                    logger.info("Overview summary cycle skipped: maintenance mode is enabled.")
                    return None

                summary = _build_summary(
                    repo,
                    trigger_source=trigger_source,
                    period_end=period_end,
                )
                if summary is None:
                    return None
                repo.set_system_setting(
                    OVERVIEW_SUMMARY_SETTING_KEY,
                    summary.model_dump(mode="json"),
                )
                return summary
            finally:
                _release_summary_lock(conn)
    except Exception as exc:
        logger.error("Overview summary cycle failed: {}.", exc)
        return None


def _execute_summary_cycle() -> None:
    """Background wrapper that updates scheduler status."""
    global _SUMMARY_LAST_RUN_STARTED_AT, _SUMMARY_LAST_RUN_FINISHED_AT
    global _SUMMARY_LAST_STATUS, _SUMMARY_LAST_MESSAGE

    _SUMMARY_LAST_RUN_STARTED_AT = datetime.now(timezone.utc)
    summary = run_overview_summary_cycle()
    _SUMMARY_LAST_RUN_FINISHED_AT = datetime.now(timezone.utc)

    if summary is None:
        _SUMMARY_LAST_STATUS = "skipped"
        _SUMMARY_LAST_MESSAGE = "Summary run skipped: no readings yet or backend logs show an error."
        return

    _SUMMARY_LAST_STATUS = "completed"
    _SUMMARY_LAST_MESSAGE = (
        f"Generated Overview summary with {summary.reading_count} readings "
        f"and {summary.dosing_event_count} dosing events."
    )
    logger.info(
        "Overview summary saved readings={} dosing_events={} llm_used={}.",
        summary.reading_count,
        summary.dosing_event_count,
        summary.llm_used,
    )


def _build_summary(
    repo: PostgresSensorReadingRepository,
    *,
    trigger_source: str,
    period_end: datetime | None = None,
) -> OverviewSummary | None:
    period_end = _as_utc(period_end or datetime.now(timezone.utc))
    period_start = period_end - timedelta(seconds=settings.overview_summary_interval_seconds)

    readings = repo.get_sensor_logs_since(
        period_start,
        limit=settings.overview_summary_max_readings,
        control_strategy=settings.default_control_strategy,
    )
    readings = sorted(readings, key=lambda reading: _as_utc(reading.timestamp))
    readings = [
        reading for reading in readings
        if _as_utc(reading.timestamp) <= period_end
        and not _is_maintenance_reading(reading)
    ]
    if not readings:
        return None

    reference_range = repo.get_active_reference_range(settings.default_control_strategy)
    metrics = _summary_metrics(readings, reference_range)
    metrics["crop_lifecycle"] = _summary_crop_lifecycle_context(
        build_crop_lifecycle_context(repo, reference_range)
    )
    llm_narrative = _llm_narrative(metrics)
    narrative = llm_narrative or _deterministic_narrative(metrics)

    return OverviewSummary(
        data_policy_version=OVERVIEW_SUMMARY_DATA_POLICY_VERSION,
        generated_at=datetime.now(timezone.utc),
        period_start=period_start,
        period_end=period_end,
        title=narrative.title,
        summary=narrative.summary,
        highlights=narrative.highlights[:4],
        risks=narrative.risks[:4],
        recommended_actions=narrative.recommended_actions[:4],
        trend_notes=narrative.trend_notes[:4],
        anomaly_events=narrative.anomaly_events[:4],
        dosing_events=narrative.dosing_events[:4],
        reading_count=metrics["reading_count"],
        dosing_event_count=metrics["dosing_event_count"],
        total_dose_ml=round(metrics["total_dose_ml"], 2),
        ph_min=metrics["ph_min"],
        ph_max=metrics["ph_max"],
        ec_min=metrics["ec_min"],
        ec_max=metrics["ec_max"],
        reservoir_min_liters=metrics["reservoir_min_liters"],
        reservoir_max_liters=metrics["reservoir_max_liters"],
        latest_reservoir_liters=metrics["latest"].get("reservoir_liters"),
        latest_reservoir_percent=metrics["latest"].get("reservoir_percent"),
        llm_used=llm_narrative is not None,
        model=settings.agentic_ai_model if llm_narrative is not None else None,
    )


def _summary_matches_current_logs(
    repo: PostgresSensorReadingRepository,
    summary: OverviewSummary,
) -> bool:
    """Return False when a persisted summary no longer matches current logs."""
    if summary.data_policy_version != OVERVIEW_SUMMARY_DATA_POLICY_VERSION:
        return False

    readings = repo.get_sensor_logs_since(
        summary.period_start,
        limit=max(summary.reading_count + 1, 2),
        control_strategy=settings.default_control_strategy,
    )
    readings = [
        reading for reading in readings
        if not _is_maintenance_reading(reading)
    ]
    if not readings:
        return False
    if len(readings) < summary.reading_count:
        return False
    if _as_utc(readings[0].timestamp) > _as_utc(summary.generated_at):
        return False

    return True


def _is_maintenance_reading(reading: SensorHistoryEntry) -> bool:
    """Return whether a stored reading was captured during maintenance."""
    metadata = reading.decision_metadata
    triggered_by = metadata.get("triggered_by") if isinstance(metadata, dict) else None
    maintenance_enabled = (
        metadata.get("maintenance_mode_enabled")
        if isinstance(metadata, dict)
        else None
    )
    return (
        reading.decision == "maintenance_mode"
        or reading.status == "maintenance_mode"
        or triggered_by == "maintenance_mode"
        or maintenance_enabled is True
        or (
            isinstance(maintenance_enabled, str)
            and maintenance_enabled.strip().lower() == "true"
        )
    )


def _summary_metrics(readings: list[SensorHistoryEntry], reference_range: Any) -> dict[str, Any]:
    readings = sorted(readings, key=lambda reading: _as_utc(reading.timestamp))
    ph_values = [float(r.ph) for r in readings]
    ec_values = [float(r.ec) for r in readings]
    temp_values = [float(r.temperature) for r in readings if r.temperature is not None]
    reservoir_values = [
        float(r.reservoir_volume_liters)
        for r in readings
        if r.reservoir_volume_liters is not None
    ]
    dose_commands = [
        r for r in readings
        if (r.pump_activated or "none") != "none" and float(r.dose_ml or 0) > 0
    ]
    # A planned command is not evidence of a completed dose. Estimated
    # completions remain explicitly distinguished from device-confirmed ones.
    dose_events = [r for r in dose_commands if r.status in {"completed", "completed_estimated"}]
    decision_counts = Counter(r.decision or "unknown" for r in readings)
    status_counts = Counter(r.status or "unknown" for r in readings)
    dose_by_pump = Counter()
    for event in dose_events:
        dose_by_pump[_pump_label(event.pump_activated)] += float(event.dose_ml or 0)
    latest = readings[-1]
    ph_in_range = sum(reference_range.ph_target_min <= r.ph <= reference_range.ph_target_max for r in readings)
    ec_in_range = sum(reference_range.ec_target_min <= r.ec <= reference_range.ec_target_max for r in readings)
    temperature_in_range = (
        sum(18 <= float(r.temperature) <= 26 for r in readings if r.temperature is not None)
        if temp_values else 0
    )
    temperature_count = len(temp_values)
    anomaly_events = _summary_anomaly_events(readings, reference_range)
    dosing_events = _summary_dosing_events(dose_events)
    trend_notes = [
        _metric_trend_note("pH", ph_values, "pH"),
        _metric_trend_note("EC", ec_values, "mS/cm"),
    ]
    if temp_values:
        trend_notes.append(_metric_trend_note("Water Temperature", temp_values, "°C"))
    if reservoir_values:
        trend_notes.append(_metric_trend_note("Reservoir Volume", reservoir_values, "L"))

    reservoir_capacity = float(settings.default_reservoir_max_volume_liters or 0)
    latest_reservoir = (
        float(latest.reservoir_volume_liters)
        if latest.reservoir_volume_liters is not None
        else None
    )
    latest_reservoir_percent = (
        round(latest_reservoir / reservoir_capacity * 100)
        if latest_reservoir is not None and reservoir_capacity > 0
        else None
    )
    reservoir_operating = _reservoir_operating_metrics(
        readings,
        latest_reservoir=latest_reservoir,
        capacity_liters=reservoir_capacity,
        minimum_liters=float(settings.minimum_pumpable_reservoir_volume_liters),
    )

    return {
        "reading_count": len(readings),
        "dosing_event_count": len(dose_events),
        "dose_command_count": len(dose_commands),
        "unconfirmed_dose_command_count": sum(r.status not in {"completed", "completed_estimated", "cancelled", "rejected", "failed"} for r in dose_commands),
        "estimated_dosing_event_count": sum(r.status == "completed_estimated" for r in dose_events),
        "total_dose_ml": sum(float(r.dose_ml or 0) for r in dose_events),
        "ph_min": round(min(ph_values), 2),
        "ph_max": round(max(ph_values), 2),
        "ec_min": round(min(ec_values), 2),
        "ec_max": round(max(ec_values), 2),
        "temperature_min": round(min(temp_values), 1) if temp_values else None,
        "temperature_max": round(max(temp_values), 1) if temp_values else None,
        "reservoir_min_liters": round(min(reservoir_values), 1) if reservoir_values else None,
        "reservoir_max_liters": round(max(reservoir_values), 1) if reservoir_values else None,
        "reservoir_capacity_liters": reservoir_capacity or None,
        **reservoir_operating,
        "latest": {
            "ph": float(latest.ph),
            "ec": float(latest.ec),
            "temperature": float(latest.temperature) if latest.temperature is not None else None,
            "reservoir_liters": latest_reservoir,
            "reservoir_percent": latest_reservoir_percent,
            "decision": latest.decision,
            "status": latest.status,
            "is_stable": (latest.decision_metadata.get("monitoring_agent") or {}).get("is_stable")
            if isinstance(latest.decision_metadata.get("monitoring_agent"), dict) else None,
        },
        "ph_in_range_percent": round(ph_in_range / len(readings) * 100),
        "ec_in_range_percent": round(ec_in_range / len(readings) * 100),
        "temperature_in_range_percent": (
            round(temperature_in_range / temperature_count * 100)
            if temperature_count else None
        ),
        "decision_counts": dict(decision_counts.most_common(6)),
        "status_counts": dict(status_counts.most_common(6)),
        "dose_by_pump_ml": {pump: round(total, 2) for pump, total in dose_by_pump.most_common(6)},
        "trend_notes": trend_notes,
        "anomaly_events": anomaly_events,
        "dosing_events": dosing_events,
        "worst_deviations": _worst_deviations(readings, reference_range),
        "reference_range": {
            "ph_min": reference_range.ph_target_min,
            "ph_max": reference_range.ph_target_max,
            "ec_min": reference_range.ec_target_min,
            "ec_max": reference_range.ec_target_max,
            "temperature_min": 18,
            "temperature_max": 26,
        },
    }


def _reservoir_operating_metrics(
    readings: list[SensorHistoryEntry],
    *,
    latest_reservoir: float | None,
    capacity_liters: float,
    minimum_liters: float,
) -> dict[str, float | str | None]:
    """Estimate pumpable headroom and recent reservoir consumption."""
    usable_capacity = max(capacity_liters - minimum_liters, 0)
    headroom = (
        max(latest_reservoir - minimum_liters, 0)
        if latest_reservoir is not None else None
    )
    usable_percent = (
        round(min(headroom / usable_capacity, 1) * 100)
        if headroom is not None and usable_capacity > 0 else None
    )

    points = sorted(
        (
            (_as_utc(reading.timestamp), float(reading.reservoir_volume_liters))
            for reading in readings
            if reading.reservoir_volume_liters is not None
        ),
        key=lambda point: point[0],
    )
    rate_liters_per_hour: float | None = None
    if len(points) >= 3:
        latest_time = points[-1][0]
        points = [
            point for point in points
            if latest_time - point[0] <= RESERVOIR_CONSUMPTION_WINDOW
        ]
        refill_jump = max(
            RESERVOIR_REFILL_JUMP_MINIMUM_LITERS,
            capacity_liters * 0.01,
        )
        last_refill_index = 0
        for index in range(1, len(points)):
            if points[index][1] - points[index - 1][1] >= refill_jump:
                last_refill_index = index
        points = points[last_refill_index:]
        if len(points) >= 3:
            elapsed_seconds = (points[-1][0] - points[0][0]).total_seconds()
            decline_intervals = sum(
                previous[1] - current[1] >= RESERVOIR_MEANINGFUL_CHANGE_LITERS
                for previous, current in zip(points, points[1:])
            )
            if (
                elapsed_seconds >= RESERVOIR_MINIMUM_TREND_SECONDS
                and decline_intervals >= 2
            ):
                elapsed_values = [
                    (timestamp - points[0][0]).total_seconds()
                    for timestamp, _volume in points
                ]
                volume_values = [volume for _timestamp, volume in points]
                mean_elapsed = sum(elapsed_values) / len(elapsed_values)
                mean_volume = sum(volume_values) / len(volume_values)
                variance = sum(
                    (elapsed - mean_elapsed) ** 2 for elapsed in elapsed_values
                )
                covariance = sum(
                    (elapsed - mean_elapsed) * (volume - mean_volume)
                    for elapsed, volume in zip(elapsed_values, volume_values)
                )
                slope_liters_per_second = covariance / variance if variance > 0 else 0
                rate_liters_per_hour = round(
                    max(-slope_liters_per_second * 3600, 0),
                    2,
                )

    hours_to_minimum = (
        round(headroom / rate_liters_per_hour, 1)
        if headroom is not None and rate_liters_per_hour and rate_liters_per_hour > 0
        else None
    )
    if latest_reservoir is None:
        status = "unknown"
    elif capacity_liters > 0 and latest_reservoir > capacity_liters:
        status = "overfilled"
    elif latest_reservoir <= minimum_liters:
        status = "critical"
    elif (
        usable_percent is not None
        and usable_percent <= RESERVOIR_WARNING_USABLE_PERCENT
    ) or (
        hours_to_minimum is not None
        and hours_to_minimum <= RESERVOIR_WARNING_HOURS
    ):
        status = "warning"
    else:
        status = "sufficient"

    return {
        "reservoir_minimum_pumpable_liters": round(minimum_liters, 1),
        "reservoir_pumpable_headroom_liters": round(headroom, 1) if headroom is not None else None,
        "reservoir_usable_remaining_percent": usable_percent,
        "reservoir_recent_consumption_liters_per_hour": rate_liters_per_hour,
        "reservoir_estimated_hours_to_minimum": hours_to_minimum,
        "reservoir_operating_status": status,
    }


def _metric_trend_note(label: str, values: list[float], unit: str) -> str:
    if not values:
        return f"{label}: no readings available."
    first = values[0]
    latest = values[-1]
    delta = latest - first
    if label == "pH":
        threshold = 0.03
    elif label == "Reservoir Volume":
        threshold = 0.5
    else:
        threshold = 0.05
    if abs(delta) < threshold:
        direction = "held steady"
    elif delta > 0:
        direction = "moved upward"
    else:
        direction = "moved downward"
    return (
        f"{label} {direction}: {first:.2f} to {latest:.2f} {unit} "
        f"(change {delta:+.2f} {unit})."
    )


def _summary_anomaly_events(readings: list[SensorHistoryEntry], reference_range: Any) -> list[str]:
    events: list[str] = []

    worst_ph = max(
        readings,
        key=lambda r: max(float(reference_range.ph_target_min - r.ph), float(r.ph - reference_range.ph_target_max), 0),
    )
    ph_gap = max(
        float(reference_range.ph_target_min - worst_ph.ph),
        float(worst_ph.ph - reference_range.ph_target_max),
        0,
    )
    if ph_gap > 0:
        side = "below" if worst_ph.ph < reference_range.ph_target_min else "above"
        events.append(
            f"{_local_time(worst_ph.timestamp)}: pH {float(worst_ph.ph):.2f} was {ph_gap:.2f} {side} target."
        )

    worst_ec = max(
        readings,
        key=lambda r: max(float(reference_range.ec_target_min - r.ec), float(r.ec - reference_range.ec_target_max), 0),
    )
    ec_gap = max(
        float(reference_range.ec_target_min - worst_ec.ec),
        float(worst_ec.ec - reference_range.ec_target_max),
        0,
    )
    if ec_gap > 0:
        side = "below" if worst_ec.ec < reference_range.ec_target_min else "above"
        events.append(
            f"{_local_time(worst_ec.timestamp)}: EC {float(worst_ec.ec):.2f} mS/cm was {ec_gap:.2f} {side} target."
        )

    temp_readings = [r for r in readings if r.temperature is not None]
    if temp_readings:
        worst_temp = max(
            temp_readings,
            key=lambda r: max(18 - float(r.temperature or 0), float(r.temperature or 0) - 26, 0),
        )
        temp = float(worst_temp.temperature or 0)
        temp_gap = max(18 - temp, temp - 26, 0)
        if temp_gap > 0:
            side = "below" if temp < 18 else "above"
            events.append(
                f"{_local_time(worst_temp.timestamp)}: water temperature {temp:.1f}°C was {temp_gap:.1f}°C {side} optimal."
            )

    return events[:4]


def _summary_dosing_events(dose_events: list[SensorHistoryEntry]) -> list[str]:
    latest_events = dose_events[-4:]
    return [
        (
            f"{_local_time(event.timestamp)}: {_pump_label(event.pump_activated)} "
            f"{float(event.dose_ml or 0):.2f} mL"
            f"{' (estimated completion)' if event.status == 'completed_estimated' else ''}."
        )
        for event in latest_events
    ]


def _worst_deviations(readings: list[SensorHistoryEntry], reference_range: Any) -> dict[str, Any]:
    return {
        "ph": _max_metric_deviation(
            readings,
            "ph",
            float(reference_range.ph_target_min),
            float(reference_range.ph_target_max),
        ),
        "ec": _max_metric_deviation(
            readings,
            "ec",
            float(reference_range.ec_target_min),
            float(reference_range.ec_target_max),
        ),
    }


def _max_metric_deviation(
    readings: list[SensorHistoryEntry],
    field: str,
    target_min: float,
    target_max: float,
) -> dict[str, Any] | None:
    worst: SensorHistoryEntry | None = None
    worst_gap = 0.0
    for reading in readings:
        value = float(getattr(reading, field))
        gap = max(target_min - value, value - target_max, 0)
        if gap > worst_gap:
            worst_gap = gap
            worst = reading
    if worst is None:
        return None
    value = float(getattr(worst, field))
    return {
        "time": _local_time(worst.timestamp),
        "value": round(value, 2),
        "deviation": round(worst_gap, 2),
        "direction": "below" if value < target_min else "above",
    }


def _pump_label(pump: str | None) -> str:
    return {
        "ph_up": "pH Up",
        "ph_down": "pH Down",
        "ec_up": "EC Up",
        "ec_down": "EC Down",
        None: "No pump",
        "none": "No pump",
    }.get(pump, str(pump).replace("_", " ").title())


def _local_time(value: datetime) -> str:
    local = _as_utc(value).astimezone(ZoneInfo(settings.app_timezone))
    return local.strftime("%I:%M %p")


def _llm_narrative(metrics: dict[str, Any]) -> _SummaryNarrative | None:
    if not settings.openai_api_key:
        return None

    try:
        llm = OpenAIJsonLLM(
            settings.openai_api_key,
            settings.agentic_ai_model,
            settings.agentic_ai_fallback_model,
            settings.agentic_ai_llm_timeout_seconds,
        )
        result = llm.complete_json(
            "overview_summarizer_agent",
            OVERVIEW_SUMMARIZER_AGENT_PROMPT,
            {"metrics": metrics},
            _SummaryNarrative,
        )
        return _normalize_operator_narrative(result.output, metrics)
    except Exception as exc:
        logger.warning("Overview LLM summary failed, using deterministic fallback: {}.", exc)
        return None


def _normalize_operator_narrative(
    narrative: _SummaryNarrative,
    metrics: dict[str, Any] | None = None,
) -> _SummaryNarrative:
    """Normalize terminology and value-dependent phrasing in operator summaries."""
    summary = narrative.summary.strip()
    replacements = (
        (r"^The hydroponic system is operating within normal parameters,?\s*(with\s*)?", ""),
        (r"^The hydroponic system is operating normally,?\s*(with\s*)?", ""),
        (r"^The hydroponic system is operating\s*", ""),
        (r"^The system is operating within normal parameters,?\s*(with\s*)?", ""),
        (r"^The system is operating normally,?\s*(with\s*)?", ""),
    )
    for pattern, replacement in replacements:
        summary = re.sub(pattern, replacement, summary, flags=re.IGNORECASE)
    if summary and not summary.startswith("pH"):
        summary = summary[:1].upper() + summary[1:]
    summary = re.sub(r"\bnormal parameters\b", "target ranges", summary, flags=re.IGNORECASE)
    summary = re.sub(r"\boverall environment\b", "reservoir condition", summary, flags=re.IGNORECASE)

    def fix_temperature_label(match: re.Match[str]) -> str:
        prefix = match.group("prefix")
        word = match.group("word")
        value = float(match.group("value"))
        if value > 26 and word.lower() == "low":
            word = "high"
        elif value < 18 and word.lower() == "high":
            word = "low"
        return f"{prefix}{word} at {value:.1f}°C"

    summary = re.sub(
        r"(?P<prefix>water temperature is (?:currently )?)(?P<word>low|high) at (?P<value>\d+(?:\.\d+)?)°C",
        fix_temperature_label,
        summary,
        flags=re.IGNORECASE,
    )

    updates: dict[str, Any] = {"summary": summary}
    reservoir_status = metrics.get("reservoir_operating_status") if metrics else None
    if metrics is not None and reservoir_status in {"warning", "critical", "overfilled"}:
        reservoir_sentence = _reservoir_narrative_sentence(metrics)
        summary_sentences = re.split(r"(?<=[.!?])\s+", summary)
        summary_without_unsafe_reservoir_claim = " ".join(
            sentence for sentence in summary_sentences
            if not (
                ("reservoir" in sentence.lower() or "water level" in sentence.lower())
                and any(
                    claim in sentence.lower()
                    for claim in ("sufficient", "adequate", "healthy", "comfortable")
                )
            )
        ).strip()
        if "pumpable minimum" not in summary_without_unsafe_reservoir_claim.lower():
            available = max(0, 520 - len(reservoir_sentence) - 2)
            trimmed_summary = summary_without_unsafe_reservoir_claim
            if len(trimmed_summary) > available:
                trimmed_summary = trimmed_summary[:available].rsplit(" ", 1)[0]
            trimmed_summary = trimmed_summary.rstrip(" ,;:")
            if trimmed_summary and trimmed_summary[-1] not in ".!?":
                trimmed_summary += "."
            updates["summary"] = f"{trimmed_summary} {reservoir_sentence}".strip()
        if narrative.title.strip().lower() not in {
            "system needs attention",
            "operator review needed",
        }:
            updates["title"] = "System needs attention"
        if reservoir_status == "critical":
            reservoir_risk = "Reservoir is at or below the configured pumpable minimum."
        elif reservoir_status == "overfilled":
            reservoir_risk = "Reservoir reading exceeds configured capacity."
        else:
            reservoir_risk = "Reservoir pumpable headroom is approaching the configured minimum."
        updates["risks"] = [reservoir_risk, *narrative.risks][:4]
        updates["recommended_actions"] = [
            (
                "Verify the water-level sensor and correct the reservoir volume."
                if reservoir_status == "overfilled"
                else "Plan a reservoir refill and verify the water-level sensor trend."
            ),
            *narrative.recommended_actions,
        ][:4]
    if metrics is not None and not _crop_lifecycle_configured(metrics):
        updates["summary"] = _remove_unconfigured_crop_claims(str(updates["summary"]))
        for field in (
            "highlights",
            "risks",
            "recommended_actions",
            "trend_notes",
            "anomaly_events",
            "dosing_events",
        ):
            updates[field] = _remove_unconfigured_crop_claim_items(
                list(updates.get(field, getattr(narrative, field)))
            )

    if metrics is not None and metrics.get("reference_range"):
        safe = _deterministic_narrative(metrics)
        latest = metrics.get("latest") or {}
        limits = metrics["reference_range"]
        for metric in ("ph", "ec", "temperature"):
            value = latest.get(metric)
            lower, upper = limits.get(f"{metric}_min"), limits.get(f"{metric}_max")
            if value is not None and lower is not None and upper is not None and not lower <= value <= upper:
                text = str(updates["summary"]).lower()
                direction = "low" if value < lower else "high"
                if (f"{value:g}" not in text or direction not in text
                        or re.search(r"all (?:readings|parameters).*within", text)):
                    return safe
        if latest.get("decision") in {"wait_human_review", "sensor_anomaly"} or latest.get("status") in {"wait_human_review", "sensor_anomaly"}:
            return safe
        if narrative.title == "System stable" and safe.title != "System stable":
            return safe
        if updates.get("title", narrative.title) not in {
            "System stable", "System needs attention", "Operator review needed", "Summary unavailable"
        }:
            updates["title"] = safe.title
        if (len(str(updates["summary"])) > 520 or len(str(updates["summary"]).split()) > 95
                or not 2 <= len(re.findall(r"[.!?](?:\s|$)", str(updates["summary"]))) <= 4):
            return safe
    normalized = _SummaryNarrative.model_validate({**narrative.model_dump(), **updates})
    if len(normalized.summary.split()) > 95:
        raise ValueError("Overview recap exceeds the 95-word limit.")
    return normalized


def _summary_crop_lifecycle_context(context: dict[str, Any]) -> dict[str, Any]:
    """Hide default harvest timing from LLM summaries until crop age is configured."""
    if context.get("age_days") is not None:
        return context

    sanitized = dict(context)
    sanitized.update(
        {
            "transplant_date": None,
            "age_days": None,
            "stage": "not_configured",
            "stage_label": "Not configured",
            "stage_guidance": (
                "Transplant date is not set; do not infer crop stage, crop age, "
                "or harvest timing."
            ),
            "harvest_start_day": None,
            "harvest_end_day": None,
            "days_until_harvest_window": None,
            "days_past_harvest_window": None,
            "source_note": (
                "Crop age is unavailable because no valid transplant date is set."
            ),
        }
    )
    return sanitized


def _crop_lifecycle_configured(metrics: dict[str, Any]) -> bool:
    lifecycle = metrics.get("crop_lifecycle") or {}
    return (
        isinstance(lifecycle, dict)
        and lifecycle.get("age_days") is not None
        and bool(lifecycle.get("transplant_date"))
        and lifecycle.get("stage") != "not_configured"
    )


def _remove_unconfigured_crop_claims(summary: str) -> str:
    """Remove LLM crop timeline claims when no transplant date backs them."""
    sentences = re.split(r"(?<=[.!?])\s+", summary.strip())
    kept = [
        sentence
        for sentence in sentences
        if sentence and not _is_unconfigured_crop_claim(sentence)
    ]
    return " ".join(kept).strip() or summary


def _remove_unconfigured_crop_claim_items(items: list[str]) -> list[str]:
    return [item for item in items if not _is_unconfigured_crop_claim(item)]


def _is_unconfigured_crop_claim(text: str) -> bool:
    lowered = text.lower()
    setup_reminder = (
        "set transplant date" in lowered
        or "transplant date is not set" in lowered
        or "transplant date not set" in lowered
        or "crop age is unavailable" in lowered
    )
    if setup_reminder:
        return False

    claim_patterns = (
        r"\bafter transplant(?:ing|ed)?\b",
        r"\bvegetative\b",
        r"\bestablishment\b",
        r"\bharvest(?:\s+window|\s+expected|\s+starts|\s+ends)?\b",
        r"\bcrop age\b",
        r"\bday\s+\d+\b",
        r"\b\d+\s*-\s*\d+\s+days\b",
    )
    return any(re.search(pattern, lowered) for pattern in claim_patterns)


def _deterministic_narrative(metrics: dict[str, Any]) -> _SummaryNarrative:
    """Build a bounded recap from latest facts, never window-majority labels."""
    latest = metrics.get("latest") or {}
    limits = metrics.get("reference_range") or {}
    concerns: list[str] = []
    for metric, label, unit in (("ph", "pH", ""), ("ec", "EC", " mS/cm"),
                                ("temperature", "Water temperature", "°C")):
        value = latest.get(metric)
        lower, upper = limits.get(f"{metric}_min"), limits.get(f"{metric}_max")
        if value is not None and lower is not None and upper is not None:
            if value < lower or value > upper:
                direction = "low" if value < lower else "high"
                concerns.append(f"Latest recorded {label} is {direction} at {value:g}{unit}.")

    reservoir_status = metrics.get("reservoir_operating_status", "unknown")
    reservoir_concern = reservoir_status in {"warning", "critical", "overfilled", "unknown"}
    review = latest.get("decision") == "wait_human_review" or latest.get("status") == "wait_human_review"
    anomaly = latest.get("decision") == "sensor_anomaly" or latest.get("status") == "sensor_anomaly"
    history_concerns = list(metrics.get("anomaly_events") or [])
    stable = (latest.get("is_stable") is True and not concerns and not reservoir_concern
              and not review and not anomaly and not history_concerns
              and latest.get("temperature") is not None
              and latest.get("ph") is not None and latest.get("ec") is not None
              and latest.get("decision") == "within_range"
              and latest.get("status") not in {"dosing", "mixing", "error", "failed"}
              and all(limits.get(f"{metric}_{bound}") is not None
                      for metric in ("ph", "ec", "temperature") for bound in ("min", "max")))
    title = "System stable" if stable else "System needs attention"
    if review:
        title = "Operator review needed"
    sentences: list[str] = []
    reservoir_sentence = _reservoir_narrative_sentence(metrics)
    if anomaly:
        sentences.append("Latest recorded readings flag a sensor anomaly.")
    if reservoir_concern and reservoir_sentence:
        sentences.append(reservoir_sentence)
    sentences.extend(concerns)
    if not concerns and not anomaly:
        if all(latest.get(k) is not None and limits.get(f"{k}_min") is not None
               and limits.get(f"{k}_max") is not None for k in ("ph", "ec")):
            sentences.append(f"Latest recorded pH {latest['ph']:g} and EC {latest['ec']:g} mS/cm are on target.")
        else:
            sentences.append("Latest pH or EC range context is unavailable.")
    if review:
        note = "Validate readings, selected pump, proposed dose, and safety state before acting."
    elif concerns or anomaly:
        note = "Validate the readings and sensor condition before acting."
    elif reservoir_concern and not reservoir_sentence:
        note = "Validate the water level and operating reserve."
    elif history_concerns:
        note = "Review earlier window anomalies and validate the latest readings."
    elif not stable:
        note = "Validate reading stability and operating condition."
    else:
        note = "Continue monitoring the recorded condition."
    # Reservoir text includes its specific validation; avoid a fifth sentence.
    if reservoir_status in {"warning", "critical", "overfilled"} and not review and not concerns and not anomaly:
        note = ""
    if reservoir_concern and (concerns or anomaly) and not reservoir_sentence:
        note = "Validate readings, sensors, and water level before acting."
    essential = " ".join([*sentences, *([note] if note else [])])
    # Optional history details never displace the concern or validation note.
    optional: list[str] = []
    if not reservoir_concern and reservoir_sentence:
        optional.append(reservoir_sentence)
    crop = metrics.get("crop_lifecycle") or {}
    if _crop_lifecycle_configured(metrics) and isinstance(crop.get("stage_label"), str):
        optional.append(f"Crop lifecycle: day {crop['age_days']}, {crop['stage_label'].lower()}.")
    if latest.get("temperature") is None and metrics.get("temperature_min") is not None:
        optional.insert(0, f"Historical temperature ranged from {metrics['temperature_min']:g} to {metrics['temperature_max']:g}°C.")
    summary = essential
    for detail in optional:
        candidate = " ".join([*sentences, detail, *([note] if note else [])])
        if len(candidate) <= 520 and len(candidate.split()) <= 95 and len(re.findall(r"[.!?](?:\s|$)", candidate)) <= 4:
            summary = candidate
            break
    if len(summary) > 520 or len(summary.split()) > 95 or len(re.findall(r"[.!?](?:\s|$)", summary)) > 4:
        # Compact multiple metric concerns into one sentence, preserving values.
        compact = " ".join(concerns).replace(". Latest recorded ", "; ")
        summary = " ".join(filter(None, [
            "Latest readings flag a sensor anomaly." if anomaly else "",
            reservoir_sentence if reservoir_concern else "",
            compact,
            note,
        ]))
    actions = [note] if note and not stable else []
    if reservoir_status == "critical":
        actions.insert(0, "Refill promptly and validate the water level.")
    elif reservoir_status == "warning":
        actions.insert(0, "Plan a reservoir refill and verify the water-level sensor trend.")
    elif reservoir_status == "overfilled":
        actions.insert(0, "Verify the water-level sensor before correcting volume.")
    return _SummaryNarrative(
        title=title, summary=summary,
        highlights=([f"Window pH on target {metrics['ph_in_range_percent']}%; EC {metrics['ec_in_range_percent']}%."]
                    if metrics.get("ph_in_range_percent") is not None and metrics.get("ec_in_range_percent") is not None else []),
        risks=[*concerns, *history_concerns][:4], recommended_actions=actions[:4],
        trend_notes=list(metrics.get("trend_notes") or [])[:2],
        anomaly_events=history_concerns[:2], dosing_events=list(metrics.get("dosing_events") or [])[:2],
    )


def _reservoir_narrative_sentence(metrics: dict[str, Any]) -> str:
    """Return deterministic reservoir wording based on pumpable headroom."""
    latest = metrics.get("latest") or {}
    current = latest.get("reservoir_liters")
    minimum = metrics.get("reservoir_minimum_pumpable_liters")
    if current is None or minimum is None:
        return ""

    status = metrics.get("reservoir_operating_status")
    headroom = metrics.get("reservoir_pumpable_headroom_liters")
    usable_percent = metrics.get("reservoir_usable_remaining_percent")
    rate = metrics.get("reservoir_recent_consumption_liters_per_hour")
    hours = metrics.get("reservoir_estimated_hours_to_minimum")
    if status == "overfilled":
        capacity = metrics.get("reservoir_capacity_liters")
        capacity_label = f"{capacity:.1f} L" if isinstance(capacity, int | float) else "maximum"
        return (
            f"Reservoir volume is {current:g} L, above the configured "
            f"{capacity_label} capacity; verify the level sensor and correct the volume."
        )
    if status == "critical":
        return (
            f"Reservoir volume is {current:.1f} L with {headroom or 0:.1f} L pumpable headroom, at or below the "
            f"{minimum:.1f} L pumpable minimum; refill promptly and verify the "
            "level before further dosing."
        )

    if headroom is None:
        return f"Reservoir reading is {current:.1f} L; validate water level and operating reserve."

    sentence = (
        f"Reservoir is {current:.1f} L, {headroom:.1f} L above the "
        f"{minimum:.1f} L pumpable minimum"
    )
    if usable_percent is not None:
        sentence += f" ({usable_percent}% usable headroom)"
    if rate is not None and rate > 0 and hours is not None:
        sentence += f"; recent decline {rate:.2f} L/h gives about {hours:.1f} h to minimum"
    if status == "warning":
        sentence += "; Plan a refill and verify the level trend"
    sentence += "."
    return sentence


def _try_acquire_summary_lock(conn: connection) -> bool:
    with conn.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s);", (OVERVIEW_SUMMARY_LOCK_KEY,))
        row = cursor.fetchone()
    return bool(row and row[0])


def _release_summary_lock(conn: connection) -> None:
    with conn.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_unlock(%s);", (OVERVIEW_SUMMARY_LOCK_KEY,))


def _as_utc(value: datetime) -> datetime:
    """Return a timezone-aware UTC datetime for summary freshness checks."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
