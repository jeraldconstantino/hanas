"""Repository for persisted sensor readings."""

import json
import math
from hashlib import sha256
from datetime import datetime
from typing import Any, Protocol

from loguru import logger
from psycopg2 import sql
from psycopg2.extensions import connection
from psycopg2.extras import Json
from pydantic import BaseModel

from app.core.config import settings
from app.schemas.control_cycle import (
    HumanReviewPayload,
    HumanReviewResponse,
    LatestControlCycle,
    PendingCommandResponse,
)
from app.schemas.notification import NotificationLogEntry
from app.schemas.sensor import (
    DosingDecision,
    LatestSystemLog,
    ReferenceRange,
    SensorHistoryEntry,
    SensorPayload,
)


AGENTIC_CYCLE_LOCK_KEY = 340_240_501


class SensorLogRecord(BaseModel):
    """Created sensor log identifiers."""

    log_id: int
    control_cycle_id: int


class SensorReadingRepository(Protocol):
    """Persistence contract for sensor readings."""

    def get_active_reference_range(self, control_strategy: str | None = None) -> ReferenceRange:
        """Return the reference range used by the current active experiment run."""

    def create_sensor_log(
        self,
        payload: SensorPayload,
        decision: DosingDecision,
    ) -> SensorLogRecord:
        """Persist one sensor payload and dosing decision, then return created IDs."""

    def get_recent_sensor_logs(
        self,
        limit: int = 20,
        control_strategy: str | None = None,
        since_hours: int | None = None,
        control_cycle_id: int | None = None,
    ) -> list[SensorHistoryEntry]:
        """Return recent sensor logs for history-aware decision strategies."""

    def get_latest_system_log(self) -> LatestSystemLog | None:
        """Return the newest persisted system log for the frontend dashboard."""

    def get_latest_control_cycle(self) -> LatestControlCycle | None:
        """Return the newest control-cycle summary for the frontend dashboard."""

    def mark_control_cycle_action_started(
        self,
        control_cycle_id: int,
        status: str = "dosing",
    ) -> None:
        """Mark the moment the ESP32 starts dosing actuation."""

    def mark_control_cycle_action_completed(
        self,
        control_cycle_id: int,
        status: str = "mixing",
    ) -> None:
        """Mark pump shutdown while the post-dose mixing window continues."""

    def complete_control_cycle(self, control_cycle_id: int, status: str = "completed") -> None:
        """Mark a control cycle complete."""

    def emergency_stop_active_commands(self, control_strategy: str) -> int:
        """Mark active or pending pump commands as emergency stopped."""

    def cancel_pending_commands(self, control_strategy: str, status: str) -> int:
        """Cancel queued commands that have not yet been dispatched to the ESP32."""

    def apply_human_review(
        self,
        control_cycle_id: int,
        payload: HumanReviewPayload,
    ) -> HumanReviewResponse:
        """Apply an operator review to a pending agentic decision."""

    def get_pending_human_review_command(self) -> PendingCommandResponse:
        """Return and dispatch the next queued ESP32 pump command."""

    def create_notification_log(
        self,
        *,
        provider: str,
        recipient_number: str,
        sender_id: str | None,
        alert_type: str,
        message_body: str,
        status: str,
        system_log_id: int,
        control_cycle_id: int,
        provider_response: str | None = None,
        provider_message_id: int | None = None,
        provider_status_updated_at: datetime | None = None,
        error_message: str | None = None,
    ) -> None:
        """Persist one SMS notification attempt."""

    def get_recent_notification_logs(self, limit: int = 20) -> list[NotificationLogEntry]:
        """Return recent notification log entries for the dashboard audit trail."""

    def get_system_setting(self, key: str) -> Any:
        """Return a runtime setting value from the DB, or None if not set."""

    def try_acquire_agentic_cycle_lock(self) -> bool:
        """Acquire the cross-worker lock used by full and scheduled agentic cycles."""

    def release_agentic_cycle_lock(self) -> None:
        """Release the cross-worker agentic cycle lock."""

    def set_system_setting(self, key: str, value: Any) -> None:
        """Persist a runtime setting to the DB (upsert)."""

    def set_exclusive_safety_mode(self, enabled_key: str) -> None:
        """Enable one global safety mode and disable the others atomically."""

    def get_active_experiment_run_summary(
        self,
        control_strategy: str,
    ) -> dict[str, Any] | None:
        """Return operator-facing metadata for the active experiment run."""

    def start_new_experiment_run(self, control_strategy: str) -> dict[str, Any]:
        """Close the active run and create a clean successor without deleting history."""

    def mark_control_cycle_batch_pending(self, control_cycle_id: int) -> None:
        """Mark a batch LLM dosing decision as pending ESP32 pickup."""

    def expire_stale_batch_pending_commands(
        self,
        control_strategy: str,
        max_age_seconds: int,
    ) -> int:
        """Expire batch commands that were never dispatched before their safety window ended."""

    def has_recent_batch_analysis(
        self,
        control_strategy: str,
        interval_seconds: int,
    ) -> bool:
        """Return True when a scheduled batch decision was already saved recently."""

    def has_unresolved_batch_command(self, control_strategy: str) -> bool:
        """Return True when a prior batch pump command is still not complete."""

    def get_unresolved_batch_command(self, control_strategy: str) -> dict[str, Any] | None:
        """Return the latest unresolved batch command for operator visibility."""

    def get_recent_or_active_pump_command(
        self,
        lookback_seconds: int,
    ) -> dict[str, Any] | None:
        """Return any active pump command or recent physical dose inside the mixing window."""

    def get_latest_batch_analysis(self, control_strategy: str) -> dict[str, Any] | None:
        """Return the newest saved batch analysis row for operator visibility."""

    def get_sensor_logs_since(
        self,
        since: datetime,
        *,
        limit: int,
        control_strategy: str | None = None,
    ) -> list[SensorHistoryEntry]:
        """Return sensor logs recorded since a timestamp, oldest first."""


