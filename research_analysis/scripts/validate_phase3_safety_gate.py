#!/usr/bin/env python3
"""Run the backend tests used for the thesis safety-verification report.

Checks software behavior, not physical hardware or field diagnostic accuracy."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path


GROUPS = {
    "input_validity": [
        "tests/test_api.py::test_batch_worker_rejects_impossible_live_reservoir_volume",
        "tests/test_api.py::test_batch_worker_rejects_zero_live_reservoir_volume",
        "tests/test_api.py::test_zero_sensor_volume_is_rejected_without_fixed_volume",
        "tests/test_api.py::test_sensor_volume_above_configured_maximum_is_rejected",
        "tests/test_api.py::test_sensor_payload_rejects_values_outside_firmware_validity_bounds",
    ],
    "physical_interlocks": [
        "tests/test_api.py::test_maintenance_mode_saves_reading_without_pump_command",
        "tests/test_api.py::test_emergency_stop_saves_reading_and_forces_pumps_off",
        "tests/test_api.py::test_physical_pump_guard_spans_strategies_and_starts_after_actuation",
        "tests/test_api.py::test_phase3_emergency_guard_suppressed_within_mixing_window",
        "tests/test_api.py::test_phase3_emergency_guard_fires_after_mixing_window_elapsed",
        "tests/test_api.py::test_phase3_emergency_guard_suppressed_by_active_command",
        "tests/test_api.py::test_baseline_dosing_is_held_during_active_command_or_mixing",
        "tests/test_api.py::test_physical_guard_precedes_agentic_human_review",
    ],
    "direction_and_actuation_bounds": [
        "tests/test_api.py::test_phase3_emergency_guard_fires_at_moderate_ph_low",
        "tests/test_api.py::test_phase3_emergency_guard_fires_at_moderate_ph_high",
        "tests/test_api.py::test_phase3_emergency_guard_fires_at_moderate_ec_low",
        "tests/test_api.py::test_phase3_emergency_guard_fires_at_moderate_ec_high",
        "tests/test_dosing_rules.py::test_large_reservoir_midpoint_request_is_capped_to_single_safe_cycle",
        "tests/test_dosing_rules.py::test_dosing_uses_pump_specific_safety_caps",
        "tests/test_agentic_decision_engine.py::test_agentic_factor_still_obeys_pump_dose_cap",
        "tests/test_agentic_decision_engine.py::test_agentic_ec_high_targets_midpoint_without_bypassing_ec_down_cap",
    ],
    "agent_output_consistency": [
        "tests/test_agentic_decision_engine.py::test_agentic_waits_when_reading_is_unstable",
        "tests/test_agentic_decision_engine.py::test_agentic_rejects_suspicious_sensor_jump",
        "tests/test_agentic_decision_engine.py::test_agentic_graph_rejects_unjustified_llm_wait_on_stable_deviation",
        "tests/test_agentic_decision_engine.py::test_agentic_graph_forces_llm_outputs_to_match_in_range_payload",
        "tests/test_agentic_decision_engine.py::test_agentic_graph_rejects_false_combined_diagnosis_for_single_metric_deviation",
        "tests/test_agentic_decision_engine.py::test_consistency_review_blocks_agent_outputs_that_conflict_with_payload",
    ],
}


def junit_counts(path: Path) -> dict[str, int | float]:
    root = ET.parse(path).getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    if suite is None:
        raise RuntimeError("pytest did not produce a readable JUnit test suite")
    return {
        "tests": int(suite.attrib.get("tests", 0)),
        "failures": int(suite.attrib.get("failures", 0)),
        "errors": int(suite.attrib.get("errors", 0)),
        "skipped": int(suite.attrib.get("skipped", 0)),
    }


def main() -> None:
    if len(sys.argv) not in {1, 2}:
        raise SystemExit("usage: validate_phase3_safety_gate.py [OUTPUT.json]")

    project_root = Path(__file__).resolve().parents[2]
    backend = project_root / "hanas_backend"
    pytest = backend / "venv" / "bin" / "pytest"
    if not pytest.exists():
        raise SystemExit(f"pytest executable not found: {pytest}")

    group_results = {}
    combined_stdout = []
    with tempfile.TemporaryDirectory(prefix="hanas-safety-validation-") as temporary:
        temporary_path = Path(temporary)
        for group, nodes in GROUPS.items():
            junit = temporary_path / f"{group}.xml"
            command = [str(pytest), "-q", *nodes, f"--junitxml={junit}"]
            completed = subprocess.run(
                command,
                cwd=backend,
                text=True,
                capture_output=True,
                check=False,
            )
            combined_stdout.append(completed.stdout)
            if completed.returncode != 0:
                raise SystemExit(
                    f"{group} failed with exit code {completed.returncode}\n"
                    f"{completed.stdout}\n{completed.stderr}"
                )
            group_results[group] = {
                **junit_counts(junit),
                "selected_test_nodes": nodes,
            }

    totals = {
        key: sum(result[key] for result in group_results.values())
        for key in ("tests", "failures", "errors", "skipped")
    }
    totals["passed"] = (
        totals["tests"] - totals["failures"] - totals["errors"] - totals["skipped"]
    )
    result = {
        "scope": "offline deterministic software verification",
        "groups": group_results,
        "totals": totals,
        "interpretation_limit": (
            "Passing these cases verifies specified software behavior for the "
            "tested inputs. It does not estimate field sensitivity, specificity, "
            "hardware reliability, or the frequency of unsafe model proposals."
        ),
    }
    serialized = json.dumps(result, indent=2)
    if len(sys.argv) == 2:
        Path(sys.argv[1]).write_text(serialized + "\n", encoding="utf-8")
    print(serialized)


if __name__ == "__main__":
    main()
