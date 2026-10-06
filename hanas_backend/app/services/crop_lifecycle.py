"""Crop lifecycle helpers shared by settings, summaries, and agent context."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.schemas.sensor import ReferenceRange


CROP_TRANSPLANT_DATE_KEY = "crop_transplant_date"
CROP_VARIETY_KEY = "crop_variety"
CROP_HARVEST_START_DAY_KEY = "crop_harvest_start_day"
CROP_HARVEST_END_DAY_KEY = "crop_harvest_end_day"


def runtime_crop_value(repository: Any, key: str, default: Any = None) -> Any:
    """Read a crop runtime setting when the repository supports it."""
    getter = getattr(repository, "get_system_setting", None)
    if not callable(getter):
        return default
    value = getter(key)
    return default if value is None else value


def crop_lifecycle_settings(repository: Any) -> dict[str, Any]:
    """Return crop lifecycle settings with config defaults and runtime overrides."""
    transplant_date = runtime_crop_value(repository, CROP_TRANSPLANT_DATE_KEY)
    if isinstance(transplant_date, date):
        transplant_date = transplant_date.isoformat()
    if not isinstance(transplant_date, str) or not transplant_date.strip():
        transplant_date = None

    return {
        "crop_variety": str(runtime_crop_value(repository, CROP_VARIETY_KEY, "") or ""),
        "crop_transplant_date": transplant_date,
        "crop_harvest_start_day": _positive_int(
            runtime_crop_value(
                repository,
                CROP_HARVEST_START_DAY_KEY,
                settings.default_crop_harvest_start_day,
            ),
            settings.default_crop_harvest_start_day,
        ),
        "crop_harvest_end_day": _positive_int(
            runtime_crop_value(
                repository,
                CROP_HARVEST_END_DAY_KEY,
                settings.default_crop_harvest_end_day,
            ),
            settings.default_crop_harvest_end_day,
        ),
    }


def build_crop_lifecycle_context(
    repository: Any,
    reference_range: ReferenceRange | None = None,
) -> dict[str, Any]:
    """Build the dated crop context supplied to LLM agents and summaries."""
    crop_settings = crop_lifecycle_settings(repository)
    crop_type = reference_range.crop_type if reference_range is not None else settings.default_crop_type
    hydroponic_system = (
        reference_range.hydroponic_system_type
        if reference_range is not None
        else settings.default_hydroponic_system_type
    )
    configured_stage = reference_range.growth_stage if reference_range is not None else None
    harvest_start = crop_settings["crop_harvest_start_day"]
    harvest_end = max(harvest_start, crop_settings["crop_harvest_end_day"])
    transplant_date = crop_settings["crop_transplant_date"]
    today = datetime.now(ZoneInfo(settings.app_timezone)).date()
    age_days: int | None = None
    if transplant_date:
        parsed = _parse_date(transplant_date)
        if parsed is not None and parsed <= today:
            age_days = (today - parsed).days

    stage = lifecycle_stage(age_days, harvest_start, harvest_end)
    return {
        "crop_type": crop_type,
        "crop_variety": crop_settings["crop_variety"] or None,
        "hydroponic_system_type": hydroponic_system,
        "configured_reference_stage": configured_stage,
        "transplant_date": transplant_date,
        "age_days": age_days,
        "stage": stage["key"],
        "stage_label": stage["label"],
        "stage_guidance": stage["guidance"],
        "harvest_start_day": harvest_start,
        "harvest_end_day": harvest_end,
        "days_until_harvest_window": (
            max(0, harvest_start - age_days) if age_days is not None else None
        ),
        "days_past_harvest_window": (
            max(0, age_days - harvest_end) if age_days is not None else None
        ),
        "source_note": (
            "Age is counted from the configured transplant date."
            if age_days is not None
            else "Transplant date is not configured or is in the future; do not infer crop age."
        ),
    }


def lifecycle_stage(age_days: int | None, harvest_start: int, harvest_end: int) -> dict[str, str]:
    """Return the operator-facing lifecycle stage for a lettuce crop."""
    if age_days is None:
        return {
            "key": "not_configured",
            "label": "Set transplant date",
            "guidance": "Configure the transplant date so HANAS can track crop age.",
        }
    if age_days <= 0:
        return {
            "key": "transplant",
            "label": "Transplant day",
            "guidance": "Minimize dosing changes while seedlings establish.",
        }
    if age_days <= 7:
        return {
            "key": "establishment",
            "label": "Establishment",
            "guidance": "Watch root recovery and keep readings steady.",
        }
    if age_days <= 20:
        return {
            "key": "vegetative",
            "label": "Vegetative growth",
            "guidance": "Maintain stable pH, EC, water temperature, and reservoir level.",
        }
    if age_days < harvest_start:
        return {
            "key": "sizing",
            "label": "Sizing / harvest prep",
            "guidance": "Avoid aggressive swings as heads approach harvest size.",
        }
    if age_days <= harvest_end:
        return {
            "key": "harvest_window",
            "label": "Harvest window",
            "guidance": "Crop is in the expected harvest window; verify size and quality.",
        }
    return {
        "key": "overdue",
        "label": "Past harvest window",
        "guidance": "Check crop quality and schedule harvest before bolting or bitterness risk increases.",
    }


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _positive_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback
