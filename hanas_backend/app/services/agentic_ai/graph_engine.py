"""LangGraph-orchestrated LLM agentic decision strategy for HANAS."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any, Sequence
import warnings

from pydantic import BaseModel
from langchain_core._api.deprecation import LangChainPendingDeprecationWarning
from loguru import logger

from app.core.config import settings
from app.schemas.sensor import DosingDecision, ReferenceRange, SensorHistoryEntry, SensorPayload
from app.services.agentic_ai.llm import AgenticLLM, OpenAIJsonLLM
from app.services.agentic_ai.tools import agentic_tool_results
from app.services import pipeline_tracker
from app.services.agentic_ai.prompts import (
    DECISION_AGENT_PROMPT,
    DIAGNOSTIC_REASONING_AGENT_PROMPT,
    DOSE_PLANNING_AGENT_PROMPT,
    MONITORING_AGENT_PROMPT,
    ORCHESTRATOR_AGENT_PROMPT,
)
from app.services.agentic_ai.schemas import (
    MAX_AGENTIC_DOSE_FACTOR,
    MAX_MIXING_ADJUSTMENT_FACTOR,
    MIN_AGENTIC_DOSE_FACTOR,
    MIN_MIXING_ADJUSTMENT_FACTOR,
    AgenticGraphState,
    DiagnosticResult,
    DosePlanResult,
    MonitoringResult,
    StrategyResult,
    OrchestratorResult,
)
from app.services.baseline.dosing_rules import (
    calculate_safe_corrective_actuation,
    effective_max_dose_for_pump,
    evaluate_persistent_dosing_decision,
    max_duration_for_pump,
    reentry_target_value,
)
from app.services.dosing_math import calculate_bounded_dose_and_duration_ms


AGENTIC_STRATEGY = "agentic_ai"
LLM_SOURCE = "llm"
DETERMINISTIC_FALLBACK_SOURCE = "deterministic_fallback"
DEFAULT_AGENTIC_DOSE_FACTOR = 1.0
TARGET_REENTRY_AGENTIC_DOSE_FACTOR = 1.0
LOW_CONFIDENCE_AGENTIC_DOSE_FACTOR = 0.7
DEFAULT_MIXING_ADJUSTMENT_FACTOR = 1.0
MIN_MIXING_TIME_SECONDS = 60
MAX_MIXING_TIME_SECONDS = 450
PH_SENSOR_JUMP_THRESHOLD = 1.0
EC_SENSOR_JUMP_THRESHOLD = 1.0
NATURAL_RECOVERY_HISTORY_POINTS = 4
PROACTIVE_TREND_HISTORY_POINTS = 3
PH_PROACTIVE_BOUNDARY_MARGIN = 0.15
EC_PROACTIVE_BOUNDARY_MARGIN = 0.15
STALE_DOSING_GRACE_SECONDS = 30
PH_DELIVERY_RESPONSE_THRESHOLD = 0.03
EC_DELIVERY_RESPONSE_THRESHOLD = 0.03


def evaluate_agentic_decision(
    payload: SensorPayload,
    reference_range: ReferenceRange,
    recent_logs: Sequence[SensorHistoryEntry] | None = None,
    llm: AgenticLLM | None = None,
    crop_lifecycle: dict[str, Any] | None = None,
    control_history: Sequence[SensorHistoryEntry] | None = None,
) -> DosingDecision:
    """Evaluate an LLM-agentic decision through a state graph and safety gate."""
    history = _fresh_history(list(recent_logs or []))
    adaptive_history = _fresh_history(
        list(control_history) if control_history is not None else list(recent_logs or [])
    )
    baseline = evaluate_persistent_dosing_decision(payload, reference_range, history)
    logger.info(
        "Agentic AI cycle started: ph={} ec={} history_count={} baseline_decision={} baseline_pump={}",
        payload.ph,
        payload.ec,
        len(history),
        baseline.decision,
        baseline.pump_activated,
    )
    state: AgenticGraphState = {
        "payload": payload,
        "reference_range": reference_range,
        "history": history,
        "control_history": adaptive_history,
        "baseline": baseline,
        "llm_trace": [],
        "crop_lifecycle": crop_lifecycle or {},
    }

    llm_adapter = llm or _configured_llm_adapter()
    try:
        final_state = _invoke_agent_graph(state, llm_adapter)
    except Exception:
        pipeline_tracker.clear()
        raise

    pipeline_tracker.set_stage("completed")
    final_decision = final_state["final_decision"]
    logger.info(
        "Agentic AI cycle completed: decision={} pump={} dose_ml={} duration_ms={} action={} reasoning_source={}",
        final_decision.decision,
        final_decision.pump_activated,
        final_decision.dose_ml,
        final_decision.duration_ms,
        final_decision.metadata.get("agentic_action"),
        final_decision.metadata.get("reasoning_source"),
    )
    return final_decision


def _fresh_history(history: list[SensorHistoryEntry]) -> list[SensorHistoryEntry]:
    """Keep the bounded adaptive history window fetched for this experiment run."""
    cutoff = _now_utc() - timedelta(seconds=settings.agentic_adaptive_history_window_seconds)
    windowed_history = [entry for entry in history if _history_freshness_time(entry) >= cutoff]
    if len(windowed_history) != len(history):
        logger.info(
            "Agentic AI history window trimmed: kept_count={} input_count={} window_seconds={}",
            len(windowed_history),
            len(history),
            settings.agentic_adaptive_history_window_seconds,
        )
    return windowed_history


def _continuity_history(
    history: Sequence[SensorHistoryEntry],
) -> list[SensorHistoryEntry]:
    """Return recent rows safe for immediate continuity checks."""
    cutoff = _now_utc() - timedelta(seconds=settings.agentic_history_freshness_gap_seconds)
    return [entry for entry in history if _history_freshness_time(entry) >= cutoff]


def _control_history(state: AgenticGraphState) -> list[SensorHistoryEntry]:
    """Return history that retains pump events for response-aware dosing."""
    return state.get("control_history", state["history"])


def _invoke_agent_graph(
    state: AgenticGraphState,
    llm: AgenticLLM | None,
) -> AgenticGraphState:
    """Invoke the LangGraph workflow, falling back to an equivalent local sequence."""
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                category=LangChainPendingDeprecationWarning,
            )
            from langgraph.graph import END, START, StateGraph
    except ImportError:
        return _invoke_agent_sequence(state, llm)

    graph = StateGraph(AgenticGraphState)
    graph.add_node("orchestrator_agent", lambda graph_state: _orchestrator_node(graph_state, llm))
    graph.add_node("monitoring_agent", lambda graph_state: _monitoring_node(graph_state, llm))
    graph.add_node("diagnostic_reasoning_agent", lambda graph_state: _diagnostic_node(graph_state, llm))
    graph.add_node("decision_agent", lambda graph_state: _decision_node(graph_state, llm))
    graph.add_node("dose_planning_agent", lambda graph_state: _dose_planning_node(graph_state, llm))
    graph.add_node("consistency_review", _consistency_review_node)
    graph.add_node("safety_gate", _safety_gate_node)
    graph.add_node("human_review_gate", _human_review_gate_node)
    graph.add_edge(START, "orchestrator_agent")
    graph.add_conditional_edges(
        "orchestrator_agent",
        _orchestrator_route,
        {
            "monitoring_agent": "monitoring_agent",
            "diagnostic_reasoning_agent": "diagnostic_reasoning_agent",
            "decision_agent": "decision_agent",
            "dose_planning_agent": "dose_planning_agent",
            "consistency_review": "consistency_review",
            "safety_gate": "safety_gate",
        },
    )
    graph.add_edge("monitoring_agent", "orchestrator_agent")
    graph.add_edge("diagnostic_reasoning_agent", "orchestrator_agent")
    graph.add_edge("decision_agent", "orchestrator_agent")
    graph.add_edge("dose_planning_agent", "orchestrator_agent")
    graph.add_edge("consistency_review", "orchestrator_agent")
    graph.add_edge("safety_gate", "human_review_gate")
    graph.add_edge("human_review_gate", END)
    return graph.compile().invoke(state)


def _invoke_agent_sequence(
    state: AgenticGraphState,
    llm: AgenticLLM | None,
) -> AgenticGraphState:
    """Run the same graph nodes sequentially when LangGraph is unavailable."""
    state.update(_orchestrator_node(state, llm))
    route = _orchestrator_route(state)
    for _ in range(12):
        if route == "safety_gate":
            state.update(_safety_gate_node(state))
            state.update(_human_review_gate_node(state))
            return state
        if route == "monitoring_agent":
            state.update(_monitoring_node(state, llm))
        elif route == "diagnostic_reasoning_agent":
            state.update(_diagnostic_node(state, llm))
        elif route == "decision_agent":
            state.update(_decision_node(state, llm))
        elif route == "dose_planning_agent":
            state.update(_dose_planning_node(state, llm))
        elif route == "consistency_review":
            state.update(_consistency_review_node(state))
        else:
            raise RuntimeError(f"Unsupported orchestrator route: {route}")
        state.update(_orchestrator_node(state, llm))
        route = _orchestrator_route(state)
    raise RuntimeError("Agentic orchestrator loop exceeded maximum route count")


def _orchestrator_node(
    state: AgenticGraphState,
    llm: AgenticLLM | None,
) -> dict[str, Any]:
    """Use the orchestrator agent to route the cycle before deeper reasoning."""
    logger.info("Agentic AI stage: orchestrator_agent evaluating route")
    if _orchestrator_has_specialist_feedback(state):
        return _orchestrator_feedback_state(state)

    pipeline_tracker.set_stage("orchestrator_agent")
    heuristic = _heuristic_orchestrator(state)
    if heuristic.route == "safety_gate":
        result, source, model = heuristic, DETERMINISTIC_FALLBACK_SOURCE, None
    else:
        result, source, model = _llm_or_heuristic(
            llm,
            "Orchestrator Agent",
            ORCHESTRATOR_AGENT_PROMPT,
            state,
            OrchestratorResult,
            heuristic,
        )
        result = _ensure_orchestrator_consistency(state, result)

    return _orchestrator_result_to_state(state, result, source, model)


def _orchestrator_has_specialist_feedback(state: AgenticGraphState) -> bool:
    """Return whether a specialist has already written output for this cycle."""
    return any(
        state.get(key) is not None
        for key in ("monitoring", "diagnosis", "strategy", "dose_plan", "consistency_review")
    )


def _orchestrator_feedback_state(state: AgenticGraphState) -> dict[str, Any]:
    """Deterministically route specialist outputs without another LLM call."""
    logger.info("Agentic AI stage: orchestrator_agent evaluating specialist feedback")
    route = _orchestrator_feedback_route(state)
    orchestration = dict(state.get("orchestration") or {})
    route_history = list(orchestration.get("route_history", []))
    reason = _orchestrator_feedback_reason(state, route)
    route_history.append(
        {
            "from": _latest_feedback_source(state),
            "route": route,
            "reason": reason,
        }
    )
    orchestration.update(
        {
            "agent": "orchestrator_agent",
            "route": route,
            "action": _orchestrator_feedback_action(state, route),
            "reason": reason,
            "feedback_source": DETERMINISTIC_FALLBACK_SOURCE,
            "route_history": route_history,
        }
    )
    if route == "safety_gate":
        skipped_agents = list(orchestration.get("skipped_agents", []))
        orchestration["safety_handoff"] = {
            "agent": "orchestrator_agent",
            "status": "ready_for_safety_gate",
            "consulted_agents": _consulted_specialist_agents(state, skipped_agents),
            "skipped_agents": skipped_agents,
            "next_agent": "safety_gate",
            "summary": _pre_safety_manager_summary(state),
        }
    logger.info(
        "Agentic AI stage: orchestrator_agent feedback_from={} route={} skipped_agents={} reason={}",
        _latest_feedback_source(state),
        route,
        orchestration.get("skipped_agents", []),
        orchestration["reason"],
    )
    return {
        "orchestration": orchestration,
    }


def _orchestrator_feedback_route(state: AgenticGraphState) -> str:
    """Choose the next specialist or Safety Gate from current specialist outputs."""
    if state.get("monitoring") is None:
        return "monitoring_agent"
    if _monitoring_route(state) == "safety_gate":
        return "safety_gate"
    if state.get("diagnosis") is None:
        return "diagnostic_reasoning_agent"
    if state.get("strategy") is None:
        return "decision_agent"
    if _decision_route(state) == "safety_gate":
        return "safety_gate"
    if state.get("dose_plan") is None:
        return "dose_planning_agent"
    if state.get("consistency_review") is None:
        return "consistency_review"
    return "safety_gate"


def _latest_feedback_source(state: AgenticGraphState) -> str:
    """Return the most recent stage that handed feedback to the orchestrator."""
    skipped = set(state.get("orchestration", {}).get("skipped_agents", []))
    if state.get("consistency_review") is not None and "consistency_review" not in skipped:
        return "consistency_review"
    if state.get("dose_plan") is not None and "dose_planning_agent" not in skipped:
        return "dose_planning_agent"
    if state.get("strategy") is not None and "decision_agent" not in skipped:
        return "decision_agent"
    if state.get("diagnosis") is not None and "diagnostic_reasoning_agent" not in skipped:
        return "diagnostic_reasoning_agent"
    if state.get("monitoring") is not None:
        return "monitoring_agent"
    return "initial_context"


def _orchestrator_feedback_action(state: AgenticGraphState, route: str) -> str:
    """Return a compact manager action for the selected route."""
    if route != "safety_gate":
        return "proceed"
    strategy = state.get("strategy")
    if strategy is not None:
        return strategy.action
    monitoring = state.get("monitoring")
    if monitoring is not None and monitoring.status == "mixing":
        return "mix_longer"
    if monitoring is not None and monitoring.status == "in_range":
        return "no_action"
    return "wait"


def _orchestrator_feedback_reason(state: AgenticGraphState, route: str) -> str:
    """Describe why the manager chose the next route."""
    source = _latest_feedback_source(state)
    if route == "diagnostic_reasoning_agent":
        monitoring = state["monitoring"]
        return f"Monitoring reported {monitoring.status}; Diagnostic Reasoning Agent should classify the condition."
    if route == "decision_agent":
        diagnosis = state["diagnosis"]
        return f"Diagnostic classified {diagnosis.classification}; Decision Agent should choose the action."
    if route == "dose_planning_agent":
        strategy = state["strategy"]
        return f"Decision selected {strategy.action}; Dose Planning Agent should size the candidate command."
    if route == "consistency_review":
        dose_plan = state["dose_plan"]
        return f"Dose Planning selected {dose_plan.pump_activated}; Consistency Review should cross-check the plan."
    if route == "safety_gate":
        return _pre_safety_manager_summary(state)
    return f"{source} feedback received; Monitoring Agent should evaluate the reading."


def _heuristic_orchestrator(
    state: AgenticGraphState,
) -> OrchestratorResult:
    """Return the deterministic routing result used when the orchestrator is local."""
    hard_status = _hard_monitoring_status(state)
    if hard_status is not None:
        return OrchestratorResult(
            route="safety_gate",
            action="mix_longer" if hard_status.status == "mixing" else "wait",
            confidence=0.92,
            reason=hard_status.summary,
        )

    return OrchestratorResult(
        route="monitoring_agent",
        action="proceed",
        confidence=0.82,
        reason="Monitoring agent should classify the current range, trend, and projection state.",
    )


def _ensure_orchestrator_consistency(
    state: AgenticGraphState,
    result: OrchestratorResult,
) -> OrchestratorResult:
    """Keep orchestrator routing aligned with deterministic safety checks."""
    heuristic = _heuristic_orchestrator(state)
    if heuristic.route == "safety_gate":
        return heuristic
    if result.route == "safety_gate":
        return heuristic
    return result.model_copy(
        update={
            "route": "monitoring_agent",
            "action": "proceed",
            "confidence": min(max(result.confidence, 0.0), 1.0),
            "reason": heuristic.reason,
        }
    )


def _orchestrator_result_to_state(
    state: AgenticGraphState,
    orchestrator_result: OrchestratorResult,
    source: str,
    model: str | None,
) -> dict[str, Any]:
    """Convert the orchestrator agent result into graph state updates."""
    monitoring = _hard_monitoring_status(state)
    orchestration = {
        "agent": "orchestrator_agent",
        "route": orchestrator_result.route,
        "action": orchestrator_result.action,
        "confidence": orchestrator_result.confidence,
        "reason": orchestrator_result.reason,
        "routing_reason": orchestrator_result.reason,
        "source": source,
        "model": model,
        "initial_source": source,
        "initial_model": model,
        "consulted_agent": "monitoring_agent" if orchestrator_result.route == "monitoring_agent" else None,
        "monitoring_precheck": monitoring.model_dump() if monitoring is not None else None,
        "skipped_agents": [],
        "initial_route": orchestrator_result.route,
        "route_history": [
            {
                "from": "initial_context",
                "route": orchestrator_result.route,
                "reason": orchestrator_result.reason,
            }
        ],
    }

    if monitoring is not None:
        action = "mix_longer" if monitoring.status == "mixing" else "wait"
        result = _orchestrated_terminal_state(
            state,
            monitoring=monitoring,
            strategy=StrategyResult(
                action=action,
                confidence=orchestrator_result.confidence,
                reason=monitoring.summary,
            ),
            orchestration=orchestration,
        )
        return _with_orchestrator_trace(state, result, orchestrator_result, source, model)

    return _with_orchestrator_trace(state, {"orchestration": orchestration}, orchestrator_result, source, model)


def _orchestrated_terminal_state(
    state: AgenticGraphState,
    monitoring: MonitoringResult,
    strategy: StrategyResult,
    orchestration: dict[str, Any],
) -> dict[str, Any]:
    diagnosis = _diagnosis_for_monitoring(state, monitoring)
    dose_plan = DosePlanResult(
        pump_activated="none",
        mixing_adjustment_factor=DEFAULT_MIXING_ADJUSTMENT_FACTOR,
        dose_adjustment_factor=DEFAULT_AGENTIC_DOSE_FACTOR,
        reason="No dose is planned because the orchestrator routed directly to safety.",
    )
    orchestration["skipped_agents"] = [
        "monitoring_agent",
        "diagnostic_reasoning_agent",
        "decision_agent",
        "dose_planning_agent",
    ]
    orchestration["safety_handoff"] = {
        "agent": "orchestrator_agent",
        "status": "ready_for_safety_gate",
        "consulted_agents": [],
        "skipped_agents": orchestration["skipped_agents"],
        "next_agent": "safety_gate",
        "summary": "Manager routed directly to Safety Gate for a hard precheck stop.",
    }
    return {
        "orchestration": orchestration,
        "monitoring": monitoring,
        "diagnosis": diagnosis,
        "strategy": strategy,
        "dose_plan": dose_plan,
    }


def _with_orchestrator_trace(
    state: AgenticGraphState,
    result: dict[str, Any],
    orchestrator_result: OrchestratorResult,
    source: str,
    model: str | None,
) -> dict[str, Any]:
    """Attach trace metadata for the orchestrator agent result."""
    _log_orchestration_result(result)
    result["llm_trace"] = _trace(state, "orchestrator_agent", orchestrator_result, source, model)
    return result


def _orchestrator_route(state: AgenticGraphState) -> str:
    """Return the next LangGraph node selected by the orchestrator agent."""
    route = state.get("orchestration", {}).get("route")
    if route in {
        "monitoring_agent",
        "diagnostic_reasoning_agent",
        "decision_agent",
        "dose_planning_agent",
        "consistency_review",
        "safety_gate",
    }:
        return str(route)
    return "monitoring_agent"


def _log_orchestration_result(result: dict[str, Any]) -> None:
    orchestration = result["orchestration"]
    logger.info(
        "Agentic AI stage: orchestrator_agent route={} skipped_agents={} reason={}",
        orchestration["route"],
        orchestration["skipped_agents"],
        orchestration["reason"],
    )


def _monitoring_node(
    state: AgenticGraphState,
    llm: AgenticLLM | None,
) -> dict[str, Any]:
    logger.info("Agentic AI stage: monitoring_agent started")
    pipeline_tracker.set_stage("monitoring_agent")
    heuristic = _heuristic_monitoring(state)
    result, source, model = _llm_or_heuristic(
        llm,
        "Monitoring Agent",
        MONITORING_AGENT_PROMPT,
        state,
        MonitoringResult,
        heuristic,
    )
    result = _ensure_monitoring_consistency(state, result, require_recovery_assessment=source == LLM_SOURCE)
    if source == LLM_SOURCE and "recovery_assessment_conflict" in result.risk_flags:
        # Ask the agent to reconcile its own evidence and flag once. A second
        # invalid assessment stays a no-pump hold rather than forcing recovery.
        context = _monitoring_context(state)
        context["monitoring_review"] = {
            "previous_output": result.model_dump(),
            "instruction": "Reconcile is_recovering with your per-metric recovery_assessment. Apply the same recovery definition to pH and EC; reaching the target is not required. Return a complete corrected monitoring result.",
        }
        try:
            completion = llm.complete_json("Monitoring Agent", MONITORING_AGENT_PROMPT, context, MonitoringResult)
            if isinstance(completion.output, MonitoringResult):
                result = _ensure_monitoring_consistency(state, completion.output, require_recovery_assessment=True)
                model = completion.model
        except Exception:
            pass  # Preserve the conflicting assessment's conservative hold.
    logger.info(
        "Agentic AI stage: monitoring_agent completed status={} stable={} recovering={} source={} model={}",
        result.status,
        result.is_stable,
        result.is_recovering,
        source,
        model or "none",
    )
    updates: dict[str, Any] = {
        "monitoring": result,
        "llm_trace": _trace(state, "monitoring_agent", result, source, model),
    }
    if _monitoring_is_terminal(result):
        updates.update(_monitoring_terminal_state(state, result))
    return updates


def _monitoring_route(state: AgenticGraphState) -> str:
    """Route after monitoring so in-range/noise cases stop before deeper agents."""
    monitoring = state["monitoring"]
    if _monitoring_is_terminal(monitoring):
        return "safety_gate"
    return "diagnostic_reasoning_agent"


def _monitoring_is_terminal(monitoring: MonitoringResult) -> bool:
    return (
        monitoring.status in {"in_range", "unstable", "sensor_anomaly", "mixing", "confirming"}
        or monitoring.is_recovering
        or "near_boundary" in monitoring.risk_flags
        or "recovery_assessment_conflict" in monitoring.risk_flags
    )


def _monitoring_terminal_state(
    state: AgenticGraphState,
    monitoring: MonitoringResult,
) -> dict[str, Any]:
    if monitoring.status == "in_range":
        strategy = StrategyResult(
            action="no_action",
            confidence=0.95,
            reason="pH and EC are within target ranges, so no dosing is needed.",
        )
    elif monitoring.status == "mixing":
        strategy = StrategyResult(action="mix_longer", confidence=0.9, reason=monitoring.summary)
    else:
        strategy = StrategyResult(action="wait", confidence=0.9, reason=monitoring.summary)

    diagnosis = _diagnosis_for_monitoring(state, monitoring)
    dose_plan = DosePlanResult(
        pump_activated="none",
        mixing_adjustment_factor=DEFAULT_MIXING_ADJUSTMENT_FACTOR,
        dose_adjustment_factor=DEFAULT_AGENTIC_DOSE_FACTOR,
        reason="No dose is planned because monitoring did not identify an actionable dosing state.",
    )
    orchestration = {
        **state.get("orchestration", {}),
        "route_after_monitoring": "safety_gate",
        "skipped_agents": [
            "diagnostic_reasoning_agent",
            "decision_agent",
            "dose_planning_agent",
        ],
    }
    return {
        "orchestration": orchestration,
        "diagnosis": diagnosis,
        "strategy": strategy,
        "dose_plan": dose_plan,
    }


def _diagnostic_node(
    state: AgenticGraphState,
    llm: AgenticLLM | None,
) -> dict[str, Any]:
    logger.info("Agentic AI stage: diagnostic_reasoning_agent started")
    pipeline_tracker.set_stage("diagnostic_reasoning_agent")
    heuristic = _heuristic_diagnosis(state)
    result, source, model = _llm_or_heuristic(
        llm,
        "Diagnostic Reasoning Agent",
        DIAGNOSTIC_REASONING_AGENT_PROMPT,
        state,
        DiagnosticResult,
        heuristic,
    )
    result = _ensure_diagnosis_consistency(state, result)
    logger.info(
        "Agentic AI stage: diagnostic_reasoning_agent completed classification={} primary_metric={} source={} model={}",
        result.classification,
        result.primary_metric,
        source,
        model or "none",
    )
    return {"diagnosis": result, "llm_trace": _trace(state, "diagnostic_reasoning_agent", result, source, model)}


def _decision_node(
    state: AgenticGraphState,
    llm: AgenticLLM | None,
) -> dict[str, Any]:
    logger.info("Agentic AI stage: decision_agent started")
    pipeline_tracker.set_stage("decision_agent")
    heuristic = _heuristic_strategy(state)
    result, source, model = _llm_or_heuristic(
        llm,
        "Decision Agent",
        DECISION_AGENT_PROMPT,
        state,
        StrategyResult,
        heuristic,
    )
    result = _ensure_strategy_consistency(state, result)
    logger.info(
        "Agentic AI stage: decision_agent completed action={} confidence={} source={} model={}",
        result.action,
        result.confidence,
        source,
        model or "none",
    )
    updates: dict[str, Any] = {
        "strategy": result,
        "llm_trace": _trace(state, "decision_agent", result, source, model),
    }
    if result.action not in {"dose", "fallback_baseline"}:
        updates.update(_decision_terminal_state(state, result))
    return updates


def _decision_route(state: AgenticGraphState) -> str:
    """Skip Dose Planning Agent when Decision Agent does not choose dosing."""
    strategy = state["strategy"]
    if strategy.action in {"dose", "fallback_baseline"}:
        return "dose_planning_agent"
    return "safety_gate"


def _decision_terminal_state(
    state: AgenticGraphState,
    strategy: StrategyResult,
) -> dict[str, Any]:
    dose_plan = DosePlanResult(
        pump_activated="none",
        mixing_adjustment_factor=DEFAULT_MIXING_ADJUSTMENT_FACTOR,
        dose_adjustment_factor=DEFAULT_AGENTIC_DOSE_FACTOR,
        reason="No dose is planned because the Decision Agent did not choose dosing.",
    )
    orchestration = {
        **state.get("orchestration", {}),
        "route_after_decision": "safety_gate",
        "skipped_agents": [
            *state.get("orchestration", {}).get("skipped_agents", []),
            "dose_planning_agent",
            "consistency_review",
        ],
    }
    return {
        "orchestration": orchestration,
        "dose_plan": dose_plan,
    }


def _dose_planning_node(
    state: AgenticGraphState,
    llm: AgenticLLM | None,
) -> dict[str, Any]:
    logger.info("Agentic AI stage: dose_planning_agent started")
    pipeline_tracker.set_stage("dose_planning_agent")
    heuristic = _heuristic_dose_plan(state)
    result, source, model = _llm_or_heuristic(
        llm,
        "Dose Planning Agent",
        DOSE_PLANNING_AGENT_PROMPT,
        state,
        DosePlanResult,
        heuristic,
    )
    result = _ensure_dose_plan_consistency(state, result)
    candidate = _calculate_dose_plan_candidate(state, result)
    result = result.model_copy(update={
        "candidate_dose_ml": candidate["dose_ml"],
        "candidate_duration_ms": candidate["duration_ms"],
        "candidate_mixing_time_seconds": candidate["mixing_window"]["effective_seconds"],
    })
    logger.info(
        "Agentic AI stage: dose_planning_agent completed pump={} dose_factor={} mixing_factor={} candidate_ml={} candidate_ms={} candidate_mixing_seconds={} source={} model={}",
        result.pump_activated,
        result.dose_adjustment_factor,
        result.mixing_adjustment_factor,
        result.candidate_dose_ml,
        result.candidate_duration_ms,
        result.candidate_mixing_time_seconds,
        source,
        model or "none",
    )
    return {"dose_plan": result, "llm_trace": _trace(state, "dose_planning_agent", result, source, model)}


def _consistency_review_node(state: AgenticGraphState) -> dict[str, Any]:
    """Run a deterministic review of agent outputs before hardware safety gating."""
    logger.info("Agentic AI stage: consistency_review started")
    pipeline_tracker.set_stage("consistency_review")
    review = _consistency_review(state)
    logger.info(
        "Agentic AI stage: consistency_review completed status={} issues={}",
        review["review_status"],
        len(review["issues"]),
    )
    return {"consistency_review": review}


def _consulted_specialist_agents(
    state: AgenticGraphState,
    skipped_agents: Sequence[str],
) -> list[str]:
    """Return specialist agents that actually produced outputs for this graph path."""
    skipped = set(skipped_agents)
    consulted: list[str] = []
    for state_key, agent_name in (
        ("monitoring", "monitoring_agent"),
        ("diagnosis", "diagnostic_reasoning_agent"),
        ("strategy", "decision_agent"),
        ("dose_plan", "dose_planning_agent"),
    ):
        if state.get(state_key) is not None and agent_name not in skipped:
            consulted.append(agent_name)
    if state.get("consistency_review") is not None and "consistency_review" not in skipped:
        consulted.append("consistency_review")
    return consulted


def _pre_safety_manager_summary(state: AgenticGraphState) -> str:
    """Summarize the manager's pre-safety handoff without issuing commands."""
    monitoring = state.get("monitoring")
    diagnosis = state.get("diagnosis")
    strategy = state.get("strategy")
    dose_plan = state.get("dose_plan")
    skipped_agents = set(state.get("orchestration", {}).get("skipped_agents", []))

    if monitoring is None:
        return "Manager routed directly to Safety Gate for a hard precheck stop."

    if "diagnostic_reasoning_agent" in skipped_agents:
        return (
            f"Manager asked Monitoring Agent; it reported {monitoring.status}, "
            "so specialist dosing agents were not needed before Safety Gate."
        )

    if diagnosis is None or strategy is None:
        return (
            f"Manager asked Monitoring Agent; it reported {monitoring.status}, "
            "so Safety Gate will make the deterministic final check."
        )

    if "dose_planning_agent" in skipped_agents or dose_plan is None or dose_plan.pump_activated == "none":
        return (
            "Manager asked Monitoring, Diagnostic, and Decision agents; "
            f"Diagnostic classified {diagnosis.classification}, Decision selected "
            f"{strategy.action}, and no dose plan is being sent to Safety Gate."
        )

    return (
        "Manager asked Monitoring, Diagnostic, Decision, and Dose Planning agents; "
        f"Diagnostic classified {diagnosis.classification}, Decision selected "
        f"{strategy.action}, and the {dose_plan.pump_activated} plan is being sent "
        "to Safety Gate for deterministic bounds."
    )