class PostgresSensorReadingRepository:
    """PostgreSQL-backed sensor reading repository."""

    def __init__(self, db_connection: connection) -> None:
        """Create a repository using an existing database connection."""
        self._connection = db_connection
        self._schema = settings.db_schema

    def try_acquire_agentic_cycle_lock(self) -> bool:
        """Allow only one LLM control cycle across backend workers."""
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s);", (AGENTIC_CYCLE_LOCK_KEY,))
            row = cursor.fetchone()
        return bool(row and row[0])

    def release_agentic_cycle_lock(self) -> None:
        """Release the session-level LLM control-cycle lock."""
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s);", (AGENTIC_CYCLE_LOCK_KEY,))

    def get_active_reference_range(self, control_strategy: str | None = None) -> ReferenceRange:
        """Return the reference range used by the current active experiment run."""
        experiment_run_id = self._get_or_create_experiment_run(control_strategy)

        with self._connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                SELECT
                    reference_ranges.id,
                    reference_ranges.crop_type,
                    reference_ranges.growth_stage,
                    reference_ranges.hydroponic_system_type,
                    reference_ranges.ph_target_min,
                    reference_ranges.ph_target_max,
                    reference_ranges.ec_target_min,
                    reference_ranges.ec_target_max,
                    reference_ranges.ph_up_dose_ml_per_liter_per_unit,
                    reference_ranges.ph_down_dose_ml_per_liter_per_unit,
                    reference_ranges.ec_up_dose_ml_per_liter_per_unit,
                    reference_ranges.ec_down_dose_ml_per_liter_per_unit,
                    reference_ranges.ph_pump_flow_ml_per_min,
                    reference_ranges.ec_pump_flow_ml_per_min,
                    reference_ranges.max_dose_ml_per_cycle,
                    reference_ranges.ph_up_max_dose_ml_per_cycle,
                    reference_ranges.ph_down_max_dose_ml_per_cycle,
                    reference_ranges.ec_up_max_dose_ml_per_cycle,
                    reference_ranges.ec_down_max_dose_ml_per_cycle,
                    reference_ranges.ph_up_max_duration_ms,
                    reference_ranges.ph_down_max_duration_ms,
                    reference_ranges.ec_up_max_duration_ms,
                    reference_ranges.ec_down_max_duration_ms
                FROM {} AS experiment_runs
                INNER JOIN {} AS reference_ranges
                    ON reference_ranges.id = experiment_runs.reference_range_id
                WHERE experiment_runs.id = %s;
                    """
                ).format(
                    self._table("experiment_runs"),
                    self._table("reference_ranges"),
                ),
                (experiment_run_id,),
            )
            row = cursor.fetchone()

        return ReferenceRange(
            id=row[0],
            crop_type=row[1],
            growth_stage=row[2],
            hydroponic_system_type=row[3],
            ph_target_min=row[4],
            ph_target_max=row[5],
            ec_target_min=row[6],
            ec_target_max=row[7],
            ph_up_dose_ml_per_liter_per_unit=row[8],
            ph_down_dose_ml_per_liter_per_unit=row[9],
            ec_up_dose_ml_per_liter_per_unit=row[10],
            ec_down_dose_ml_per_liter_per_unit=row[11],
            ph_pump_flow_ml_per_min=row[12],
            ec_pump_flow_ml_per_min=row[13],
            max_dose_ml_per_cycle=row[14],
            ph_up_max_dose_ml_per_cycle=row[15],
            ph_down_max_dose_ml_per_cycle=row[16],
            ec_up_max_dose_ml_per_cycle=row[17],
            ec_down_max_dose_ml_per_cycle=row[18],
            ph_up_max_duration_ms=row[19],
            ph_down_max_duration_ms=row[20],
            ec_up_max_duration_ms=row[21],
            ec_down_max_duration_ms=row[22],
        )

    def create_sensor_log(
        self,
        payload: SensorPayload,
        decision: DosingDecision,
    ) -> SensorLogRecord:
        """Persist one sensor payload and dosing decision, then return created IDs."""
        control_strategy = _control_strategy_for_decision(payload, decision)
        experiment_run_id = self._get_or_create_experiment_run(control_strategy)
        status = _status_for_decision(decision)
        control_cycle_id = self._create_control_cycle(experiment_run_id, status, control_strategy)
        mixing_columns = _mixing_columns_for_decision(decision)

        with self._connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                INSERT INTO {} (
                    experiment_run_id,
                    control_strategy,
                    ph,
                    ec,
                    temperature,
                    reservoir_volume_liters,
                    ph_stable_for_seconds,
                    ec_stable_for_seconds,
                    ph_stability_threshold,
                    ec_stability_threshold,
                    decision,
                    pump_activated,
                    dose_ml,
                    duration_ms,
                    mixing_base_seconds,
                    mixing_effective_seconds,
                    mixing_elapsed_seconds,
                    mixing_remaining_seconds,
                    mixing_adjustment_factor,
                    ph_within_range,
                    ec_within_range,
                    ph_deviation,
                    ec_deviation,
                    decision_reason,
                    decision_metadata,
                    status,
                    control_cycle_id
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s
                )
                RETURNING id;
                    """
                ).format(self._table("system_logs")),
                (
                    experiment_run_id,
                    control_strategy,
                    payload.ph,
                    payload.ec,
                    payload.temperature,
                    payload.reservoir_volume_liters,
                    payload.ph_stable_for_seconds,
                    payload.ec_stable_for_seconds,
                    payload.ph_stability_threshold,
                    payload.ec_stability_threshold,
                    decision.decision,
                    decision.pump_activated,
                    decision.dose_ml,
                    decision.duration_ms,
                    mixing_columns["base_seconds"],
                    mixing_columns["effective_seconds"],
                    mixing_columns["elapsed_seconds"],
                    mixing_columns["remaining_seconds"],
                    mixing_columns["adjustment_factor"],
                    decision.ph_within_range,
                    decision.ec_within_range,
                    decision.ph_deviation,
                    decision.ec_deviation,
                    decision.reason,
                    Json(decision.metadata),
                    status,
                    control_cycle_id,
                ),
            )
            row = cursor.fetchone()

        if decision.pump_activated == "none":
            self.complete_control_cycle(control_cycle_id)

        self._connection.commit()
        return SensorLogRecord(log_id=int(row[0]), control_cycle_id=control_cycle_id)

    def get_recent_sensor_logs(
        self,
        limit: int = 20,
        control_strategy: str | None = None,
        since_hours: int | None = None,
        control_cycle_id: int | None = None,
    ) -> list[SensorHistoryEntry]:
        """Return recent sensor logs for the active experiment run, newest first."""
        control_strategy = _normalize_control_strategy(control_strategy)
        hours_filter = max(1, since_hours) if since_hours is not None else None
        cycle_filter = int(control_cycle_id) if control_cycle_id is not None else None
        experiment_run_id = (
            None if cycle_filter is not None else self._get_active_experiment_run_id(control_strategy)
        )
        if cycle_filter is None and experiment_run_id is None:
            return []

        with self._connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                SELECT
                    system_logs.control_cycle_id,
                    system_logs.timestamp,
                    system_logs.control_strategy,
                    system_logs.ph,
                    system_logs.ec,
                    system_logs.temperature,
                    system_logs.reservoir_volume_liters,
                    system_logs.ph_stable_for_seconds,
                    system_logs.ec_stable_for_seconds,
                    system_logs.decision,
                    system_logs.pump_activated,
                    system_logs.dose_ml,
                    system_logs.duration_ms,
                    system_logs.ph_deviation,
                    system_logs.ec_deviation,
                    control_cycles.action_started_at,
                    control_cycles.action_completed_at,
                    CASE
                        WHEN system_logs.pump_activated <> 'none'
                            THEN COALESCE(control_cycles.status, system_logs.status)
                        ELSE system_logs.status
                    END AS status,
                    system_logs.decision_metadata
                FROM {} AS system_logs
                LEFT JOIN {} AS control_cycles
                    ON control_cycles.id = system_logs.control_cycle_id
                WHERE (
                    (%s IS NOT NULL AND system_logs.control_cycle_id = %s)
                    OR (
                        %s IS NULL
                        AND system_logs.experiment_run_id = %s
                        AND system_logs.control_strategy = %s
                        AND (%s IS NULL OR system_logs.timestamp >= CURRENT_TIMESTAMP - (%s * INTERVAL '1 hour'))
                    )
                )
                ORDER BY system_logs.timestamp DESC
                LIMIT %s;
                    """
                ).format(self._table("system_logs"), self._table("control_cycles")),
                (
                    cycle_filter,
                    cycle_filter,
                    cycle_filter,
                    experiment_run_id,
                    control_strategy,
                    hours_filter,
                    hours_filter,
                    limit,
                ),
            )
            rows = cursor.fetchall()

        return [
            SensorHistoryEntry(
                control_cycle_id=row[0],
                timestamp=row[1],
                control_strategy=row[2],
                ph=row[3],
                ec=row[4],
                temperature=row[5],
                reservoir_volume_liters=row[6],
                ph_stable_for_seconds=row[7],
                ec_stable_for_seconds=row[8],
                decision=row[9],
                pump_activated=row[10],
                dose_ml=row[11],
                duration_ms=row[12],
                ph_deviation=row[13],
                ec_deviation=row[14],
                action_started_at=row[15],
                action_completed_at=row[16],
                status=row[17],
                decision_metadata=row[18] or {},
            )
            for row in rows
        ]

    def get_latest_system_log(self) -> LatestSystemLog | None:
        """Return the newest persisted system log for the frontend dashboard."""
        with self._connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                SELECT
                    system_logs.id,
                    system_logs.control_cycle_id,
                    system_logs.timestamp,
                    system_logs.ph,
                    system_logs.ec,
                    system_logs.temperature,
                    system_logs.reservoir_volume_liters,
                    system_logs.ph_stable_for_seconds,
                    system_logs.ec_stable_for_seconds,
                    system_logs.control_strategy,
                    system_logs.decision,
                    system_logs.pump_activated,
                    system_logs.dose_ml,
                    system_logs.duration_ms,
                    COALESCE(system_logs.mixing_effective_seconds, 0) * 1000 AS mixing_time_ms,
                    system_logs.ph_deviation,
                    system_logs.ec_deviation,
                    CASE
                        WHEN system_logs.pump_activated <> 'none'
                            THEN COALESCE(control_cycles.status, system_logs.status)
                        ELSE system_logs.status
                    END AS status,
                    system_logs.decision_metadata
                FROM {} AS system_logs
                LEFT JOIN {} AS control_cycles
                    ON control_cycles.id = system_logs.control_cycle_id
                ORDER BY system_logs.timestamp DESC
                LIMIT 1;
                    """
                ).format(self._table("system_logs"), self._table("control_cycles"))
            )
            row = cursor.fetchone()

        if row is None:
            return None

        return LatestSystemLog(
            log_id=row[0],
            control_cycle_id=row[1],
            timestamp=row[2],
            ph=row[3],
            ec=row[4],
            temperature=row[5],
            reservoir_volume_liters=row[6],
            ph_stable_for_seconds=row[7],
            ec_stable_for_seconds=row[8],
            control_strategy=row[9],
            decision=row[10],
            pump_activated=row[11],
            dose_ml=row[12] or 0,
            duration_ms=row[13] or 0,
            mixing_time_ms=row[14] or 0,
            ph_deviation=row[15] or 0,
            ec_deviation=row[16] or 0,
            status=row[17] or "unknown",
            decision_metadata=row[18] or {},
        )

    def get_sensor_logs_since(
        self,
        since: datetime,
        *,
        limit: int,
        control_strategy: str | None = None,
    ) -> list[SensorHistoryEntry]:
        """Return operational logs for summary generation, oldest first.

        Maintenance readings remain in ``system_logs`` for auditability, but are
        intentionally omitted here because probe handling, draining, refilling,
        and calibration can produce values that do not represent normal system
        operation.
        """
        control_strategy = _normalize_control_strategy(control_strategy)
        experiment_run_id = self._get_active_experiment_run_id(control_strategy)
        if experiment_run_id is None:
            return []

        with self._connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                SELECT
                    system_logs.timestamp,
                    system_logs.control_strategy,
                    system_logs.ph,
                    system_logs.ec,
                    system_logs.temperature,
                    system_logs.reservoir_volume_liters,
                    system_logs.ph_stable_for_seconds,
                    system_logs.ec_stable_for_seconds,
                    system_logs.decision,
                    system_logs.pump_activated,
                    system_logs.dose_ml,
                    system_logs.duration_ms,
                    system_logs.ph_deviation,
                    system_logs.ec_deviation,
                    control_cycles.action_started_at,
                    control_cycles.action_completed_at,
                    CASE
                        WHEN system_logs.pump_activated <> 'none'
                            THEN COALESCE(control_cycles.status, system_logs.status)
                        ELSE system_logs.status
                    END AS status,
                    system_logs.decision_metadata
                FROM {} AS system_logs
                LEFT JOIN {} AS control_cycles
                    ON control_cycles.id = system_logs.control_cycle_id
                WHERE system_logs.experiment_run_id = %s
                    AND system_logs.control_strategy = %s
                    AND system_logs.timestamp >= %s
                    AND COALESCE(system_logs.decision, '') <> 'maintenance_mode'
                    AND COALESCE(system_logs.status, '') <> 'maintenance_mode'
                    AND COALESCE(
                        system_logs.decision_metadata ->> 'triggered_by',
                        ''
                    ) <> 'maintenance_mode'
                    AND LOWER(COALESCE(
                        system_logs.decision_metadata ->> 'maintenance_mode_enabled',
                        'false'
                    )) <> 'true'
                ORDER BY system_logs.timestamp ASC
                LIMIT %s;
                    """
                ).format(self._table("system_logs"), self._table("control_cycles")),
                (experiment_run_id, control_strategy, since, limit),
            )
            rows = cursor.fetchall()

        return [
            SensorHistoryEntry(
                timestamp=row[0],
                control_strategy=row[1],
                ph=row[2],
                ec=row[3],
                temperature=row[4],
                reservoir_volume_liters=row[5],
                ph_stable_for_seconds=row[6],
                ec_stable_for_seconds=row[7],
                decision=row[8],
                pump_activated=row[9],
                dose_ml=row[10],
                duration_ms=row[11],
                ph_deviation=row[12],
                ec_deviation=row[13],
                action_started_at=row[14],
                action_completed_at=row[15],
                status=row[16],
                decision_metadata=row[17] or {},
            )
            for row in rows
        ]

    def get_latest_control_cycle(self) -> LatestControlCycle | None:
        """Return the newest control-cycle summary for the frontend dashboard."""
        with self._connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                SELECT
                    control_cycles.id,
                    CASE
                        WHEN COALESCE(control_cycles.status, system_logs.status) = 'completed'
                            AND control_cycles.action_completed_at IS NOT NULL
                            AND COALESCE(system_logs.mixing_effective_seconds, 0) > 0
                            AND control_cycles.action_completed_at
                                + (
                                    COALESCE(system_logs.mixing_effective_seconds, 0)
                                    * INTERVAL '1 second'
                                ) > CURRENT_TIMESTAMP
                            THEN 'mixing'
                        ELSE COALESCE(control_cycles.status, system_logs.status, 'unknown')
                    END AS status,
                    COALESCE(system_logs.pump_activated, 'none') AS pump_activated,
                    COALESCE(system_logs.dose_ml, 0) AS dose_ml,
                    COALESCE(system_logs.duration_ms, 0) AS duration_ms,
                    COALESCE(system_logs.mixing_effective_seconds, 0) AS mixing_duration_seconds,
                    CASE
                        WHEN COALESCE(control_cycles.status, system_logs.status) = 'inter_dose_mixing'
                            AND control_cycles.action_started_at IS NOT NULL
                            THEN GREATEST(
                                EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - control_cycles.action_started_at)),
                                0
                            )
                        WHEN control_cycles.action_started_at IS NULL
                            THEN COALESCE(system_logs.mixing_elapsed_seconds, 0)
                        WHEN control_cycles.action_completed_at IS NOT NULL
                            THEN LEAST(
                                COALESCE(system_logs.mixing_effective_seconds, 0),
                                GREATEST(
                                    EXTRACT(
                                        EPOCH FROM (
                                            CURRENT_TIMESTAMP - control_cycles.action_completed_at
                                        )
                                    ),
                                    0
                                )
                            )
                        ELSE LEAST(
                            COALESCE(system_logs.mixing_effective_seconds, 0),
                            GREATEST(EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - control_cycles.action_started_at)), 0)
                        )
                    END AS mixing_elapsed_seconds,
                    COALESCE(control_cycles.action_started_at, control_cycles.started_at) AS action_started_at,
                    control_cycles.action_completed_at
                FROM {} AS control_cycles
                LEFT JOIN {} AS system_logs
                    ON system_logs.control_cycle_id = control_cycles.id
                ORDER BY control_cycles.started_at DESC
                LIMIT 1;
                    """
                ).format(self._table("control_cycles"), self._table("system_logs"))
            )
            row = cursor.fetchone()

        if row is None:
            return None

        action_completed_at = row[8].isoformat() if row[8] is not None else None
        return LatestControlCycle(
            id=row[0],
            status=row[1],
            pump_activated=row[2],
            dose_ml=float(row[3]),
            duration_ms=int(row[4]),
            mixing_duration_seconds=int(row[5]),
            mixing_elapsed_seconds=round(float(row[6] or 0)),
            action_started_at=row[7].isoformat(),
            action_completed_at=action_completed_at,
        )

    def mark_control_cycle_action_started(
        self,
        control_cycle_id: int,
        status: str = "dosing",
    ) -> None:
        """Mark the moment the ESP32 starts dosing actuation."""
        with self._connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                UPDATE {}
                AS cycle SET action_started_at = CASE
                        WHEN status = %s THEN COALESCE(action_started_at, CURRENT_TIMESTAMP)
                        WHEN %s IN ('inter_dose_mixing', 'ec_up_b_dosing')
                            THEN CURRENT_TIMESTAMP
                        ELSE COALESCE(action_started_at, CURRENT_TIMESTAMP)
                    END,
                    status = %s
                WHERE id = %s AND status NOT IN ('completed', 'completed_estimated', 'emergency_stopped', 'cancelled', 'batch_expired', 'rejected', 'human_rejected', 'error', 'failed')
                    AND action_completed_at IS NULL
                    AND (
                        (%s = 'dosing' AND status IN ('dosing', 'batch_dispatched', 'human_command_dispatched'))
                        OR (%s = 'inter_dose_mixing' AND status IN ('dosing', 'inter_dose_mixing'))
                        OR (%s = 'ec_up_b_dosing' AND status IN ('inter_dose_mixing', 'ec_up_b_dosing'))
                    )
                    AND EXISTS (
                        SELECT 1 FROM {} AS command
                        WHERE command.control_cycle_id = cycle.id
                            AND command.pump_activated <> 'none'
                            AND command.duration_ms > 0
                            AND (%s = 'dosing' OR command.pump_activated = 'ec_up')
                    )
                RETURNING id;
                    """
                ).format(self._table("control_cycles"), self._table("system_logs")),
                (status, status, status, control_cycle_id, status, status, status, status),
            )
            if cursor.fetchone() is None:
                raise ControlCycleConflict("Cycle is missing or its state no longer permits this callback.")
            cursor.execute(
                sql.SQL(
                    """
                UPDATE {}
                SET status = %s
                WHERE control_cycle_id = %s
                    AND pump_activated <> 'none';
                    """
                ).format(self._table("system_logs")),
                (status, control_cycle_id),
            )

        self._connection.commit()

    def complete_control_cycle(self, control_cycle_id: int, status: str = "completed") -> None:
        """Mark a control cycle complete."""
        with self._connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                UPDATE {}
                SET completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP),
                    action_completed_at = CASE
                        WHEN action_started_at IS NULL THEN action_completed_at
                        ELSE COALESCE(action_completed_at, CURRENT_TIMESTAMP)
                    END,
                    status = %s
                WHERE id = %s AND (status NOT IN ('completed', 'completed_estimated', 'emergency_stopped', 'cancelled', 'batch_expired', 'rejected', 'human_rejected', 'error', 'failed') OR status = %s)
                RETURNING id;
                    """
                ).format(self._table("control_cycles")),
                (status, control_cycle_id, status),
            )
            if cursor.fetchone() is None:
                raise ControlCycleConflict("Cycle is missing or its state no longer permits this callback.")
            cursor.execute(
                sql.SQL(
                    """
                UPDATE {}
                SET status = %s
                WHERE control_cycle_id = %s
                    AND pump_activated <> 'none';
                    """
                ).format(self._table("system_logs")),
                (status, control_cycle_id),
            )

        self._connection.commit()

    def mark_control_cycle_action_completed(
        self,
        control_cycle_id: int,
        status: str = "mixing",
    ) -> None:
        """Mark pump shutdown without completing the post-dose mixing cycle."""
        with self._connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                UPDATE {}
                SET action_completed_at = COALESCE(action_completed_at, CURRENT_TIMESTAMP),
                    status = %s
                WHERE id = %s AND status NOT IN ('completed', 'completed_estimated', 'emergency_stopped', 'cancelled', 'batch_expired', 'rejected', 'human_rejected', 'error', 'failed') AND action_started_at IS NOT NULL
                RETURNING id;
                    """
                ).format(self._table("control_cycles")),
                (status, control_cycle_id),
            )
            if cursor.fetchone() is None:
                raise ControlCycleConflict("Cycle is missing or its state no longer permits this callback.")
            cursor.execute(
                sql.SQL(
                    """
                UPDATE {}
                SET status = %s
                WHERE control_cycle_id = %s
                    AND pump_activated <> 'none';
                    """
                ).format(self._table("system_logs")),
                (status, control_cycle_id),
            )

        self._connection.commit()

    def emergency_stop_active_commands(self, control_strategy: str) -> int:
        """Resolve pending or active pump commands when the operator engages emergency stop."""
        control_strategy = _normalize_control_strategy(control_strategy)
        experiment_run_id = self._get_active_experiment_run_id(control_strategy)
        if experiment_run_id is None:
            return 0

        with self._connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                WITH stopped AS (
                    UPDATE {} AS control_cycles
                    SET status = 'emergency_stopped',
                        completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP),
                        action_completed_at = COALESCE(action_completed_at, CURRENT_TIMESTAMP)
                    FROM {} AS system_logs
                    WHERE system_logs.control_cycle_id = control_cycles.id
                        AND system_logs.experiment_run_id = %s
                        AND system_logs.control_strategy = %s
                        AND (
                            system_logs.pump_activated <> 'none'
                            OR COALESCE(
                                NULLIF(
                                    system_logs.decision_metadata #>> ARRAY[
                                        'human_in_the_loop',
                                        'pending_decision',
                                        'pump_activated'
                                    ],
                                    'none'
                                ),
                                ''
                            ) <> ''
                        )
                        AND control_cycles.status IN (
                            'waiting',
                            'human_approved_pending_execution',
                            'human_override_pending_execution',
                            'batch_pending',
                            'batch_dispatched',
                            'human_command_dispatched',
                            'dosing',
                            'inter_dose_mixing',
                            'ec_up_b_dosing',
                            'mixing'
                        )
                    RETURNING control_cycles.id
                )
                UPDATE {} AS system_logs
                SET status = 'emergency_stopped'
                FROM stopped
                WHERE system_logs.control_cycle_id = stopped.id;
                    """
                ).format(
                    self._table("control_cycles"),
                    self._table("system_logs"),
                    self._table("system_logs"),
                ),
                (experiment_run_id, control_strategy),
            )
            stopped_count = cursor.rowcount

        self._connection.commit()
        if stopped_count:
            logger.warning("Emergency stop resolved {} active/pending command(s).", stopped_count)
        return stopped_count

    def cancel_pending_commands(self, control_strategy: str, status: str) -> int:
        """Cancel undelivered commands so they cannot execute after a mode change."""
        control_strategy = _normalize_control_strategy(control_strategy)
        experiment_run_id = self._get_active_experiment_run_id(control_strategy)
        if experiment_run_id is None:
            return 0

        with self._connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                WITH cancelled AS (
                    UPDATE {} AS control_cycles
                    SET status = %s,
                        completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP),
                        action_completed_at = COALESCE(action_completed_at, CURRENT_TIMESTAMP)
                    FROM {} AS system_logs
                    WHERE system_logs.control_cycle_id = control_cycles.id
                        AND system_logs.experiment_run_id = %s
                        AND system_logs.control_strategy = %s
                        AND control_cycles.status IN (
                            'waiting',
                            'human_approved_pending_execution',
                            'human_override_pending_execution',
                            'batch_pending'
                        )
                        AND (
                            system_logs.pump_activated <> 'none'
                            OR COALESCE(
                                NULLIF(
                                    system_logs.decision_metadata #>> ARRAY[
                                        'human_in_the_loop',
                                        'pending_decision',
                                        'pump_activated'
                                    ],
                                    'none'
                                ),
                                ''
                            ) <> ''
                        )
                    RETURNING control_cycles.id
                )
                UPDATE {} AS system_logs
                SET status = %s
                FROM cancelled
                WHERE system_logs.control_cycle_id = cancelled.id;
                    """
                ).format(
                    self._table("control_cycles"),
                    self._table("system_logs"),
                    self._table("system_logs"),
                ),
                (status, experiment_run_id, control_strategy, status),
            )
            cancelled_count = cursor.rowcount

        self._connection.commit()
        if cancelled_count:
            logger.info(
                "Cancelled {} queued command(s) with status={}.",
                cancelled_count,
                status,
            )
        return cancelled_count

    def apply_human_review(
        self,
        control_cycle_id: int,
        payload: HumanReviewPayload,
    ) -> HumanReviewResponse:
        """Apply an operator review to a pending agentic decision."""
        with self._connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                SELECT id, decision_metadata, status
                FROM {}
                WHERE control_cycle_id = %s
                ORDER BY timestamp DESC
                LIMIT 1
                FOR UPDATE;
                    """
                ).format(self._table("system_logs")),
                (control_cycle_id,),
            )
            row = cursor.fetchone()

        if row is None:
            raise ValueError(f"Control cycle {control_cycle_id} has no system log.")

        system_log_id = int(row[0])
        if row[2] != "wait_human_review":
            raise InvalidHumanReview("This cycle is no longer awaiting operator review.")
        current_metadata = row[1] or {}
        hitl_metadata = current_metadata.get("human_in_the_loop")
        if not isinstance(hitl_metadata, dict) or hitl_metadata.get("review_required") is not True:
            raise ValueError(f"Control cycle {control_cycle_id} is not awaiting human review.")
        reviewed = _reviewed_decision_from_payload(current_metadata, payload)
        if payload.action in {"approve", "override"}:
            snapshot = hitl_metadata.get("sensor_snapshot")
            strategy = snapshot.get("control_strategy") if isinstance(snapshot, dict) else None
            reference = self.get_active_reference_range(strategy)
            _validate_override(
                HumanReviewPayload(
                    action="override", pump_activated=reviewed["pump_activated"],
                    dose_ml=reviewed["dose_ml"], duration_ms=reviewed["duration_ms"],
                    mixing_time_ms=reviewed["mixing_time_ms"],
                ), reference,
            )

        with self._connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                UPDATE {}
                SET decision = %s,
                    pump_activated = %s,
                    dose_ml = %s,
                    duration_ms = %s,
                    mixing_effective_seconds = %s,
                    decision_reason = %s,
                    decision_metadata = %s,
                    status = %s
                WHERE id = %s;
                    """
                ).format(self._table("system_logs")),
                (
                    reviewed["decision"],
                    reviewed["pump_activated"],
                    reviewed["dose_ml"],
                    reviewed["duration_ms"],
                    reviewed["mixing_time_ms"] / 1000 if reviewed["mixing_time_ms"] else None,
                    reviewed["reason"],
                    Json(reviewed["metadata"]),
                    reviewed["status"],
                    system_log_id,
                ),
            )
            cursor.execute(
                sql.SQL(
                    """
                UPDATE {}
                SET status = %s
                WHERE id = %s;
                    """
                ).format(self._table("control_cycles")),
                (reviewed["status"], control_cycle_id),
            )

        self._connection.commit()
        return HumanReviewResponse(
            status=str(reviewed["status"]),
            control_cycle_id=control_cycle_id,
            decision=str(reviewed["decision"]),
            pump_activated=str(reviewed["pump_activated"]),
            dose_ml=float(reviewed["dose_ml"]),
            duration_ms=int(reviewed["duration_ms"]),
            mixing_time_ms=int(reviewed["mixing_time_ms"]),
            message=str(reviewed["message"]),
        )

    def get_pending_human_review_command(self) -> PendingCommandResponse:
        """Return and dispatch the oldest reviewed HITL or batch LLM command for ESP32 execution."""
        self.expire_stale_batch_pending_commands(
            settings.default_control_strategy,
            settings.batch_pending_command_expiry_seconds,
        )
        hitl_statuses = (
            "human_approved_pending_execution",
            "human_override_pending_execution",
        )
        all_pending_statuses = hitl_statuses + ("batch_pending",)

        with self._connection.cursor() as cursor:
            # Row locks alone allow concurrent polls to pick different commands.
            # Serialize the shared-pump availability check and dispatch together.
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s);",
                (_experiment_run_lock_key(self._schema, "pump_command_dispatch", "all"),),
            )
            cursor.execute(
                sql.SQL(
                    """
                SELECT
                    control_cycles.id,
                    control_cycles.status,
                    system_logs.decision,
                    system_logs.pump_activated,
                    system_logs.dose_ml,
                    system_logs.duration_ms,
                    COALESCE(system_logs.mixing_effective_seconds, 0) * 1000 AS mixing_time_ms,
                    COALESCE(system_logs.decision_metadata ->> 'agentic_mode', 'unknown') AS agentic_mode,
                    COALESCE(system_logs.decision_metadata ->> 'actuation_source', 'unknown') AS actuation_source
                FROM {} AS control_cycles
                INNER JOIN {} AS system_logs
                    ON system_logs.control_cycle_id = control_cycles.id
                WHERE control_cycles.status = ANY(%s)
                    AND system_logs.pump_activated <> 'none'
                    AND COALESCE(system_logs.duration_ms, 0) > 0
                    AND NOT EXISTS (
                        SELECT 1
                        FROM {} AS active_cycles
                        INNER JOIN {} AS active_logs
                            ON active_logs.control_cycle_id = active_cycles.id
                        WHERE active_cycles.id <> control_cycles.id
                            AND active_logs.pump_activated <> 'none'
                            AND COALESCE(active_logs.duration_ms, 0) > 0
                            AND active_cycles.status NOT IN (
                                'human_approved_pending_execution',
                                'human_override_pending_execution',
                                'batch_pending'
                            )
                            AND (
                                active_cycles.status IN (
                                    'batch_dispatched',
                                    'human_command_dispatched',
                                    'dosing',
                                    'inter_dose_mixing',
                                    'ec_up_b_dosing',
                                    'mixing'
                                )
                                OR COALESCE(
                                    active_cycles.action_completed_at,
                                    active_cycles.action_started_at,
                                    active_logs.timestamp
                                ) >= CURRENT_TIMESTAMP - (
                                    GREATEST(
                                        %s,
                                        COALESCE(active_logs.mixing_effective_seconds, 0)
                                    ) * INTERVAL '1 second'
                                )
                            )
                    )
                ORDER BY control_cycles.started_at ASC
                LIMIT 1
                FOR UPDATE OF control_cycles SKIP LOCKED;
                    """
                ).format(
                    self._table("control_cycles"),
                    self._table("system_logs"),
                    self._table("control_cycles"),
                    self._table("system_logs"),
                ),
                (list(all_pending_statuses), settings.default_mixing_time_seconds),
            )
            row = cursor.fetchone()

            if row is None:
                return PendingCommandResponse(status="idle", has_command=False)

            control_cycle_id = int(row[0])
            original_status = row[1]
            is_batch = original_status == "batch_pending"
            dispatch_status = "batch_dispatched" if is_batch else "human_command_dispatched"

            cursor.execute(
                sql.SQL(
                    """
                UPDATE {}
                SET status = %s
                WHERE id = %s;
                    """
                ).format(self._table("control_cycles")),
                (dispatch_status, control_cycle_id),
            )
            cursor.execute(
                sql.SQL(
                    "UPDATE {} SET status = %s WHERE control_cycle_id = %s;"
                ).format(self._table("system_logs")),
                (dispatch_status, control_cycle_id),
            )

        self._connection.commit()
        message = (
            "Batch LLM command dispatched to ESP32."
            if is_batch
            else "Reviewed HITL command dispatched to ESP32."
        )
        return PendingCommandResponse(
            status=dispatch_status,
            has_command=True,
            control_cycle_id=control_cycle_id,
            decision=row[2],
            pump_activated=row[3],
            dose_ml=float(row[4] or 0),
            duration_ms=int(row[5] or 0),
            mixing_time_ms=int(row[6] or 0),
            agentic_mode=str(row[7] or "unknown"),
            actuation_source=str(row[8] or "unknown"),
            message=message,
        )

    def create_notification_log(
        self,
        *,
        provider: str,
        recipient_number: str,
        sender_id: str | None,
        alert_type: str,
        message_body: str,
        status: str,
        system_log_id: int,
        control_cycle_id: int,
        provider_response: str | None = None,
        provider_message_id: int | None = None,
        provider_status_updated_at: datetime | None = None,
        error_message: str | None = None,
    ) -> None:
        """Persist one SMS notification attempt for audit/research evidence."""
        self._ensure_notification_logs_table()
        with self._connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                INSERT INTO {} (
                    provider,
                    recipient_number,
                    sender_id,
                    alert_type,
                    message_body,
                    status,
                    provider_response,
                    provider_message_id,
                    provider_status_updated_at,
                    error_message,
                    system_log_id,
                    control_cycle_id
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                    """
                ).format(self._table("notification_logs")),
                (
                    provider,
                    recipient_number,
                    sender_id,
                    alert_type,
                    message_body,
                    status,
                    provider_response,
                    provider_message_id,
                    provider_status_updated_at,
                    error_message,
                    system_log_id,
                    control_cycle_id,
                ),
            )

        self._connection.commit()

    def get_recent_notification_logs(self, limit: int = 20) -> list[NotificationLogEntry]:
        """Return recent notification log entries for the dashboard audit trail."""
        self._ensure_notification_logs_table()
        with self._connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                SELECT
                    notification_logs.id,
                    notification_logs.timestamp,
                    notification_logs.provider,
                    notification_logs.recipient_number,
                    notification_logs.sender_id,
                    notification_logs.alert_type,
                    notification_logs.message_body,
                    notification_logs.status,
                    notification_logs.provider_response,
                    notification_logs.error_message,
                    notification_logs.system_log_id,
                    notification_logs.control_cycle_id,
                    notification_logs.created_at,
                    notification_logs.provider_message_id,
                    notification_logs.latest_provider_response,
                    notification_logs.provider_status_updated_at,
                    notification_logs.status_checked_at,
                    notification_logs.status_check_count
                FROM {} AS notification_logs
                INNER JOIN {} AS system_logs
                    ON system_logs.id = notification_logs.system_log_id
                ORDER BY notification_logs.timestamp DESC
                LIMIT %s;
                    """
                ).format(self._table("notification_logs"), self._table("system_logs")),
                (limit,),
            )
            rows = cursor.fetchall()

        return [
            NotificationLogEntry(
                id=row[0],
                timestamp=row[1],
                provider=row[2],
                recipient_number=row[3],
                sender_id=row[4],
                alert_type=row[5],
                message_body=row[6],
                status=row[7],
                provider_response=row[8],
                error_message=row[9],
                system_log_id=row[10],
                control_cycle_id=row[11],
                created_at=row[12],
                provider_message_id=row[13],
                latest_provider_response=row[14],
                provider_status_updated_at=row[15],
                status_checked_at=row[16],
                status_check_count=row[17],
            )
            for row in rows
        ]

    def get_pending_notification_status_checks(
        self,
        *,
        limit: int,
        max_age_days: int,
        max_checks: int,
        min_check_interval_seconds: int,
    ) -> list[tuple[int, int]]:
        """Return provider IDs for recent notification states that remain nonterminal."""
        self._ensure_notification_logs_table()
        with self._connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                SELECT id, provider_message_id
                FROM {}
                WHERE provider = 'semaphore'
                  AND LOWER(status) IN ('pending', 'queued', 'submitted')
                  AND provider_message_id IS NOT NULL
                  AND created_at >= CURRENT_TIMESTAMP - (%s * INTERVAL '1 day')
                  AND status_check_count < %s
                  AND (
                      status_checked_at IS NULL
                      OR status_checked_at <= CURRENT_TIMESTAMP - (%s * INTERVAL '1 second')
                  )
                ORDER BY status_checked_at ASC NULLS FIRST, created_at DESC
                LIMIT %s;
                    """
                ).format(self._table("notification_logs")),
                (max_age_days, max_checks, min_check_interval_seconds, limit),
            )
            rows = cursor.fetchall()
        return [(int(row[0]), int(row[1])) for row in rows]

    def update_notification_provider_status(
        self,
        notification_log_id: int,
        *,
        status: str,
        provider_response: str,
    ) -> None:
        """Persist the latest status returned by the SMS provider."""
        self._ensure_notification_logs_table()
        with self._connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                UPDATE {}
                SET status = %s,
                    latest_provider_response = %s,
                    provider_status_updated_at = CURRENT_TIMESTAMP,
                    status_checked_at = CURRENT_TIMESTAMP,
                    status_check_count = status_check_count + 1
                WHERE id = %s;
                    """
                ).format(self._table("notification_logs")),
                (status, provider_response, notification_log_id),
            )
        self._connection.commit()

    def record_notification_status_check_failure(self, notification_log_id: int) -> None:
        """Record a failed provider lookup while leaving the last known status unchanged."""
        self._ensure_notification_logs_table()
        with self._connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                UPDATE {}
                SET status_checked_at = CURRENT_TIMESTAMP,
                    status_check_count = status_check_count + 1
                WHERE id = %s;
                    """
                ).format(self._table("notification_logs")),
                (notification_log_id,),
            )
        self._connection.commit()

    def get_system_setting(self, key: str) -> Any:
        """Return a runtime setting value from the DB, or None if not set."""
        self._ensure_system_settings_table()
        with self._connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("SELECT value FROM {} WHERE key = %s").format(
                    self._table("system_settings")
                ),
                (key,),
            )
            row = cursor.fetchone()
        return json.loads(row[0]) if row is not None else None

    def set_system_setting(self, key: str, value: Any) -> None:
        """Persist a runtime setting to the DB (upsert by key)."""
        self._ensure_system_settings_table()
        with self._connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                INSERT INTO {} (key, value, updated_at)
                VALUES (%s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value,
                        updated_at = CURRENT_TIMESTAMP;
                    """
                ).format(self._table("system_settings")),
                (key, json.dumps(value)),
            )
        self._connection.commit()

    def set_exclusive_safety_mode(self, enabled_key: str) -> None:
        """Enable one safety mode and disable its peers in one transaction."""
        safety_keys = (
            "maintenance_mode_enabled",
            "monitoring_mode_enabled",
            "emergency_stop_enabled",
        )
        if enabled_key not in safety_keys:
            raise ValueError(f"Unknown safety mode: {enabled_key}")

        self._ensure_system_settings_table()
        with self._connection.cursor() as cursor:
            for key in safety_keys:
                cursor.execute(
                    sql.SQL(
                        """
                    INSERT INTO {} (key, value, updated_at)
                    VALUES (%s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (key) DO UPDATE
                        SET value = EXCLUDED.value,
                            updated_at = CURRENT_TIMESTAMP;
                        """
                    ).format(self._table("system_settings")),
                    (key, json.dumps(key == enabled_key)),
                )
        self._connection.commit()

    def _ensure_system_settings_table(self) -> None:
        """Create the system_settings table when it is not present yet."""
        with self._connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                CREATE TABLE IF NOT EXISTS {} (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                    """
                ).format(self._table("system_settings"))
            )
        self._connection.commit()

    def mark_control_cycle_batch_pending(self, control_cycle_id: int) -> None:
        """Mark a batch LLM dosing decision as pending ESP32 pickup."""
        with self._connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "UPDATE {} SET status = 'batch_pending' WHERE id = %s;"
                ).format(self._table("control_cycles")),
                (control_cycle_id,),
            )
            cursor.execute(
                sql.SQL(
                    "UPDATE {} SET status = 'batch_pending' WHERE control_cycle_id = %s;"
                ).format(self._table("system_logs")),
                (control_cycle_id,),
            )
        self._connection.commit()

    def expire_stale_batch_pending_commands(
        self,
        control_strategy: str,
        max_age_seconds: int,
    ) -> int:
        """Expire stale batch commands that were never physically started."""
        control_strategy = _normalize_control_strategy(control_strategy)
        experiment_run_id = self._get_active_experiment_run_id(control_strategy)
        if experiment_run_id is None:
            return 0
        with self._connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                WITH expired AS (
                    UPDATE {} AS control_cycles
                    SET status = 'batch_expired',
                        action_completed_at = COALESCE(action_completed_at, CURRENT_TIMESTAMP)
                    FROM {} AS system_logs
                    WHERE system_logs.control_cycle_id = control_cycles.id
                        AND system_logs.experiment_run_id = %s
                        AND system_logs.control_strategy = %s
                        AND system_logs.decision_metadata ->> 'triggered_by'
                            IN ('batch_scheduler', 'manual_batch_trigger')
                        AND (
                            control_cycles.status = 'batch_pending'
                            OR (
                                control_cycles.status = 'batch_dispatched'
                                AND control_cycles.action_started_at IS NULL
                            )
                        )
                        AND control_cycles.started_at < CURRENT_TIMESTAMP - (%s * INTERVAL '1 second')
                    RETURNING control_cycles.id
                )
                UPDATE {} AS system_logs
                SET status = 'batch_expired'
                FROM expired
                WHERE system_logs.control_cycle_id = expired.id;
                    """
                ).format(
                    self._table("control_cycles"),
                    self._table("system_logs"),
                    self._table("system_logs"),
                ),
                (experiment_run_id, control_strategy, max_age_seconds),
            )
            expired_count = cursor.rowcount
        self._connection.commit()
        if expired_count:
            logger.warning(
                "Expired {} stale batch command(s) older than {}s before physical start.",
                expired_count,
                max_age_seconds,
            )
        return expired_count

    def has_recent_batch_analysis(
        self,
        control_strategy: str,
        interval_seconds: int,
    ) -> bool:
        """Return True if a scheduler batch log exists inside the current interval."""
        control_strategy = _normalize_control_strategy(control_strategy)
        experiment_run_id = self._get_active_experiment_run_id(control_strategy)
        if experiment_run_id is None:
            return False
        with self._connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                SELECT EXISTS (
                    SELECT 1
                    FROM {}
                    WHERE experiment_run_id = %s
                        AND control_strategy = %s
                        AND decision_metadata ->> 'triggered_by' = 'batch_scheduler'
                        AND timestamp >= CURRENT_TIMESTAMP - (%s * INTERVAL '1 second')
                );
                    """
                ).format(self._table("system_logs")),
                (experiment_run_id, control_strategy, interval_seconds),
            )
            row = cursor.fetchone()
        return bool(row and row[0])

    def has_unresolved_batch_command(self, control_strategy: str) -> bool:
        """Return True if a batch pump command is pending, dispatched, dosing, or mixing."""
        return self.get_unresolved_batch_command(control_strategy) is not None

    def get_unresolved_batch_command(self, control_strategy: str) -> dict[str, Any] | None:
        """Return details for the newest unresolved batch command, if any."""
        control_strategy = _normalize_control_strategy(control_strategy)
        experiment_run_id = self._get_active_experiment_run_id(control_strategy)
        if experiment_run_id is None:
            return None
        with self._connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                SELECT
                    system_logs.control_cycle_id,
                    system_logs.id AS system_log_id,
                    system_logs.timestamp,
                    control_cycles.status,
                    control_cycles.action_started_at,
                    control_cycles.action_completed_at,
                    system_logs.decision,
                    COALESCE(
                        NULLIF(system_logs.pump_activated, 'none'),
                        NULLIF(
                            system_logs.decision_metadata #>> ARRAY[
                                'human_in_the_loop',
                                'pending_decision',
                                'pump_activated'
                            ],
                            'none'
                        ),
                        'none'
                    ) AS effective_pump_activated,
                    COALESCE(
                        NULLIF(system_logs.dose_ml, 0),
                        NULLIF(
                            system_logs.decision_metadata #>> ARRAY[
                                'human_in_the_loop',
                                'pending_decision',
                                'dose_ml'
                            ],
                            ''
                        )::NUMERIC,
                        0
                    ) AS effective_dose_ml,
                    COALESCE(
                        NULLIF(system_logs.duration_ms, 0),
                        NULLIF(
                            system_logs.decision_metadata #>> ARRAY[
                                'human_in_the_loop',
                                'pending_decision',
                                'duration_ms'
                            ],
                            ''
                        )::INT,
                        0
                    ) AS effective_duration_ms,
                    system_logs.decision_metadata ->> 'triggered_by',
                    ROUND(
                        EXTRACT(
                            EPOCH FROM (
                                CURRENT_TIMESTAMP - COALESCE(
                                    control_cycles.action_started_at,
                                    system_logs.timestamp
                                )
                            )
                        )
                    )::INT AS age_seconds
                FROM {} AS system_logs
                INNER JOIN {} AS control_cycles
                    ON control_cycles.id = system_logs.control_cycle_id
                WHERE system_logs.experiment_run_id = %s
                    AND system_logs.control_strategy = %s
                    AND (
                        (
                            system_logs.pump_activated <> 'none'
                            AND COALESCE(system_logs.duration_ms, 0) > 0
                        )
                        OR (
                            system_logs.decision_metadata #>> ARRAY[
                                'human_in_the_loop',
                                'review_required'
                            ] = 'true'
                            AND COALESCE(
                                NULLIF(
                                    system_logs.decision_metadata #>> ARRAY[
                                        'human_in_the_loop',
                                        'pending_decision',
                                        'pump_activated'
                                    ],
                                    'none'
                                ),
                                ''
                            ) <> ''
                            AND COALESCE(
                                NULLIF(
                                    system_logs.decision_metadata #>> ARRAY[
                                        'human_in_the_loop',
                                        'pending_decision',
                                        'duration_ms'
                                    ],
                                    ''
                                )::INT,
                                0
                            ) > 0
                        )
                    )
                    AND system_logs.decision_metadata ->> 'triggered_by'
                        IN ('batch_scheduler', 'manual_batch_trigger')
                    AND control_cycles.status IN (
                        'waiting',
                        'human_approved_pending_execution',
                        'human_override_pending_execution',
                        'batch_pending',
                        'batch_dispatched',
                        'dosing',
                        'inter_dose_mixing',
                        'ec_up_b_dosing',
                        'mixing',
                        'human_command_dispatched'
                    )
                ORDER BY system_logs.timestamp DESC
                LIMIT 1;
                    """
                ).format(self._table("system_logs"), self._table("control_cycles")),
                (experiment_run_id, control_strategy),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return {
            "control_cycle_id": row[0],
            "system_log_id": row[1],
            "timestamp": row[2].isoformat() if row[2] is not None else None,
            "status": row[3],
            "action_started_at": row[4].isoformat() if row[4] is not None else None,
            "action_completed_at": row[5].isoformat() if row[5] is not None else None,
            "decision": row[6],
            "pump_activated": row[7],
            "dose_ml": float(row[8] or 0),
            "duration_ms": int(row[9] or 0),
            "triggered_by": row[10],
            "age_seconds": int(row[11] or 0),
        }

    def get_active_experiment_run_summary(
        self,
        control_strategy: str,
    ) -> dict[str, Any] | None:
        """Return the active run plus its latest reading and persisted cycle count."""
        control_strategy = _normalize_control_strategy(control_strategy)
        experiment_run_id = self._get_active_experiment_run_id(control_strategy)
        if experiment_run_id is None:
            return None
        with self._connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                SELECT
                    experiment_runs.id,
                    experiment_runs.run_name,
                    experiment_runs.control_strategy,
                    experiment_runs.start_time,
                    MAX(system_logs.timestamp) AS last_activity_at,
                    COUNT(DISTINCT control_cycles.id)::INT AS cycle_count
                FROM {} AS experiment_runs
                LEFT JOIN {} AS system_logs
                    ON system_logs.experiment_run_id = experiment_runs.id
                LEFT JOIN {} AS control_cycles
                    ON control_cycles.id = system_logs.control_cycle_id
                WHERE experiment_runs.id = %s
                GROUP BY experiment_runs.id;
                    """
                ).format(
                    self._table("experiment_runs"),
                    self._table("system_logs"),
                    self._table("control_cycles"),
                ),
                (experiment_run_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return {
            "id": int(row[0]),
            "run_name": row[1],
            "control_strategy": row[2],
            "start_time": row[3],
            "last_activity_at": row[4],
            "cycle_count": int(row[5] or 0),
        }

    def start_new_experiment_run(self, control_strategy: str) -> dict[str, Any]:
        """Atomically close the active run and clone its configuration into a new run."""
        control_strategy = _normalize_control_strategy(control_strategy)
        run_name = _run_name_for_strategy(control_strategy)
        lock_key = _experiment_run_lock_key(self._schema, run_name, control_strategy)
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s);", (lock_key,))
            cursor.execute(
                sql.SQL(
                    """
                    UPDATE {} AS experiment_runs
                    SET end_time = CURRENT_TIMESTAMP
                    WHERE experiment_runs.run_name = %s
                        AND experiment_runs.control_strategy = %s
                        AND experiment_runs.end_time IS NULL
                    RETURNING
                        reference_range_id,
                        run_name,
                        experiment_phase,
                        control_strategy,
                        reservoir_max_volume_liters,
                        sampling_interval_seconds,
                        mixing_time_seconds,
                        initial_confirmation_gap_seconds,
                        stability_required_seconds,
                        ph_stability_threshold,
                        ec_stability_threshold;
                    """
                ).format(self._table("experiment_runs")),
                (run_name, control_strategy),
            )
            previous = cursor.fetchone()
            if previous is None:
                raise RuntimeError("No active experiment run is available to replace.")
            cursor.execute(
                sql.SQL(
                    """
                    INSERT INTO {} (
                        reference_range_id,
                        run_name,
                        experiment_phase,
                        control_strategy,
                        reservoir_max_volume_liters,
                        sampling_interval_seconds,
                        mixing_time_seconds,
                        initial_confirmation_gap_seconds,
                        stability_required_seconds,
                        ph_stability_threshold,
                        ec_stability_threshold,
                        notes
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, run_name, control_strategy, start_time;
                    """
                ).format(self._table("experiment_runs")),
                (*previous, "Started by an operator through the experiment preflight gate."),
            )
            created = cursor.fetchone()
        self._connection.commit()
        return {
            "id": int(created[0]),
            "run_name": created[1],
            "control_strategy": created[2],
            "start_time": created[3],
            "last_activity_at": None,
            "cycle_count": 0,
        }

    def get_recent_or_active_pump_command(
        self,
        lookback_seconds: int,
    ) -> dict[str, Any] | None:
        """Return the newest active pump command or recent dose from any strategy."""
        # Shared pumps remain guarded across control strategies and experiment runs.
        with self._connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                SELECT
                    system_logs.control_cycle_id,
                    system_logs.id AS system_log_id,
                    system_logs.timestamp,
                    control_cycles.status,
                    control_cycles.action_started_at,
                    control_cycles.action_completed_at,
                    system_logs.decision,
                    system_logs.pump_activated,
                    COALESCE(system_logs.dose_ml, 0),
                    COALESCE(system_logs.duration_ms, 0),
                    COALESCE(system_logs.mixing_effective_seconds, 0),
                    system_logs.decision_metadata ->> 'triggered_by',
                    ROUND(
                        EXTRACT(
                            EPOCH FROM (
                                CURRENT_TIMESTAMP - COALESCE(
                                    control_cycles.action_completed_at,
                                    control_cycles.action_started_at,
                                    system_logs.timestamp
                                )
                            )
                        )
                    )::INT AS age_seconds
                FROM {} AS system_logs
                INNER JOIN {} AS control_cycles
                    ON control_cycles.id = system_logs.control_cycle_id
                INNER JOIN {} AS experiment_runs
                    ON experiment_runs.id = system_logs.experiment_run_id
                WHERE system_logs.pump_activated <> 'none'
                    AND COALESCE(system_logs.duration_ms, 0) > 0
                    AND (
                        (
                            experiment_runs.end_time IS NULL
                            AND control_cycles.status IN (
                                'batch_pending',
                                'batch_dispatched',
                                'human_approved_pending_execution',
                                'human_override_pending_execution',
                                'human_command_dispatched',
                                'dosing',
                                'inter_dose_mixing',
                                'ec_up_b_dosing',
                                'mixing'
                            )
                        )
                        OR COALESCE(
                            control_cycles.action_completed_at,
                            control_cycles.action_started_at,
                            system_logs.timestamp
                        ) >= CURRENT_TIMESTAMP - (
                            GREATEST(
                                %s,
                                COALESCE(system_logs.mixing_effective_seconds, 0)
                            ) * INTERVAL '1 second'
                        )
                    )
                ORDER BY COALESCE(
                    control_cycles.action_completed_at,
                    control_cycles.action_started_at,
                    system_logs.timestamp
                ) DESC
                LIMIT 1;
                    """
                ).format(
                    self._table("system_logs"),
                    self._table("control_cycles"),
                    self._table("experiment_runs"),
                ),
                (lookback_seconds,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return {
            "control_cycle_id": row[0],
            "system_log_id": row[1],
            "timestamp": row[2].isoformat() if row[2] is not None else None,
            "status": row[3],
            "action_started_at": row[4].isoformat() if row[4] is not None else None,
            "action_completed_at": row[5].isoformat() if row[5] is not None else None,
            "decision": row[6],
            "pump_activated": row[7],
            "dose_ml": float(row[8] or 0),
            "duration_ms": int(row[9] or 0),
            "mixing_duration_seconds": int(row[10] or 0),
            "triggered_by": row[11],
            "age_seconds": int(row[12] or 0),
        }

    def get_latest_batch_analysis(self, control_strategy: str) -> dict[str, Any] | None:
        """Return details for the newest scheduled or manual batch analysis."""
        control_strategy = _normalize_control_strategy(control_strategy)
        experiment_run_id = self._get_active_experiment_run_id(control_strategy)
        if experiment_run_id is None:
            return None
        with self._connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                SELECT
                    system_logs.control_cycle_id,
                    system_logs.id AS system_log_id,
                    system_logs.timestamp,
                    control_cycles.status,
                    system_logs.decision,
                    system_logs.pump_activated,
                    COALESCE(system_logs.dose_ml, 0),
                    COALESCE(system_logs.duration_ms, 0),
                    system_logs.decision_metadata ->> 'triggered_by',
                    system_logs.decision_metadata ->> 'batch_trigger_mode',
                    COALESCE(
                        (system_logs.decision_metadata ->> 'batch_window_readings')::INT,
                        0
                    ),
                    COALESCE(
                        (system_logs.decision_metadata ->> 'batch_candidate_rows_read')::INT,
                        0
                    ),
                    system_logs.decision_metadata,
                    ROUND(EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - system_logs.timestamp)))::INT
                        AS age_seconds
                FROM {} AS system_logs
                INNER JOIN {} AS control_cycles
                    ON control_cycles.id = system_logs.control_cycle_id
                WHERE system_logs.experiment_run_id = %s
                    AND system_logs.control_strategy = %s
                    AND system_logs.decision_metadata ->> 'triggered_by'
                        IN ('batch_scheduler', 'manual_batch_trigger')
                ORDER BY system_logs.timestamp DESC
                LIMIT 1;
                    """
                ).format(self._table("system_logs"), self._table("control_cycles")),
                (experiment_run_id, control_strategy),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return {
            "control_cycle_id": row[0],
            "system_log_id": row[1],
            "timestamp": row[2].isoformat() if row[2] is not None else None,
            "status": row[3],
            "decision": row[4],
            "pump_activated": row[5],
            "dose_ml": float(row[6] or 0),
            "duration_ms": int(row[7] or 0),
            "triggered_by": row[8],
            "batch_trigger_mode": row[9],
            "batch_window_readings": int(row[10] or 0),
            "batch_candidate_rows_read": int(row[11] or 0),
            "decision_metadata": row[12] or {},
            "age_seconds": int(row[13] or 0),
        }

    def _get_or_create_experiment_run(self, control_strategy: str | None = None) -> int:
        """Return the configured experiment run, creating it when missing."""
        control_strategy = _normalize_control_strategy(control_strategy)
        run_name = _run_name_for_strategy(control_strategy)
        lock_key = _experiment_run_lock_key(self._schema, run_name, control_strategy)

        with self._connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s);", (lock_key,))
            try:
                cursor.execute(
                    sql.SQL(
                        """
                    SELECT id
                    FROM {}
                    WHERE run_name = %s
                        AND control_strategy = %s
                        AND end_time IS NULL
                    ORDER BY start_time DESC
                    LIMIT 1;
                        """
                    ).format(self._table("experiment_runs")),
                    (run_name, control_strategy),
                )
                row = cursor.fetchone()

                if row:
                    return int(row[0])

                cursor.execute(
                    sql.SQL(
                        """
                    INSERT INTO {} (
                        reference_range_id,
                        run_name,
                        experiment_phase,
                        control_strategy,
                        reservoir_max_volume_liters,
                        sampling_interval_seconds,
                        mixing_time_seconds,
                        initial_confirmation_gap_seconds,
                        stability_required_seconds,
                        ph_stability_threshold,
                        ec_stability_threshold,
                        notes
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id;
                        """
                    ).format(self._table("experiment_runs")),
                    (
                        settings.default_reference_range_id,
                        run_name,
                        settings.default_experiment_phase,
                        control_strategy,
                        settings.default_reservoir_max_volume_liters,
                        settings.default_sampling_interval_seconds,
                        settings.default_mixing_time_seconds,
                        settings.default_initial_confirmation_gap_seconds,
                        settings.default_stability_required_seconds,
                        settings.default_ph_stability_threshold,
                        settings.default_ec_stability_threshold,
                        "Automatically created by HANAS for incoming ESP32 sensor readings.",
                    ),
                )
                created_row = cursor.fetchone()
            finally:
                cursor.execute("SELECT pg_advisory_unlock(%s);", (lock_key,))

        return int(created_row[0])

    def _get_active_experiment_run_id(self, control_strategy: str | None = None) -> int | None:
        """Return the current active experiment run without creating one."""
        control_strategy = _normalize_control_strategy(control_strategy)
        run_name = _run_name_for_strategy(control_strategy)
        with self._connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                SELECT id
                FROM {}
                WHERE run_name = %s
                    AND control_strategy = %s
                    AND end_time IS NULL
                ORDER BY start_time DESC
                LIMIT 1;
                    """
                ).format(self._table("experiment_runs")),
                (run_name, control_strategy),
            )
            row = cursor.fetchone()
        return int(row[0]) if row else None

    def _table(self, table_name: str) -> sql.Composed:
        """Return a safely quoted schema-qualified table identifier."""
        return sql.Identifier(self._schema, table_name)

    def _ensure_notification_logs_table(self) -> None:
        """Create the notification audit table when it is not present yet."""
        with self._connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                CREATE TABLE IF NOT EXISTS {} (
                    id BIGSERIAL PRIMARY KEY,
                    timestamp TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    provider TEXT NOT NULL,
                    recipient_number TEXT NOT NULL,
                    sender_id TEXT,
                    alert_type TEXT NOT NULL,
                    message_body TEXT NOT NULL,
                    status TEXT NOT NULL,
                    provider_response TEXT,
                    provider_message_id BIGINT,
                    latest_provider_response TEXT,
                    provider_status_updated_at TIMESTAMPTZ,
                    status_checked_at TIMESTAMPTZ,
                    status_check_count INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT,
                    system_log_id BIGINT REFERENCES {}(id) ON DELETE SET NULL,
                    control_cycle_id BIGINT REFERENCES {}(id) ON DELETE SET NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                    """
                ).format(
                    self._table("notification_logs"),
                    self._table("system_logs"),
                    self._table("control_cycles"),
                )
            )
            cursor.execute(
                sql.SQL(
                    """
                CREATE INDEX IF NOT EXISTS {}
                ON {} (timestamp DESC);
                    """
                ).format(
                    sql.Identifier("notification_logs_timestamp_idx"),
                    self._table("notification_logs"),
                )
            )
            cursor.execute(
                sql.SQL(
                    """
                CREATE INDEX IF NOT EXISTS {}
                ON {} (status, alert_type);
                    """
                ).format(
                    sql.Identifier("notification_logs_status_alert_idx"),
                    self._table("notification_logs"),
                )
            )

    def _create_control_cycle(
        self,
        experiment_run_id: int,
        status: str,
        control_strategy: str,
    ) -> int:
        """Create a read-decide-act cycle and return its ID."""
        deviation_detected = status == "dosing"
        with self._connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    """
                INSERT INTO {} (
                    experiment_run_id,
                    control_strategy,
                    deviation_detected_at,
                    status
                )
                VALUES (
                    %s,
                    %s,
                    CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE NULL END,
                    %s
                )
                RETURNING id;
                    """
                ).format(self._table("control_cycles")),
                (experiment_run_id, control_strategy, deviation_detected, status),
            )
            row = cursor.fetchone()

        return int(row[0])


