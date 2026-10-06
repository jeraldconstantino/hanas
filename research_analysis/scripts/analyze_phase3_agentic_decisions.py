#!/usr/bin/env python3
"""Summarize scheduled Phase 3 decisions and write their audit records."""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from pathlib import Path
from zipfile import ZipFile

from generate_phase3_timeseries import shared_strings, worksheet_records


def pct(count: int, total: int) -> float:
    return count / total * 100.0 if total else 0.0


def main() -> None:
    if len(sys.argv) not in {2, 3}:
        raise SystemExit(
            "usage: analyze_phase3_agentic_decisions.py INPUT.xlsx [OUTPUT.json]"
        )

    workbook = Path(sys.argv[1])
    counters: dict[str, Counter[str]] = {
        name: Counter()
        for name in (
            "decision",
            "agentic_action",
            "reasoning_source",
            "safety_gate_source",
            "consistency_review_status",
            "model_used",
            "trace_source",
        )
    }
    scheduled_rows = []
    consistency_issue_rows = 0
    consistency_issue_count = 0
    review_eligible = 0
    actuating_rows = 0
    fallback_involved_rows = 0
    fallback_involved_actuating_rows = 0
    trace_entries = 0

    with ZipFile(workbook) as archive:
        strings = shared_strings(archive)
        for row in worksheet_records(archive, 3, strings):
            metadata = json.loads(row["decision_metadata"])
            if metadata.get("triggered_by") != "batch_scheduler":
                continue

            pump = row["pump_activated"]
            is_actuating = pump != "none"
            actuating_rows += is_actuating
            reasoning_source = str(metadata.get("reasoning_source"))
            fallback_involved = reasoning_source in {
                "mixed_llm_deterministic_fallback",
                "deterministic_fallback",
            }
            fallback_involved_rows += fallback_involved
            fallback_involved_actuating_rows += fallback_involved and is_actuating

            review = metadata.get("consistency_review") or {}
            issues = review.get("issues") or []
            consistency_issue_rows += bool(issues)
            consistency_issue_count += len(issues)
            human_review = metadata.get("human_review_gate") or {}
            review_eligible += human_review.get("eligible_for_review") is True

            counters["decision"][str(row["decision"])] += 1
            counters["agentic_action"][str(metadata.get("agentic_action"))] += 1
            counters["reasoning_source"][reasoning_source] += 1
            counters["safety_gate_source"][str(metadata.get("safety_gate_source"))] += 1
            counters["consistency_review_status"][str(review.get("review_status"))] += 1
            for model in metadata.get("llm_models_used") or []:
                counters["model_used"][str(model)] += 1
            trace = metadata.get("llm_trace") or []
            trace_entries += len(trace)
            for entry in trace:
                counters["trace_source"][str(entry.get("source"))] += 1

            scheduled_rows.append(
                {
                    "id": row["id"],
                    "timestamp": row["timestamp"],
                    "decision": row["decision"],
                    "pump_activated": pump,
                    "agentic_action": metadata.get("agentic_action"),
                    "reasoning_source": reasoning_source,
                    "consistency_review_status": review.get("review_status"),
                    "consistency_issue_count": len(issues),
                    "safety_gate_source": metadata.get("safety_gate_source"),
                    "eligible_for_human_review": human_review.get(
                        "eligible_for_review"
                    ),
                }
            )

    total = len(scheduled_rows)
    result = {
        "scheduled_decisions": total,
        "actuating_decisions": {
            "count": actuating_rows,
            "pct": pct(actuating_rows, total),
        },
        "nonactuating_decisions": {
            "count": total - actuating_rows,
            "pct": pct(total - actuating_rows, total),
        },
        "decision_counts": dict(counters["decision"]),
        "agentic_action_counts": dict(counters["agentic_action"]),
        "reasoning_source_counts": dict(counters["reasoning_source"]),
        "fallback_involved_decisions": fallback_involved_rows,
        "fallback_involved_actuating_decisions": fallback_involved_actuating_rows,
        "consistency_review_status_counts": dict(
            counters["consistency_review_status"]
        ),
        "rows_with_consistency_issues": consistency_issue_rows,
        "consistency_issue_count": consistency_issue_count,
        "safety_gate_source_counts": dict(counters["safety_gate_source"]),
        "human_review_eligible_decisions": review_eligible,
        "llm_trace_entries": trace_entries,
        "llm_trace_source_counts": dict(counters["trace_source"]),
        "model_use_row_counts": dict(counters["model_used"]),
        "interpretation": (
            "These production-record counts describe executed decision routing and "
            "trace completeness. They do not estimate counterfactual improvement "
            "over a deterministic-only controller or the diagnostic accuracy of "
            "the safety gate."
        ),
    }

    serialized = json.dumps(result, indent=2)
    if len(sys.argv) == 3:
        output = Path(sys.argv[2])
        output.write_text(serialized + "\n", encoding="utf-8")
        audit_output = output.with_name("scheduled_decision_audit.csv")
        with audit_output.open("w", newline="", encoding="utf-8") as destination:
            writer = csv.DictWriter(destination, fieldnames=scheduled_rows[0].keys())
            writer.writeheader()
            writer.writerows(scheduled_rows)
    print(serialized)


if __name__ == "__main__":
    main()