def _calculate_dose_plan_candidate(state: AgenticGraphState, dose_plan: DosePlanResult) -> dict[str, Any]:
    """Calibrated calculation tool used by planning and independently by Safety Gate."""
    if dose_plan.pump_activated == "none" or state["strategy"].action not in {"dose", "fallback_baseline"}:
        return {"dose_ml": 0.0, "duration_ms": 0, "mixing_window": {"effective_seconds": 0}}
    control_decision = _agentic_control_decision(state)
    reference_range = state["reference_range"]
    strategy = state["strategy"]
    crop_policy = _crop_dosing_policy(state)
    dose_factor = _safe_dose_factor(state, dose_plan, control_decision, strategy)
    max_dose_ml = effective_max_dose_for_pump(reference_range, control_decision.pump_activated)
    calibrated_dose_ml = control_decision.metadata.get("requested_actuation_dose_ml")
    if not isinstance(calibrated_dose_ml, int | float):
        calibrated_dose_ml = control_decision.dose_ml
    dose_ml = round(
        min(float(calibrated_dose_ml) * dose_factor, max_dose_ml),
        2,
    )
    dose_ml, duration_ms, actuation_metadata = calculate_safe_corrective_actuation(
        dose_ml,
        state["payload"],
        reference_range,
        control_decision.decision,
        control_decision.pump_activated,
    )
    mixing_window = _mixing_window_for_dose(
        dose_plan,
        control_decision,
        _control_history(state),
        strategy,
        dose_factor,
        dose_ml,
        reference_range,
        crop_policy,
    )
    return {
        "dose_ml": dose_ml, "duration_ms": duration_ms, "mixing_window": mixing_window,
        "actuation_metadata": actuation_metadata, "max_dose_ml": max_dose_ml,
        "calibrated_dose_ml": calibrated_dose_ml, "dose_factor": dose_factor,
    }


def _safety_gate_node(state: AgenticGraphState) -> dict[str, Any]:
    """Create the final ESP32 command with deterministic safety enforcement."""
    logger.info("Agentic AI stage: safety_gate started")
    pipeline_tracker.set_stage("safety_gate")
    baseline = state["baseline"]
    monitoring = state["monitoring"]
    strategy = state["strategy"]
    dose_plan = state["dose_plan"]
    reference_range = state["reference_range"]
    history = _control_history(state)
    metadata = _base_metadata(state)

    hard_block_reason = _hard_safety_block_reason(state)
    delivery_issue = _delivery_issue_context(state, baseline)
    projected = _active_projected_context(state)
    consistency_review = state.get("consistency_review") or _consistency_review(state)
    if strategy.action in {"dose", "fallback_baseline"} and not hard_block_reason and consistency_review["review_status"] != "block":
        selected_pump = _agentic_control_decision(state).pump_activated
        expected = (
            _calculate_dose_plan_candidate(state, dose_plan)
            if dose_plan.pump_activated == selected_pump and selected_pump != "none" else None
        )
        valid_candidate = (
            expected is not None
            and dose_plan.candidate_dose_ml == expected["dose_ml"]
            and dose_plan.candidate_duration_ms == expected["duration_ms"]
            and dose_plan.candidate_mixing_time_seconds == expected["mixing_window"]["effective_seconds"]
        )
        metadata["dose_plan_validation"] = {
            "source": "independent_calibrated_recalculation", "valid": valid_candidate,
        }
        if not valid_candidate:
            consistency_review = {
                **consistency_review,
                "review_status": "block",
                "issues": [*consistency_review.get("issues", []), "dose_plan_candidate_conflicts_with_calibration"],
                "reason": "Dose Planning candidate is missing or conflicts with calibrated dose, runtime, or mixing calculations.",
            }
            metadata["consistency_review"] = consistency_review
    if baseline.decision == "wait_near_boundary":
        final = _agentic_no_action(
            baseline,
            decision="wait_near_boundary",
            reason=strategy.reason,
            metadata={
                **metadata,
                "agentic_action": "wait",
                "risk_flags": list(dict.fromkeys([*monitoring.risk_flags, "near_boundary"])),
                "confidence": strategy.confidence,
            },
        )
    elif (
        baseline.pump_activated == "none"
        and monitoring.status != "projected_deviation"
        and not hard_block_reason
    ):
        final = _agentic_no_action(
            baseline,
            decision="within_range",
            reason=_in_range_reason(state),
            metadata={
                **metadata,
                "agentic_action": "monitor",
                "risk_flags": [
                    *monitoring.risk_flags,
                    *baseline.metadata.get("risk_flags", []),
                ],
                "confidence": strategy.confidence,
            },
        )
    elif consistency_review["review_status"] == "block":
        final = _agentic_no_action(
            baseline,
            decision="wait_consistency_review",
            reason=consistency_review["reason"],
            metadata={
                **metadata,
                "agentic_action": "wait",
                "risk_flags": monitoring.risk_flags + ["consistency_review_block"],
                "confidence": strategy.confidence,
            },
        )
    elif hard_block_reason:
        mixing_window_check = _mixing_window_check_from_history(history)
        final = _agentic_no_action(
            baseline,
            decision=_blocked_decision_name(monitoring),
            reason=hard_block_reason,
            metadata={
                **metadata,
                "agentic_action": "wait",
                "risk_flags": monitoring.risk_flags,
                **({"mixing_window_check": mixing_window_check} if mixing_window_check else {}),
                "confidence": strategy.confidence,
            },
        )
    elif strategy.action in {"wait", "mix_longer", "no_action"}:
        final = _agentic_no_action(
            baseline,
            decision=_strategy_decision_name(strategy, monitoring),
            reason=strategy.reason,
            metadata={
                **metadata,
                "agentic_action": strategy.action,
                "risk_flags": monitoring.risk_flags,
                "trend": "moving_toward_target" if monitoring.is_recovering else "not_recovering",
                "confidence": strategy.confidence,
            },
        )
    else:
        control_decision = _agentic_control_decision(state)
        crop_policy = _crop_dosing_policy(state)
        dose_factor = _safe_dose_factor(state, dose_plan, control_decision, strategy)
        final_reason = _final_dose_reason(
            state,
            control_decision,
            dose_factor,
            dose_plan.reason or strategy.reason,
        )
        max_dose_ml = expected["max_dose_ml"]
        calibrated_dose_ml = expected["calibrated_dose_ml"]
        # These concrete values originate in Dose Planning and were independently
        # checked above; the gate never trusts a model's unaided arithmetic.
        dose_ml = dose_plan.candidate_dose_ml
        duration_ms = dose_plan.candidate_duration_ms
        actuation_metadata = expected["actuation_metadata"]
        mixing_window = expected["mixing_window"]
        mixing_window["effective_seconds"] = dose_plan.candidate_mixing_time_seconds
        mixing_window["reason"] = final_reason
        logger.info(
            "Agentic AI mixing window calculated: base_seconds={} effective_seconds={} adjustment_factor={} "
            "llm_factor={} deterministic_factor={} bounds={}..{}",
            mixing_window["base_seconds"],
            mixing_window["effective_seconds"],
            mixing_window["adjustment_factor"],
            mixing_window["llm_recommended_factor"],
            mixing_window["deterministic_factor"],
            mixing_window["min_seconds"],
            mixing_window["max_seconds"],
        )
        final = control_decision.model_copy(
            update={
                "dose_ml": dose_ml,
                "duration_ms": duration_ms,
                "reason": final_reason,
                "metadata": {
                    **control_decision.metadata,
                    **metadata,
                    "agentic_action": "dose",
                    "agentic_primary_decision": {
                        "decision": control_decision.decision,
                        "pump_activated": control_decision.pump_activated,
                        "reason": control_decision.reason,
                    },
                    "one_action_per_cycle": True,
                    "requested_dose_adjustment_factor": dose_plan.dose_adjustment_factor,
                    "dose_adjustment_factor": dose_factor,
                    "applied_dose_adjustment_factor": dose_factor,
                    "target_range_factor_cap": _target_range_factor_cap(control_decision),
                    "dose_factor_source": "dose_planning_agent_bounded_by_safety_gate",
                    "crop_dosing_policy": crop_policy,
                    "dose_before_factor_ml": calibrated_dose_ml,
                    "duration_before_factor_ms": control_decision.duration_ms,
                    **actuation_metadata,
                    "baseline_dose_ml": baseline.dose_ml,
                    "baseline_duration_ms": baseline.duration_ms,
                    "mixing_window": mixing_window,
                    "risk_flags": monitoring.risk_flags,
                    "safety_bounds": {
                        "max_dose_ml_per_cycle": reference_range.max_dose_ml_per_cycle,
                        "pump_max_dose_ml_per_cycle": max_dose_ml,
                        "pump_max_duration_ms": max_duration_for_pump(
                            reference_range,
                            control_decision.pump_activated,
                        ),
                        "global_default_max_duration_ms": settings.maximum_pump_duration_ms,
                        "min_mixing_time_seconds": MIN_MIXING_TIME_SECONDS,
                        "max_mixing_time_seconds": MAX_MIXING_TIME_SECONDS,
                    },
                    "confidence": strategy.confidence,
                },
            },
        )

    final = _with_final_orchestrator_summary(state, final)

    if delivery_issue["issue_detected"]:
        final = final.model_copy(
            update={
                "metadata": {
                    **final.metadata,
                    "risk_flags": list(
                        dict.fromkeys(
                            [*final.metadata.get("risk_flags", []), "possible_delivery_issue"]
                        )
                    ),
                    "delivery_issue": delivery_issue,
                    "delivery_issue_policy": "warn_only",
                },
            }
        )

    logger.info(
        "Agentic AI stage: safety_gate completed decision={} pump={} dose_ml={} duration_ms={} reason={}",
        final.decision,
        final.pump_activated,
        final.dose_ml,
        final.duration_ms,
        final.reason,
    )
    return {"final_decision": final}