def _status_for_decision(decision: DosingDecision) -> str:
    """Return the persisted system status for a dosing decision."""
    if decision.pump_activated != "none":
        return "dosing"

    if decision.decision in {"within_range", "within_control_tolerance"}:
        return "within_range"
    if decision.metadata.get("triggered_by") == "emergency_guard_mixing_wait":
        return "mixing"
    if decision.decision == "wait_for_mixing":
        return "mixing"
    if decision.decision == "wait_for_stability":
        return "unstable"
    if decision.decision == "wait_initial_confirmation":
        return "confirming"
    if decision.decision == "sensor_anomaly":
        return "sensor_anomaly"
    if decision.decision in {
        "wait_human_review",
        "wait_agentic_decision",
        "wait_consistency_review",
        "wait_natural_recovery",
        "wait_near_boundary",
        "wait_minimum_reliable_dose",
        "wait_safety_gate",
    }:
        return "waiting"

    return "no_action"


def _reviewed_decision_from_payload(
    current_metadata: dict,
    payload: HumanReviewPayload,
) -> dict[str, object]:
    """Return the decision fields produced by an operator review."""
    current_metadata = dict(current_metadata)
    hitl_metadata = current_metadata.get("human_in_the_loop")
    if not isinstance(hitl_metadata, dict):
        hitl_metadata = {}

    original = hitl_metadata.get("pending_decision")
    if not isinstance(original, dict):
        original = {}

    review_metadata = {
        "action": payload.action,
        "reviewer": payload.reviewer,
        "reason": payload.reason,
    }

    if payload.action == "reject":
        current_metadata["agentic_mode"] = "hitl_rejected"
        current_metadata["actuation_source"] = "human_review_reject"
        reviewed = {
            "decision": "human_rejected",
            "pump_activated": "none",
            "dose_ml": 0.0,
            "duration_ms": 0,
            "mixing_time_ms": 0,
            "reason": payload.reason or "Operator rejected the pending agentic AI decision.",
            "status": "human_rejected",
            "message": "Agentic AI decision rejected; no pump command is pending.",
        }
    elif payload.action == "approve":
        current_metadata["agentic_mode"] = "human_approved_command"
        current_metadata["actuation_source"] = "human_review_approve"
        reviewed = _approved_original_decision(original, payload)
    else:
        current_metadata["agentic_mode"] = "human_override_command"
        current_metadata["actuation_source"] = "human_review_override"
        reviewed = _override_decision(payload)

    hitl_metadata.update(
        {
            "status": payload.action,
            "review_required": False,
            "review": review_metadata,
            "reviewed_decision": {
                "decision": reviewed["decision"],
                "pump_activated": reviewed["pump_activated"],
                "dose_ml": reviewed["dose_ml"],
                "duration_ms": reviewed["duration_ms"],
                "mixing_time_ms": reviewed["mixing_time_ms"],
            },
        }
    )
    current_metadata["human_in_the_loop"] = hitl_metadata
    reviewed["metadata"] = current_metadata
    return reviewed


