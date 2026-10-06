"""Shared test configuration."""

import os

import pytest

# Tests use the development control profile regardless of local deployment settings.
os.environ["APP_ENV"] = "dev"

from app.core.config import settings


@pytest.fixture(autouse=True)
def disable_configured_llm() -> None:
    """Keep tests deterministic even when local .env enables OpenAI calls."""
    settings.agentic_ai_use_llm = False
    settings.openai_api_key = ""