def _human_review_gate_node(state: AgenticGraphState) -> dict[str, Any]:
    """Mark where database-backed HITL review can hold an approved command."""
    logger.info("Agentic AI stage: human_review_gate evaluating operator gate")
    pipeline_tracker.set_stage("human_review_gate")
    final = state["final_decision"]
    review_eligible = (
        final.metadata.get("executed_strategy") == AGENTIC_STRATEGY
        and final.pump_activated != "none"
        and final.dose_ml > 0
        and final.duration_ms > 0
    )
    metadata = {
        **final.metadata,
        "human_review_gate": {
            "stage": "after_safety_gate",
            "eligible_for_review": review_eligible,
            "runtime_setting": "hitl_enabled",
            "review_applied_by": "sensor_ingestion_route",
            "persistence": "control_cycle_database_state",
            "next_stage_if_not_held": "esp32_execution",
        },
    }
    return {"final_decision": final.model_copy(update={"metadata": metadata})}


def _with_final_orchestrator_summary(
    state: AgenticGraphState,
    final: DosingDecision,
) -> DosingDecision:
    """Replace the routing reason with an end-of-cycle orchestration summary."""
    orchestrator = dict(final.metadata.get("orchestrator_agent") or {})
    if not orchestrator:
        return final

    routing_reason = orchestrator.get("reason")
    if routing_reason:
        orchestrator.setdefault("routing_reason", routing_reason)
    orchestrator["reason"] = _final_orchestrator_summary(state, final)

    return final.model_copy(
        update={
            "metadata": {
                **final.metadata,
                "orchestrator_agent": orchestrator,
            }
        }
    )


def _final_orchestrator_summary(
    state: AgenticGraphState,
    final: DosingDecision,
) -> str:
    """Summarize what the manager observed across the completed graph path."""
    monitoring = state["monitoring"]
    diagnosis = state["diagnosis"]
    strategy = state["strategy"]
    dose_plan = state["dose_plan"]
    skipped_agents = state.get("orchestration", {}).get("skipped_agents", [])

    if state.get("orchestration", {}).get("initial_route") == "safety_gate":
        return (
            "Orchestrator stopped at Safety Gate after the monitoring precheck "
            f"reported {monitoring.status}; final decision is {final.decision}."
        )

    if "diagnostic_reasoning_agent" in skipped_agents:
        return (
            "Orchestrator routed to Monitoring; Monitoring reported "
            f"{monitoring.status}, so downstream dosing agents were skipped and "
            f"Safety Gate returned {final.decision}."
        )

    if "dose_planning_agent" in skipped_agents:
        return (
            "Orchestrator routed through Monitoring, Diagnostic, and Decision; "
            f"Monitoring reported {monitoring.status}, Diagnostic classified "
            f"{diagnosis.classification}, Decision selected {strategy.action}, "
            f"and Safety Gate returned {final.decision} without dosing."
        )

    if final.pump_activated != "none":
        return (
            "Orchestrator routed through Monitoring, Diagnostic, Decision, and "
            f"Dose Planning; Monitoring reported {monitoring.status}, Diagnostic "
            f"classified {diagnosis.classification}, Decision selected {strategy.action}, "
            f"Dose Planning selected {dose_plan.pump_activated}, and Safety Gate "
            f"issued bounded {final.pump_activated} dosing."
        )

    return (
        "Orchestrator routed the full agent flow; Monitoring reported "
        f"{monitoring.status}, Diagnostic classified {diagnosis.classification}, "
        f"Decision selected {strategy.action}, and Safety Gate returned "
        f"{final.decision}."
    )


def _llm_or_heuristic(
    llm: AgenticLLM | None,
    agent_name: str,
    prompt: str,
    state: AgenticGraphState,
    output_model: type[BaseModel],
    heuristic: BaseModel,
) -> tuple[BaseModel, str, str | None]:
    """Return LLM output when available and valid, otherwise heuristic output."""
    if llm is None:
        return heuristic, DETERMINISTIC_FALLBACK_SOURCE, None

    try:
        context = _monitoring_context(state) if output_model is MonitoringResult else _agent_context(state)
        completion = llm.complete_json(agent_name, prompt, context, output_model)
        if not isinstance(completion.output, output_model):
            logger.warning(
                "Agentic AI stage: {} returned {} instead of {}; using deterministic fallback",
                agent_name,
                type(completion.output).__name__,
                output_model.__name__,
            )
            return heuristic, DETERMINISTIC_FALLBACK_SOURCE, None
        return completion.output, LLM_SOURCE, completion.model
    except Exception:
        return heuristic, DETERMINISTIC_FALLBACK_SOURCE, None


def _ensure_monitoring_consistency(
    state: AgenticGraphState,
    result: MonitoringResult,
    *,
    require_recovery_assessment: bool = False,
) -> MonitoringResult:
    baseline = state["baseline"]
    hard_status = _hard_monitoring_status(state)
    if hard_status is not None:
        return hard_status

    near_boundary_reason = _near_boundary_wait_reason(state)
    if near_boundary_reason:
        return MonitoringResult(
            status="deviation",
            is_stable=True,
            is_recovering=False,
            risk_flags=["near_boundary"],
            summary=near_boundary_reason,
        )

    if baseline.pump_activated == "none":
        if result.status == "projected_deviation":
            risk_flags = result.risk_flags
            if "projected_out_of_range" not in risk_flags:
                risk_flags = [*risk_flags, "projected_out_of_range"]
            return result.model_copy(
                update={
                    "status": "projected_deviation",
                    "is_stable": True,
                    "is_recovering": False,
                    "risk_flags": _non_hard_risk_flags(risk_flags),
                }
            )
        if result.status == "sensor_anomaly":
            risk_flags = result.risk_flags
            if "sensor_anomaly" not in risk_flags:
                risk_flags = [*risk_flags, "sensor_anomaly"]
            return result.model_copy(
                update={
                    "status": "sensor_anomaly",
                    "is_stable": True,
                    "is_recovering": False,
                    "risk_flags": risk_flags,
                }
            )
        return MonitoringResult(
            status="in_range",
            is_stable=True,
            is_recovering=False,
            risk_flags=[],
            summary=_in_range_summary(state),
        )

    initial_confirmation_reason = _initial_confirmation_reason(state)
    if initial_confirmation_reason:
        return MonitoringResult(
            status="confirming",
            is_stable=True,
            is_recovering=False,
            risk_flags=["initial_confirmation"],
            summary=initial_confirmation_reason,
        )

    # Recovery is the monitoring agent's assessment. The deterministic fallback
    # supplies its own assessment before reaching this consistency check.
    if require_recovery_assessment and (result.is_recovering or result.recovery_assessment is not None):
        assessment = result.recovery_assessment
        payload = state["payload"]
        reference_range = state["reference_range"]
        expected_ph = (
            "in_range" if reference_range.ph_target_min <= payload.ph <= reference_range.ph_target_max
            else "toward_range"
        )
        expected_ec = (
            "in_range" if reference_range.ec_target_min <= payload.ec <= reference_range.ec_target_max
            else "toward_range"
        )
        assessment_supports_recovery = (
            assessment is not None
            and assessment.ph_trend == expected_ph
            and assessment.ec_trend == expected_ec
            and assessment.history_is_fresh
            and assessment.sufficient_observations
            and not assessment.executed_dose_in_window
        )
        if result.is_recovering != assessment_supports_recovery:
            return MonitoringResult(
                status="deviation", is_stable=True, is_recovering=False,
                risk_flags=["recovery_assessment_conflict"],
                recovery_assessment=assessment,
                summary="Monitoring recovery claim lacks consistent evidence; hold this cycle and reassess the next reading.",
            )
    risk_flags = _non_hard_risk_flags(result.risk_flags)
    summary = result.summary
    if not result.is_recovering and (
        result.status != "deviation"
        or not result.is_stable
        or risk_flags != result.risk_flags
    ):
        summary = _stable_deviation_summary(state)
    return result.model_copy(
        update={
            "status": "deviation",
            "is_stable": True,
            "risk_flags": risk_flags,
            "summary": summary,
        }
    )


def _stable_deviation_summary(state: AgenticGraphState) -> str:
    """Return a deterministic summary for a stable unresolved deviation."""
    payload = state["payload"]
    reference_range = state["reference_range"]
    parts: list[str] = []

    if payload.ph < reference_range.ph_target_min:
        parts.append("pH is below target")
    elif payload.ph > reference_range.ph_target_max:
        parts.append("pH is above target")
    else:
        parts.append("pH is within range")

    if payload.ec < reference_range.ec_target_min:
        parts.append("EC is below target")
    elif payload.ec > reference_range.ec_target_max:
        parts.append("EC is above target")
    else:
        parts.append("EC is within range")

    return f"{'; '.join(parts)}; readings are stable and not yet recovering."


def _single_metric_diagnosis_summary(state: AgenticGraphState) -> str:
    """Summarize the current single-metric deviation without dose/action claims."""
    payload = state["payload"]
    reference_range = state["reference_range"]
    baseline = state["baseline"]

    if baseline.decision == "ph_low":
        return (
            f"pH {payload.ph} is below the configured minimum "
            f"{reference_range.ph_target_min}; EC {payload.ec} remains within range."
        )
    if baseline.decision == "ph_high":
        return (
            f"pH {payload.ph} is above the configured maximum "
            f"{reference_range.ph_target_max}; EC {payload.ec} remains within range."
        )
    if baseline.decision == "ec_low":
        return (
            f"EC {payload.ec} is below the configured minimum "
            f"{reference_range.ec_target_min}; pH {payload.ph} remains within range."
        )
    if baseline.decision == "ec_high":
        return (
            f"EC {payload.ec} is above the configured maximum "
            f"{reference_range.ec_target_max}; pH {payload.ph} remains within range."
        )
    return _current_range_summary(state)


def _non_hard_risk_flags(risk_flags: list[str]) -> list[str]:
    """Remove hard safety flags when deterministic checks did not confirm them."""
    hard_flags = {"unstable_reading", "sensor_anomaly", "recent_dose_mixing", "initial_confirmation"}
    return [flag for flag in risk_flags if flag not in hard_flags]


def _ensure_diagnosis_consistency(
    state: AgenticGraphState,
    result: DiagnosticResult,
) -> DiagnosticResult:
    baseline = state["baseline"]
    monitoring = state["monitoring"]
    projected = _active_projected_context(state)

    if monitoring.status == "projected_deviation" and projected is not None:
        return DiagnosticResult(
            classification=str(projected["decision"]),
            primary_metric=str(projected["metric"]),
            summary=str(projected["summary"]),
        )
    if monitoring.status == "projected_deviation":
        return result.model_copy(
            update={
                "primary_metric": result.primary_metric if result.primary_metric != "none" else "ph",
                "summary": monitoring.summary,
            }
        )

    if baseline.pump_activated == "none" or monitoring.status == "in_range":
        return DiagnosticResult(
            classification="within_range",
            primary_metric="none",
            summary=_current_range_summary(state),
        )

    if monitoring.status == "unstable":
        return DiagnosticResult(
            classification="unstable_reading",
            primary_metric="none",
            summary=monitoring.summary,
        )

    if monitoring.status == "sensor_anomaly":
        return DiagnosticResult(
            classification="sensor_anomaly",
            primary_metric="none",
            summary=monitoring.summary,
        )

    if _has_combined_disturbance(state):
        requested_primary = result.primary_metric if result.classification == "combined_disturbance" else None
        selected_primary = _combined_primary_metric(state, requested_primary)
        primary = _combined_control_decision(state, selected_primary)
        return DiagnosticResult(
            classification="combined_disturbance",
            primary_metric=selected_primary,
            summary=(
                "Both pH and EC are outside target range; agentic control will "
                f"correct {primary.decision} first and defer the other correction."
            ),
        )

    if result.classification != baseline.decision:
        return _heuristic_diagnosis(state)

    primary_metric = "ph" if baseline.decision.startswith("ph") else "ec"
    return result.model_copy(
        update={
            "primary_metric": primary_metric,
            "summary": _single_metric_diagnosis_summary(state),
        }
    )


def _ensure_strategy_consistency(
    state: AgenticGraphState,
    result: StrategyResult,
) -> StrategyResult:
    baseline = state["baseline"]
    monitoring = state["monitoring"]
    projected = _active_projected_context(state)

    if monitoring.status == "projected_deviation" and projected is not None:
        reason = result.reason or str(projected["summary"])
        # Preserve Decision's action, but do not let this early projection path
        # bypass the upstream ownership of recovery and mixing facts.
        if _strategy_wait_conflicts_with_context(state, result):
            reason = str(projected["summary"])
            if result.action == "wait":
                reason += " Decision chose to wait for confirmation before preventive correction."
        return result.model_copy(
            update={
                "action": result.action,
                "confidence": min(max(result.confidence, 0.0), 1.0),
                "reason": reason,
            }
        )

    if baseline.pump_activated == "none" or monitoring.status == "in_range":
        return StrategyResult(
            action="no_action",
            confidence=min(max(result.confidence, 0.0), 1.0),
            reason="pH and EC are within target ranges, so no dosing is needed.",
        )

    if monitoring.status in {"unstable", "sensor_anomaly"} and result.action == "dose":
        return StrategyResult(
            action="wait",
            confidence=min(max(result.confidence, 0.0), 1.0),
            reason=monitoring.summary,
        )

    if monitoring.status == "mixing" and result.action == "dose":
        return StrategyResult(
            action="mix_longer",
            confidence=min(max(result.confidence, 0.0), 1.0),
            reason=monitoring.summary,
        )

    if monitoring.is_recovering:
        return StrategyResult(
            action="wait",
            confidence=min(max(result.confidence, 0.0), 1.0),
            reason=monitoring.summary,
        )

    if _strategy_wait_conflicts_with_context(state, result):
        return _heuristic_strategy(state)

    if result.action == "dose" and not _has_recent_dose(_control_history(state)):
        return result.model_copy(update={"reason": _remove_false_recent_dose_claim(result.reason)})

    return result


