"""LLM adapters for the HANAS agentic AI graph."""

import json
from typing import Any, Protocol

from pydantic import BaseModel

from app.services.agentic_ai.schemas import LLMCompletionResult, MonitoringResult


class AgenticLLM(Protocol):
    """LLM adapter used by graph nodes."""

    def complete_json(
        self,
        agent_name: str,
        system_prompt: str,
        context: dict[str, Any],
        output_model: type[BaseModel],
    ) -> LLMCompletionResult:
        """Return a structured model from an agent prompt and JSON context."""


class OpenAIJsonLLM:
    """OpenAI JSON adapter for LLM-backed agent nodes."""

    def __init__(
        self,
        api_key: str,
        model: str,
        fallback_model: str | None = None,
        timeout_seconds: float = 10.0,
        *,
        monitoring_model: str | None = None,
        diagnostic_model: str | None = None,
        dose_planning_model: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._fallback_model = fallback_model
        self._timeout_seconds = timeout_seconds
        self._monitoring_model = monitoring_model
        self._diagnostic_model = diagnostic_model
        self._dose_planning_model = dose_planning_model

    def complete_json(
        self,
        agent_name: str,
        system_prompt: str,
        context: dict[str, Any],
        output_model: type[BaseModel],
    ) -> LLMCompletionResult:
        """Call OpenAI with JSON-only output and validate it with Pydantic."""
        from openai import OpenAI

        client = OpenAI(
            api_key=self._api_key,
            timeout=self._timeout_seconds,
            max_retries=0,
        )
        last_error: Exception | None = None

        for model in self._models_to_try(agent_name):
            try:
                response = client.chat.completions.create(
                    model=model,
                    temperature=0,
                    response_format=(
                        {"type": "json_schema", "json_schema": {
                            "name": "monitoring_result", "strict": True,
                            "schema": _strict_json_schema(output_model.model_json_schema()),
                        }}
                        if output_model is MonitoringResult else {"type": "json_object"}
                    ),
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                f"{system_prompt}\n\n"
                                "Return only JSON matching this schema:\n"
                                f"{json.dumps(output_model.model_json_schema(), default=str)}"
                            ),
                        },
                        {
                            "role": "user",
                            "content": json.dumps(_jsonable_context(context), default=str),
                        },
                    ],
                )
                content = response.choices[0].message.content or "{}"
                return LLMCompletionResult(
                    output=output_model.model_validate_json(content),
                    model=model,
                )
            except Exception as exc:
                last_error = exc

        if last_error is not None:
            raise last_error

        raise RuntimeError(f"No LLM models configured for {agent_name}.")

    def _models_to_try(self, agent_name: str = "") -> list[str]:
        stage_model = {
            "Monitoring Agent": self._monitoring_model,
            "Diagnostic Reasoning Agent": self._diagnostic_model,
            "Dose Planning Agent": self._dose_planning_model,
        }.get(agent_name)
        primary = stage_model or self._model
        models = [primary.strip()]
        if self._fallback_model and self._fallback_model.strip() not in models:
            models.append(self._fallback_model.strip())
        return [model for model in models if model]


def _jsonable_context(context: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(context, default=str))


def _strict_json_schema(value: Any) -> Any:
    """Require explicit monitoring fields, including nullable optional fields."""
    if isinstance(value, list):
        return [_strict_json_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    schema = {key: _strict_json_schema(item) for key, item in value.items() if key != "default"}
    if schema.get("type") == "object":
        schema["additionalProperties"] = False
        schema["required"] = list(schema.get("properties", {}))
    return schema