def _approved_original_decision(original: dict, payload: HumanReviewPayload) -> dict[str, object]:
    """Return the original pending agentic decision as an approved command."""
    pump = str(original.get("pump_activated") or "none")
    dose_ml = float(original.get("dose_ml") or 0)
    duration_ms = int(original.get("duration_ms") or 0)
    mixing_time_ms = _mixing_time_ms_from_decision_metadata(original)
    status = "human_approved_pending_execution" if pump != "none" and duration_ms > 0 else "completed"
    return {
        "decision": str(original.get("decision") or "human_approved"),
        "pump_activated": pump,
        "dose_ml": dose_ml,
        "duration_ms": duration_ms,
        "mixing_time_ms": mixing_time_ms,
        "reason": payload.reason or "Operator approved the pending agentic AI decision.",
        "status": status,
        "message": "Agentic AI decision approved; pump command is pending execution.",
    }


class ControlCycleConflict(ValueError):
    """A delayed or invalid device callback cannot change the current cycle."""


class InvalidHumanReview(ValueError):
    """A review command violates the calibrated actuator contract."""


def _validate_override(payload: HumanReviewPayload, reference: ReferenceRange) -> None:
    """Reject unsafe overrides; operator authority does not remove pump caps."""
    from app.services.baseline.dosing_rules import effective_max_dose_for_pump, max_duration_for_pump
    from app.services.dosing_math import estimate_dose_ml_for_duration

    pump = payload.pump_activated or "none"
    dose, duration = payload.dose_ml or 0, payload.duration_ms or 0
    if not math.isfinite(dose):
        raise InvalidHumanReview("Override dose must be finite.")
    if pump == "none":
        if dose != 0 or duration != 0 or (payload.mixing_time_ms or 0) != 0:
            raise InvalidHumanReview("No-pump overrides must have zero dose and duration.")
        return
    if dose <= 0 or duration < settings.minimum_pump_duration_ms:
        raise InvalidHumanReview("Pump overrides require a positive dose and a valid minimum runtime.")
    if dose > effective_max_dose_for_pump(reference, pump) or duration > max_duration_for_pump(reference, pump):
        raise InvalidHumanReview("Override exceeds the configured pump dose or runtime limit.")
    flow = reference.ph_pump_flow_ml_per_min if pump.startswith("ph_") else reference.ec_pump_flow_ml_per_min
    expected_dose = estimate_dose_ml_for_duration(duration, flow)
    if expected_dose > effective_max_dose_for_pump(reference, pump):
        raise InvalidHumanReview("Override runtime would deliver more than the configured pump dose limit.")
    if abs(dose - expected_dose) > 0.011:
        raise InvalidHumanReview(
            f"Override runtime of {duration} ms corresponds to {expected_dose:.2f} mL; "
            "dose and runtime must agree with the calibrated pump flow."
        )
    if payload.mixing_time_ms is not None and not 60_000 <= payload.mixing_time_ms <= 450_000:
        raise InvalidHumanReview("Override mixing time must be between 60 and 450 seconds.")