def _strategy_wait_conflicts_with_context(
    state: AgenticGraphState,
    result: StrategyResult,
) -> bool:
    if result.action not in {"wait", "mix_longer", "no_action"}:
        return False

    monitoring = state["monitoring"]
    baseline = state["baseline"]
    control_history = _control_history(state)
    reason = result.reason.lower()

    if result.action == "no_action" and baseline.pump_activated != "none":
        return True
    if result.action == "mix_longer" and _recent_dose_mixing_reason(control_history) is None:
        return True
    if "natural recovery" in reason and not monitoring.is_recovering:
        return True
    if "recovering" in reason and not monitoring.is_recovering:
        return True
    if "recent dose" in reason and not _has_recent_dose(control_history):
        return True
    if (
        result.action == "wait"
        and result.confidence >= 0.8
        and baseline.pump_activated != "none"
        and monitoring.status == "deviation"
        and monitoring.is_stable
        and not monitoring.is_recovering
        and _recent_dose_mixing_reason(control_history) is None
        and _near_boundary_wait_reason(state) is None
    ):
        return True

    return False


def _ensure_dose_plan_consistency(
    state: AgenticGraphState,
    result: DosePlanResult,
) -> DosePlanResult:
    baseline = state["baseline"]
    monitoring = state["monitoring"]
    control_decision = _agentic_control_decision(state)
    strategy = state["strategy"]
    projected = _active_projected_context(state)

    if (
        baseline.pump_activated == "none"
        and monitoring.status != "projected_deviation"
    ) or strategy.action not in {
        "dose",
        "fallback_baseline",
    }:
        return DosePlanResult(
            pump_activated="none",
            mixing_adjustment_factor=_clamp_mixing_factor(result.mixing_adjustment_factor),
            dose_adjustment_factor=_clamp_dose_factor(result.dose_adjustment_factor),
            reason="No dose is planned because the selected strategy does not require dosing.",
        )

    if result.pump_activated != control_decision.pump_activated:
        aligned_result = DosePlanResult(
            pump_activated=control_decision.pump_activated,
            mixing_adjustment_factor=_clamp_mixing_factor(result.mixing_adjustment_factor),
            dose_adjustment_factor=_clamp_dose_factor(result.dose_adjustment_factor),
            reason=(
                "Dose plan pump was aligned to the agentic primary correction "
                "while preserving one action per cycle."
            ),
        )
        dose_factor = _safe_dose_factor(
            state,
            aligned_result,
            control_decision,
            strategy,
        )
        return aligned_result.model_copy(
            update={
                "reason": _final_dose_reason(
                    state,
                    control_decision,
                    dose_factor,
                    aligned_result.reason,
                )
            }
        )

    dose_factor = _safe_dose_factor(
        state,
        result,
        control_decision,
        strategy,
    )
    return result.model_copy(
        update={
            "dose_adjustment_factor": _clamp_dose_factor(result.dose_adjustment_factor),
            "mixing_adjustment_factor": _clamp_mixing_factor(result.mixing_adjustment_factor),
            "reason": _final_dose_reason(state, control_decision, dose_factor, result.reason),
        }
    )


def _hard_monitoring_status(state: AgenticGraphState) -> MonitoringResult | None:
    payload = state["payload"]
    instability_reason = _reading_instability_reason(payload)
    anomaly_reason = _sensor_anomaly_reason(payload, state["history"])
    mixing_reason = _recent_dose_mixing_reason(_control_history(state))

    if instability_reason:
        return MonitoringResult(
            status="unstable",
            is_stable=False,
            risk_flags=["unstable_reading"],
            summary=instability_reason,
        )

    if anomaly_reason:
        return MonitoringResult(
            status="sensor_anomaly",
            is_stable=True,
            risk_flags=["sensor_anomaly"],
            summary=anomaly_reason,
        )

    if mixing_reason:
        return MonitoringResult(
            status="mixing",
            is_stable=True,
            risk_flags=["recent_dose_mixing"],
            summary=mixing_reason,
        )

    return None


def _current_range_summary(state: AgenticGraphState) -> str:
    payload = state["payload"]
    return (
        f"pH {payload.ph} and EC {payload.ec} are within the configured target ranges."
    )


def _in_range_summary(state: AgenticGraphState) -> str:
    """Summarize an in-range payload with current readings and recent context."""
    payload = state["payload"]
    latest_dose = _latest_dose(_continuity_history(_control_history(state)))
    if latest_dose is not None:
        return (
            f"Current pH {payload.ph} and EC {payload.ec} are within target range "
            f"after the recent {_pump_label(latest_dose.pump_activated or 'dose')} "
            "correction and post-dose remeasurement."
        )
    return f"Current pH {payload.ph} and EC {payload.ec} are within target range."


def _in_range_reason(state: AgenticGraphState) -> str:
    """Return final in-range rationale that is useful in experiment logs."""
    return f"{_in_range_summary(state)} Agentic control continues monitoring."


def _has_combined_disturbance(state: AgenticGraphState) -> bool:
    payload = state["payload"]
    reference_range = state["reference_range"]
    ph_out = payload.ph < reference_range.ph_target_min or payload.ph > reference_range.ph_target_max
    ec_out = payload.ec < reference_range.ec_target_min or payload.ec > reference_range.ec_target_max
    return ph_out and ec_out


def _projected_deviation_context(state: AgenticGraphState) -> dict[str, object] | None:
    """Detect an in-range value trending toward an out-of-range boundary."""
    payload = state["payload"]
    reference_range = state["reference_range"]

    if (
        payload.ph < reference_range.ph_target_min
        or payload.ph > reference_range.ph_target_max
        or payload.ec < reference_range.ec_target_min
        or payload.ec > reference_range.ec_target_max
    ):
        return None

    chronological = list(reversed(_newest_first(_continuity_history(state["history"]))))
    if len(chronological) < PROACTIVE_TREND_HISTORY_POINTS:
        return None
    trend_window = chronological[-PROACTIVE_TREND_HISTORY_POINTS:]

    ph_context = _projected_metric_context(
        metric="ph",
        values=[entry.ph for entry in trend_window] + [payload.ph],
        current_value=payload.ph,
        target_min=reference_range.ph_target_min,
        target_max=reference_range.ph_target_max,
        boundary_margin=PH_PROACTIVE_BOUNDARY_MARGIN,
        stability_threshold=payload.ph_stability_threshold or settings.default_ph_stability_threshold,
    )
    if ph_context is not None:
        return ph_context

    return _projected_metric_context(
        metric="ec",
        values=[entry.ec for entry in trend_window] + [payload.ec],
        current_value=payload.ec,
        target_min=reference_range.ec_target_min,
        target_max=reference_range.ec_target_max,
        boundary_margin=EC_PROACTIVE_BOUNDARY_MARGIN,
        stability_threshold=payload.ec_stability_threshold or settings.default_ec_stability_threshold,
    )


def _active_projected_context(state: AgenticGraphState) -> dict[str, object] | None:
    """Return projection details only after monitoring selected projected_deviation."""
    monitoring = state.get("monitoring")
    if monitoring is None or monitoring.status != "projected_deviation":
        return None
    return _projected_deviation_context(state) or _monitoring_projected_context(state, monitoring)


def _monitoring_projected_context(
    state: AgenticGraphState,
    monitoring: MonitoringResult,
) -> dict[str, object] | None:
    """Return the Monitoring Agent's own projected deviation when it is complete and valid."""
    decision = monitoring.projected_decision
    metric = monitoring.projected_metric
    pump_activated = monitoring.projected_pump_activated
    projected_deviation = monitoring.projected_deviation
    projected_value = monitoring.projected_value
    boundary = monitoring.projected_boundary
    if (
        decision is None
        or metric is None
        or pump_activated is None
        or projected_deviation is None
        or projected_value is None
        or boundary is None
    ):
        return None

    payload = state["payload"]
    reference_range = state["reference_range"]
    if not (
        reference_range.ph_target_min <= payload.ph <= reference_range.ph_target_max
        and reference_range.ec_target_min <= payload.ec <= reference_range.ec_target_max
    ):
        return None
    if not _projected_fields_align(decision, metric, pump_activated):
        return None
    if not _projected_boundary_matches_range(decision, boundary, reference_range):
        return None
    if not _projected_value_crosses_boundary(decision, projected_value, boundary):
        return None
    if projected_deviation <= 0:
        return None

    current_value = payload.ph if metric == "ph" else payload.ec
    direction = "rising" if decision.endswith("_high") else "falling"
    return {
        "metric": metric,
        "direction": direction,
        "decision": decision,
        "pump_activated": pump_activated,
        "current_value": current_value,
        "boundary": boundary,
        "projected_value": projected_value,
        "projected_deviation": round(projected_deviation, 4),
        "last_delta": round(projected_value - current_value, 4),
        "deltas": [],
        "summary": monitoring.summary,
        "source": "monitoring_agent",
    }


def _projected_fields_align(decision: str, metric: str, pump_activated: str) -> bool:
    expected = {
        "ph_low": ("ph", "ph_up"),
        "ph_high": ("ph", "ph_down"),
        "ec_low": ("ec", "ec_up"),
        "ec_high": ("ec", "ec_down"),
    }
    return expected.get(decision) == (metric, pump_activated)


def _projected_boundary_matches_range(
    decision: str,
    boundary: float,
    reference_range: ReferenceRange,
) -> bool:
    expected = {
        "ph_low": reference_range.ph_target_min,
        "ph_high": reference_range.ph_target_max,
        "ec_low": reference_range.ec_target_min,
        "ec_high": reference_range.ec_target_max,
    }
    expected_boundary = expected.get(decision)
    return expected_boundary is not None and abs(boundary - expected_boundary) <= 0.0001


def _projected_value_crosses_boundary(
    decision: str,
    projected_value: float,
    boundary: float,
) -> bool:
    if decision.endswith("_high"):
        return projected_value > boundary
    if decision.endswith("_low"):
        return projected_value < boundary
    return False


def _projected_metric_context(
    metric: str,
    values: list[float],
    current_value: float,
    target_min: float,
    target_max: float,
    boundary_margin: float,
    stability_threshold: float,
) -> dict[str, object] | None:
    if len(values) < PROACTIVE_TREND_HISTORY_POINTS + 1:
        return None

    deltas = [round(following - current, 4) for current, following in zip(values, values[1:])]
    last_delta = deltas[-1]
    minimum_delta = max(stability_threshold, 0.0001)

    if (
        current_value >= target_max - boundary_margin
        and _strictly_increasing(values)
        and last_delta >= minimum_delta
    ):
        projected_value = round(current_value + last_delta, 4)
        if projected_value > target_max:
            decision = f"{metric}_high"
            pump_activated = f"{metric}_down"
            projected_deviation = round(projected_value - target_max, 4)
            summary = (
                f"{metric.upper()} is still inside range at {current_value}, but recent readings "
                f"are rising toward the {target_max} upper limit and project to {projected_value}; "
                "monitoring is escalating this to preventive dosing review."
            )
            return {
                "metric": metric,
                "direction": "rising",
                "decision": decision,
                "pump_activated": pump_activated,
                "current_value": current_value,
                "boundary": target_max,
                "projected_value": projected_value,
                "projected_deviation": projected_deviation,
                "last_delta": last_delta,
                "deltas": deltas,
                "summary": summary,
            }

    if (
        current_value <= target_min + boundary_margin
        and _strictly_decreasing(values)
        and abs(last_delta) >= minimum_delta
    ):
        projected_value = round(current_value + last_delta, 4)
        if projected_value < target_min:
            decision = f"{metric}_low"
            pump_activated = f"{metric}_up"
            projected_deviation = round(target_min - projected_value, 4)
            summary = (
                f"{metric.upper()} is still inside range at {current_value}, but recent readings "
                f"are falling toward the {target_min} lower limit and project to {projected_value}; "
                "monitoring is escalating this to preventive dosing review."
            )
            return {
                "metric": metric,
                "direction": "falling",
                "decision": decision,
                "pump_activated": pump_activated,
                "current_value": current_value,
                "boundary": target_min,
                "projected_value": projected_value,
                "projected_deviation": projected_deviation,
                "last_delta": last_delta,
                "deltas": deltas,
                "summary": summary,
            }

    return None


def _projected_monitoring_result(projected: dict[str, object]) -> MonitoringResult:
    return MonitoringResult(
        status="projected_deviation",
        is_stable=True,
        is_recovering=False,
        risk_flags=["projected_out_of_range"],
        summary=str(projected["summary"]),
    )


def _agentic_control_decision(state: AgenticGraphState) -> DosingDecision:
    """Return the single correction agentic AI should execute this cycle."""
    baseline = state["baseline"]
    if baseline.pump_activated == "none":
        projected = _active_projected_context(state)
        if projected is not None:
            return _proactive_control_decision(state, projected)
    if not _has_combined_disturbance(state):
        return _single_disturbance_control_decision(state)

    diagnosis = state.get("diagnosis")
    requested_primary = (
        diagnosis.primary_metric
        if diagnosis is not None and diagnosis.classification == "combined_disturbance"
        else None
    )
    return _combined_control_decision(state, _combined_primary_metric(state, requested_primary))


def _combined_primary_metric(state: AgenticGraphState, requested_primary: str | None = None) -> str:
    """Return the validated primary metric for a mixed pH/EC disturbance."""
    if requested_primary in {"ph", "ec"}:
        return requested_primary
    # Preserve a local fallback priority when no valid agent choice is available.
    return "ph" if state["baseline"].ph_deviation >= 0.5 else "ec"


def _combined_control_decision(state: AgenticGraphState, primary_metric: str) -> DosingDecision:
    """Build the one allowed correction for a combined pH/EC disturbance."""
    payload = state["payload"]
    reference_range = state["reference_range"]

    if primary_metric == "ph":
        reason = (
            "Combined pH/EC disturbance detected; pH is the selected primary correction, "
            "while EC is deferred to the next cycle."
        )
        if payload.ph < reference_range.ph_target_min:
            return _single_metric_decision(
                state,
                decision="ph_low",
                pump_activated="ph_up",
                dose_ml_per_liter_per_unit=reference_range.ph_up_dose_ml_per_liter_per_unit,
                reason=reason,
            )
        return _single_metric_decision(
            state,
            decision="ph_high",
            pump_activated="ph_down",
            dose_ml_per_liter_per_unit=reference_range.ph_down_dose_ml_per_liter_per_unit,
            reason=reason,
        )

    if payload.ec < reference_range.ec_target_min:
        return _single_metric_decision(
            state,
            decision="ec_low",
            pump_activated="ec_up",
            dose_ml_per_liter_per_unit=reference_range.ec_up_dose_ml_per_liter_per_unit,
            reason=(
                "Combined pH/EC disturbance detected; EC is corrected first because "
                "Diagnostic Agent selected EC as the primary correction and nutrient "
                "changes can shift pH during mixing."
            ),
        )

    return _single_metric_decision(
        state,
        decision="ec_high",
        pump_activated="ec_down",
        dose_ml_per_liter_per_unit=reference_range.ec_down_dose_ml_per_liter_per_unit,
        reason=(
            "Combined pH/EC disturbance detected; EC is corrected first because "
            "Diagnostic Agent selected EC as the primary correction and dilution "
            "can shift pH during mixing."
        ),
    )


def _single_disturbance_control_decision(state: AgenticGraphState) -> DosingDecision:
    """Return an agentic one-metric correction aimed at its range midpoint."""
    baseline = state["baseline"]
    reference_range = state["reference_range"]
    if baseline.decision == "ph_low":
        return _single_metric_decision(
            state,
            decision="ph_low",
            pump_activated="ph_up",
            dose_ml_per_liter_per_unit=reference_range.ph_up_dose_ml_per_liter_per_unit,
            reason="Confirmed pH-low deviation; agentic correction targets the bounded pH midpoint.",
        )
    if baseline.decision == "ph_high":
        return _single_metric_decision(
            state,
            decision="ph_high",
            pump_activated="ph_down",
            dose_ml_per_liter_per_unit=reference_range.ph_down_dose_ml_per_liter_per_unit,
            reason="Confirmed pH-high deviation; agentic correction targets the bounded pH midpoint.",
        )
    if baseline.decision == "ec_low":
        return _single_metric_decision(
            state,
            decision="ec_low",
            pump_activated="ec_up",
            dose_ml_per_liter_per_unit=reference_range.ec_up_dose_ml_per_liter_per_unit,
            reason="Confirmed EC-low deviation; agentic correction targets the bounded EC midpoint.",
        )
    if baseline.decision == "ec_high":
        return _single_metric_decision(
            state,
            decision="ec_high",
            pump_activated="ec_down",
            dose_ml_per_liter_per_unit=reference_range.ec_down_dose_ml_per_liter_per_unit,
            reason="Confirmed EC-high deviation; agentic correction targets the bounded EC midpoint.",
        )
    return baseline


def _proactive_control_decision(
    state: AgenticGraphState,
    projected: dict[str, object],
) -> DosingDecision:
    """Return a small preventive correction based on projected boundary crossing."""
    baseline = state["baseline"]
    reference_range = state["reference_range"]
    dose_details = _proactive_dose_details(state, projected)
    dose_ml, duration_ms = calculate_bounded_dose_and_duration_ms(
        dose_details["dose_ml"],
        dose_details["flow_ml_per_min"],
        max_duration_for_pump(reference_range, str(projected["pump_activated"])),
    )
    decision = str(projected["decision"])
    metric = str(projected["metric"])
    return baseline.model_copy(
        update={
            "decision": decision,
            "pump_activated": str(projected["pump_activated"]),
            "dose_ml": dose_ml,
            "duration_ms": duration_ms,
            "reason": (
                f"Projected {metric.upper()} trend may cross the target boundary before the "
                "next confirmed cycle; agentic control selected a bounded preventive correction."
            ),
            "metadata": {
                **baseline.metadata,
                "agentic_target_policy": "projected_boundary_prevention",
                "projected_deviation": projected,
                "agentic_dose_deviation": projected["projected_deviation"],
                "agentic_target_value": projected["boundary"],
                "agentic_target_range_headroom_factor": TARGET_REENTRY_AGENTIC_DOSE_FACTOR,
            },
        }
    )


