#!/usr/bin/env python3
"""Calculate Phase 3 pH, EC, and dosing-response metrics from an Excel workbook."""

from __future__ import annotations

import json
import hashlib
import math
import csv
import sys
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from statistics import mean, median
from zipfile import ZipFile

from generate_phase3_timeseries import shared_strings, worksheet_records


PH_LIMITS = (5.5, 6.5)
EC_LIMITS = (1.2, 2.0)
MAX_INTERVAL_SECONDS = 120.0
SUSTAINED_OBSERVATIONS = 10
Z_95 = 1.959963984540054


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def wilson_interval(successes: int, total: int):
    """Return a two-sided 95% Wilson score interval for a binomial proportion."""
    proportion = successes / total
    denominator = 1.0 + Z_95**2 / total
    center = (proportion + Z_95**2 / (2.0 * total)) / denominator
    half_width = (
        Z_95
        * math.sqrt(
            proportion * (1.0 - proportion) / total
            + Z_95**2 / (4.0 * total**2)
        )
        / denominator
    )
    return {
        "count": successes,
        "total": total,
        "pct": proportion * 100.0,
        "lower_95_pct": (center - half_width) * 100.0,
        "upper_95_pct": (center + half_width) * 100.0,
    }


def load_run_data(workbook: Path):
    observations = []
    actions = []
    log_ids = []
    control_cycle_links = []
    metadata_parse_errors = 0
    with ZipFile(workbook) as archive:
        strings = shared_strings(archive)
        cycle_rows = list(worksheet_records(archive, 2, strings))
        cycles = {row["id"]: row for row in cycle_rows}
        for row in worksheet_records(archive, 3, strings):
            log_ids.append(row["id"])
            control_cycle_links.append(row["control_cycle_id"])
            try:
                metadata = json.loads(row["decision_metadata"])
            except (TypeError, json.JSONDecodeError):
                metadata_parse_errors += 1
                metadata = {}
            trigger = metadata.get("triggered_by")
            if trigger not in {"batch_scheduler", "maintenance_mode"}:
                observations.append(
                    (
                        datetime.fromisoformat(row["timestamp"]),
                        float(row["ph"]),
                        float(row["ec"]),
                        float(row["temperature"]),
                        float(row["reservoir_volume_liters"]),
                    )
                )
            if row["pump_activated"] != "none":
                actions.append(
                    {
                        "timestamp": datetime.fromisoformat(row["timestamp"]),
                        "pump": row["pump_activated"],
                        "trigger": trigger,
                        "reasoning_source": metadata.get("reasoning_source"),
                        "action_completed_at": datetime.fromisoformat(
                            cycles[row["control_cycle_id"]]["action_completed_at"]
                        ),
                        "mixing_seconds": float(row["mixing_effective_seconds"]),
                    }
                )
    integrity = {
        "system_log_rows": len(log_ids),
        "control_cycle_rows": len(cycle_rows),
        "duplicate_system_log_ids": len(log_ids) - len(set(log_ids)),
        "duplicate_control_cycle_ids": len(cycle_rows) - len(cycles),
        "orphan_control_cycle_links": sum(
            control_cycle_id not in cycles for control_cycle_id in control_cycle_links
        ),
        "decision_metadata_parse_errors": metadata_parse_errors,
    }
    return (
        sorted(observations),
        sorted(actions, key=lambda item: item["timestamp"]),
        integrity,
    )


def state(value: float, limits: tuple[float, float]) -> str:
    lower, upper = limits
    if value < lower:
        return "below"
    if value > upper:
        return "above"
    return "in"


def episode_durations(observations, predicate):
    durations = []
    current = 0.0
    for first, second in zip(observations, observations[1:]):
        delta = (second[0] - first[0]).total_seconds()
        if not 0 < delta <= MAX_INTERVAL_SECONDS:
            if current:
                durations.append(current)
                current = 0.0
        elif predicate(first):
            current += delta
        elif current:
            durations.append(current)
            current = 0.0
    if current:
        durations.append(current)
    return durations


def summarize_episodes(durations):
    if not durations:
        return {"count": 0, "median_minutes": 0.0, "maximum_minutes": 0.0}
    return {
        "count": len(durations),
        "median_minutes": median(durations) / 60.0,
        "maximum_minutes": max(durations) / 60.0,
    }