def _override_decision(payload: HumanReviewPayload) -> dict[str, object]:
    """Return an operator override command."""
    pump = payload.pump_activated or "none"
    dose_ml = float(payload.dose_ml or 0)
    duration_ms = int(payload.duration_ms or 0)
    mixing_time_ms = int(
        payload.mixing_time_ms
        if payload.mixing_time_ms is not None
        else (settings.default_mixing_time_seconds * 1000 if pump != "none" else 0)
    )
    status = "human_override_pending_execution" if pump != "none" and duration_ms > 0 else "human_override"
    return {
        "decision": payload.decision or "human_override",
        "pump_activated": pump,
        "dose_ml": dose_ml,
        "duration_ms": duration_ms,
        "mixing_time_ms": mixing_time_ms,
        "reason": payload.reason or "Operator overrode the pending agentic AI decision.",
        "status": status,
        "message": "Operator override recorded; pump command is pending execution.",
    }


def _mixing_time_ms_from_decision_metadata(decision: dict) -> int:
    """Return mixing milliseconds from a serialized DosingDecision metadata payload."""
    metadata = decision.get("metadata")
    if not isinstance(metadata, dict):
        return settings.default_mixing_time_seconds * 1000
    mixing_window = metadata.get("mixing_window")
    if isinstance(mixing_window, dict):
        effective_seconds = mixing_window.get("effective_seconds")
        if isinstance(effective_seconds, int | float) and effective_seconds > 0:
            return round(effective_seconds * 1000)
    return settings.default_mixing_time_seconds * 1000


