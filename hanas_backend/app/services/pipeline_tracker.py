"""Thread-safe in-memory tracker for the current agentic AI pipeline stage."""

import threading
from datetime import datetime, timezone

_lock = threading.Lock()
_state: dict = {}

# A single LLM stage should normally finish well below this. If a worker dies or
# a request hangs, the UI must not show a live stage forever.
ACTIVE_STAGE_TTL_SECONDS = 60

STAGES = [
    "orchestrator_agent",
    "monitoring_agent",
    "diagnostic_reasoning_agent",
    "decision_agent",
    "dose_planning_agent",
    "consistency_review",
    "safety_gate",
    "human_review_gate",
    "completed",
]


def set_stage(stage: str) -> None:
    """Record that the pipeline has entered a new stage."""
    now = datetime.now(timezone.utc)
    with _lock:
        _state.clear()
        _state.update(
            {
                "stage": stage,
                "updated_at": now.isoformat(),
            }
        )


def get_progress() -> dict:
    """Return the current pipeline stage, or stage=None when idle."""
    with _lock:
        if not _state:
            return {"stage": None, "updated_at": None, "stale": False}

        state = dict(_state)

    updated_at = state.get("updated_at")
    stage = state.get("stage")
    age_seconds = None
    if isinstance(updated_at, str):
        try:
            updated = datetime.fromisoformat(updated_at)
            age_seconds = max(0.0, (datetime.now(timezone.utc) - updated).total_seconds())
        except ValueError:
            age_seconds = None

    is_stale = (
        stage not in (None, "completed")
        and age_seconds is not None
        and age_seconds > ACTIVE_STAGE_TTL_SECONDS
    )
    if is_stale:
        return {
            "stage": None,
            "updated_at": updated_at,
            "stale": True,
            "last_stage": stage,
            "age_seconds": age_seconds,
        }

    state["stale"] = False
    state["age_seconds"] = age_seconds
    return state


def clear() -> None:
    """Reset tracker after a pipeline cycle finishes."""
    with _lock:
        _state.clear()