def command_response_metrics(observations, actions):
    """Evaluate accepted stable observations after completed dosing commands.

    A command attains its target at the first accepted stable observation at or
    after action completion plus the configured mixing interval. If that first
    observation remains out of range, later observations are searched only
    until the next command affecting the same regulated variable.
    """
    timestamps = [row[0] for row in observations]
    evaluated_actions = actions
    first_post_mixing_attainment = 0
    delayed_without_repeat = 0
    repeat_required = 0
    opposite_boundary_crossings = 0
    sustained_ten_observations = 0
    verified_minutes = []
    post_mixing_observation_lag_seconds = []
    by_pump = {}
    by_source = {}

    for action in evaluated_actions:
        variable = "ph" if action["pump"].startswith("ph") else "ec"
        value_index = 1 if variable == "ph" else 2
        limits = PH_LIMITS if variable == "ph" else EC_LIMITS
        direction = "down" if action["pump"].endswith("down") else "up"
        ready_at = action["action_completed_at"] + timedelta(
            seconds=action["mixing_seconds"]
        )
        observation_index = bisect_left(timestamps, ready_at)
        if observation_index >= len(observations):
            repeat_required += 1
            continue
        first = observations[observation_index]
        first_state = state(first[value_index], limits)
        post_mixing_observation_lag_seconds.append(
            (first[0] - ready_at).total_seconds()
        )
        pump_counts = by_pump.setdefault(
            action["pump"],
            {"commands": 0, "first_post_mixing_attainment": 0,
             "attainment_before_repeat": 0},
        )
        pump_counts["commands"] += 1
        source_counts = by_source.setdefault(
            action["trigger"],
            {"commands": 0, "first_post_mixing_attainment": 0,
             "attainment_before_repeat": 0},
        )
        source_counts["commands"] += 1

        if (direction == "down" and first_state == "below") or (
            direction == "up" and first_state == "above"
        ):
            opposite_boundary_crossings += 1

        confirmation = observations[
            observation_index:observation_index + SUSTAINED_OBSERVATIONS
        ]
        confirmation_is_consecutive = len(confirmation) == SUSTAINED_OBSERVATIONS
        if confirmation_is_consecutive:
            confirmation_is_consecutive = all(
                0 < (second[0] - first_row[0]).total_seconds()
                <= MAX_INTERVAL_SECONDS
                for first_row, second in zip(confirmation, confirmation[1:])
            )
        if confirmation_is_consecutive and all(
            state(row[value_index], limits) == "in" for row in confirmation
        ):
            sustained_ten_observations += 1

        if first_state == "in":
            first_post_mixing_attainment += 1
            pump_counts["first_post_mixing_attainment"] += 1
            pump_counts["attainment_before_repeat"] += 1
            source_counts["first_post_mixing_attainment"] += 1
            source_counts["attainment_before_repeat"] += 1
            verified_minutes.append(
                (first[0] - action["action_completed_at"]).total_seconds() / 60.0
            )
            continue

        next_same_variable = None
        for later_action in actions:
            if later_action["timestamp"] <= action["timestamp"]:
                continue
            later_variable = (
                "ph" if later_action["pump"].startswith("ph") else "ec"
            )
            if later_variable == variable:
                next_same_variable = later_action["timestamp"]
                break

        attained = None
        for observation in observations[observation_index + 1:]:
            if next_same_variable and observation[0] >= next_same_variable:
                break
            if state(observation[value_index], limits) == "in":
                attained = observation
                break
        if attained is None:
            repeat_required += 1
        else:
            delayed_without_repeat += 1
            pump_counts["attainment_before_repeat"] += 1
            source_counts["attainment_before_repeat"] += 1
            verified_minutes.append(
                (attained[0] - action["action_completed_at"]).total_seconds()
                / 60.0
            )

    count = len(evaluated_actions)
    return {
        "commands": count,
        "first_post_mixing_attainment": wilson_interval(
            first_post_mixing_attainment, count
        ),
        "delayed_attainment_without_repeat": delayed_without_repeat,
        "attainment_before_repeat": wilson_interval(
            count - repeat_required, count
        ),
        "repeat_correction_required": wilson_interval(repeat_required, count),
        "sustained_ten_observations": wilson_interval(
            sustained_ten_observations, count
        ),
        "opposite_boundary_at_first_post_mixing_observation": (
            opposite_boundary_crossings
        ),
        "median_verified_attainment_minutes": median(verified_minutes),
        "maximum_verified_attainment_minutes": max(verified_minutes),
        "median_post_mixing_observation_lag_seconds": median(
            post_mixing_observation_lag_seconds
        ),
        "maximum_post_mixing_observation_lag_seconds": max(
            post_mixing_observation_lag_seconds
        ),
        "by_pump": by_pump,
        "by_source": by_source,
    }


