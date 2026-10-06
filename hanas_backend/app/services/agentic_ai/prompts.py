"""Prompt loader for the HANAS LLM-backed agentic decision graph."""

from functools import lru_cache
from pathlib import Path


PROMPT_DIR = Path(__file__).with_name("prompts")


@lru_cache
def _load_prompt(filename: str) -> str:
    """Load one agent prompt from the prompt text folder."""
    return (PROMPT_DIR / filename).read_text(encoding="utf-8").strip()


ORCHESTRATOR_AGENT_PROMPT = _load_prompt("orchestrator_agent.txt")
MONITORING_AGENT_PROMPT = _load_prompt("monitoring_agent.txt")
DIAGNOSTIC_REASONING_AGENT_PROMPT = _load_prompt("diagnostic_reasoning_agent.txt")
DECISION_AGENT_PROMPT = _load_prompt("decision_agent.txt")
DOSE_PLANNING_AGENT_PROMPT = _load_prompt("dose_planning_agent.txt")
OVERVIEW_SUMMARIZER_AGENT_PROMPT = _load_prompt("overview_summarizer_agent.txt")