def _mixing_columns_for_decision(decision: DosingDecision) -> dict[str, float | int | None]:
    """Extract first-class mixing values from agentic decision metadata."""
    mixing_window = decision.metadata.get("mixing_window") or {}
    mixing_check = decision.metadata.get("mixing_window_check") or {}
    source = mixing_window or mixing_check

    return {
        "base_seconds": _optional_int(source.get("base_seconds")),
        "effective_seconds": _optional_int(source.get("effective_seconds")),
        "elapsed_seconds": _optional_float(mixing_check.get("elapsed_seconds")),
        "remaining_seconds": _optional_float(mixing_check.get("remaining_seconds")),
        "adjustment_factor": _optional_float(source.get("adjustment_factor")),
    }


def _optional_int(value: object) -> int | None:
    if isinstance(value, int | float):
        return round(value)
    return None


def _optional_float(value: object) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    return None


def _control_strategy_for_decision(payload: SensorPayload, decision: DosingDecision) -> str:
    """Return the strategy that actually produced the persisted decision."""
    strategy = (
        decision.metadata.get("executed_strategy")
        or decision.metadata.get("strategy")
        or decision.metadata.get("requested_strategy")
        or payload.control_strategy
    )
    return _normalize_control_strategy(strategy)


def _normalize_control_strategy(control_strategy: object | None) -> str:
    """Normalize a control strategy value for experiment-run and log partitioning."""
    strategy = str(control_strategy or settings.default_control_strategy).strip().lower()
    if strategy not in {"baseline", "agentic_ai"}:
        return settings.default_control_strategy.strip().lower()

    return strategy


def _run_name_for_strategy(control_strategy: str) -> str:
    """Return a visible run name while keeping the configured default as the base."""
    base_name = settings.default_experiment_run_name.strip()
    default_strategy = settings.default_control_strategy.strip().lower()
    if control_strategy == default_strategy:
        return base_name

    label = "Agentic AI" if control_strategy == "agentic_ai" else "Baseline"
    return f"{base_name} ({label})"


def _experiment_run_lock_key(schema: str, run_name: str, control_strategy: str) -> int:
    """Return a stable PostgreSQL advisory lock key for one active experiment run."""
    digest = sha256(f"{schema}:{run_name}:{control_strategy}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)