def _proactive_dose_details(
    state: AgenticGraphState,
    projected: dict[str, object],
) -> dict[str, float]:
    reference_range = state["reference_range"]
    payload = state["payload"]
    pump_activated = str(projected["pump_activated"])
    if pump_activated == "ph_up":
        dose_ml_per_liter_per_unit = reference_range.ph_up_dose_ml_per_liter_per_unit
        flow_ml_per_min = reference_range.ph_pump_flow_ml_per_min
    elif pump_activated == "ph_down":
        dose_ml_per_liter_per_unit = reference_range.ph_down_dose_ml_per_liter_per_unit
        flow_ml_per_min = reference_range.ph_pump_flow_ml_per_min
    elif pump_activated == "ec_up":
        dose_ml_per_liter_per_unit = reference_range.ec_up_dose_ml_per_liter_per_unit
        flow_ml_per_min = reference_range.ec_pump_flow_ml_per_min
    else:
        dose_ml_per_liter_per_unit = reference_range.ec_down_dose_ml_per_liter_per_unit
        flow_ml_per_min = reference_range.ec_pump_flow_ml_per_min

    dose_ml = round(
        min(
            float(projected["projected_deviation"])
            * payload.reservoir_volume_liters
            * dose_ml_per_liter_per_unit,
            effective_max_dose_for_pump(reference_range, pump_activated),
        ),
        2,
    )
    return {
        "dose_ml": dose_ml,
        "flow_ml_per_min": flow_ml_per_min,
    }


def _single_metric_decision(
    state: AgenticGraphState,
    decision: str,
    pump_activated: str,
    dose_ml_per_liter_per_unit: float,
    reason: str,
) -> DosingDecision:
    baseline = state["baseline"]
    payload = state["payload"]
    reference_range = state["reference_range"]
    boundary_deviation = baseline.ph_deviation if decision.startswith("ph") else baseline.ec_deviation
    target_value = _agentic_target_value(reference_range, decision)
    dose_deviation = _agentic_target_deviation(payload, decision, target_value)
    target_margin = max(dose_deviation - boundary_deviation, 0)
    range_headroom_factor = _target_range_headroom_factor(
        payload,
        reference_range,
        decision,
        dose_deviation,
    )
    dose_ml = round(
        min(
            dose_deviation * payload.reservoir_volume_liters * dose_ml_per_liter_per_unit,
            effective_max_dose_for_pump(reference_range, pump_activated),
        ),
        2,
    )
    dose_ml, duration_ms, actuation_metadata = calculate_safe_corrective_actuation(
        dose_ml,
        payload,
        reference_range,
        decision,
        pump_activated,
    )
    return baseline.model_copy(
        update={
            "decision": decision,
            "pump_activated": pump_activated,
            "dose_ml": dose_ml,
            "duration_ms": duration_ms,
            "reason": reason,
            "metadata": {
                **baseline.metadata,
                "agentic_target_value": target_value,
                "agentic_target_policy": "midpoint_single_correction_capped",
                "agentic_target_inward_margin": round(target_margin, 4),
                "agentic_dose_deviation": round(dose_deviation, 4),
                "agentic_target_range_headroom_factor": range_headroom_factor,
                **actuation_metadata,
            },
        }
    )


def _agentic_target_value(reference_range: ReferenceRange, decision: str) -> float:
    """Return the configured midpoint for pH or EC."""
    return reentry_target_value(reference_range, decision)


def _agentic_target_deviation(
    payload: SensorPayload,
    decision: str,
    target_value: float,
) -> float:
    """Return calibrated units needed to move the disturbed metric to target."""
    if decision.startswith("ph"):
        return round(abs(target_value - payload.ph), 4)
    if decision.startswith("ec"):
        return round(abs(target_value - payload.ec), 4)
    return 0.0


def _target_range_headroom_factor(
    payload: SensorPayload,
    reference_range: ReferenceRange,
    decision: str,
    dose_deviation: float,
) -> float:
    """Bound factor growth so the calibrated correction stays inside target range."""
    if dose_deviation <= 0:
        return TARGET_REENTRY_AGENTIC_DOSE_FACTOR
    if decision == "ph_low":
        headroom_deviation = reference_range.ph_target_max - payload.ph
    elif decision == "ph_high":
        headroom_deviation = payload.ph - reference_range.ph_target_min
    elif decision == "ec_low":
        headroom_deviation = reference_range.ec_target_max - payload.ec
    elif decision == "ec_high":
        headroom_deviation = payload.ec - reference_range.ec_target_min
    else:
        return TARGET_REENTRY_AGENTIC_DOSE_FACTOR
    return round(
        min(
            MAX_AGENTIC_DOSE_FACTOR,
            max(TARGET_REENTRY_AGENTIC_DOSE_FACTOR, headroom_deviation / dose_deviation),
        ),
        4,
    )


def _clamp_dose_factor(value: float) -> float:
    return min(max(value, MIN_AGENTIC_DOSE_FACTOR), MAX_AGENTIC_DOSE_FACTOR)


def _clamp_mixing_factor(value: float) -> float:
    return min(max(value, MIN_MIXING_ADJUSTMENT_FACTOR), MAX_MIXING_ADJUSTMENT_FACTOR)


def _heuristic_monitoring(state: AgenticGraphState) -> MonitoringResult:
    baseline = state["baseline"]
    instability_reason = _reading_instability_reason(state["payload"])
    anomaly_reason = _sensor_anomaly_reason(state["payload"], state["history"])
    mixing_reason = _recent_dose_mixing_reason(_control_history(state))
    recovery_reason = _natural_recovery_reason(
        state["payload"], baseline, state["history"],
        reference_range=state["reference_range"], control_history=_control_history(state),
    )
    projected = _projected_deviation_context(state)

    if baseline.pump_activated == "none" and projected is not None:
        return _projected_monitoring_result(projected)
    near_boundary_reason = _near_boundary_wait_reason(state)
    if near_boundary_reason:
        return MonitoringResult(
            status="deviation",
            is_stable=True,
            risk_flags=["near_boundary"],
            summary=near_boundary_reason,
        )
    if baseline.pump_activated == "none":
        return MonitoringResult(
            status="in_range",
            is_stable=True,
            summary="pH and EC are within target range.",
        )
    if instability_reason:
        return MonitoringResult(
            status="unstable",
            is_stable=False,
            risk_flags=["unstable_reading"],
            summary=instability_reason,
        )
    if anomaly_reason:
        return MonitoringResult(
            status="sensor_anomaly",
            is_stable=True,
            risk_flags=["sensor_anomaly"],
            summary=anomaly_reason,
        )
    if mixing_reason:
        return MonitoringResult(
            status="mixing",
            is_stable=True,
            risk_flags=["recent_dose_mixing"],
            summary=mixing_reason,
        )

    return MonitoringResult(
        status="deviation",
        is_stable=True,
        is_recovering=bool(recovery_reason),
        summary=recovery_reason or "A stable deviation is present.",
    )


def _heuristic_diagnosis(state: AgenticGraphState) -> DiagnosticResult:
    baseline = state["baseline"]
    monitoring = state["monitoring"]
    projected = _active_projected_context(state)

    if monitoring.status == "unstable":
        classification = "unstable_reading"
    elif monitoring.status == "sensor_anomaly":
        classification = "sensor_anomaly"
    elif projected is not None:
        classification = str(projected["decision"])
    elif baseline.pump_activated == "none":
        classification = "within_range"
    elif _has_combined_disturbance(state):
        classification = "combined_disturbance"
    else:
        classification = baseline.decision

    primary_decision = _agentic_control_decision(state)
    primary_metric = (
        "ph"
        if primary_decision.decision.startswith("ph")
        else "ec"
        if primary_decision.decision.startswith("ec")
        else "none"
    )
    return DiagnosticResult(
        classification=classification,
        primary_metric=primary_metric,
        summary=f"Primary classification is {classification}.",
    )


def _diagnosis_for_monitoring(
    state: AgenticGraphState,
    monitoring: MonitoringResult,
) -> DiagnosticResult:
    """Create a deterministic diagnosis for an orchestration-level terminal route."""
    baseline = state["baseline"]

    if monitoring.status == "projected_deviation":
        projected = _active_projected_context(state)
        classification = str(projected["decision"]) if projected is not None else baseline.decision
    elif monitoring.status == "in_range":
        classification = "within_range"
    elif monitoring.status == "unstable":
        classification = "unstable_reading"
    elif monitoring.status == "sensor_anomaly":
        classification = "sensor_anomaly"
    elif baseline.decision == "wait_near_boundary":
        payload = state["payload"]
        reference_range = state["reference_range"]
        if baseline.ph_deviation > 0:
            classification = "ph_low" if payload.ph < reference_range.ph_target_min else "ph_high"
        elif baseline.ec_deviation > 0:
            classification = "ec_low" if payload.ec < reference_range.ec_target_min else "ec_high"
        else:
            classification = "within_range"
    elif monitoring.is_recovering:
        classification = "combined_disturbance" if _has_combined_disturbance(state) else baseline.decision
    elif monitoring.status == "mixing":
        classification = baseline.decision
    else:
        classification = baseline.decision

    primary_decision = baseline.decision if classification == "combined_disturbance" else classification
    primary_metric = "ph" if primary_decision.startswith("ph") else "ec" if primary_decision.startswith("ec") else "none"
    return DiagnosticResult(
        classification=classification,
        primary_metric=primary_metric,
        summary=monitoring.summary,
    )


def _crop_dosing_policy(state: AgenticGraphState) -> dict[str, object]:
    """Return conservative dosing constraints derived from crop age/stage."""
    lifecycle = state.get("crop_lifecycle") or {}
    stage = str(lifecycle.get("stage") or "not_configured")
    stage_label = str(lifecycle.get("stage_label") or "Not configured")
    age_days = lifecycle.get("age_days")
    base_policy: dict[str, object] = {
        "stage": stage,
        "stage_label": stage_label,
        "age_days": age_days if isinstance(age_days, int) else None,
        "max_dose_factor": MAX_AGENTIC_DOSE_FACTOR,
        "max_corrective_dose_factor": MAX_AGENTIC_DOSE_FACTOR,
        "min_mixing_factor": DEFAULT_MIXING_ADJUSTMENT_FACTOR,
        "preventive_dosing_allowed": True,
        "prefer_confirmation": False,
        "reason": "Crop lifecycle does not require additional dosing conservatism.",
    }

    if stage in {"transplant", "establishment"}:
        return {
            **base_policy,
            "max_dose_factor": 0.65,
            "max_corrective_dose_factor": 0.65,
            "min_mixing_factor": 1.25,
            "preventive_dosing_allowed": False,
            "reason": (
                "Crop is in the transplant/establishment period; avoid preventive dosing "
                "and use smaller bounded corrections with longer mixing."
            ),
        }

    if stage == "sizing":
        return {
            **base_policy,
            "max_dose_factor": 0.9,
            "max_corrective_dose_factor": 1.25,
            "min_mixing_factor": 1.1,
            "reason": (
                "Crop is approaching harvest size; keep preventive dosing conservative, "
                "allow bounded response-aware correction, and verify after mixing."
            ),
        }

    if stage == "harvest_window":
        return {
            **base_policy,
            "max_dose_factor": 0.75,
            "max_corrective_dose_factor": 0.75,
            "min_mixing_factor": 1.2,
            "preventive_dosing_allowed": False,
            "reason": (
                "Crop is inside the harvest window; avoid preventive dosing and keep "
                "required corrections conservative."
            ),
        }

    if stage == "overdue":
        return {
            **base_policy,
            "max_dose_factor": 0.65,
            "max_corrective_dose_factor": 0.65,
            "min_mixing_factor": 1.25,
            "preventive_dosing_allowed": False,
            "reason": (
                "Crop is past the expected harvest window; avoid preventive dosing and "
                "limit corrections while the operator checks crop quality."
            ),
        }

    return base_policy


def _crop_max_dose_factor(state: AgenticGraphState) -> float:
    """Keep preventive dosing conservative without weakening required correction."""
    policy = _crop_dosing_policy(state)
    if _active_projected_context(state) is not None:
        return float(policy["max_dose_factor"])
    return float(policy.get("max_corrective_dose_factor", policy["max_dose_factor"]))


def _heuristic_strategy(state: AgenticGraphState) -> StrategyResult:
    baseline = state["baseline"]
    monitoring = state["monitoring"]
    projected = _active_projected_context(state)
    crop_policy = _crop_dosing_policy(state)

    if baseline.pump_activated == "none" and projected is not None:
        if crop_policy["preventive_dosing_allowed"] is False:
            return StrategyResult(
                action="wait",
                confidence=0.82,
                reason=(
                    f"{projected['summary']} Crop stage is {crop_policy['stage_label']}; "
                    "hold preventive dosing and recheck unless the reading leaves the target range."
                ),
            )
        return StrategyResult(
            action="dose",
            confidence=0.78,
            reason=str(projected["summary"]),
        )
    if baseline.pump_activated == "none":
        return StrategyResult(action="no_action", confidence=0.95, reason=monitoring.summary)
    if monitoring.status in {"unstable", "sensor_anomaly"}:
        return StrategyResult(action="wait", confidence=0.9, reason=monitoring.summary)
    if monitoring.status == "mixing":
        return StrategyResult(action="mix_longer", confidence=0.88, reason=monitoring.summary)
    if monitoring.is_recovering:
        return StrategyResult(action="wait", confidence=0.78, reason=monitoring.summary)
    if crop_policy["prefer_confirmation"] is True and monitoring.status == "deviation":
        return StrategyResult(
            action="wait",
            confidence=0.76,
            reason=(
                f"{monitoring.summary} Crop stage is {crop_policy['stage_label']}; "
                "wait for the next stable confirmation before dosing unless deviation worsens."
            ),
        )
    return StrategyResult(
        action="dose",
        confidence=0.82,
        reason=(
            "Stable deviation remains after monitoring and diagnostic checks; "
            "dose planning should select a bounded adaptive correction."
        ),
    )


def _heuristic_dose_plan(state: AgenticGraphState) -> DosePlanResult:
    control_decision = _agentic_control_decision(state)
    if state["strategy"].action not in {"dose", "fallback_baseline"}:
        return DosePlanResult(
            pump_activated="none",
            mixing_adjustment_factor=DEFAULT_MIXING_ADJUSTMENT_FACTOR,
            dose_adjustment_factor=DEFAULT_AGENTIC_DOSE_FACTOR,
            reason="No dose planned because the selected strategy is not dosing.",
        )

    dose_factor = _adaptive_dose_factor(
        control_decision,
        _control_history(state),
        state["strategy"].confidence,
    )
    crop_policy = _crop_dosing_policy(state)
    dose_factor = min(dose_factor, _crop_max_dose_factor(state))
    mixing_factor = _adaptive_mixing_factor(control_decision, _control_history(state), dose_factor)
    mixing_factor = max(mixing_factor, float(crop_policy["min_mixing_factor"]))
    return DosePlanResult(
        pump_activated=control_decision.pump_activated,
        mixing_adjustment_factor=mixing_factor,
        dose_adjustment_factor=dose_factor,
        reason=_heuristic_dose_plan_reason(state, dose_factor, control_decision),
    )


def _heuristic_dose_plan_reason(
    state: AgenticGraphState,
    dose_factor: float,
    control_decision: DosingDecision | None = None,
) -> str:
    control_decision = control_decision or _agentic_control_decision(state)
    projected = _active_projected_context(state)
    crop_clause = _crop_policy_reason_clause(state)
    if projected is not None:
        return (
            f"Use the bounded {_pump_label(control_decision.pump_activated)} preventive correction "
            "because monitoring projects the current in-range trend will cross the target boundary; "
            f"{crop_clause}remeasure after mixing before any follow-up action."
        )
    if _has_combined_disturbance(state):
        factor_clause = "Use the bounded"
        if dose_factor != 1.0:
            factor_clause = f"Use dose factor {dose_factor} for a bounded"
        return (
            f"{control_decision.reason} {factor_clause} {_pump_label(control_decision.pump_activated)} "
            f"{_correction_target_label(control_decision.pump_activated)} this cycle, "
            f"defer the other out-of-range metric, {crop_clause}and remeasure "
            "after mixing."
        )
    same_pump_response = _same_pump_response_context(state, control_decision)
    pump_label = _pump_label(control_decision.pump_activated)
    correction_target = _correction_target_label(control_decision.pump_activated)
    if same_pump_response.get("reason") == "No recent same-pump dose exists inside the fresh history window.":
        if dose_factor < 1.0:
            return (
                f"Use reduced dose factor {dose_factor} for a cautious {pump_label} "
                f"{correction_target} because no recent same-pump response is available "
                f"in the fresh agentic history; {crop_clause}remeasure after mixing."
            )
        return (
            f"Use the bounded {pump_label} {correction_target} because no recent same-pump "
            f"response is available in the fresh agentic history; {crop_clause}remeasure after mixing."
        )
    if same_pump_response.get("interpretation") == "previous_same_pump_still_unresolved":
        if same_pump_response.get("latest_same_pump_hit_pump_cap") is True:
            return (
                f"Use the bounded {pump_label} {correction_target} follow-up because the previous "
                "same-pump correction hit its cycle cap and remains unresolved; "
                "remeasure after mixing."
            )
        return (
            f"Use dose factor {dose_factor} because the previous {pump_label} "
            "correction remains unresolved at the current reading; remeasure "
            "after mixing."
        )
    if same_pump_response.get("interpretation") == "previous_same_pump_overshot_opposite_direction":
        return (
            f"Use a reduced adaptive {pump_label} correction because recent same-pump "
            "history indicates overshoot risk; remeasure after extended mixing."
        )
    if same_pump_response.get("interpretation") == "previous_same_pump_resolved_or_changed_condition":
        if dose_factor < 1.0:
            return (
                f"Use reduced dose factor {dose_factor} for a cautious new {pump_label} "
                "correction because the previous same-pump correction returned to range "
                "or the condition changed; remeasure after mixing."
            )
        return (
            f"Use the bounded {pump_label} {correction_target} because the previous "
            "same-pump correction returned to range or the condition changed; "
            "treat this as a new confirmed disturbance and remeasure after mixing."
        )
    return (
        "Dose planning agent selected an adaptive correction after reviewing "
        f"stability, confidence, recent response history, and crop lifecycle. {crop_clause}".strip()
    )


def _correction_target_label(pump_activated: str) -> str:
    """Describe the shared supervisory target for pH and EC corrections."""
    return "midpoint-directed correction"


def _crop_policy_reason_clause(state: AgenticGraphState) -> str:
    policy = _crop_dosing_policy(state)
    if float(policy["max_dose_factor"]) >= MAX_AGENTIC_DOSE_FACTOR:
        return ""
    return f"crop stage {policy['stage_label']} caps the correction factor; "