def main() -> None:
    if len(sys.argv) not in {2, 3}:
        raise SystemExit(
            "usage: analyze_phase3_standard_metrics.py INPUT.xlsx [OUTPUT.json]"
        )

    observations, actions, integrity = load_run_data(Path(sys.argv[1]))
    weighted_seconds = {
        "ph": {"below": 0.0, "in": 0.0, "above": 0.0},
        "ec": {"below": 0.0, "in": 0.0, "above": 0.0},
    }
    joint_in_seconds = 0.0
    evaluated_seconds = 0.0
    excluded_gaps = 0
    excluded_gap_seconds = 0.0
    out_of_range_start_gap_seconds = 0.0
    out_of_range_start_gaps = 0
    violation_area = {"ph": 0.0, "ec": 0.0}
    daily = defaultdict(lambda: {"evaluated": 0.0, "joint_in": 0.0})

    for first, second in zip(observations, observations[1:]):
        delta = (second[0] - first[0]).total_seconds()
        if not 0 < delta <= MAX_INTERVAL_SECONDS:
            excluded_gaps += 1
            excluded_gap_seconds += delta
            if (
                state(first[1], PH_LIMITS) != "in"
                or state(first[2], EC_LIMITS) != "in"
            ):
                out_of_range_start_gaps += 1
                out_of_range_start_gap_seconds += delta
            continue
        evaluated_seconds += delta
        ph_state = state(first[1], PH_LIMITS)
        ec_state = state(first[2], EC_LIMITS)
        weighted_seconds["ph"][ph_state] += delta
        weighted_seconds["ec"][ec_state] += delta
        violation_area["ph"] += max(
            PH_LIMITS[0] - first[1], 0.0, first[1] - PH_LIMITS[1]
        ) * delta
        violation_area["ec"] += max(
            EC_LIMITS[0] - first[2], 0.0, first[2] - EC_LIMITS[1]
        ) * delta
        if ph_state == "in" and ec_state == "in":
            joint_in_seconds += delta
        if first[0].date() == second[0].date():
            day = daily[first[0].date().isoformat()]
            day["evaluated"] += delta
            if ph_state == "in" and ec_state == "in":
                day["joint_in"] += delta

    run_seconds = (observations[-1][0] - observations[0][0]).total_seconds()
    percentages = {
        variable: {
            category: seconds / evaluated_seconds * 100.0
            for category, seconds in categories.items()
        }
        for variable, categories in weighted_seconds.items()
    }
    joint_out = lambda row: not (
        state(row[1], PH_LIMITS) == "in" and state(row[2], EC_LIMITS) == "in"
    )
    daily_rows = [
        {
            "date": date,
            "evaluated_hours": values["evaluated"] / 3600.0,
            "joint_time_in_range_pct": (
                values["joint_in"] / values["evaluated"] * 100.0
            ),
        }
        for date, values in sorted(daily.items())
        if values["evaluated"] > 0
    ]
    daily_values = [row["joint_time_in_range_pct"] for row in daily_rows]
    result = {
        "source_workbook_sha256": sha256(Path(sys.argv[1])),
        "data_integrity": integrity,
        "operational_observations": len(observations),
        "excluded_inter_snapshot_gaps": excluded_gaps,
        "excluded_gap_hours": excluded_gap_seconds / 3600.0,
        "gaps_starting_out_of_range": out_of_range_start_gaps,
        "hours_in_gaps_starting_out_of_range": (
            out_of_range_start_gap_seconds / 3600.0
        ),
        "run_hours": run_seconds / 3600.0,
        "evaluated_hours": evaluated_seconds / 3600.0,
        "evaluated_time_coverage_pct": evaluated_seconds / run_seconds * 100.0,
        "time_pct": percentages,
        "joint_time_in_range_pct": joint_in_seconds / evaluated_seconds * 100.0,
        "full_span_joint_lower_bound_pct": joint_in_seconds / run_seconds * 100.0,
        "full_span_joint_upper_bound_pct": (
            joint_in_seconds + run_seconds - evaluated_seconds
        ) / run_seconds * 100.0,
        "joint_excursions_with_evaluable_duration": summarize_episodes(
            episode_durations(observations, joint_out)
        ),
        "ph_violation_area_unit_hours": violation_area["ph"] / 3600.0,
        "ec_violation_area_mscm_hours": violation_area["ec"] / 3600.0,
        "daily_joint_time_in_range": {
            "calendar_dates": len(daily_rows),
            "median_pct": median(daily_values),
            "minimum_pct": min(daily_values),
            "minimum_date": min(
                daily_rows, key=lambda row: row["joint_time_in_range_pct"]
            )["date"],
            "dates_at_100_pct": sum(
                math.isclose(value, 100.0) for value in daily_values
            ),
        },
        "daily_rows": daily_rows,
        "observed_ph_range": [
            min(row[1] for row in observations),
            max(row[1] for row in observations),
        ],
        "observed_ec_range": [
            min(row[2] for row in observations),
            max(row[2] for row in observations),
        ],
        "mean_reservoir_temperature_c": mean(row[3] for row in observations),
        "observed_reservoir_volume_liters": [
            min(row[4] for row in observations),
            max(row[4] for row in observations),
        ],
        "system_command_response": command_response_metrics(observations, actions),
    }
    serialized = json.dumps(result, indent=2)
    if len(sys.argv) == 3:
        output_path = Path(sys.argv[2])
        output_path.write_text(serialized + "\n", encoding="utf-8")
        daily_path = output_path.with_name("daily_nutrient_summary.csv")
        with daily_path.open("w", newline="", encoding="utf-8") as destination:
            writer = csv.DictWriter(
                destination,
                fieldnames=["date", "evaluated_hours", "joint_time_in_range_pct"],
            )
            writer.writeheader()
            writer.writerows(result["daily_rows"])
    print(serialized)


if __name__ == "__main__":
    main()
