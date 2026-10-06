"""Fresh-start startup validates the current schema without historical migrations."""
import pytest
from app.database import runtime_schema


def test_current_schema_is_validated(monkeypatch):
    calls = []
    monkeypatch.setattr(runtime_schema, "read_ready", lambda: calls.append(True))
    runtime_schema.ensure_runtime_schema()
    assert calls == [True]


def test_invalid_schema_is_reported(monkeypatch):
    def fail():
        raise RuntimeError("schema incomplete")
    monkeypatch.setattr(runtime_schema, "read_ready", fail)
    with pytest.raises(RuntimeError, match="schema incomplete"):
        runtime_schema.ensure_runtime_schema()