def _final_dose_reason(
    state: AgenticGraphState,
    control_decision: DosingDecision,
    dose_factor: float,
    proposed_reason: str,
) -> str:
    """Return a persisted dose reason that cannot contradict deterministic context."""
    same_pump_response = _same_pump_response_context(state, control_decision)
    crop_policy = _crop_dosing_policy(state)
    if float(crop_policy["max_dose_factor"]) < MAX_AGENTIC_DOSE_FACTOR:
        if str(crop_policy["stage_label"]).lower() not in proposed_reason.lower():
            return _heuristic_dose_plan_reason(state, dose_factor, control_decision)
    if _has_combined_disturbance(state) and not _reason_claims_combined_disturbance(
        proposed_reason,
        control_decision,
        dose_factor,
    ):
        return _heuristic_dose_plan_reason(state, dose_factor, control_decision)
    if _reason_claims_severe_deviation(proposed_reason) and not _decision_has_severe_deviation(
        control_decision
    ):
        return _heuristic_dose_plan_reason(state, dose_factor, control_decision)
    if dose_factor < 1.0 and _reason_claims_full_midpoint_correction(proposed_reason):
        return _heuristic_dose_plan_reason(state, dose_factor, control_decision)
    if same_pump_response.get("reason") == "No recent same-pump dose exists inside the fresh history window.":
        if _reason_is_pump_alignment_only(proposed_reason):
            return _heuristic_dose_plan_reason(state, dose_factor, control_decision)
        if _reason_claims_same_pump_history(proposed_reason):
            return _heuristic_dose_plan_reason(state, dose_factor, control_decision)
        return proposed_reason
    if same_pump_response.get("interpretation") == "previous_same_pump_still_unresolved":
        if _reason_claims_no_same_pump_history(proposed_reason) or _reason_claims_resolved_history(
            proposed_reason
        ):
            return _heuristic_dose_plan_reason(state, dose_factor, control_decision)
        return proposed_reason
    if same_pump_response.get("interpretation") == "previous_same_pump_resolved_or_changed_condition":
        if _reason_claims_no_same_pump_history(proposed_reason) or _reason_claims_unresolved_history(
            proposed_reason
        ):
            return _heuristic_dose_plan_reason(state, dose_factor, control_decision)
        return proposed_reason
    if same_pump_response.get("interpretation") == "previous_same_pump_overshot_opposite_direction":
        return _heuristic_dose_plan_reason(state, dose_factor, control_decision)
    if same_pump_response.get("current_baseline_hits_pump_cap") is True:
        return _heuristic_dose_plan_reason(state, dose_factor, control_decision)
    return proposed_reason


def _reason_claims_no_same_pump_history(reason: str) -> bool:
    reason = reason.lower()
    return "no recent same-pump" in reason or "no same-pump" in reason


def _reason_claims_unresolved_history(reason: str) -> bool:
    reason = reason.lower()
    return "unresolved" in reason or "undershot" in reason


def _reason_claims_resolved_history(reason: str) -> bool:
    reason = reason.lower()
    return "returned to range" in reason or ("resolved" in reason and "unresolved" not in reason)


def _reason_claims_same_pump_history(reason: str) -> bool:
    return (
        _reason_claims_unresolved_history(reason)
        or _reason_claims_resolved_history(reason)
        or "previous same-pump" in reason.lower()
    )


def _reason_is_pump_alignment_only(reason: str) -> bool:
    return "pump was aligned to the agentic primary correction" in reason.lower()


def _reason_claims_full_midpoint_correction(reason: str) -> bool:
    lowered = reason.lower()
    claims_midpoint = "midpoint correction" in lowered or "midpoint-directed correction" in lowered
    return "bounded" in lowered and claims_midpoint and "reduced" not in lowered


def _reason_claims_combined_disturbance(
    reason: str,
    control_decision: DosingDecision,
    dose_factor: float,
) -> bool:
    lowered = reason.lower()
    pump_label = _pump_label(control_decision.pump_activated).lower()
    decision_label = control_decision.decision.replace("_", "-").lower()
    if dose_factor != 1.0 and str(dose_factor) not in lowered:
        return False
    return (
        ("defer" in lowered or "next cycle" in lowered or "after mixing" in lowered)
        and (pump_label in lowered or decision_label in lowered)
    )


def _reason_claims_severe_deviation(reason: str) -> bool:
    return "severe" in reason.lower()


def _decision_has_severe_deviation(control_decision: DosingDecision) -> bool:
    boundary_deviation = control_decision.ph_deviation
    if control_decision.decision.startswith("ec"):
        boundary_deviation = control_decision.ec_deviation
    return boundary_deviation >= 0.5


def _pump_label(pump_activated: str) -> str:
    labels = {
        "ph_up": "pH-up",
        "ph_down": "pH-down",
        "ec_up": "EC-up",
        "ec_down": "EC-down",
    }
    return labels.get(pump_activated, pump_activated)


def _base_metadata(state: AgenticGraphState) -> dict[str, object]:
    baseline = state["baseline"]
    agentic_primary = _agentic_control_decision(state)
    tool_results = _tool_results_context(state, agentic_primary)
    reasoning_source = _reasoning_source(state)
    orchestrator = _initial_orchestrator_metadata(state)
    return {
        "strategy": AGENTIC_STRATEGY,
        "requested_strategy": AGENTIC_STRATEGY,
        "executed_strategy": AGENTIC_STRATEGY,
        "agentic_mode": "per_reading_graph",
        "actuation_source": "agentic_graph",
        "pipeline_supervision_model": "orchestrator_managed_specialist_feedback_loop",
        "orchestration": "langgraph",
        "orchestrator_agent": state.get("orchestration", {}),
        "initial_orchestrator_agent": orchestrator,
        "reasoning_source": reasoning_source,
        "llm_enabled": reasoning_source in {"llm_agents", "mixed_llm_deterministic_fallback"},
        "llm_configured": bool(settings.agentic_ai_use_llm and settings.openai_api_key),
        "llm_primary_model": settings.agentic_ai_model,
        "llm_fallback_model": settings.agentic_ai_fallback_model,
        "llm_timeout_seconds": settings.agentic_ai_llm_timeout_seconds,
        "llm_monitoring_model": settings.agentic_ai_monitoring_model,
        "llm_diagnostic_model": settings.agentic_ai_diagnostic_model,
        "llm_dose_planning_model": settings.agentic_ai_dose_planning_model,
        "llm_models_used": _llm_models_used(state),
        "safety_gate_source": "deterministic_python",
        "baseline_shadow": {
            "decision": baseline.decision,
            "pump_activated": baseline.pump_activated,
            "dose_ml": baseline.dose_ml,
            "duration_ms": baseline.duration_ms,
            "reason": baseline.reason,
            "target_policy": baseline.metadata.get("target_policy"),
        },
        "agentic_primary_shadow": {
            "decision": agentic_primary.decision,
            "pump_activated": agentic_primary.pump_activated,
            "dose_ml": agentic_primary.dose_ml,
            "duration_ms": agentic_primary.duration_ms,
            "reason": agentic_primary.reason,
        },
        "agentic_tool_results": tool_results,
        "combined_disturbance": _has_combined_disturbance(state),
        "projected_deviation": _active_projected_context(state),
        "crop_lifecycle": state.get("crop_lifecycle") or {},
        "crop_dosing_policy": _crop_dosing_policy(state),
        "monitoring_agent": state["monitoring"].model_dump(),
        "diagnostic_reasoning_agent": state["diagnosis"].model_dump(),
        "decision_agent": state["strategy"].model_dump(),
        "dose_planning_agent": state["dose_plan"].model_dump(),
        "consistency_review": state.get("consistency_review") or _consistency_review(state),
        "agent_communication": _agent_communication_context(state),
        "same_pump_response": _same_pump_response_context(state),
        "llm_trace": state.get("llm_trace", []),
        "recent_log_count": len(state["history"]),
        "recent_control_log_count": len(_control_history(state)),
    }


def _initial_orchestrator_metadata(state: AgenticGraphState) -> dict[str, object]:
    """Return the intake route before later orchestrator feedback routing."""
    orchestration = dict(state.get("orchestration") or {})
    initial_route = str(orchestration.get("initial_route") or orchestration.get("route") or "monitoring_agent")
    route_history = list(orchestration.get("route_history") or [])
    initial_step = route_history[0] if route_history else {}
    reason = str(initial_step.get("reason") or orchestration.get("routing_reason") or orchestration.get("reason") or "")
    source = orchestration.get("initial_source") or orchestration.get("source") or DETERMINISTIC_FALLBACK_SOURCE
    model = orchestration.get("initial_model") or orchestration.get("model")
    skipped_agents = (
        list(orchestration.get("skipped_agents", []))
        if initial_route == "safety_gate"
        else []
    )
    return {
        "agent": "orchestrator_agent",
        "route": initial_route,
        "initial_route": initial_route,
        "action": orchestration.get("action", "proceed"),
        "confidence": orchestration.get("confidence"),
        "reason": reason,
        "routing_reason": reason,
        "source": source,
        "model": model,
        "consulted_agent": "monitoring_agent" if initial_route == "monitoring_agent" else None,
        "monitoring_precheck": orchestration.get("monitoring_precheck"),
        "skipped_agents": skipped_agents,
        "route_history": route_history[:1],
    }


def _hard_safety_block_reason(state: AgenticGraphState) -> str | None:
    payload = state["payload"]
    return (
        _reading_instability_reason(payload)
        or _sensor_anomaly_reason(payload, state["history"])
        or _recent_dose_mixing_reason(_control_history(state))
    )


def _consistency_review(state: AgenticGraphState) -> dict[str, object]:
    """Return a structured pre-safety review of agent output consistency."""
    payload = state["payload"]
    reference_range = state["reference_range"]
    baseline = state["baseline"]
    monitoring = state["monitoring"]
    diagnosis = state["diagnosis"]
    strategy = state["strategy"]
    dose_plan = state["dose_plan"]
    projected = _active_projected_context(state)
    crop_policy = _crop_dosing_policy(state)
    expected_classification = (
        str(projected["decision"])
        if projected is not None
        else "wait_near_boundary"
        if baseline.decision == "wait_near_boundary"
        else _expected_classification_from_payload(payload, reference_range)
    )
    hard_block_reason = _hard_safety_block_reason(state)
    issues: list[str] = []

    if monitoring.status in {"unstable", "sensor_anomaly", "mixing", "confirming"} or hard_block_reason:
        if strategy.action == "dose":
            issues.append("decision_doses_despite_safety_review_status")
        if dose_plan.pump_activated != "none":
            issues.append("dose_plan_selects_pump_despite_safety_review_status")
    elif projected is not None:
        if monitoring.status != "projected_deviation":
            issues.append("monitoring_status_misses_projected_deviation")
        if diagnosis.classification != expected_classification:
            issues.append("diagnosis_conflicts_with_projected_deviation")
        if crop_policy["preventive_dosing_allowed"] is False and strategy.action in {"dose", "fallback_baseline"}:
            issues.append("crop_lifecycle_blocks_preventive_dosing")
        if strategy.action not in {"dose", "fallback_baseline"}:
            if crop_policy["preventive_dosing_allowed"] is not False:
                issues.append("decision_misses_projected_deviation")
        if dose_plan.pump_activated != projected["pump_activated"]:
            if crop_policy["preventive_dosing_allowed"] is not False:
                issues.append("dose_plan_pump_conflicts_with_projected_deviation")
    elif expected_classification == "within_range":
        if monitoring.status != "in_range":
            issues.append("monitoring_status_conflicts_with_in_range_payload")
        if diagnosis.classification != "within_range":
            issues.append("diagnosis_conflicts_with_in_range_payload")
        if strategy.action == "dose":
            issues.append("decision_doses_despite_in_range_payload")
        if dose_plan.pump_activated != "none":
            issues.append("dose_plan_selects_pump_despite_in_range_payload")
    elif expected_classification == "wait_near_boundary":
        if "near_boundary" not in monitoring.risk_flags:
            issues.append("monitoring_status_misses_near_boundary_deadband")
        if strategy.action == "dose":
            issues.append("decision_doses_despite_near_boundary_deadband")
        if dose_plan.pump_activated != "none":
            issues.append("dose_plan_selects_pump_despite_near_boundary_deadband")
    elif expected_classification == "combined_disturbance":
        if diagnosis.classification != "combined_disturbance":
            issues.append("diagnosis_misses_combined_disturbance")
        if diagnosis.primary_metric not in {"ph", "ec"}:
            issues.append("combined_disturbance_has_no_primary_metric")
    else:
        if diagnosis.classification != expected_classification:
            issues.append("diagnosis_conflicts_with_payload_range_check")
        expected_primary = "ph" if expected_classification.startswith("ph") else "ec"
        if diagnosis.primary_metric != expected_primary:
            issues.append("diagnosis_primary_metric_conflicts_with_payload")

    if monitoring.status in {"unstable", "sensor_anomaly", "mixing"} and strategy.action == "dose":
        issues.append("decision_doses_despite_monitoring_safety_status")

    if diagnosis.classification in {"within_range", "unstable_reading", "sensor_anomaly"} and strategy.action == "dose":
        issues.append("decision_doses_despite_non_dosing_diagnosis")

    if strategy.action in {"dose", "fallback_baseline"}:
        control_decision = _agentic_control_decision(state)
        if baseline.pump_activated == "none" and projected is None:
            issues.append("decision_doses_without_baseline_pump")
        if dose_plan.pump_activated != control_decision.pump_activated:
            issues.append("dose_plan_pump_conflicts_with_agentic_primary")
    elif dose_plan.pump_activated != "none":
        issues.append("dose_plan_selects_pump_when_decision_is_not_dose")

    review_status = "block" if issues else "pass"
    return {
        "review_status": review_status,
        "issues": issues,
        "recommended_action": "wait" if issues else "continue",
        "reason": (
            "Consistency review blocked dosing because agent outputs conflict with "
            "payload, monitoring, diagnosis, or safety context."
            if issues
            else "Agent outputs are consistent with payload, history, and safety context."
        ),
        "expected_classification_from_payload": expected_classification,
        "hard_safety_block_reason": hard_block_reason,
    }


def _expected_classification_from_payload(
    payload: SensorPayload,
    reference_range: ReferenceRange,
) -> str:
    ph_low = payload.ph < reference_range.ph_target_min
    ph_high = payload.ph > reference_range.ph_target_max
    ec_low = payload.ec < reference_range.ec_target_min
    ec_high = payload.ec > reference_range.ec_target_max
    ph_out = ph_low or ph_high
    ec_out = ec_low or ec_high

    if ph_out and ec_out:
        return "combined_disturbance"
    if ph_low:
        return "ph_low"
    if ph_high:
        return "ph_high"
    if ec_low:
        return "ec_low"
    if ec_high:
        return "ec_high"
    return "within_range"


def _agentic_no_action(
    baseline: DosingDecision,
    decision: str,
    reason: str,
    metadata: dict[str, object],
) -> DosingDecision:
    return DosingDecision(
        decision=decision,
        pump_activated="none",
        dose_ml=0,
        duration_ms=0,
        ph_within_range=baseline.ph_within_range,
        ec_within_range=baseline.ec_within_range,
        ph_deviation=baseline.ph_deviation,
        ec_deviation=baseline.ec_deviation,
        reason=reason,
        metadata=metadata,
    )


def _blocked_decision_name(monitoring: MonitoringResult) -> str:
    if monitoring.status == "unstable":
        return "wait_for_stability"
    if monitoring.status == "sensor_anomaly":
        return "sensor_anomaly"
    if monitoring.status == "mixing":
        return "wait_for_mixing"
    if monitoring.status == "confirming":
        return "wait_initial_confirmation"
    return "wait_safety_gate"


def _strategy_decision_name(strategy: StrategyResult, monitoring: MonitoringResult) -> str:
    if monitoring.is_recovering:
        return "wait_natural_recovery"
    if "near_boundary" in monitoring.risk_flags:
        return "wait_near_boundary"
    if monitoring.status == "confirming":
        return "wait_initial_confirmation"
    if strategy.action == "mix_longer":
        return "wait_for_mixing"
    if strategy.action == "no_action":
        return "within_range"
    return "wait_agentic_decision"


def _safe_dose_factor(
    state: AgenticGraphState,
    dose_plan: DosePlanResult,
    baseline: DosingDecision,
    strategy: StrategyResult,
) -> float:
    """Apply a planned factor without exceeding calibrated target-range headroom."""
    if dose_plan.pump_activated not in {baseline.pump_activated, "none"}:
        planned_factor = _adaptive_dose_factor(baseline, _control_history(state), strategy.confidence)
    else:
        planned_factor = _clamp_dose_factor(dose_plan.dose_adjustment_factor)
    return min(
        planned_factor,
        _target_range_factor_cap(baseline),
        _crop_max_dose_factor(state),
    )


def _target_range_factor_cap(control_decision: DosingDecision) -> float:
    """Return the largest adaptive factor predicted to stay inside target range."""
    headroom_factor = control_decision.metadata.get("agentic_target_range_headroom_factor")
    if isinstance(headroom_factor, int | float):
        return _clamp_dose_factor(float(headroom_factor))
    return MAX_AGENTIC_DOSE_FACTOR


def _reading_instability_reason(payload: SensorPayload) -> str | None:
    required_seconds = settings.default_stability_required_seconds
    if payload.ph_stable_for_seconds is not None and payload.ph_stable_for_seconds < required_seconds:
        return "pH reading has not remained stable for the required observation window."
    if payload.ec_stable_for_seconds is not None and payload.ec_stable_for_seconds < required_seconds:
        return "EC reading has not remained stable for the required observation window."
    return None


def _sensor_anomaly_reason(
    payload: SensorPayload,
    history: Sequence[SensorHistoryEntry],
) -> str | None:
    latest = _latest_log(_continuity_history(history))
    if latest is None:
        return None
    if (
        abs(payload.ph - latest.ph) >= PH_SENSOR_JUMP_THRESHOLD
        and not _is_expected_post_dose_metric_response(payload, latest, "ph")
    ):
        return "pH changed abruptly compared with the latest stored reading; dosing is rejected."
    if (
        abs(payload.ec - latest.ec) >= EC_SENSOR_JUMP_THRESHOLD
        and not _is_expected_post_dose_metric_response(payload, latest, "ec")
    ):
        return "EC changed abruptly compared with the latest stored reading; dosing is rejected."
    return None


