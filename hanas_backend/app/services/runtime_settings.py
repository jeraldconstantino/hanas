"""Helpers for runtime settings that are shared across backend workers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


EXPERIMENT_PREFLIGHT_RUN_ID_KEY = "experiment_preflight_confirmed_run_id"
EXPERIMENT_PREFLIGHT_BLOCKED_RUN_ID_KEY = "experiment_preflight_blocked_run_id"


def runtime_bool_setting(
    repository: Any,
    key: str,
    *,
    default: bool,
) -> bool:
    """Read a backend-persisted boolean setting from repositories that support settings."""
    get_system_setting = getattr(repository, "get_system_setting", None)
    if not callable(get_system_setting):
        return default

    value = get_system_setting(key)
    if isinstance(value, bool):
        return value
    return default


def maintenance_mode_enabled(repository: Any) -> bool:
    """Return whether global maintenance mode is currently enabled."""
    return runtime_bool_setting(
        repository,
        "maintenance_mode_enabled",
        default=False,
    )


def monitoring_mode_enabled(repository: Any) -> bool:
    """Return whether readings should be stored without AI analysis or dosing."""
    return runtime_bool_setting(
        repository,
        "monitoring_mode_enabled",
        default=False,
    )


def emergency_stop_enabled(repository: Any) -> bool:
    """Return whether the global emergency stop is currently engaged."""
    return runtime_bool_setting(
        repository,
        "emergency_stop_enabled",
        default=False,
    )


def full_agentic_mode_enabled(repository: Any) -> bool:
    """Return whether every incoming sensor reading should run the full LLM pipeline."""
    from app.core.config import settings

    return runtime_bool_setting(
        repository,
        "full_agentic_mode_enabled",
        default=settings.full_agentic_mode_enabled,
    )


def experiment_preflight_required(repository: Any, control_strategy: str) -> bool:
    """Fail closed until a run is chosen, including after a sensor-data interruption."""
    if control_strategy != "agentic_ai":
        return False
    get_summary = getattr(repository, "get_active_experiment_run_summary", None)
    get_setting = getattr(repository, "get_system_setting", None)
    if not callable(get_summary) or not callable(get_setting):
        return False
    active = get_summary(control_strategy)
    if active is None:
        return True
    active_run_id = active["id"]
    if get_setting(EXPERIMENT_PREFLIGHT_BLOCKED_RUN_ID_KEY) == active_run_id:
        return True

    confirmation = get_setting(EXPERIMENT_PREFLIGHT_RUN_ID_KEY)
    if not isinstance(confirmation, dict) or confirmation.get("run_id") != active_run_id:
        return True
    try:
        confirmed_at = datetime.fromisoformat(str(confirmation["confirmed_at"]))
    except (KeyError, TypeError, ValueError):
        return True
    if confirmed_at.tzinfo is None:
        confirmed_at = confirmed_at.replace(tzinfo=timezone.utc)

    last_activity = active.get("last_activity_at")
    if not isinstance(last_activity, datetime):
        last_activity = confirmed_at
    elif last_activity.tzinfo is None:
        last_activity = last_activity.replace(tzinfo=timezone.utc)

    from app.core.config import settings

    stale_before = datetime.now(timezone.utc) - timedelta(
        seconds=settings.agentic_history_freshness_gap_seconds
    )
    continuity_anchor = max(last_activity, confirmed_at)
    if continuity_anchor >= stale_before:
        return False

    # Latch the interruption so the just-persisted held reading cannot silently
    # make the run look fresh on the next device sample.
    set_setting = getattr(repository, "set_system_setting", None)
    if callable(set_setting):
        set_setting(EXPERIMENT_PREFLIGHT_BLOCKED_RUN_ID_KEY, active_run_id)
    return True