def _is_expected_post_dose_metric_response(
    payload: SensorPayload,
    latest: SensorHistoryEntry,
    metric: str,
) -> bool:
    """Allow large movements caused by the immediately previous pump action."""
    latest_status = _effective_history_status(latest)
    if latest_status not in {"completed", "completed_estimated"}:
        return False

    if metric == "ph":
        if latest.pump_activated == "ph_up":
            return payload.ph > latest.ph
        if latest.pump_activated == "ph_down":
            return payload.ph < latest.ph
    if metric == "ec":
        if latest.pump_activated == "ec_up":
            return payload.ec > latest.ec
        if latest.pump_activated == "ec_down":
            return payload.ec < latest.ec
    return False


def _recent_dose_mixing_reason(history: Sequence[SensorHistoryEntry]) -> str | None:
    latest_dose = _latest_dose(history)
    if latest_dose is None:
        return None
    if _is_active_dosing(latest_dose):
        return "A previous dosing action is still active; waiting for completion before another dose."
    elapsed_seconds = (_now_utc() - _mixing_reference_time(latest_dose)).total_seconds()
    effective_seconds = _effective_mixing_seconds_from_history(latest_dose)
    logger.info(
        "Agentic AI mixing window check: elapsed_seconds={} effective_seconds={} latest_pump={} latest_status={}",
        round(elapsed_seconds, 1),
        effective_seconds,
        latest_dose.pump_activated,
        latest_dose.status,
    )
    if elapsed_seconds < effective_seconds:
        remaining_seconds = round(effective_seconds - elapsed_seconds)
        return (
            "A recent dose is still inside the configured mixing window; "
            f"waiting about {remaining_seconds} more seconds before another action."
        )
    return None


def _latest_dose(history: Sequence[SensorHistoryEntry]) -> SensorHistoryEntry | None:
    """Return the latest history row that activated a real pump."""
    return next(
        (entry for entry in _newest_first(history) if entry.pump_activated not in {None, "none"}),
        None,
    )


def _initial_confirmation_reason(state: AgenticGraphState) -> str | None:
    baseline = state["baseline"]
    history = _continuity_history(state["history"])
    if baseline.pump_activated == "none":
        return None
    if not history:
        return (
            "Initial out-of-range reading has no fresh matching confirmation in the "
            "current agentic history window; waiting for one confirmation reading "
            "before dosing to avoid reacting to startup sensor shock."
        )

    anchor = _initial_confirmation_anchor(history, baseline, state["reference_range"])
    if anchor is None:
        has_prior_matching_deviation = _has_matching_deviation_since_in_range(
            history,
            baseline,
            state["reference_range"],
        )
        if has_prior_matching_deviation:
            return None
        return (
            "Current out-of-range reading has no fresh matching confirmation in the "
            "current agentic history window; waiting for one confirmation reading "
            "before dosing."
        )

    elapsed_seconds = (_now_utc() - _as_utc(anchor.timestamp)).total_seconds()
    required_gap = settings.default_initial_confirmation_gap_seconds
    if elapsed_seconds < required_gap:
        remaining_seconds = round(required_gap - elapsed_seconds)
        return (
            "Initial out-of-range reading is awaiting confirmation; waiting about "
            f"{remaining_seconds} more seconds before dosing."
        )

    return None


def _near_boundary_wait_reason(state: AgenticGraphState) -> str | None:
    """Observe the first tiny deviation inside the confirmation band."""
    baseline = state["baseline"]
    payload = state["payload"]
    if baseline.pump_activated == "none" and baseline.decision != "wait_near_boundary":
        return None
    if baseline.decision == "wait_near_boundary":
        ph_deviation = float(baseline.metadata.get("ph_deviation") or 0)
        ec_deviation = float(baseline.metadata.get("ec_deviation") or 0)
        if ph_deviation > 0:
            threshold = payload.ph_stability_threshold or settings.default_ph_stability_threshold
            return (
                f"pH is {ph_deviation} outside the target boundary and inside the "
                f"{threshold} near-boundary confirmation band. "
                f"{_near_boundary_confirmation_status(payload.ph_stable_for_seconds)}"
            )
        if ec_deviation > 0:
            threshold = payload.ec_stability_threshold or settings.default_ec_stability_threshold
            return (
                f"EC is {ec_deviation} outside the target boundary and inside the "
                f"{threshold} near-boundary confirmation band. "
                f"{_near_boundary_confirmation_status(payload.ec_stable_for_seconds)}"
            )
        return baseline.reason
    if _has_matching_deviation_since_in_range(
        _continuity_history(state["history"]),
        baseline,
        state["reference_range"],
    ):
        return None

    if baseline.decision.startswith("ph"):
        threshold = payload.ph_stability_threshold or settings.default_ph_stability_threshold
        if 0 < baseline.ph_deviation <= threshold:
            side = "below the minimum" if baseline.decision == "ph_low" else "above the maximum"
            return (
                f"pH is {baseline.ph_deviation} {side} target and remains inside the "
                f"{threshold} near-boundary confirmation band. "
                f"{_near_boundary_confirmation_status(payload.ph_stable_for_seconds)}"
            )

    if baseline.decision.startswith("ec"):
        threshold = payload.ec_stability_threshold or settings.default_ec_stability_threshold
        if 0 < baseline.ec_deviation <= threshold:
            side = "below the minimum" if baseline.decision == "ec_low" else "above the maximum"
            return (
                f"EC is {baseline.ec_deviation} {side} target and remains inside the "
                f"{threshold} near-boundary confirmation band. "
                f"{_near_boundary_confirmation_status(payload.ec_stable_for_seconds)}"
            )

    return None


def _near_boundary_confirmation_status(stable_for_seconds: int | None) -> str:
    """Explain the remaining evidence needed before a small corrective dose."""
    required_seconds = settings.default_stability_required_seconds
    if stable_for_seconds is None:
        return (
            "Persisted stability duration is unavailable, so dosing remains paused "
            "until a stable follow-up reading confirms the excursion."
        )
    if stable_for_seconds < required_seconds:
        remaining_seconds = required_seconds - stable_for_seconds
        return (
            f"The reading needs about {remaining_seconds} more seconds of stability "
            "before it can be confirmed."
        )
    return (
        f"The reading is stable for {stable_for_seconds} seconds; one independent "
        "follow-up reading is required before the bounded correction."
    )


def _initial_confirmation_anchor(
    history: Sequence[SensorHistoryEntry],
    baseline: DosingDecision,
    reference_range: ReferenceRange,
) -> SensorHistoryEntry | None:
    matching_waits: list[SensorHistoryEntry] = []
    for entry in _newest_first(history):
        if entry.decision != "wait_initial_confirmation":
            break
        if not _history_entry_matches_deviation(entry, baseline, reference_range):
            break
        matching_waits.append(entry)

    if not matching_waits:
        return None

    return min(matching_waits, key=lambda entry: _as_utc(entry.timestamp))


def _has_matching_deviation_since_in_range(
    history: Sequence[SensorHistoryEntry],
    baseline: DosingDecision,
    reference_range: ReferenceRange,
) -> bool:
    """Find current-streak deviation evidence without crossing an in-range row."""
    for entry in _newest_first(history):
        if entry.decision == "within_range":
            return False
        if _history_entry_matches_deviation(entry, baseline, reference_range):
            return True
    return False


def _history_entry_matches_deviation(
    entry: SensorHistoryEntry,
    baseline: DosingDecision,
    reference_range: ReferenceRange,
) -> bool:
    if baseline.decision == "ph_low":
        return entry.ph < reference_range.ph_target_min
    if baseline.decision == "ph_high":
        return entry.ph > reference_range.ph_target_max
    if baseline.decision == "ec_low":
        return entry.ec < reference_range.ec_target_min
    if baseline.decision == "ec_high":
        return entry.ec > reference_range.ec_target_max
    return False


def _has_recent_dose(history: Sequence[SensorHistoryEntry]) -> bool:
    return any(entry.pump_activated not in {None, "none"} for entry in history)


def _remove_false_recent_dose_claim(reason: str) -> str:
    lowered = reason.lower()
    if "recent dose" not in lowered and "still mixing" not in lowered:
        return reason

    return (
        "Stable deviation remains with no recent dose in history; "
        "adaptive dose planning should select a bounded correction."
    )


def _natural_recovery_reason(
    payload: SensorPayload,
    baseline: DosingDecision,
    history: Sequence[SensorHistoryEntry],
    *,
    reference_range: ReferenceRange,
    control_history: Sequence[SensorHistoryEntry],
) -> str | None:
    """Conservative local fallback only; never override an LLM trend assessment."""
    chronological = list(reversed(_newest_first(_continuity_history(history))))
    distinct = {_as_utc(entry.timestamp): entry for entry in chronological}
    if len(distinct) < NATURAL_RECOVERY_HISTORY_POINTS:
        return None
    recovery_window = list(distinct.values())[-NATURAL_RECOVERY_HISTORY_POINTS:]
    window_start = _as_utc(recovery_window[0].timestamp)
    for entry in control_history:
        if entry.pump_activated in {None, "none"}:
            continue
        event_times = [entry.timestamp, entry.action_started_at, entry.action_completed_at]
        if any(value is not None and _as_utc(value) >= window_start for value in event_times):
            return None

    recovering = []
    for metric, lower, upper in (
        ("ph", reference_range.ph_target_min, reference_range.ph_target_max),
        ("ec", reference_range.ec_target_min, reference_range.ec_target_max),
    ):
        current = getattr(payload, metric)
        if lower <= current <= upper:
            continue
        values = [getattr(entry, metric) for entry in recovery_window] + [current]
        toward = _strictly_increasing(values) if current < lower else _strictly_decreasing(values)
        if not toward:
            return None
        recovering.append("pH" if metric == "ph" else "EC")
    if not recovering:
        return None
    return " and ".join(recovering) + " remain outside range but are moving toward target without another dose."


def _adaptive_dose_factor(
    baseline: DosingDecision,
    history: Sequence[SensorHistoryEntry],
    confidence: float = 1.0,
) -> float:
    latest_same_pump = next(
        (entry for entry in _newest_first(history) if entry.pump_activated == baseline.pump_activated),
        None,
    )
    if latest_same_pump is None:
        if confidence < 0.75:
            return LOW_CONFIDENCE_AGENTIC_DOSE_FACTOR
        return TARGET_REENTRY_AGENTIC_DOSE_FACTOR

    same_pump_interpretation = _same_pump_response_interpretation(latest_same_pump, baseline, history)
    if same_pump_interpretation == "previous_same_pump_overshot_opposite_direction":
        return MIN_AGENTIC_DOSE_FACTOR

    response_factor = _observed_response_dose_factor(latest_same_pump, baseline, history)
    if response_factor is not None:
        return response_factor

    if confidence < 0.75:
        return LOW_CONFIDENCE_AGENTIC_DOSE_FACTOR

    return TARGET_REENTRY_AGENTIC_DOSE_FACTOR


def _observed_response_dose_factor(
    latest_same_pump: SensorHistoryEntry,
    baseline: DosingDecision,
    history: Sequence[SensorHistoryEntry],
) -> float | None:
    """Conservatively strengthen a dose that missed its calibrated pH target."""
    if baseline.pump_activated not in {"ph_up", "ph_down"}:
        return None

    newer = [
        entry
        for entry in history
        if _as_utc(entry.timestamp) > _as_utc(latest_same_pump.timestamp)
        and entry.pump_activated in {None, "none"}
    ]
    if len(newer) < 3:
        return None

    expected = latest_same_pump.decision_metadata.get("agentic_dose_deviation")
    if not isinstance(expected, int | float) or expected <= 0:
        expected = latest_same_pump.decision_metadata.get("dose_deviation")
    if not isinstance(expected, int | float) or expected <= 0:
        return None

    if baseline.pump_activated == "ph_down":
        observed = latest_same_pump.ph - min(entry.ph for entry in newer)
    else:
        observed = max(entry.ph for entry in newer) - latest_same_pump.ph

    minimum_response = max(
        PH_DELIVERY_RESPONSE_THRESHOLD,
        float(expected) * 0.25,
    )
    if observed < minimum_response:
        return None
    return round(min(max(float(expected) / observed, 1.0), 1.25), 2)


def _unresolved_same_pump_dose_count(
    baseline: DosingDecision,
    history: Sequence[SensorHistoryEntry],
) -> int:
    """Count same-pump doses in the active unresolved deviation streak."""
    dose_count = 0
    for entry in _newest_first(_continuity_history(history)):
        if entry.decision == "within_range":
            break
        if _latest_same_pump_overshot(entry, baseline):
            break
        if (
            entry.pump_activated == baseline.pump_activated
            and entry.decision == baseline.decision
        ):
            dose_count += 1
    return dose_count


def _adaptive_mixing_factor(
    baseline: DosingDecision,
    history: Sequence[SensorHistoryEntry],
    dose_factor: float,
) -> float:
    """Return a bounded heuristic multiplier for the next post-dose observation window."""
    factor = DEFAULT_MIXING_ADJUSTMENT_FACTOR

    if dose_factor >= 0.75:
        factor += 0.25
    if dose_factor > TARGET_REENTRY_AGENTIC_DOSE_FACTOR:
        factor += 0.25
    if baseline.pump_activated in {"ec_up", "ec_down"}:
        factor += 0.25

    latest_same_pump = next(
        (entry for entry in _newest_first(history) if entry.pump_activated == baseline.pump_activated),
        None,
    )
    if (
        latest_same_pump
        and _same_pump_response_interpretation(latest_same_pump, baseline, history)
        == "previous_same_pump_overshot_opposite_direction"
    ):
        factor += 0.75

    return _clamp_mixing_factor(factor)


def _mixing_window_for_dose(
    dose_plan: DosePlanResult,
    baseline: DosingDecision,
    history: Sequence[SensorHistoryEntry],
    strategy: StrategyResult,
    dose_factor: float,
    dose_ml: float,
    reference_range: ReferenceRange,
    crop_policy: dict[str, object],
) -> dict[str, object]:
    """Return the bounded mixing window that future decisions should respect."""
    base_seconds = settings.agentic_base_mixing_time_seconds
    llm_factor = _clamp_mixing_factor(dose_plan.mixing_adjustment_factor)
    deterministic_factor = _adaptive_mixing_factor(
        baseline,
        history,
        dose_factor,
    )
    dose_fraction = dose_ml / effective_max_dose_for_pump(reference_range, baseline.pump_activated)
    dose_size_factor = 1.25 if dose_fraction >= 0.75 else 1.0
    crop_factor = float(crop_policy["min_mixing_factor"])
    selected_factor = max(llm_factor, deterministic_factor, dose_size_factor, crop_factor)
    effective_seconds = _clamp_mixing_seconds(round(base_seconds * selected_factor))

    return {
        "base_seconds": base_seconds,
        "effective_seconds": effective_seconds,
        "min_seconds": MIN_MIXING_TIME_SECONDS,
        "max_seconds": MAX_MIXING_TIME_SECONDS,
        "adjustment_factor": round(effective_seconds / base_seconds, 2)
        if base_seconds > 0
        else DEFAULT_MIXING_ADJUSTMENT_FACTOR,
        "llm_recommended_factor": llm_factor,
        "deterministic_factor": deterministic_factor,
        "crop_lifecycle_factor": crop_factor,
        "source": "agentic_ai_bounded",
        "reason": dose_plan.reason,
    }


def _clamp_mixing_seconds(value: int) -> int:
    return min(max(value, MIN_MIXING_TIME_SECONDS), MAX_MIXING_TIME_SECONDS)


def _effective_mixing_seconds_from_history(entry: SensorHistoryEntry) -> int:
    metadata_window = entry.decision_metadata.get("mixing_window", {})
    effective_seconds = metadata_window.get("effective_seconds")
    if isinstance(effective_seconds, int | float):
        return _clamp_mixing_seconds(round(effective_seconds))
    return _clamp_mixing_seconds(settings.default_mixing_time_seconds)


def _mixing_window_check_from_history(
    history: Sequence[SensorHistoryEntry],
) -> dict[str, object] | None:
    latest_dose = next(
        (entry for entry in _newest_first(history) if entry.pump_activated not in {None, "none"}),
        None,
    )
    if latest_dose is None or latest_dose.status == "dosing":
        if latest_dose is None or _is_active_dosing(latest_dose):
            return None

    elapsed_seconds = (_now_utc() - _mixing_reference_time(latest_dose)).total_seconds()
    effective_seconds = _effective_mixing_seconds_from_history(latest_dose)
    base_seconds = settings.default_mixing_time_seconds
    remaining_seconds = max(effective_seconds - elapsed_seconds, 0)
    return {
        "base_seconds": base_seconds,
        "effective_seconds": effective_seconds,
        "elapsed_seconds": round(elapsed_seconds, 1),
        "remaining_seconds": round(remaining_seconds, 1),
        "adjustment_factor": round(effective_seconds / base_seconds, 2)
        if base_seconds > 0
        else DEFAULT_MIXING_ADJUSTMENT_FACTOR,
        "latest_pump": latest_dose.pump_activated,
        "latest_status": _effective_history_status(latest_dose),
    }


def _is_active_dosing(entry: SensorHistoryEntry) -> bool:
    """Return whether a dosing row still plausibly represents active actuation."""
    if entry.status in {"inter_dose_mixing", "ec_up_b_dosing"}:
        # An EC pair remains unfinished until the device reports completion.
        return entry.action_completed_at is None
    if entry.status != "dosing":
        return False

    expected_complete_at = _mixing_reference_time(entry)
    active_until = expected_complete_at + timedelta(seconds=STALE_DOSING_GRACE_SECONDS)
    is_active = _now_utc() <= active_until
    if not is_active:
        logger.warning(
            "Ignoring stale dosing status for history entry: pump={} decision={} expected_complete_at={}",
            entry.pump_activated,
            entry.decision,
            expected_complete_at.isoformat(),
        )
    return is_active


def _effective_history_status(entry: SensorHistoryEntry) -> str | None:
    if entry.status == "dosing" and not _is_active_dosing(entry):
        return "completed_estimated"
    return entry.status


def _mixing_reference_time(entry: SensorHistoryEntry) -> datetime:
    """Return the time when post-dose mixing should be considered to have started."""
    if entry.action_completed_at is not None:
        return _as_utc(entry.action_completed_at)

    if entry.action_started_at is not None and entry.duration_ms:
        return _as_utc(entry.action_started_at) + timedelta(milliseconds=entry.duration_ms)

    if entry.duration_ms:
        return _as_utc(entry.timestamp) + timedelta(milliseconds=entry.duration_ms)

    return _as_utc(entry.timestamp)


def _history_freshness_time(entry: SensorHistoryEntry) -> datetime:
    """Return the timestamp that determines whether history is still actionable."""
    if entry.pump_activated not in {None, "none"}:
        return _mixing_reference_time(entry)
    return _as_utc(entry.timestamp)


def _latest_same_pump_overshot(entry: SensorHistoryEntry, baseline: DosingDecision) -> bool:
    if baseline.decision == "ph_low":
        return entry.decision == "ph_high"
    if baseline.decision == "ph_high":
        return entry.decision == "ph_low"
    if baseline.decision == "ec_low":
        return entry.decision == "ec_high"
    if baseline.decision == "ec_high":
        return entry.decision == "ec_low"
    return False


def _same_pump_response_interpretation(
    latest_same_pump: SensorHistoryEntry,
    baseline: DosingDecision,
    history: Sequence[SensorHistoryEntry],
) -> str:
    """Classify what happened after the latest same-pump correction."""
    newer_entries = [
        entry
        for entry in _newest_first(history)
        if _as_utc(entry.timestamp) > _as_utc(latest_same_pump.timestamp)
    ]
    if any(_latest_same_pump_overshot(entry, baseline) for entry in newer_entries):
        return "previous_same_pump_overshot_opposite_direction"
    if any(entry.decision == "within_range" for entry in newer_entries):
        return "previous_same_pump_resolved_or_changed_condition"
    if latest_same_pump.decision == baseline.decision:
        return "previous_same_pump_still_unresolved"
    if _latest_same_pump_overshot(latest_same_pump, baseline):
        return "previous_same_pump_overshot_opposite_direction"
    return "previous_same_pump_resolved_or_changed_condition"


def _delivery_issue_context(
    state: AgenticGraphState,
    control_decision: DosingDecision,
) -> dict[str, object]:
    """Detect a completed dose that produced little or no expected response."""
    if control_decision.pump_activated in {None, "none"}:
        return {"issue_detected": False, "reason": "No active pump correction is selected."}

    latest_same_pump = next(
        (
            entry
            for entry in _newest_first(_control_history(state))
            if entry.pump_activated == control_decision.pump_activated
        ),
        None,
    )
    if latest_same_pump is None:
        return {"issue_detected": False, "reason": "No previous same-pump dose is available."}

    if _effective_history_status(latest_same_pump) == "dosing":
        return {"issue_detected": False, "reason": "The previous same-pump dose is still active."}

    if not _history_dose_is_reliable_delivery_evidence(latest_same_pump):
        return {
            "issue_detected": False,
            "reason": (
                "Previous same-pump dose is below the reliable pump pulse threshold, "
                "so it is not used as delivery-failure evidence."
            ),
            "latest_same_pump_duration_ms": latest_same_pump.duration_ms,
            "minimum_reliable_pump_duration_ms": settings.minimum_reliable_pump_duration_ms,
        }

    response = _same_pump_metric_response(
        state["payload"],
        latest_same_pump,
        control_decision.pump_activated,
    )
    if response is None:
        return {"issue_detected": False, "reason": "Pump response metric is not applicable."}

    interpretation = _same_pump_response_interpretation(
        latest_same_pump,
        control_decision,
        _control_history(state),
    )
    expected_delta = response["expected_direction_delta"]
    threshold = response["minimum_expected_delta"]
    issue_detected = (
        interpretation == "previous_same_pump_still_unresolved"
        and expected_delta < threshold
    )

    return {
        "issue_detected": issue_detected,
        "reason": (
            "Previous same-pump dose produced little or no expected movement."
            if issue_detected
            else "Previous same-pump response is not small enough to indicate delivery failure."
        ),
        "latest_same_pump_timestamp": _as_utc(latest_same_pump.timestamp).isoformat(),
        "latest_same_pump_decision": latest_same_pump.decision,
        "latest_same_pump_status": _effective_history_status(latest_same_pump),
        "latest_same_pump_dose_ml": latest_same_pump.dose_ml,
        "latest_same_pump_duration_ms": latest_same_pump.duration_ms,
        "current_decision": control_decision.decision,
        "current_pump": control_decision.pump_activated,
        "interpretation": interpretation,
        "metric": response["metric"],
        "previous_value": response["previous_value"],
        "current_value": response["current_value"],
        "expected_direction_delta": expected_delta,
        "minimum_expected_delta": threshold,
        "potential_causes": [
            "empty nutrient or pH adjustment container",
            "air lock or clogged dosing tube",
            "pump, relay, wiring, or check-valve failure",
            "dose outlet not reaching circulating water",
            "insufficient mixing before remeasurement",
            "sensor drift or calibration issue",
        ],
    }


def _history_dose_is_reliable_delivery_evidence(entry: SensorHistoryEntry) -> bool:
    return (
        entry.duration_ms is not None
        and entry.duration_ms >= settings.minimum_reliable_pump_duration_ms
    )


def _same_pump_metric_response(
    payload: SensorPayload,
    entry: SensorHistoryEntry,
    pump_activated: str,
) -> dict[str, object] | None:
    if pump_activated == "ph_up":
        return _metric_response("ph", entry.ph, payload.ph, payload.ph - entry.ph, payload.ph_stability_threshold)
    if pump_activated == "ph_down":
        return _metric_response("ph", entry.ph, payload.ph, entry.ph - payload.ph, payload.ph_stability_threshold)
    if pump_activated == "ec_up":
        return _metric_response("ec", entry.ec, payload.ec, payload.ec - entry.ec, payload.ec_stability_threshold)
    if pump_activated == "ec_down":
        return _metric_response("ec", entry.ec, payload.ec, entry.ec - payload.ec, payload.ec_stability_threshold)
    return None


def _metric_response(
    metric: str,
    previous_value: float,
    current_value: float,
    expected_delta: float,
    payload_threshold: float | None,
) -> dict[str, object]:
    default_threshold = PH_DELIVERY_RESPONSE_THRESHOLD if metric == "ph" else EC_DELIVERY_RESPONSE_THRESHOLD
    threshold = max(payload_threshold or default_threshold, default_threshold)
    return {
        "metric": metric,
        "previous_value": round(previous_value, 4),
        "current_value": round(current_value, 4),
        "expected_direction_delta": round(expected_delta, 4),
        "minimum_expected_delta": round(threshold, 4),
    }


def _configured_llm_adapter() -> AgenticLLM | None:
    if not settings.agentic_ai_use_llm or not settings.openai_api_key:
        return None
    return OpenAIJsonLLM(
        settings.openai_api_key,
        settings.agentic_ai_model,
        settings.agentic_ai_fallback_model,
        settings.agentic_ai_llm_timeout_seconds,
        monitoring_model=settings.agentic_ai_monitoring_model,
        diagnostic_model=settings.agentic_ai_diagnostic_model,
        dose_planning_model=settings.agentic_ai_dose_planning_model,
    )


def _monitoring_context(state: AgenticGraphState) -> dict[str, Any]:
    """Supply chronological observations without downstream dosing-plan noise."""
    context = _agent_context(state)
    derived = context["derived_context"]
    ordered = list(reversed(_newest_first(state["history"])))
    evaluated_at = datetime.fromisoformat(derived["history_evaluated_at"])
    return {
        "payload": context["payload"],
        "reference_range": {
            key: context["reference_range"][key]
            for key in ("ph_target_min", "ph_target_max", "ec_target_min", "ec_target_max")
        },
        "history_order": "oldest_to_newest",
        "history_timing": {
            "distinct_timestamp_count": len({entry.timestamp for entry in ordered}),
            "newest_age_seconds": max(0, (evaluated_at - _as_utc(ordered[-1].timestamp)).total_seconds()) if ordered else None,
            "history_span_seconds": (_as_utc(ordered[-1].timestamp) - _as_utc(ordered[0].timestamp)).total_seconds() if ordered else 0,
        },
        "recent_history": [
            entry.model_dump(include={"timestamp", "ph", "ec", "temperature", "reservoir_volume_liters"})
            for entry in ordered
        ],
        "recent_control_events": context["recent_control_events"],
        "combined_disturbance": context["combined_disturbance"],
        "crop_lifecycle": context["crop_lifecycle"],
        "orchestrator": state.get("orchestration", {}),
        "derived_context": {
            "initial_confirmation_reason": _initial_confirmation_reason(state),
            **{key: derived[key] for key in (
                "history_evaluated_at", "history_freshness_gap_seconds",
                "reading_instability_reason", "sensor_anomaly_reason",
                "recent_dose_mixing_reason", "near_boundary_wait_reason",
                "has_recent_dose",
            )},
        },
    }


def _agent_context(state: AgenticGraphState) -> dict[str, Any]:
    payload = state["payload"]
    history = state["history"]
    control_history = _control_history(state)
    monitoring = state.get("monitoring").model_dump() if state.get("monitoring") else None
    diagnosis = state.get("diagnosis").model_dump() if state.get("diagnosis") else None
    strategy = state.get("strategy").model_dump() if state.get("strategy") else None
    dose_plan = state.get("dose_plan").model_dump() if state.get("dose_plan") else None
    agentic_primary_decision = _agentic_control_decision(state)
    agentic_primary = agentic_primary_decision.model_dump()
    tool_results = _tool_results_context(state, agentic_primary_decision)
    return {
        "payload": payload.model_dump(),
        "reference_range": state["reference_range"].model_dump(),
        "crop_lifecycle": state.get("crop_lifecycle") or {},
        "recent_history": [entry.model_dump() for entry in history],
        # Batch trend history omits synthetic control rows. Keep pump events
        # visible so agents can distinguish natural recovery from dose response.
        "recent_control_events": [
            entry.model_dump(include={
                "control_cycle_id", "timestamp", "action_started_at",
                "action_completed_at", "ph", "ec", "decision",
                "pump_activated", "dose_ml", "duration_ms", "status",
            })
            for entry in _newest_first(control_history)
            if entry.pump_activated not in {None, "none"}
        ],
        "baseline_shadow": state["baseline"].model_dump(),
        "agentic_primary_shadow": agentic_primary,
        "agentic_tool_results": tool_results,
        "combined_disturbance": _has_combined_disturbance(state),
        "projected_deviation": _active_projected_context(state),
        "monitoring": monitoring,
        "diagnosis": diagnosis,
        "strategy": strategy,
        "dose_plan": dose_plan,
        "consistency_review": state.get("consistency_review"),
        "agent_communication": _agent_communication_context(state),
        "monitoring_agent": monitoring,
        "diagnostic_reasoning_agent": diagnosis,
        "decision_agent": strategy,
        "dose_planning_agent": dose_plan,
        "derived_context": {
            "history_evaluated_at": _now_utc().isoformat(),
            "history_freshness_gap_seconds": settings.agentic_history_freshness_gap_seconds,
            "has_recent_dose": _has_recent_dose(control_history),
            "recent_dose_mixing_reason": _recent_dose_mixing_reason(control_history),
            "base_mixing_time_seconds": settings.agentic_base_mixing_time_seconds,
            "mixing_time_bounds_seconds": {
                "min": MIN_MIXING_TIME_SECONDS,
                "max": MAX_MIXING_TIME_SECONDS,
            },
            "reading_instability_reason": _reading_instability_reason(payload),
            "sensor_anomaly_reason": _sensor_anomaly_reason(payload, history),
            "near_boundary_wait_reason": _near_boundary_wait_reason(state),
            "same_pump_response": _same_pump_response_context(state),
            "crop_dosing_policy": _crop_dosing_policy(state),
            "tool_results": tool_results,
        },
    }


def _tool_results_context(
    state: AgenticGraphState,
    control_decision: DosingDecision,
) -> dict[str, Any]:
    dose_plan = state.get("dose_plan")
    return agentic_tool_results(
        state["history"],
        control_decision,
        state["reference_range"],
        dose_adjustment_factor=(
            dose_plan.dose_adjustment_factor if dose_plan is not None else DEFAULT_AGENTIC_DOSE_FACTOR
        ),
    )


def _agent_communication_context(state: AgenticGraphState) -> dict[str, object]:
    """Describe how agent outputs are passed through the current graph state."""
    trace = state.get("llm_trace", [])
    completed_agents = [entry.get("agent") for entry in trace if entry.get("agent")]
    available_outputs = [
        key
        for key in ("orchestration", "monitoring", "diagnosis", "strategy", "dose_plan", "consistency_review")
        if state.get(key) is not None
    ]
    return {
        "mode": "structured_graph_state",
        "description": (
            "Agents communicate by writing structured outputs into shared graph "
            "state; the Orchestrator performs intake routing, then deterministically "
            "routes specialist feedback to the next specialist or Safety Gate."
        ),
        "sequence": [
            "orchestrator_agent",
            "monitoring_agent",
            "orchestrator_agent",
            "diagnostic_reasoning_agent",
            "orchestrator_agent",
            "decision_agent",
            "orchestrator_agent",
            "dose_planning_agent",
            "orchestrator_agent",
            "consistency_review",
            "orchestrator_agent",
            "safety_gate",
        ],
        "completed_agents": completed_agents,
        "available_outputs": available_outputs,
        "route": state.get("orchestration", {}).get("route"),
        "skipped_agents": state.get("orchestration", {}).get("skipped_agents", []),
    }


def _same_pump_response_context(
    state: AgenticGraphState,
    control_decision: DosingDecision | None = None,
) -> dict[str, object]:
    """Return compact context for adaptive dose explanations."""
    control_decision = control_decision or _agentic_control_decision(state)
    history = _control_history(state)
    reference_range = state["reference_range"]
    if control_decision.pump_activated in {None, "none"}:
        return {
            "available": False,
            "reason": "No current pump correction is selected.",
        }

    latest_same_pump = next(
        (
            entry
            for entry in _newest_first(history)
            if entry.pump_activated == control_decision.pump_activated
        ),
        None,
    )
    deterministic_factor = _adaptive_dose_factor(
        control_decision,
        history,
        state.get("strategy").confidence if state.get("strategy") else 1.0,
    )
    range_headroom_factor = control_decision.metadata.get(
        "agentic_target_range_headroom_factor",
        TARGET_REENTRY_AGENTIC_DOSE_FACTOR,
    )
    max_dose_ml = effective_max_dose_for_pump(reference_range, control_decision.pump_activated)

    if latest_same_pump is None:
        return {
            "available": False,
            "reason": "No recent same-pump dose exists inside the fresh history window.",
            # Keep the ESP32 summary factor name while exposing richer factor
            # context to agents and stored metadata.
            "recommended_dose_factor": deterministic_factor,
            "fallback_reference_factor": deterministic_factor,
            "target_range_headroom_factor": range_headroom_factor,
            "current_baseline_dose_ml": control_decision.dose_ml,
            "pump_max_dose_ml_per_cycle": max_dose_ml,
            "current_baseline_hits_pump_cap": control_decision.dose_ml >= max_dose_ml,
        }

    interpretation = _same_pump_response_interpretation(latest_same_pump, control_decision, history)
    latest_same_pump_hit_pump_cap = _same_pump_entry_hit_cap(latest_same_pump, max_dose_ml)
    unresolved_count = (
        _unresolved_same_pump_dose_count(control_decision, history)
        if interpretation == "previous_same_pump_still_unresolved"
        else 0
    )

    return {
        "available": True,
        "latest_same_pump_timestamp": _as_utc(latest_same_pump.timestamp).isoformat(),
        "latest_same_pump_decision": latest_same_pump.decision,
        "latest_same_pump_status": _effective_history_status(latest_same_pump),
        "latest_same_pump_dose_ml": latest_same_pump.dose_ml,
        "latest_same_pump_duration_ms": latest_same_pump.duration_ms,
        "latest_same_pump_hit_pump_cap": latest_same_pump_hit_pump_cap,
        "latest_same_pump_hit_cap_and_unresolved": (
            latest_same_pump_hit_pump_cap
            and interpretation == "previous_same_pump_still_unresolved"
        ),
        "interpretation": interpretation,
        "unresolved_same_pump_dose_count": unresolved_count,
        "recommended_dose_factor": deterministic_factor,
        "fallback_reference_factor": deterministic_factor,
        "target_range_headroom_factor": range_headroom_factor,
        "current_baseline_dose_ml": control_decision.dose_ml,
        "pump_max_dose_ml_per_cycle": max_dose_ml,
        "current_baseline_hits_pump_cap": control_decision.dose_ml >= max_dose_ml,
    }


def _same_pump_entry_hit_cap(entry: SensorHistoryEntry, max_dose_ml: float) -> bool:
    """Return whether the recorded pump dose reached its configured cycle cap."""
    if entry.dose_ml is None:
        return False
    return entry.dose_ml >= round(max_dose_ml - 0.01, 2)


def _reasoning_source(state: AgenticGraphState) -> str:
    trace = [
        entry
        for entry in state.get("llm_trace", [])
        if not (
            entry.get("agent") == "orchestrator_agent"
            and entry.get("source") == DETERMINISTIC_FALLBACK_SOURCE
        )
    ]
    sources = {entry.get("source") for entry in trace}
    if sources == {LLM_SOURCE}:
        return "llm_agents"
    if LLM_SOURCE in sources:
        return "mixed_llm_deterministic_fallback"
    return DETERMINISTIC_FALLBACK_SOURCE


def _llm_models_used(state: AgenticGraphState) -> list[str]:
    models = [
        entry["model"]
        for entry in state.get("llm_trace", [])
        if entry.get("source") == LLM_SOURCE and entry.get("model")
    ]
    return sorted(set(models))


def _trace(
    state: AgenticGraphState,
    agent_name: str,
    result: BaseModel,
    source: str,
    model: str | None,
) -> list[dict[str, Any]]:
    return state.get("llm_trace", []) + [
        {
            "agent": agent_name,
            "source": source,
            "model": model,
            "output": result.model_dump(),
        }
    ]


def _latest_log(history: Sequence[SensorHistoryEntry]) -> SensorHistoryEntry | None:
    newest_logs = _newest_first(history)
    if not newest_logs:
        return None
    return newest_logs[0]


def _newest_first(history: Sequence[SensorHistoryEntry]) -> list[SensorHistoryEntry]:
    return sorted(history, key=lambda entry: entry.timestamp, reverse=True)


def _strictly_increasing(values: Sequence[float]) -> bool:
    return all(current < following for current, following in zip(values, values[1:]))


def _strictly_decreasing(values: Sequence[float]) -> bool:
    return all(current > following for current, following in zip(values, values[1:]))


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
