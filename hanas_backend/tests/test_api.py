"""API route tests."""

from datetime import date, datetime, timedelta, timezone
from contextlib import contextmanager
import inspect


import pytest
from fastapi import HTTPException

from app.api.routes.control_cycles import (
    apply_human_review,
    complete_control_cycle,
    get_pending_command,
    mark_control_cycle_action_completed,
    mark_control_cycle_action_started,
)
from app.api.routes import health
from app.api.routes.health import read_ready, read_root
from app.api.routes.sensors import (
    _apply_human_review_gate,
    _control_payload,
    _mixing_time_ms_for_decision,
    receive_sensor_data,
)
from app.api.routes.settings import (
    confirm_experiment_preflight,
    get_system_settings,
    update_system_settings,
)
from app.api.dependencies import verify_device_token, verify_operator_token
from app.core.config import settings
from app.database.repositories.sensor_repository import (
    SensorLogRecord,
    _experiment_run_lock_key,
    _status_for_decision,
)
from app.database.repositories.sensor_repository import PostgresSensorReadingRepository
from app.main import app
from app.schemas.control_cycle import (
    ControlCycleActionStartedPayload,
    ControlCycleActionCompletedPayload,
    ControlCycleCompletionPayload,
    HumanReviewPayload,
    HumanReviewResponse,
    PendingCommandResponse,
)
from app.schemas.notification import ExperimentPreflightRequest, SystemSettingsUpdate
from app.schemas.sensor import DosingDecision, ReferenceRange, SensorHistoryEntry, SensorPayload
from app.schemas.sensor import OverviewSummary
from app.services import batch_scheduler, notifications, overview_summarizer
from app.services.batch_scheduler import (
    _batch_histories,
    _batch_payload_from_latest,
    _is_batch_source_reading,
)
from app.services.crop_lifecycle import build_crop_lifecycle_context
from app.services.baseline.dosing_rules import evaluate_persistent_dosing_decision
from app.services.agentic_ai.schemas import LLMCompletionResult, MonitoringResult, OrchestratorResult
from app.services.overview_summarizer import (
    _build_summary,
    _next_summary_run_at,
    _normalize_operator_narrative,
    _previous_summary_run_at,
    _SummaryNarrative,
    _summary_matches_current_logs,
    _summary_metrics,
)
from app.services.runtime_settings import (
    emergency_stop_enabled,
    experiment_preflight_required,
    maintenance_mode_enabled,
    monitoring_mode_enabled,
    runtime_bool_setting,
)
from tests.factories import build_reference_range


@pytest.fixture(autouse=True)
def use_phase2_fixed_reservoir_volume(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep API route tests independent from the runner's APP_ENV."""
    monkeypatch.setattr(settings, "force_fixed_reservoir_volume", True)
    monkeypatch.setattr(settings, "fixed_reservoir_volume_liters", 20.0)
    monkeypatch.setattr(settings, "default_reservoir_max_volume_liters", 70.0)
    monkeypatch.setattr(settings, "batch_analysis_enabled", False)


class FakeSensorRepository:
    """Fake repository used to test route behavior without PostgreSQL."""

    def __init__(self) -> None:
        self.reference_strategy: str | None = None
        self.recent_logs: list[SensorHistoryEntry] = []
        self.recent_log_limits: list[int] = []
        self.recent_or_active_pump_command: dict[str, object] | None = None
        self.notification_logs: list[dict[str, object]] = []
        self.system_settings: dict[str, object] = {}
        self.expire_stale_batch_pending_commands_calls = 0
        self.emergency_stop_active_commands_calls = 0
        self.cancel_pending_commands_calls: list[tuple[str, str]] = []
        self.agentic_cycle_lock_available = True
        self.agentic_cycle_lock_releases = 0
        self.events: list[str] = []
        self.pending_command = PendingCommandResponse(
            status="human_command_dispatched",
            has_command=True,
            control_cycle_id=456,
            decision="ph_low",
            pump_activated="ph_up",
            dose_ml=4,
            duration_ms=1983,
            mixing_time_ms=240000,
            message="Reviewed HITL command dispatched to ESP32.",
        )

    def get_active_reference_range(self, control_strategy: str | None = None) -> ReferenceRange:
        """Return a lettuce reference range."""
        self.reference_strategy = control_strategy
        return build_reference_range()

    def get_system_setting(self, key: str) -> object | None:
        """Return a runtime setting when a test configures it."""
        return self.system_settings.get(key)

    def set_system_setting(self, key: str, value: object) -> None:
        """Persist a runtime setting in memory."""
        self.system_settings[key] = value

    def set_exclusive_safety_mode(self, enabled_key: str) -> None:
        """Mirror the repository's atomic safety-mode transition."""
        for key in (
            "maintenance_mode_enabled",
            "monitoring_mode_enabled",
            "emergency_stop_enabled",
        ):
            self.system_settings[key] = key == enabled_key

    def try_acquire_agentic_cycle_lock(self) -> bool:
        """Pretend to acquire the shared LLM cycle lock."""
        return self.agentic_cycle_lock_available

    def release_agentic_cycle_lock(self) -> None:
        """Record release of the shared LLM cycle lock."""
        self.agentic_cycle_lock_releases += 1
        self.events.append("agentic_lock_released")

    def create_sensor_log(
        self,
        payload: SensorPayload,
        decision: DosingDecision,
    ) -> SensorLogRecord:
        """Pretend to persist one sensor payload and decision."""
        assert -10 < payload.temperature < 80
        assert payload.reservoir_volume_liters > 0
        assert decision.pump_activated in {"none", "ph_up", "ph_down", "ec_up", "ec_down"}
        self.events.append("decision_persisted")
        return SensorLogRecord(log_id=123, control_cycle_id=456)

    def mark_control_cycle_batch_pending(self, control_cycle_id: int) -> None:
        """Pretend to mark a batch LLM decision as pending ESP32 pickup."""

    def expire_stale_batch_pending_commands(
        self,
        control_strategy: str,
        max_age_seconds: int,
    ) -> int:
        """Pretend no stale batch command exists."""
        self.expire_stale_batch_pending_commands_calls += 1
        return 0

    def has_recent_batch_analysis(self, control_strategy: str, interval_seconds: int) -> bool:
        """Pretend no scheduler batch has run recently."""
        return False

    def has_unresolved_batch_command(self, control_strategy: str) -> bool:
        """Pretend no batch command is waiting for ESP32 completion."""
        return False

    def get_unresolved_batch_command(self, control_strategy: str) -> dict[str, object] | None:
        """Pretend no batch command is waiting for ESP32 completion."""
        return None

    def get_recent_or_active_pump_command(
        self,
        lookback_seconds: int,
    ) -> dict[str, object] | None:
        """Pretend no physical pump command is active or still mixing."""
        return self.recent_or_active_pump_command

    def get_latest_batch_analysis(self, control_strategy: str) -> dict[str, object] | None:
        """Pretend no batch analysis has been saved yet."""
        return None

    def get_recent_sensor_logs(
        self,
        limit: int = 20,
        control_strategy: str | None = None,
    ) -> list[SensorHistoryEntry]:
        """Return no prior readings by default."""
        assert limit in {10, 180, 1440}
        assert control_strategy in {None, "baseline", "agentic_ai"}
        self.recent_log_limits.append(limit)
        return self.recent_logs

    def complete_control_cycle(self, control_cycle_id: int, status: str = "completed") -> None:
        """Pretend to mark one control cycle complete."""
        assert control_cycle_id == 456
        assert status == "completed"

    def mark_control_cycle_action_completed(
        self,
        control_cycle_id: int,
        status: str = "mixing",
    ) -> None:
        """Pretend to mark pump shutdown before mixing completes."""
        assert control_cycle_id == 456
        assert status == "mixing"

    def emergency_stop_active_commands(self, control_strategy: str) -> int:
        """Pretend to stop active or pending pump commands."""
        assert control_strategy in {"baseline", "agentic_ai"}
        self.emergency_stop_active_commands_calls += 1
        return 0

    def cancel_pending_commands(self, control_strategy: str, status: str) -> int:
        """Pretend to cancel queued commands during a safe mode transition."""
        self.cancel_pending_commands_calls.append((control_strategy, status))
        return 1

    def mark_control_cycle_action_started(
        self,
        control_cycle_id: int,
        status: str = "dosing",
    ) -> None:
        """Pretend to mark the start of ESP32 actuation."""
        assert control_cycle_id == 456
        assert status == "dosing"

    def create_notification_log(self, **kwargs: object) -> None:
        """Pretend to persist one notification audit row."""
        self.notification_logs.append(kwargs)

    def apply_human_review(
        self,
        control_cycle_id: int,
        payload: HumanReviewPayload,
    ) -> HumanReviewResponse:
        """Pretend to apply an operator review."""
        assert control_cycle_id == 456
        return HumanReviewResponse(
            status="human_approved_pending_execution",
            control_cycle_id=control_cycle_id,
            decision="ph_low",
            pump_activated="ph_up",
            dose_ml=4,
            duration_ms=1983,
            mixing_time_ms=240000,
            message=f"review={payload.action}",
        )

    def get_pending_human_review_command(self) -> PendingCommandResponse:
        """Pretend to fetch one reviewed HITL command."""
        return self.pending_command


class ExperimentLifecycleRepository(FakeSensorRepository):
    """In-memory experiment boundary behavior for preflight route tests."""

    def __init__(self) -> None:
        super().__init__()
        self.setting_writes: list[tuple[str, object]] = []
        self.active_run = {
            "id": 41,
            "run_name": "HANAS live run",
            "control_strategy": "agentic_ai",
            "start_time": datetime(2026, 9, 12, tzinfo=timezone.utc),
            "last_activity_at": datetime(2026, 9, 13, tzinfo=timezone.utc),
            "cycle_count": 18,
        }

    def set_system_setting(self, key: str, value: object) -> None:
        self.setting_writes.append((key, value))
        super().set_system_setting(key, value)

    def get_active_experiment_run_summary(self, control_strategy: str) -> dict[str, object]:
        assert control_strategy == "agentic_ai"
        return self.active_run

    def start_new_experiment_run(self, control_strategy: str) -> dict[str, object]:
        assert control_strategy == "agentic_ai"
        self.active_run = {
            "id": 42,
            "run_name": "HANAS live run",
            "control_strategy": "agentic_ai",
            "start_time": datetime.now(timezone.utc),
            "last_activity_at": None,
            "cycle_count": 0,
        }
        return self.active_run


def test_read_root() -> None:
    """The root route reports that the backend is running."""
    response = read_root()
    assert response["app"] == "HANAS"
    assert response["name"] == "Hydroponic Agentic Nutrient Adjustment System"
    assert response["version"] == "0.3.8"
    assert response["app_env"]
    assert response["db_schema"]
    assert response["build_sha"] == "unknown"
    assert response["message"] == "HANAS backend is running"


def test_read_ready_confirms_database_and_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """The readiness probe requires PostgreSQL and every runtime table."""
    class FakeCursor:
        query_number = 0

        def __enter__(self) -> "FakeCursor":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, query: str, params: tuple[object, ...]) -> None:
            self.query_number += 1
            if self.query_number == 1:
                assert query.count("to_regclass(%s)") == 6
                assert all(str(name).startswith(f"{settings.db_schema}.") for name in params)
            elif self.query_number == 2:
                assert "information_schema.columns" in query
                assert params == (settings.db_schema, ["control_cycles", "system_logs"])
            else:
                assert "notification_logs" in query
                assert params[0] == settings.db_schema
                assert set(params[1]) == health._REQUIRED_NOTIFICATION_COLUMNS

        def fetchone(self) -> tuple[str, ...]:
            return tuple(f"table_{index}" for index in range(6))

        def fetchall(self) -> list[tuple[str, int]]:
            if self.query_number == 2:
                return [("control_cycles", 64), ("system_logs", 64)]
            return [(column,) for column in health._REQUIRED_NOTIFICATION_COLUMNS]

    class FakeConnection:
        def cursor(self) -> FakeCursor:
            return FakeCursor()

    @contextmanager
    def fake_connection():
        yield FakeConnection()

    monkeypatch.setattr(health, "get_db_connection", fake_connection)

    response = read_ready()
    assert response == {
        "app": "HANAS",
        "status": "ready",
        "app_env": settings.app_env,
        "db_schema": settings.db_schema,
        "build_sha": "unknown",
    }


def test_read_ready_fails_closed_when_schema_is_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing production table makes readiness fail with HTTP 503."""
    class FakeCursor:
        query_number = 0

        def __enter__(self) -> "FakeCursor":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, _query: str, _params: tuple[str, ...]) -> None:
            self.query_number += 1
            return None

        def fetchone(self) -> tuple[str | None, ...]:
            return ("reference_ranges", "experiment_runs", "control_cycles", "system_logs", None, "notification_logs")

        def fetchall(self) -> list[tuple[str, int]]:
            if self.query_number == 2:
                return [("control_cycles", 64), ("system_logs", 64)]
            return [(column,) for column in health._REQUIRED_NOTIFICATION_COLUMNS]

    class FakeConnection:
        def cursor(self) -> FakeCursor:
            return FakeCursor()

    @contextmanager
    def fake_connection():
        yield FakeConnection()

    monkeypatch.setattr(health, "get_db_connection", fake_connection)

    with pytest.raises(HTTPException) as exc_info:
        read_ready()
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == {
        "message": "Backend database schema is not ready.",
        "reason": "schema_missing_tables",
    }


def test_read_ready_fails_closed_when_status_columns_need_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Undersized status columns fail the production readiness check."""
    class FakeCursor:
        query_number = 0

        def __enter__(self) -> "FakeCursor":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, _query: str, _params: tuple[object, ...]) -> None:
            self.query_number += 1
            return None

        def fetchone(self) -> tuple[str, ...]:
            return tuple(f"table_{index}" for index in range(6))

        def fetchall(self) -> list[tuple[str, int]]:
            if self.query_number == 2:
                return [("control_cycles", 20), ("system_logs", 64)]
            return [(column,) for column in health._REQUIRED_NOTIFICATION_COLUMNS]

    class FakeConnection:
        def cursor(self) -> FakeCursor:
            return FakeCursor()

    @contextmanager
    def fake_connection():
        yield FakeConnection()

    monkeypatch.setattr(health, "get_db_connection", fake_connection)

    with pytest.raises(HTTPException) as exc_info:
        read_ready()
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == {
        "message": "Backend database schema is not ready.",
        "reason": "schema_status_columns_outdated",
    }


def test_read_ready_requires_notification_status_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The readiness probe rejects an incomplete notification reconciliation schema."""
    class FakeCursor:
        query_number = 0

        def __enter__(self) -> "FakeCursor":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, _query: str, _params: tuple[object, ...]) -> None:
            self.query_number += 1

        def fetchone(self) -> tuple[str, ...]:
            return tuple(f"table_{index}" for index in range(6))

        def fetchall(self) -> list[tuple[str, int]]:
            if self.query_number == 2:
                return [("control_cycles", 64), ("system_logs", 64)]
            return [("provider_message_id",)]

    class FakeConnection:
        def cursor(self) -> FakeCursor:
            return FakeCursor()

    @contextmanager
    def fake_connection():
        yield FakeConnection()

    monkeypatch.setattr(health, "get_db_connection", fake_connection)

    with pytest.raises(HTTPException) as exc_info:
        read_ready()
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == {
        "message": "Backend database schema is not ready.",
        "reason": "schema_notification_columns_outdated",
    }


def test_read_ready_reports_database_connection_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Readiness identifies dependency failures without exposing connection details."""
    @contextmanager
    def unavailable_connection():
        raise ConnectionError("sensitive database connection detail")
        yield

    monkeypatch.setattr(health, "get_db_connection", unavailable_connection)

    with pytest.raises(HTTPException) as exc_info:
        read_ready()
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == {
        "message": "Backend database connection is not ready.",
        "reason": "database_connection_failed",
    }
    assert "sensitive" not in str(exc_info.value.detail)


def test_receive_sensor_data() -> None:
    """The sensor ingestion route accepts and echoes valid readings."""
    payload = {
        "temperature": 24.5,
        "ph": 6.2,
        "ec": 1.8,
        "reservoir_volume_liters": 80,
        "ph_stable_for_seconds": 30,
        "ec_stable_for_seconds": 30,
        "ph_stability_threshold": 0.03,
        "ec_stability_threshold": 0.03,
        "control_strategy": "baseline",
    }

    response = receive_sensor_data(SensorPayload(**payload), FakeSensorRepository())
    expected_received = {**payload, "reservoir_volume_liters": 20.0}

    assert response.status == "success"
    assert response.received.model_dump() == expected_received
    assert response.decision.decision == "within_range"
    assert response.decision.pump_activated == "none"
    assert response.decision.dose_ml == 0
    assert response.decision.duration_ms == 0
    assert response.pump_activated == "none"
    assert response.duration_ms == 0
    assert response.mixing_time_ms == 0
    assert response.log_id == 123
    assert response.control_cycle_id == 456


def test_experiment_run_lock_key_is_stable_and_scoped() -> None:
    """Concurrent active-run creation uses one deterministic lock per run scope."""
    key = _experiment_run_lock_key("dev", "HANAS Phase 3 Live Run", "agentic_ai")

    assert key == _experiment_run_lock_key("dev", "HANAS Phase 3 Live Run", "agentic_ai")
    assert key != _experiment_run_lock_key("dev", "HANAS Phase 3 Live Run", "baseline")
    assert -(2**63) <= key < 2**63


def test_dashboard_read_methods_do_not_create_experiment_runs() -> None:
    """Dashboard polling should not create runs or consume sequence IDs."""
    read_methods = [
        "get_recent_sensor_logs",
        "get_sensor_logs_since",
        "expire_stale_batch_pending_commands",
        "has_recent_batch_analysis",
        "get_unresolved_batch_command",
        "get_recent_or_active_pump_command",
        "get_latest_batch_analysis",
    ]

    for method_name in read_methods:
        source = inspect.getsource(getattr(PostgresSensorReadingRepository, method_name))
        assert "_get_or_create_experiment_run" not in source
        if method_name != "get_recent_or_active_pump_command":
            assert "_get_active_experiment_run_id" in source


def test_physical_pump_guard_spans_strategies_and_starts_after_actuation() -> None:
    """Shared pumps stay guarded after completion even when control strategy changes."""
    source = inspect.getsource(PostgresSensorReadingRepository.get_recent_or_active_pump_command)

    assert "system_logs.control_strategy = %s" not in source
    assert "system_logs.experiment_run_id = %s" not in source
    assert "control_cycles.action_completed_at" in source
    assert "system_logs.mixing_effective_seconds" in source


    for method in (
        PostgresSensorReadingRepository.get_recent_or_active_pump_command,
        PostgresSensorReadingRepository.get_pending_human_review_command,
        PostgresSensorReadingRepository.emergency_stop_active_commands,
    ):
        guard_sql = inspect.getsource(method)
        assert "'inter_dose_mixing'" in guard_sql
        assert "'ec_up_b_dosing'" in guard_sql

def test_pending_command_query_blocks_dispatch_during_physical_activity() -> None:
    """Queued commands must remain pending while another cycle is dosing or mixing."""
    source = inspect.getsource(PostgresSensorReadingRepository.get_pending_human_review_command)

    assert "NOT EXISTS" in source
    assert "'human_command_dispatched'" in source
    assert "'dosing'" in source
    assert "'mixing'" in source
    assert "default_mixing_time_seconds" in source


def test_latest_cycle_derives_mixing_for_older_firmware_completion() -> None:
    """An early completion report must still expose the active mixing window."""
    source = inspect.getsource(PostgresSensorReadingRepository.get_latest_control_cycle)

    assert "= 'completed'" in source
    assert "THEN 'mixing'" in source
    assert "action_completed_at" in source
    assert "mixing_effective_seconds" in source


def test_fixed_phase2_volume_accepts_zero_sensor_volume() -> None:
    """Phase 2 fixed volume should override a zero water-level estimate before dosing."""
    payload = {
        "temperature": 28.87,
        "ph": 5.7,
        "ec": 1.303,
        "reservoir_volume_liters": 0,
        "ph_stable_for_seconds": 31,
        "ec_stable_for_seconds": 31,
        "ph_stability_threshold": 0.03,
        "ec_stability_threshold": 0.03,
        "control_strategy": "baseline",
    }

    response = receive_sensor_data(SensorPayload(**payload), FakeSensorRepository())

    assert response.status == "success"
    assert response.received.reservoir_volume_liters == 20.0
    assert response.decision.metadata["volume_source"] == "fixed_backend_config"
    assert response.decision.metadata["received_reservoir_volume_liters"] == 0
    assert response.decision.metadata["control_reservoir_volume_liters"] == 20.0


def test_maintenance_mode_saves_reading_without_pump_command() -> None:
    """Maintenance mode records readings but suppresses automatic dosing."""
    repository = FakeSensorRepository()
    repository.system_settings["maintenance_mode_enabled"] = True
    payload = {
        "temperature": 24.5,
        "ph": 5.1,
        "ec": 1.8,
        "reservoir_volume_liters": 20,
        "control_strategy": "agentic_ai",
    }

    response = receive_sensor_data(SensorPayload(**payload), repository)

    assert response.status == "success"
    assert response.decision.decision == "maintenance_mode"
    assert response.pump_activated == "none"
    assert response.duration_ms == 0
    assert response.mixing_time_ms == 0
    assert response.decision.metadata["triggered_by"] == "maintenance_mode"
    assert response.decision.metadata["suppressed_baseline_decision"]["pump_activated"] == "ph_up"


def test_monitoring_mode_saves_live_reading_without_ai_or_pump_command() -> None:
    """Monitoring Only keeps dashboard data live while suppressing control."""
    repository = FakeSensorRepository()
    repository.system_settings["monitoring_mode_enabled"] = True
    repository.system_settings["full_agentic_mode_enabled"] = True
    payload = {
        "temperature": 24.5,
        "ph": 5.1,
        "ec": 1.8,
        "reservoir_volume_liters": 20,
        "control_strategy": "agentic_ai",
    }

    response = receive_sensor_data(SensorPayload(**payload), repository)

    assert response.status == "success"
    assert response.received.ph == 5.1
    assert response.decision.decision == "monitoring_mode"
    assert response.pump_activated == "none"
    assert response.duration_ms == 0
    assert response.mixing_time_ms == 0
    assert response.decision.metadata["triggered_by"] == "monitoring_mode"
    assert response.decision.metadata["full_agentic_analysis_suppressed"] is True
    assert response.decision.metadata["suppressed_baseline_decision"]["pump_activated"] == "ph_up"


def test_emergency_stop_saves_reading_and_forces_pumps_off() -> None:
    """Emergency stop records readings, clears commands, and suppresses every pump."""
    repository = FakeSensorRepository()
    repository.system_settings["emergency_stop_enabled"] = True
    payload = {
        "temperature": 24.5,
        "ph": 5.1,
        "ec": 1.8,
        "reservoir_volume_liters": 20,
        "control_strategy": "agentic_ai",
    }

    response = receive_sensor_data(SensorPayload(**payload), repository)

    assert response.status == "success"
    assert response.decision.decision == "emergency_stop"
    assert response.pump_activated == "none"
    assert response.duration_ms == 0
    assert response.mixing_time_ms == 0
    assert response.decision.metadata["triggered_by"] == "emergency_stop"
    assert response.decision.metadata["suppressed_baseline_decision"]["pump_activated"] == "ph_up"
    assert repository.emergency_stop_active_commands_calls == 1


def test_runtime_settings_read_global_boolean_values() -> None:
    """Shared runtime settings only trust backend-persisted booleans."""
    repository = FakeSensorRepository()

    assert maintenance_mode_enabled(repository) is False
    assert monitoring_mode_enabled(repository) is False
    assert emergency_stop_enabled(repository) is False
    assert runtime_bool_setting(repository, "sms_enabled", default=True) is True

    repository.system_settings["maintenance_mode_enabled"] = True
    repository.system_settings["monitoring_mode_enabled"] = True
    repository.system_settings["emergency_stop_enabled"] = True
    repository.system_settings["sms_enabled"] = False
    repository.system_settings["hitl_enabled"] = "true"

    assert maintenance_mode_enabled(repository) is True
    assert monitoring_mode_enabled(repository) is True
    assert emergency_stop_enabled(repository) is True
    assert runtime_bool_setting(repository, "sms_enabled", default=True) is False
    assert runtime_bool_setting(repository, "hitl_enabled", default=False) is False


class _FakeConnectionContext:
    """Context manager used for worker tests that patch DB locks."""

    def __enter__(self) -> object:
        return object()

    def __exit__(self, *_args: object) -> bool:
        return False


def test_batch_worker_skips_llm_when_global_maintenance_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Maintenance mode must stop scheduled agent work before any LLM call."""
    repository = FakeSensorRepository()
    repository.system_settings["maintenance_mode_enabled"] = True
    llm_called = False

    def fail_if_llm_is_constructed(*_args: object, **_kwargs: object) -> None:
        nonlocal llm_called
        llm_called = True

    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(batch_scheduler, "get_db_connection", _FakeConnectionContext)
    monkeypatch.setattr(batch_scheduler, "_try_acquire_batch_lock", lambda _conn: True)
    monkeypatch.setattr(batch_scheduler, "_release_batch_lock", lambda _conn: None)
    monkeypatch.setattr(batch_scheduler, "PostgresSensorReadingRepository", lambda _conn: repository)
    monkeypatch.setattr(batch_scheduler, "OpenAIJsonLLM", fail_if_llm_is_constructed)

    assert batch_scheduler.run_batch_cycle() is None
    assert llm_called is False
    assert repository.expire_stale_batch_pending_commands_calls == 1


def test_batch_worker_skips_llm_when_monitoring_mode_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Monitoring Only must prevent scheduled agent analysis and actuation."""
    repository = FakeSensorRepository()
    repository.system_settings["monitoring_mode_enabled"] = True
    llm_called = False

    def fail_if_llm_is_constructed(*_args: object, **_kwargs: object) -> None:
        nonlocal llm_called
        llm_called = True

    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(batch_scheduler, "get_db_connection", _FakeConnectionContext)
    monkeypatch.setattr(batch_scheduler, "_try_acquire_batch_lock", lambda _conn: True)
    monkeypatch.setattr(batch_scheduler, "_release_batch_lock", lambda _conn: None)
    monkeypatch.setattr(batch_scheduler, "PostgresSensorReadingRepository", lambda _conn: repository)
    monkeypatch.setattr(batch_scheduler, "OpenAIJsonLLM", fail_if_llm_is_constructed)

    assert batch_scheduler.run_batch_cycle() is None
    assert llm_called is False
    assert repository.expire_stale_batch_pending_commands_calls == 1


def test_batch_worker_skips_llm_and_stops_commands_when_emergency_stop_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Emergency stop resolves active commands before any scheduled LLM call."""
    repository = FakeSensorRepository()
    repository.system_settings["emergency_stop_enabled"] = True
    llm_called = False

    def fail_if_llm_is_constructed(*_args: object, **_kwargs: object) -> None:
        nonlocal llm_called
        llm_called = True

    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(batch_scheduler, "get_db_connection", _FakeConnectionContext)
    monkeypatch.setattr(batch_scheduler, "_try_acquire_batch_lock", lambda _conn: True)
    monkeypatch.setattr(batch_scheduler, "_release_batch_lock", lambda _conn: None)
    monkeypatch.setattr(batch_scheduler, "PostgresSensorReadingRepository", lambda _conn: repository)
    monkeypatch.setattr(batch_scheduler, "OpenAIJsonLLM", fail_if_llm_is_constructed)

    assert batch_scheduler.run_batch_cycle() is None
    assert llm_called is False
    assert repository.expire_stale_batch_pending_commands_calls == 1
    assert repository.emergency_stop_active_commands_calls == 1


def test_scheduled_summary_skips_generation_when_global_maintenance_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scheduled summaries should not spend LLM tokens while maintenance mode is active."""
    repository = FakeSensorRepository()
    repository.system_settings["maintenance_mode_enabled"] = True
    build_summary_called = False

    def mark_summary_build(*_args: object, **_kwargs: object) -> None:
        nonlocal build_summary_called
        build_summary_called = True

    monkeypatch.setattr(overview_summarizer, "get_db_connection", _FakeConnectionContext)
    monkeypatch.setattr(overview_summarizer, "_try_acquire_summary_lock", lambda _conn: True)
    monkeypatch.setattr(overview_summarizer, "_release_summary_lock", lambda _conn: None)
    monkeypatch.setattr(overview_summarizer, "PostgresSensorReadingRepository", lambda _conn: repository)
    monkeypatch.setattr(overview_summarizer, "_build_summary", mark_summary_build)

    assert overview_summarizer.run_overview_summary_cycle() is None
    assert build_summary_called is False


def test_scheduled_summary_skips_generation_when_emergency_stop_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Emergency stop should not spend LLM tokens on automatic summaries."""
    repository = FakeSensorRepository()
    repository.system_settings["emergency_stop_enabled"] = True
    build_summary_called = False

    def mark_summary_build(*_args: object, **_kwargs: object) -> None:
        nonlocal build_summary_called
        build_summary_called = True

    monkeypatch.setattr(overview_summarizer, "get_db_connection", _FakeConnectionContext)
    monkeypatch.setattr(overview_summarizer, "_try_acquire_summary_lock", lambda _conn: True)
    monkeypatch.setattr(overview_summarizer, "_release_summary_lock", lambda _conn: None)
    monkeypatch.setattr(overview_summarizer, "PostgresSensorReadingRepository", lambda _conn: repository)
    monkeypatch.setattr(overview_summarizer, "_build_summary", mark_summary_build)

    assert overview_summarizer.run_overview_summary_cycle() is None
    assert build_summary_called is False


def test_batch_worker_does_not_create_run_when_no_source_readings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scheduled batch should not create a run just to discover there is no history."""
    repository = FakeSensorRepository()
    active_reference_called = False

    def fail_if_reference_is_loaded(*_args: object, **_kwargs: object) -> ReferenceRange:
        nonlocal active_reference_called
        active_reference_called = True
        return build_reference_range()

    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(batch_scheduler, "get_db_connection", _FakeConnectionContext)
    monkeypatch.setattr(batch_scheduler, "_try_acquire_batch_lock", lambda _conn: True)
    monkeypatch.setattr(batch_scheduler, "_release_batch_lock", lambda _conn: None)
    monkeypatch.setattr(batch_scheduler, "PostgresSensorReadingRepository", lambda _conn: repository)
    monkeypatch.setattr(repository, "get_active_reference_range", fail_if_reference_is_loaded)

    assert batch_scheduler.run_batch_cycle() is None
    assert active_reference_called is False


def test_batch_worker_rejects_impossible_live_reservoir_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stored bad level data must not reach batch dosing math or the LLM."""
    repository = FakeSensorRepository()
    repository.recent_logs = [
        SensorHistoryEntry(
            timestamp=datetime.now(timezone.utc),
            temperature=24.5,
            ph=6.54,
            ec=1.45,
            reservoir_volume_liters=70.01,
        )
    ]
    llm_called = False

    def fail_if_llm_is_constructed(*_args: object, **_kwargs: object) -> None:
        nonlocal llm_called
        llm_called = True

    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "force_fixed_reservoir_volume", False)
    monkeypatch.setattr(settings, "default_reservoir_max_volume_liters", 70.0)
    monkeypatch.setattr(batch_scheduler, "get_db_connection", _FakeConnectionContext)
    monkeypatch.setattr(batch_scheduler, "_try_acquire_batch_lock", lambda _conn: True)
    monkeypatch.setattr(batch_scheduler, "_release_batch_lock", lambda _conn: None)
    monkeypatch.setattr(batch_scheduler, "PostgresSensorReadingRepository", lambda _conn: repository)
    monkeypatch.setattr(batch_scheduler, "build_crop_lifecycle_context", lambda *_args: None)
    monkeypatch.setattr(batch_scheduler, "OpenAIJsonLLM", fail_if_llm_is_constructed)

    assert batch_scheduler.run_batch_cycle() is None
    assert llm_called is False
    assert "reservoir maximum" in batch_scheduler.get_last_batch_skip_message()


def test_batch_worker_rejects_zero_live_reservoir_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stored zero level must not fall back to a configured dosing volume."""
    repository = FakeSensorRepository()
    repository.recent_logs = [
        SensorHistoryEntry(
            timestamp=datetime.now(timezone.utc),
            temperature=24.5,
            ph=6.54,
            ec=1.45,
            reservoir_volume_liters=0,
        )
    ]

    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "force_fixed_reservoir_volume", False)
    monkeypatch.setattr(batch_scheduler, "get_db_connection", _FakeConnectionContext)
    monkeypatch.setattr(batch_scheduler, "_try_acquire_batch_lock", lambda _conn: True)
    monkeypatch.setattr(batch_scheduler, "_release_batch_lock", lambda _conn: None)
    monkeypatch.setattr(batch_scheduler, "PostgresSensorReadingRepository", lambda _conn: repository)
    monkeypatch.setattr(batch_scheduler, "build_crop_lifecycle_context", lambda *_args: None)

    assert batch_scheduler.run_batch_cycle() is None
    assert "not positive" in batch_scheduler.get_last_batch_skip_message()


def test_batch_payload_preserves_latest_stability_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scheduled AI must not lose the evidence needed to correct a stable boundary drift."""
    monkeypatch.setattr(settings, "default_control_strategy", "agentic_ai")
    latest = SensorHistoryEntry(
        timestamp=datetime.now(timezone.utc),
        temperature=26.4,
        ph=6.53,
        ec=1.42,
        reservoir_volume_liters=51.8,
        ph_stable_for_seconds=31,
        ec_stable_for_seconds=34,
        decision="wait_near_boundary",
        pump_activated="none",
    )

    payload = _batch_payload_from_latest(latest, 51.8)

    assert payload.ph_stable_for_seconds == 31
    assert payload.ec_stable_for_seconds == 34
    assert payload.ph_stability_threshold is None
    assert payload.ec_stability_threshold is None
    assert payload.control_strategy == "agentic_ai"

    prior_wait = SensorHistoryEntry(
        timestamp=datetime.now(timezone.utc) - timedelta(minutes=1),
        temperature=26.4,
        ph=6.52,
        ec=1.42,
        reservoir_volume_liters=51.9,
        decision="wait_near_boundary",
        pump_activated="none",
    )
    decision = evaluate_persistent_dosing_decision(
        payload,
        build_reference_range(),
        [prior_wait],
    )

    assert decision.decision == "ph_high"
    assert decision.pump_activated == "ph_down"
    assert decision.metadata["deadband_policy"] == "dose_after_stable_confirmation"


def test_scheduled_batch_pauses_while_full_agentic_mode_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The background batch must not add duplicate LLM calls in per-reading mode."""
    repository = FakeSensorRepository()
    repository.system_settings["full_agentic_mode_enabled"] = True
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(batch_scheduler, "get_db_connection", _FakeConnectionContext)
    monkeypatch.setattr(batch_scheduler, "_try_acquire_batch_lock", lambda _conn: True)
    monkeypatch.setattr(batch_scheduler, "_release_batch_lock", lambda _conn: None)
    monkeypatch.setattr(batch_scheduler, "PostgresSensorReadingRepository", lambda _conn: repository)

    assert batch_scheduler.run_batch_cycle() is None
    assert "Full Agentic Mode" in batch_scheduler.get_last_batch_skip_message()


def test_zero_sensor_volume_is_rejected_without_fixed_volume(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live volume mode still rejects a zero reservoir volume before any dosing math."""
    monkeypatch.setattr(settings, "force_fixed_reservoir_volume", False)
    payload = SensorPayload(temperature=24.5, ph=6.2, ec=1.8, reservoir_volume_liters=0)

    with pytest.raises(Exception) as exc_info:
        _control_payload(payload)

    assert getattr(exc_info.value, "status_code", None) == 422


def test_sensor_volume_above_configured_maximum_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An impossible level reading must fail closed instead of inflating dose volume."""
    monkeypatch.setattr(settings, "force_fixed_reservoir_volume", False)
    monkeypatch.setattr(settings, "default_reservoir_max_volume_liters", 70.0)
    payload = SensorPayload(
        temperature=24.5,
        ph=6.54,
        ec=1.45,
        reservoir_volume_liters=70.01,
    )

    with pytest.raises(Exception) as exc_info:
        _control_payload(payload)

    assert getattr(exc_info.value, "status_code", None) == 422


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("temperature", -10.0),
        ("temperature", 80.0),
        ("ph", -0.01),
        ("ph", 14.01),
        ("ec", -0.01),
        ("ec", 20.01),
        ("ph", float("nan")),
        ("ec", float("inf")),
    ],
)
def test_sensor_payload_rejects_values_outside_firmware_validity_bounds(
    field: str,
    value: float,
) -> None:
    """Backend validation must fail closed on readings firmware considers invalid."""
    values = {
        "temperature": 24.5,
        "ph": 6.2,
        "ec": 1.5,
        "reservoir_volume_liters": 35.0,
    }
    values[field] = value

    with pytest.raises(Exception):
        SensorPayload(**values)


def test_sensor_response_matches_esp32_controller_contract() -> None:
    """The ESP32 receives top-level fields it parses for pump actuation."""
    payload = {
        "temperature": 24.5,
        "ph": 5.3,
        "ec": 1.8,
        "reservoir_volume_liters": 80,
        "ph_stable_for_seconds": 30,
        "ec_stable_for_seconds": 30,
        "ph_stability_threshold": 0.03,
        "ec_stability_threshold": 0.03,
        "control_strategy": "baseline",
    }

    response = receive_sensor_data(SensorPayload(**payload), FakeSensorRepository())
    response_payload = response.model_dump()
    expected_received = {**payload, "reservoir_volume_liters": 20.0}

    assert response_payload["received"] == expected_received
    assert response_payload["pump_activated"] == "ph_up"
    assert response_payload["duration_ms"] == 4959
    assert response_payload["decision"]["metadata"]["target_policy"] == (
        "midpoint_single_correction_capped"
    )
    assert response_payload["mixing_time_ms"] == 120000
    assert response_payload["control_cycle_id"] == 456


def test_baseline_response_warns_repeat_dose_after_no_pump_effect() -> None:
    """Baseline logs delivery concerns but still returns the dosing action."""
    repository = FakeSensorRepository()
    repository.recent_logs = [
        SensorHistoryEntry(
            timestamp=datetime.now(timezone.utc) - timedelta(minutes=4),
            ph=5.0,
            ec=1.8,
            decision="ph_low",
            pump_activated="ph_up",
            dose_ml=10,
            duration_ms=4959,
            status="completed",
        )
    ]
    payload = {
        "temperature": 24.5,
        "ph": 5.0,
        "ec": 1.8,
        "reservoir_volume_liters": 20,
        "ph_stable_for_seconds": 30,
        "ec_stable_for_seconds": 30,
        "ph_stability_threshold": 0.03,
        "ec_stability_threshold": 0.03,
        "control_strategy": "baseline",
    }

    response = receive_sensor_data(SensorPayload(**payload), repository)

    assert response.decision.decision == "ph_low"
    assert response.pump_activated == "ph_up"
    assert response.duration_ms > 0
    assert response.mixing_time_ms > 0
    assert response.decision.metadata["delivery_issue"]["issue_detected"] is True
    assert response.decision.metadata["delivery_issue_policy"] == "warn_only"
    assert "possible_delivery_issue" in response.decision.metadata["risk_flags"]


def test_receive_sensor_data_invokes_sms_alert_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ingestion route checks whether a stored decision should notify the operator."""
    calls: list[tuple[str, int, int]] = []

    def fake_alert(
        payload: SensorPayload,
        decision: DosingDecision,
        *,
        log_id: int,
        control_cycle_id: int,
        notification_log_writer: object | None = None,
    ) -> bool:
        calls.append((decision.decision, log_id, control_cycle_id, notification_log_writer is not None))
        return False

    monkeypatch.setattr(notifications, "maybe_send_decision_alert", fake_alert)
    monkeypatch.setattr("app.api.routes.sensors.maybe_send_decision_alert", fake_alert)
    payload = SensorPayload(
        temperature=24.5,
        ph=6.2,
        ec=1.8,
        reservoir_volume_liters=20,
        control_strategy="baseline",
    )

    receive_sensor_data(payload, FakeSensorRepository())

    assert calls == [("within_range", 123, 456, True)]


def test_full_agentic_mode_runs_llm_pipeline_for_each_sensor_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The opt-in mode replaces per-reading batch collection with the complete agent pipeline."""
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    repository = FakeSensorRepository()
    repository.system_settings["full_agentic_mode_enabled"] = True
    calls = 0

    def fake_agentic_decision(*_args: object, **kwargs: object) -> DosingDecision:
        nonlocal calls
        calls += 1
        assert kwargs.get("llm") is not None
        return DosingDecision(
            decision="within_range",
            pump_activated="none",
            dose_ml=0,
            duration_ms=0,
            ph_within_range=True,
            ec_within_range=True,
            ph_deviation=0,
            ec_deviation=0,
            reason="Full agentic analysis completed.",
            metadata={"reasoning_source": "llm_agents"},
        )

    monkeypatch.setattr("app.api.routes.sensors.evaluate_agentic_decision", fake_agentic_decision)
    payload = SensorPayload(
        temperature=24.5,
        ph=6.1,
        ec=1.6,
        reservoir_volume_liters=20,
        control_strategy="baseline",
    )

    response = receive_sensor_data(payload, repository)

    assert calls == 1
    assert response.decision.metadata["agentic_mode"] == "full_agentic_per_reading"
    assert response.decision.metadata["triggered_by"] == "sensor_full_agentic"
    assert response.received.control_strategy == "agentic_ai"
    assert repository.reference_strategy == "agentic_ai"
    assert repository.agentic_cycle_lock_releases == 1
    assert repository.events[-2:] == ["decision_persisted", "agentic_lock_released"]


def test_full_agentic_recovery_returns_zero_actuation_to_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run the real graph through the sensor route with an LLM recovery result."""
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    repository = FakeSensorRepository()
    repository.system_settings["full_agentic_mode_enabled"] = True
    repository.recent_logs = [
        SensorHistoryEntry(
            timestamp=datetime.now(timezone.utc) - timedelta(minutes=4 - index),
            ph=ph, ec=1.8, decision="ph_low", pump_activated="none",
        )
        for index, ph in enumerate([5.1, 5.2, 5.19, 5.25])
    ]
    calls = []

    class RecoveryPipelineLLM:
        def __init__(self, *_args, **_kwargs):
            pass

        def complete_json(self, agent_name, system_prompt, context, output_model):
            calls.append(output_model)
            if output_model is OrchestratorResult:
                output = OrchestratorResult(
                    route="monitoring_agent", action="proceed", confidence=0.9,
                    reason="Monitoring should assess sensor history.",
                )
            elif output_model is MonitoringResult:
                output = MonitoringResult(
                    status="deviation", is_stable=True, is_recovering=True,
                        recovery_assessment={"ph_trend": "toward_range", "ec_trend": "in_range", "history_is_fresh": True, "sufficient_observations": True, "executed_dose_in_window": False},
                    summary="pH is moving toward range over four minutes without dosing.",
                )
            else:
                raise AssertionError("Recovery should stop before deeper agents")
            return LLMCompletionResult(output=output, model="test-model")

    monkeypatch.setattr("app.api.routes.sensors.OpenAIJsonLLM", RecoveryPipelineLLM)
    response = receive_sensor_data(
        SensorPayload(
            temperature=24.5, ph=5.3, ec=1.8, reservoir_volume_liters=20,
            ph_stable_for_seconds=30, ec_stable_for_seconds=30,
            control_strategy="agentic_ai",
        ),
        repository,
    )

    assert calls == [OrchestratorResult, MonitoringResult]
    assert response.decision.decision == "wait_natural_recovery"
    assert response.pump_activated == response.decision.pump_activated == "none"
    assert response.duration_ms == response.decision.duration_ms == 0
    assert response.decision.dose_ml == 0
    assert response.mixing_time_ms == 0
    assert _status_for_decision(response.decision) == "waiting"
    assert response.decision.metadata["monitoring_agent"]["is_recovering"] is True
    assert repository.events[-2:] == ["decision_persisted", "agentic_lock_released"]


def test_full_agentic_mode_releases_lock_when_persistence_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database error must not leave the cross-worker agentic lock held."""
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    repository = FakeSensorRepository()
    repository.system_settings["full_agentic_mode_enabled"] = True

    monkeypatch.setattr(
        "app.api.routes.sensors.evaluate_agentic_decision",
        lambda *_args, **_kwargs: DosingDecision(
            decision="within_range",
            pump_activated="none",
            dose_ml=0,
            duration_ms=0,
            ph_within_range=True,
            ec_within_range=True,
            ph_deviation=0,
            ec_deviation=0,
            reason="Full agentic analysis completed.",
            metadata={},
        ),
    )

    def fail_to_persist(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(repository, "create_sensor_log", fail_to_persist)

    with pytest.raises(RuntimeError, match="database unavailable"):
        receive_sensor_data(
            SensorPayload(
                temperature=24.5,
                ph=6.1,
                ec=1.6,
                reservoir_volume_liters=20,
                control_strategy="agentic_ai",
            ),
            repository,
        )

    assert repository.agentic_cycle_lock_releases == 1


def test_full_agentic_mode_fails_closed_when_another_cycle_is_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent uploads never fall back to deterministic dosing or overlap LLM cycles."""
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    repository = FakeSensorRepository()
    repository.system_settings["full_agentic_mode_enabled"] = True
    repository.agentic_cycle_lock_available = False
    payload = SensorPayload(
        temperature=24.5,
        ph=7.1,
        ec=1.6,
        reservoir_volume_liters=20,
        control_strategy="agentic_ai",
    )

    response = receive_sensor_data(payload, repository)

    assert response.pump_activated == "none"
    assert response.duration_ms == 0
    assert response.decision.decision == "full_agentic_busy"
    assert response.decision.metadata["actuation_source"] == "full_agentic_fail_closed"


def test_human_review_gate_holds_agentic_dosing(monkeypatch: pytest.MonkeyPatch) -> None:
    """HITL mode should turn an agentic dosing command into a review wait."""
    monkeypatch.setattr(settings, "human_in_the_loop_enabled", True)
    payload = SensorPayload(temperature=24.5, ph=5.3, ec=1.8, reservoir_volume_liters=20)
    decision = DosingDecision(
        decision="ph_low",
        pump_activated="ph_up",
        dose_ml=4,
        duration_ms=1983,
        ph_within_range=False,
        ec_within_range=True,
        ph_deviation=0.2,
        ec_deviation=0,
        reason="Agentic AI selected pH up.",
        metadata={"strategy": "agentic_ai"},
    )

    gated = _apply_human_review_gate(payload, decision, "agentic_ai")

    assert gated.decision == "wait_human_review"
    assert gated.pump_activated == "none"
    assert gated.dose_ml == 0
    assert gated.metadata["human_in_the_loop"]["pending_decision"]["pump_activated"] == "ph_up"
    assert gated.metadata["human_review_gate"]["held_for_operator"] is True
    assert gated.metadata["human_review_gate"]["next_stage_if_approved"] == "esp32_execution"
    assert "human_review_required" in gated.metadata["risk_flags"]


def test_human_review_gate_ignores_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    """HITL mode should only hold agentic AI dosing commands."""
    monkeypatch.setattr(settings, "human_in_the_loop_enabled", True)
    payload = SensorPayload(temperature=24.5, ph=5.3, ec=1.8, reservoir_volume_liters=20)
    decision = DosingDecision(
        decision="ph_low",
        pump_activated="ph_up",
        dose_ml=4,
        duration_ms=1983,
        ph_within_range=False,
        ec_within_range=True,
        ph_deviation=0.2,
        ec_deviation=0,
        reason="Baseline selected pH up.",
        metadata={"strategy": "baseline"},
    )

    gated = _apply_human_review_gate(payload, decision, "baseline")

    assert gated is decision


def test_baseline_ignores_too_short_prior_dose_as_delivery_failure() -> None:
    """A sub-reliable prior pump pulse should not block the next real correction."""
    repository = FakeSensorRepository()
    repository.recent_logs = [
        SensorHistoryEntry(
            timestamp=datetime.now(timezone.utc) - timedelta(minutes=4),
            ph=5.84,
            ec=1.197,
            decision="ec_low",
            pump_activated="ec_up",
            dose_ml=1.04,
            duration_ms=500,
            status="completed",
        )
    ]
    payload = {
        "temperature": 31.31,
        "ph": 5.84,
        "ec": 0.89,
        "reservoir_volume_liters": 0,
        "ph_stable_for_seconds": 31,
        "ec_stable_for_seconds": 31,
        "ph_stability_threshold": 0.03,
        "ec_stability_threshold": 0.03,
        "control_strategy": "baseline",
    }

    response = receive_sensor_data(SensorPayload(**payload), repository)

    assert response.decision.decision == "ec_low"
    assert response.pump_activated == "ec_up"
    assert response.decision.dose_ml == 63.19
    assert response.duration_ms == 30331
    assert response.decision.metadata["target_ec_midpoint"] == 1.6
    assert response.decision.metadata["target_policy"] == "midpoint_single_correction_capped"
    assert "delivery_issue" not in response.decision.metadata


def test_agentic_strategy_executes_history_aware_branch() -> None:
    """The first no-history agentic reading waits for confirmation safely."""
    repository = FakeSensorRepository()
    payload = {
        "temperature": 24.5,
        "ph": 5.3,
        "ec": 1.8,
        "reservoir_volume_liters": 80,
        "ph_stable_for_seconds": 30,
        "ec_stable_for_seconds": 30,
        "control_strategy": "agentic_ai",
    }

    response = receive_sensor_data(SensorPayload(**payload), repository)

    assert response.status == "success"
    assert repository.reference_strategy == "agentic_ai"
    assert response.decision.decision == "wait_initial_confirmation"
    assert response.decision.pump_activated == "none"
    assert response.decision.dose_ml == 0
    assert response.decision.metadata["executed_strategy"] == "agentic_ai"
    assert response.decision.metadata["baseline_shadow"]["dose_ml"] == 10.0
    assert response.decision.metadata["baseline_shadow"]["target_policy"] == (
        "midpoint_single_correction_capped"
    )
    assert response.decision.metadata["volume_source"] == "fixed_backend_config"
    assert response.decision.metadata["received_reservoir_volume_liters"] == 80
    assert response.decision.metadata["control_reservoir_volume_liters"] == 20.0
    assert repository.recent_log_limits == [settings.agentic_control_history_limit]


def test_no_pump_wait_decisions_keep_meaningful_log_status() -> None:
    """No-pump agentic wait states should not be mislabeled as within_range."""
    decision = DosingDecision(
        decision="wait_for_mixing",
        pump_activated="none",
        dose_ml=0,
        duration_ms=0,
        ph_within_range=False,
        ec_within_range=True,
        ph_deviation=0.2,
        ec_deviation=0,
        reason="A recent dose is still inside the configured mixing window.",
    )

    assert _status_for_decision(decision) == "mixing"


def test_phase3_emergency_guard_wait_keeps_mixing_log_status() -> None:
    """Emergency guard waits should show as mixing, not generic no_action."""
    decision = DosingDecision(
        decision="ph_high",
        pump_activated="none",
        dose_ml=0,
        duration_ms=0,
        ph_within_range=False,
        ec_within_range=True,
        ph_deviation=0.39,
        ec_deviation=0,
        reason="Previous correction is still mixing.",
        metadata={"triggered_by": "emergency_guard_mixing_wait"},
    )

    assert _status_for_decision(decision) == "mixing"


def test_near_boundary_wait_keeps_waiting_log_status() -> None:
    """A near-boundary hold should be logged as waiting, not a generic no-action."""
    decision = DosingDecision(
        decision="wait_near_boundary",
        pump_activated="none",
        dose_ml=0,
        duration_ms=0,
        ph_within_range=False,
        ec_within_range=True,
        ph_deviation=0.01,
        ec_deviation=0,
        reason="Waiting for a matching follow-up reading near the boundary.",
    )

    assert _status_for_decision(decision) == "waiting"


def test_response_mixing_time_uses_agentic_effective_window() -> None:
    """Agentic dose plans expose their bounded mixing window to the ESP32."""
    decision = DosingDecision(
        decision="ph_low",
        pump_activated="ph_up",
        dose_ml=6,
        duration_ms=2975,
        ph_within_range=False,
        ec_within_range=True,
        ph_deviation=0.2,
        ec_deviation=0,
        reason="Dose cautiously.",
        metadata={"mixing_window": {"effective_seconds": 300}},
    )

    assert _mixing_time_ms_for_decision(decision) == 300000


def test_routes_are_registered_with_expected_prefixes() -> None:
    """The app exposes health at root and sensor ingestion under /api."""
    paths = {route.path for route in app.routes}

    assert "/" in paths
    assert "/api/sensor-data" in paths
    assert "/api/control-cycles/{control_cycle_id}/action-started" in paths
    assert "/api/control-cycles/{control_cycle_id}/complete" in paths
    assert "/sensor-data" not in paths


def test_openapi_sensor_payload_has_realistic_example() -> None:
    """Swagger shows a usable sensor payload instead of generic strings."""
    schema = app.openapi()["components"]["schemas"]["SensorPayload"]

    assert schema["examples"][0]["control_strategy"] == "agentic_ai"
    assert schema["examples"][0]["ph"] == 6.2
    assert schema["examples"][0]["ec"] == 1.8
    assert schema["properties"]["control_strategy"]["anyOf"][0]["enum"] == [
        "baseline",
        "agentic_ai",
    ]


def test_operator_token_allows_local_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local development can use operator routes without a token."""
    monkeypatch.setattr(settings, "app_env", "dev")
    monkeypatch.setattr(settings, "operator_api_token", "")

    verify_operator_token(None)


def test_operator_token_fails_closed_when_unset_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing production operator secret must not disable write authentication."""
    monkeypatch.setattr(settings, "app_env", "prod")
    monkeypatch.setattr(settings, "operator_api_token", "")

    with pytest.raises(Exception) as exc_info:
        verify_operator_token(None)

    assert getattr(exc_info.value, "status_code", None) == 503


def test_device_token_fails_closed_when_unset_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing production device secret must not expose ingestion or actuation."""
    monkeypatch.setattr(settings, "app_env", "prod")
    monkeypatch.setattr(settings, "device_api_token", "")

    with pytest.raises(Exception) as exc_info:
        verify_device_token(None)

    assert getattr(exc_info.value, "status_code", None) == 503


def test_operator_token_rejects_wrong_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production operator routes reject missing or mismatched tokens."""
    monkeypatch.setattr(settings, "operator_api_token", "expected-token")

    with pytest.raises(Exception) as exc_info:
        verify_operator_token("wrong-token")

    assert getattr(exc_info.value, "status_code", None) == 401


def test_operator_token_accepts_matching_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production operator routes accept the configured dashboard token."""
    monkeypatch.setattr(settings, "operator_api_token", "expected-token")

    verify_operator_token("expected-token")


def test_system_settings_include_default_crop_lifecycle() -> None:
    """Settings summary exposes configurable crop lifecycle defaults."""
    response = get_system_settings(FakeSensorRepository())

    assert response.crop_type == "Lettuce"
    assert response.emergency_stop_enabled is False
    assert response.monitoring_mode_enabled is False
    assert response.full_agentic_mode_enabled is False
    assert response.crop_transplant_date is None
    assert response.crop_harvest_start_day == 30
    assert response.crop_harvest_end_day == 35


def test_experiment_preflight_continues_the_active_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Continuing records an explicit acknowledgement without deleting history."""
    monkeypatch.setattr(settings, "default_control_strategy", "agentic_ai")
    repository = ExperimentLifecycleRepository()

    before = get_system_settings(repository)
    response = confirm_experiment_preflight(
        ExperimentPreflightRequest(action="continue", active_run_id=41),
        repository,
        None,
    )

    assert before.experiment_preflight_required is True
    assert response.experiment_preflight_required is False
    assert response.active_experiment_run is not None
    assert response.active_experiment_run.id == 41
    confirmation = repository.system_settings["experiment_preflight_confirmed_run_id"]
    assert isinstance(confirmation, dict)
    assert confirmation["run_id"] == 41


def test_experiment_preflight_starts_a_clean_successor_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Starting new preserves the previous run and acknowledges the clean successor."""
    monkeypatch.setattr(settings, "default_control_strategy", "agentic_ai")
    repository = ExperimentLifecycleRepository()

    response = confirm_experiment_preflight(
        ExperimentPreflightRequest(action="start_new", active_run_id=41),
        repository,
        None,
    )

    assert response.experiment_preflight_required is False
    assert response.active_experiment_run is not None
    assert response.active_experiment_run.id == 42
    assert response.active_experiment_run.cycle_count == 0
    assert repository.agentic_cycle_lock_releases == 1


def test_enabling_full_agentic_requires_a_fresh_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every disabled-to-enabled Full Agentic transition expires the prior choice."""
    monkeypatch.setattr(settings, "default_control_strategy", "agentic_ai")
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    repository = ExperimentLifecycleRepository()
    repository.system_settings["experiment_preflight_confirmed_run_id"] = {
        "run_id": 41,
        "confirmed_at": datetime.now(timezone.utc).isoformat(),
    }
    repository.system_settings["full_agentic_mode_enabled"] = False

    response = update_system_settings(
        SystemSettingsUpdate(full_agentic_mode_enabled=True),
        repository,
        None,
    )

    assert response.full_agentic_mode_enabled is True
    assert response.experiment_preflight_required is True
    assert repository.system_settings["experiment_preflight_blocked_run_id"] == 41
    assert repository.setting_writes[:2] == [
        ("experiment_preflight_blocked_run_id", 41),
        ("full_agentic_mode_enabled", True),
    ]


def test_monitoring_pause_requires_preflight_when_agentic_control_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Monitoring does not discard readings, but it expires authorization to reuse history."""
    monkeypatch.setattr(settings, "default_control_strategy", "agentic_ai")
    repository = ExperimentLifecycleRepository()
    repository.system_settings["full_agentic_mode_enabled"] = True
    repository.system_settings["experiment_preflight_confirmed_run_id"] = {
        "run_id": 41,
        "confirmed_at": datetime.now(timezone.utc).isoformat(),
    }

    paused = update_system_settings(
        SystemSettingsUpdate(monitoring_mode_enabled=True),
        repository,
        None,
    )
    resumed = update_system_settings(
        SystemSettingsUpdate(monitoring_mode_enabled=False),
        repository,
        None,
    )

    assert paused.monitoring_mode_enabled is True
    assert paused.experiment_preflight_required is True
    assert resumed.monitoring_mode_enabled is False
    assert resumed.experiment_preflight_required is True


def test_experiment_preflight_blocks_new_run_during_mixing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An experiment boundary cannot bypass the shared physical mixing guard."""
    monkeypatch.setattr(settings, "default_control_strategy", "agentic_ai")
    repository = ExperimentLifecycleRepository()
    repository.recent_or_active_pump_command = {"status": "mixing"}

    with pytest.raises(HTTPException, match="protected mixing window") as exc_info:
        confirm_experiment_preflight(
            ExperimentPreflightRequest(action="start_new", active_run_id=41),
            repository,
            None,
        )

    assert exc_info.value.status_code == 409
    assert repository.agentic_cycle_lock_releases == 1


def test_experiment_preflight_blocks_new_run_during_ai_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run boundary cannot race an LLM decision that has not been persisted yet."""
    monkeypatch.setattr(settings, "default_control_strategy", "agentic_ai")
    repository = ExperimentLifecycleRepository()
    repository.agentic_cycle_lock_available = False

    with pytest.raises(HTTPException, match="AI control cycle") as exc_info:
        confirm_experiment_preflight(
            ExperimentPreflightRequest(action="start_new", active_run_id=41),
            repository,
            None,
        )

    assert exc_info.value.status_code == 409
    assert repository.active_run["id"] == 41
    assert repository.agentic_cycle_lock_releases == 0


def test_experiment_preflight_rejects_a_stale_duplicate_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delayed double-click cannot create another successor from a changed run."""
    monkeypatch.setattr(settings, "default_control_strategy", "agentic_ai")
    repository = ExperimentLifecycleRepository()
    repository.start_new_experiment_run("agentic_ai")

    with pytest.raises(HTTPException, match="active experiment changed") as exc_info:
        confirm_experiment_preflight(
            ExperimentPreflightRequest(action="start_new", active_run_id=41),
            repository,
            None,
        )

    assert exc_info.value.status_code == 409
    assert repository.active_run["id"] == 42


def test_experiment_preflight_latches_after_sensor_inactivity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A held reading cannot silently clear the gate after a long sensor interruption."""
    monkeypatch.setattr(settings, "default_control_strategy", "agentic_ai")
    repository = ExperimentLifecycleRepository()
    now = datetime.now(timezone.utc)
    repository.active_run["last_activity_at"] = now - timedelta(hours=1)
    repository.system_settings["experiment_preflight_confirmed_run_id"] = {
        "run_id": 41,
        "confirmed_at": (now - timedelta(hours=2)).isoformat(),
    }

    assert experiment_preflight_required(repository, "agentic_ai") is True
    assert repository.system_settings["experiment_preflight_blocked_run_id"] == 41

    repository.active_run["last_activity_at"] = now
    assert experiment_preflight_required(repository, "agentic_ai") is True

    waiting_repository = ExperimentLifecycleRepository()
    waiting_repository.active_run["last_activity_at"] = now - timedelta(hours=2)
    waiting_repository.system_settings["experiment_preflight_confirmed_run_id"] = {
        "run_id": 41,
        "confirmed_at": (now - timedelta(hours=1)).isoformat(),
    }
    assert experiment_preflight_required(waiting_repository, "agentic_ai") is True


def test_unconfirmed_experiment_fails_closed_before_agentic_analysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live readings remain auditable while an unconfirmed run cannot actuate pumps."""
    monkeypatch.setattr(settings, "default_control_strategy", "agentic_ai")
    repository = ExperimentLifecycleRepository()
    repository.system_settings["full_agentic_mode_enabled"] = True
    calls = 0

    def unexpected_agentic_call(*_args: object, **_kwargs: object) -> DosingDecision:
        nonlocal calls
        calls += 1
        raise AssertionError("agentic analysis must remain behind preflight")

    monkeypatch.setattr("app.api.routes.sensors.evaluate_agentic_decision", unexpected_agentic_call)
    response = receive_sensor_data(
        SensorPayload(
            temperature=24.5,
            ph=5.8,
            ec=1.03,
            reservoir_volume_liters=43.9,
            control_strategy="agentic_ai",
        ),
        repository,
    )

    assert calls == 0
    assert response.pump_activated == "none"
    assert response.duration_ms == 0
    assert response.decision.decision == "experiment_preflight_required"
    assert response.decision.metadata["experiment_preflight_required"] is True


def test_system_settings_update_persists_crop_lifecycle() -> None:
    """Runtime settings can store transplant date and harvest window."""
    repository = FakeSensorRepository()

    response = update_system_settings(
        SystemSettingsUpdate(
            crop_variety="Romaine batch A",
            crop_transplant_date="2026-06-01",
            crop_harvest_start_day=30,
            crop_harvest_end_day=35,
        ),
        repository,
        None,
    )

    assert response.crop_variety == "Romaine batch A"
    assert response.crop_transplant_date.isoformat() == "2026-06-01"
    assert repository.system_settings["crop_transplant_date"] == "2026-06-01"


def test_system_settings_update_enables_emergency_stop_and_resolves_commands() -> None:
    """The operator settings route must engage the global emergency lockout."""
    repository = FakeSensorRepository()

    response = update_system_settings(
        SystemSettingsUpdate(emergency_stop_enabled=True),
        repository,
        None,
    )

    assert response.emergency_stop_enabled is True
    assert repository.system_settings["emergency_stop_enabled"] is True
    assert repository.emergency_stop_active_commands_calls == 1


def test_system_settings_update_enables_monitoring_mode() -> None:
    """The operator can pause AI and pumps without stopping live ingestion."""
    repository = FakeSensorRepository()

    response = update_system_settings(
        SystemSettingsUpdate(monitoring_mode_enabled=True),
        repository,
        None,
    )

    assert response.monitoring_mode_enabled is True
    assert repository.system_settings["monitoring_mode_enabled"] is True
    assert repository.cancel_pending_commands_calls == [
        (settings.default_control_strategy, "monitoring_cancelled")
    ]


def test_enabling_monitoring_clears_other_safety_modes() -> None:
    repository = FakeSensorRepository()
    repository.system_settings["maintenance_mode_enabled"] = True
    repository.system_settings["emergency_stop_enabled"] = True

    response = update_system_settings(
        SystemSettingsUpdate(monitoring_mode_enabled=True),
        repository,
        None,
    )

    assert response.monitoring_mode_enabled is True
    assert response.maintenance_mode_enabled is False
    assert response.emergency_stop_enabled is False


def test_exclusive_safety_mode_repository_uses_one_commit() -> None:
    """A direct mode switch must not expose an all-disabled safety window."""
    source = inspect.getsource(PostgresSensorReadingRepository.set_exclusive_safety_mode)

    assert source.count("self._connection.commit()") == 1
    assert "key == enabled_key" in source


def test_settings_reject_enabling_multiple_safety_modes() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        SystemSettingsUpdate(
            monitoring_mode_enabled=True,
            emergency_stop_enabled=True,
        )


def test_system_settings_update_cancels_stale_commands_before_resuming_control() -> None:
    """Commands queued during a mode transition cannot execute after monitoring ends."""
    repository = FakeSensorRepository()
    repository.system_settings["monitoring_mode_enabled"] = True

    response = update_system_settings(
        SystemSettingsUpdate(monitoring_mode_enabled=False),
        repository,
        None,
    )

    assert response.monitoring_mode_enabled is False
    assert repository.cancel_pending_commands_calls == [
        (settings.default_control_strategy, "monitoring_cancelled")
    ]


def test_monitoring_command_cancellation_only_targets_undelivered_states() -> None:
    """Monitoring transitions must not relabel a pump that is already physically active."""
    source = inspect.getsource(PostgresSensorReadingRepository.cancel_pending_commands)

    assert "human_approved_pending_execution" in source
    assert "human_override_pending_execution" in source
    assert "batch_pending" in source
    assert "batch_dispatched" not in source
    assert "human_command_dispatched" not in source
    assert "'dosing'" not in source
    assert "'mixing'" not in source


def test_system_settings_update_enables_full_agentic_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full Agentic Mode is persisted only when the required backend configuration exists."""
    monkeypatch.setattr(settings, "default_control_strategy", "agentic_ai")
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    repository = FakeSensorRepository()

    response = update_system_settings(
        SystemSettingsUpdate(full_agentic_mode_enabled=True),
        repository,
        None,
    )

    assert response.full_agentic_mode_enabled is True
    assert repository.system_settings["full_agentic_mode_enabled"] is True


def test_system_settings_rejects_full_agentic_mode_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator cannot enable a token-consuming mode without an OpenAI key."""
    monkeypatch.setattr(settings, "default_control_strategy", "agentic_ai")
    monkeypatch.setattr(settings, "openai_api_key", "")

    with pytest.raises(Exception) as exc_info:
        update_system_settings(
            SystemSettingsUpdate(full_agentic_mode_enabled=True),
            FakeSensorRepository(),
            None,
        )

    assert getattr(exc_info.value, "status_code", None) == 409


def test_system_settings_update_rejects_future_transplant_date() -> None:
    """Crop lifecycle context must be based on an actual transplant date."""
    future_date = datetime.now(timezone(timedelta(hours=8))).date() + timedelta(days=1)

    with pytest.raises(ValueError):
        SystemSettingsUpdate(crop_transplant_date=future_date)


def test_system_settings_transplant_date_uses_configured_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lifecycle validator follows the app timezone, not the server locale."""
    monkeypatch.setattr(settings, "app_timezone", "Asia/Manila")
    today_in_app_timezone = datetime.now(timezone(timedelta(hours=8))).date()

    response = SystemSettingsUpdate(crop_transplant_date=today_in_app_timezone)

    assert response.crop_transplant_date == today_in_app_timezone


def test_crop_lifecycle_context_ignores_stale_future_transplant_date() -> None:
    """Previously saved future dates should not be treated as transplant day."""
    repository = FakeSensorRepository()
    repository.system_settings["crop_transplant_date"] = (date.today() + timedelta(days=7)).isoformat()

    context = build_crop_lifecycle_context(repository, build_reference_range())

    assert context["age_days"] is None
    assert context["stage"] == "not_configured"
    assert "future" in context["source_note"]


def test_mark_control_cycle_action_started() -> None:
    """The control-cycle route marks ESP32 actuation start."""
    payload = ControlCycleActionStartedPayload(status="dosing")

    response = mark_control_cycle_action_started(456, payload, FakeSensorRepository())

    assert response.status == "dosing"
    assert response.control_cycle_id == 456


def test_mark_control_cycle_action_completed_enters_mixing() -> None:
    """Pump shutdown should enter mixing without completing the whole cycle."""
    payload = ControlCycleActionCompletedPayload(status="mixing")

    response = mark_control_cycle_action_completed(456, payload, FakeSensorRepository())

    assert response.status == "mixing"
    assert response.control_cycle_id == 456


def test_pending_command_returns_idle_during_emergency_stop() -> None:
    """ESP32 must not receive pump commands while emergency stop is enabled."""
    repository = FakeSensorRepository()
    repository.system_settings["emergency_stop_enabled"] = True

    response = get_pending_command(repository)

    assert response.has_command is False
    assert response.status == "emergency_stop"
    assert repository.emergency_stop_active_commands_calls == 1


def test_pending_command_returns_idle_during_monitoring_mode() -> None:
    """ESP32 must not receive an older queued command during Monitoring Only."""
    repository = FakeSensorRepository()
    repository.system_settings["monitoring_mode_enabled"] = True

    response = get_pending_command(repository)

    assert response.has_command is False
    assert response.status == "monitoring_mode"


def test_action_start_is_rejected_during_monitoring_mode() -> None:
    """A command dispatched just before the mode change must fail before pump start."""
    repository = FakeSensorRepository()
    repository.system_settings["monitoring_mode_enabled"] = True

    with pytest.raises(HTTPException) as exc_info:
        mark_control_cycle_action_started(
            456,
            ControlCycleActionStartedPayload(status="dosing"),
            repository,
        )

    assert exc_info.value.status_code == 409
    assert "Monitoring Only" in str(exc_info.value.detail)


def test_complete_control_cycle() -> None:
    """The control-cycle route marks a cycle complete."""
    payload = ControlCycleCompletionPayload(status="completed")

    response = complete_control_cycle(456, payload, FakeSensorRepository())

    assert response.status == "completed"
    assert response.control_cycle_id == 456


def test_apply_human_review_route() -> None:
    """The control-cycle route applies an operator HITL decision."""
    payload = HumanReviewPayload(action="approve", reviewer="researcher", reason="Looks safe.")

    response = apply_human_review(456, payload, FakeSensorRepository())

    assert response.status == "human_approved_pending_execution"
    assert response.pump_activated == "ph_up"
    assert response.duration_ms == 1983


def test_human_review_is_blocked_during_monitoring_mode() -> None:
    """An operator cannot queue a pump command while Monitoring Only is active."""
    repository = FakeSensorRepository()
    repository.system_settings["monitoring_mode_enabled"] = True

    with pytest.raises(HTTPException) as exc_info:
        apply_human_review(
            456,
            HumanReviewPayload(action="approve", reviewer="researcher", reason="Looks safe."),
            repository,
        )

    assert exc_info.value.status_code == 409
    assert "Monitoring Only" in str(exc_info.value.detail)


def test_get_pending_command_route() -> None:
    """The ESP32 can fetch a reviewed HITL command after approval/override."""
    response = get_pending_command(FakeSensorRepository())

    assert response.has_command is True
    assert response.control_cycle_id == 456
    assert response.pump_activated == "ph_up"
    assert response.duration_ms == 1983


# ── Phase 3 tests ──────────────────────────────────────────────────────────────


def test_phase3_batch_mode_collection_no_pump_for_mild_deviation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 3: mild deviation (within mild disturbance band) is saved for LLM batch, pump stays off."""
    monkeypatch.setattr(settings, "batch_analysis_enabled", True)
    monkeypatch.setattr(settings, "force_fixed_reservoir_volume", False)
    # pH 6.60 is in mild disturbance zone (6.51–6.70), below moderate threshold 6.70
    payload = SensorPayload(
        temperature=24.5,
        ph=6.60,
        ec=1.6,
        reservoir_volume_liters=70.0,
        control_strategy="agentic_ai",
    )

    response = receive_sensor_data(payload, FakeSensorRepository())

    assert response.pump_activated == "none"
    assert response.duration_ms == 0
    assert response.decision.metadata["triggered_by"] == "batch_mode_collection"
    assert response.decision.metadata["executed_strategy"] == "agentic_ai"
    assert "dose_ml" not in response.decision.metadata
    assert "duration_ms" not in response.decision.metadata
    assert "dose_cap_hit" not in response.decision.metadata


def test_phase3_emergency_guard_fires_at_moderate_ph_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 3: pH in moderate disturbance zone (5.00–5.29) triggers immediate dose."""
    monkeypatch.setattr(settings, "batch_analysis_enabled", True)
    monkeypatch.setattr(settings, "force_fixed_reservoir_volume", False)
    # pH 5.25 is in moderate disturbance zone → emergency fires
    payload = SensorPayload(
        temperature=24.5,
        ph=5.25,
        ec=1.6,
        reservoir_volume_liters=70.0,
        control_strategy="agentic_ai",
    )

    response = receive_sensor_data(payload, FakeSensorRepository())

    assert response.pump_activated == "ph_up"
    assert response.duration_ms > 0
    assert response.decision.metadata["triggered_by"] == "emergency_guard"
    assert response.decision.metadata["executed_strategy"] == "agentic_ai"


def test_phase3_emergency_guard_fires_at_moderate_ph_high(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 3: pH in moderate disturbance zone (6.71–7.00) triggers immediate dose."""
    monkeypatch.setattr(settings, "batch_analysis_enabled", True)
    monkeypatch.setattr(settings, "force_fixed_reservoir_volume", False)
    monkeypatch.setattr(settings, "default_mixing_time_seconds", 300)
    # pH 6.80 is in moderate disturbance zone → emergency fires
    payload = SensorPayload(
        temperature=24.5,
        ph=6.80,
        ec=1.6,
        reservoir_volume_liters=70.0,
        control_strategy="agentic_ai",
    )

    response = receive_sensor_data(payload, FakeSensorRepository())

    assert response.pump_activated == "ph_down"
    assert response.duration_ms > 0
    assert response.mixing_time_ms == 300000
    assert response.decision.metadata["triggered_by"] == "emergency_guard"
    assert response.decision.metadata["mixing_window"]["source"] == "phase3_deterministic_guard"
    assert response.decision.metadata["mixing_window"]["effective_seconds"] == 300
    assert response.decision.metadata["executed_strategy"] == "agentic_ai"


def test_phase3_emergency_guard_fires_at_moderate_ec_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 3: EC at or beyond the moderate low boundary triggers immediate dose."""
    monkeypatch.setattr(settings, "batch_analysis_enabled", True)
    monkeypatch.setattr(settings, "force_fixed_reservoir_volume", False)
    payload = SensorPayload(
        temperature=24.5,
        ph=6.0,
        ec=1.0,
        reservoir_volume_liters=70.0,
        control_strategy="agentic_ai",
    )

    response = receive_sensor_data(payload, FakeSensorRepository())

    assert response.pump_activated == "ec_up"
    assert response.duration_ms > 0
    assert response.decision.metadata["triggered_by"] == "emergency_guard"


def test_phase3_emergency_guard_fires_at_moderate_ec_high(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 3: EC at or beyond the moderate high boundary triggers immediate dose."""
    monkeypatch.setattr(settings, "batch_analysis_enabled", True)
    monkeypatch.setattr(settings, "force_fixed_reservoir_volume", False)
    payload = SensorPayload(
        temperature=24.5,
        ph=6.0,
        ec=2.2,
        reservoir_volume_liters=70.0,
        control_strategy="agentic_ai",
    )

    response = receive_sensor_data(payload, FakeSensorRepository())

    assert response.pump_activated == "ec_down"
    assert response.duration_ms > 0
    assert response.decision.metadata["triggered_by"] == "emergency_guard"


def test_phase3_mild_ec_deviation_saved_for_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 3: mild EC deviation is stored only; the batch agent handles it later."""
    monkeypatch.setattr(settings, "batch_analysis_enabled", True)
    monkeypatch.setattr(settings, "force_fixed_reservoir_volume", False)
    payload = SensorPayload(
        temperature=24.5,
        ph=6.0,
        ec=1.05,
        reservoir_volume_liters=70.0,
        control_strategy="agentic_ai",
    )

    response = receive_sensor_data(payload, FakeSensorRepository())

    assert response.pump_activated == "none"
    assert response.duration_ms == 0
    assert response.decision.metadata["triggered_by"] == "batch_mode_collection"


def test_phase3_emergency_guard_not_triggered_in_mild_zone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 3: mild disturbance zone (5.30–5.49) is handled by LLM batch, not emergency."""
    monkeypatch.setattr(settings, "batch_analysis_enabled", True)
    monkeypatch.setattr(settings, "force_fixed_reservoir_volume", False)
    # ph_target_min=5.5, ph_emergency_buffer_below=0.20 → emergency fires at pH < 5.30
    # pH 5.35 is in mild zone (5.30–5.49) → NOT emergency, LLM handles
    payload = SensorPayload(
        temperature=24.5,
        ph=5.35,
        ec=1.6,
        reservoir_volume_liters=70.0,
        control_strategy="agentic_ai",
    )

    response = receive_sensor_data(payload, FakeSensorRepository())

    assert response.pump_activated == "none"
    assert response.decision.metadata["triggered_by"] == "batch_mode_collection"


def test_phase3_emergency_guard_suppressed_within_mixing_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 3: emergency guard does not fire again if a dose is still within the mixing window."""
    monkeypatch.setattr(settings, "batch_analysis_enabled", True)
    monkeypatch.setattr(settings, "force_fixed_reservoir_volume", False)
    monkeypatch.setattr(settings, "default_mixing_time_seconds", 120)

    repository = FakeSensorRepository()
    # Simulate a recent ph_down dose given 30 seconds ago (within 120s mixing window)
    repository.recent_logs = [
        SensorHistoryEntry(
            timestamp=datetime.now(timezone.utc) - timedelta(seconds=30),
            ph=7.3,
            ec=1.5,
            pump_activated="ph_down",
            dose_ml=10.0,
            duration_ms=4959,
            status="completed",
        )
    ]
    # pH still in emergency zone (> 6.70) but mixing window not yet elapsed
    payload = SensorPayload(
        temperature=24.5,
        ph=7.1,
        ec=1.5,
        reservoir_volume_liters=70.0,
        control_strategy="agentic_ai",
    )

    response = receive_sensor_data(payload, repository)

    assert response.pump_activated == "none"
    assert response.decision.metadata["triggered_by"] == "emergency_guard_mixing_wait"
    assert response.decision.metadata["executed_strategy"] == "agentic_ai"


def test_phase3_emergency_guard_fires_after_mixing_window_elapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 3: emergency guard fires once the mixing window has fully elapsed."""
    monkeypatch.setattr(settings, "batch_analysis_enabled", True)
    monkeypatch.setattr(settings, "force_fixed_reservoir_volume", False)
    monkeypatch.setattr(settings, "default_mixing_time_seconds", 120)

    repository = FakeSensorRepository()
    # Dose given 3 minutes ago — mixing window (120s) has elapsed
    repository.recent_logs = [
        SensorHistoryEntry(
            timestamp=datetime.now(timezone.utc) - timedelta(seconds=180),
            ph=7.3,
            ec=1.5,
            pump_activated="ph_down",
            dose_ml=10.0,
            duration_ms=4959,
            status="completed",
        )
    ]
    payload = SensorPayload(
        temperature=24.5,
        ph=7.1,
        ec=1.5,
        reservoir_volume_liters=70.0,
        control_strategy="agentic_ai",
    )

    response = receive_sensor_data(payload, repository)

    assert response.pump_activated == "ph_down"
    assert response.decision.metadata["triggered_by"] == "emergency_guard"


def test_phase3_emergency_guard_suppressed_by_active_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 3: per-reading emergency dosing must respect active physical commands."""
    monkeypatch.setattr(settings, "batch_analysis_enabled", True)
    monkeypatch.setattr(settings, "force_fixed_reservoir_volume", False)
    monkeypatch.setattr(settings, "default_mixing_time_seconds", 120)

    repository = FakeSensorRepository()
    repository.recent_or_active_pump_command = {
        "control_cycle_id": 987,
        "status": "batch_dispatched",
        "pump_activated": "ec_down",
        "dose_ml": 6000.0,
        "duration_ms": 2_880_000,
        "age_seconds": 300,
    }
    payload = SensorPayload(
        temperature=24.5,
        ph=6.0,
        ec=4.12,
        reservoir_volume_liters=70.0,
        control_strategy="agentic_ai",
    )

    response = receive_sensor_data(payload, repository)

    assert response.pump_activated == "none"
    assert response.duration_ms == 0
    assert response.decision.metadata["triggered_by"] == "emergency_guard_mixing_wait"
    assert response.decision.metadata["pump_guard"]["active_control_cycle_id"] == 987
    assert "dose_ml" not in response.decision.metadata
    assert "duration_ms" not in response.decision.metadata
    assert response.decision.metadata["suppressed_baseline_decision"]["pump_activated"] == "ec_down"


def test_phase3_baseline_strategy_unaffected_by_batch_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 3 batch mode must not change baseline strategy behavior."""
    monkeypatch.setattr(settings, "batch_analysis_enabled", True)
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=70.0,
        control_strategy="baseline",
    )

    response = receive_sensor_data(payload, FakeSensorRepository())

    # baseline always uses the deterministic pipeline regardless of batch mode
    assert response.pump_activated == "ph_up"
    assert response.decision.decision == "ph_low"


def test_baseline_dosing_is_held_during_active_command_or_mixing() -> None:
    """Baseline must not issue a second physical command inside the pump safety window."""
    repository = FakeSensorRepository()
    repository.recent_or_active_pump_command = {
        "control_cycle_id": 987,
        "status": "mixing",
        "pump_activated": "ph_up",
        "dose_ml": 4.0,
        "duration_ms": 1983,
        "age_seconds": 30,
    }
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=20,
        control_strategy="baseline",
    )

    response = receive_sensor_data(payload, repository)

    assert response.pump_activated == "none"
    assert response.decision.decision == "wait_for_mixing"
    assert response.duration_ms == 0
    assert response.mixing_time_ms == 0
    assert response.decision.metadata["physical_guard"]["active_command"]["control_cycle_id"] == 987
    assert response.decision.metadata["physical_guard"]["suppressed_command"]["pump_activated"] == "ph_up"


def test_physical_guard_precedes_agentic_human_review() -> None:
    """HITL must not queue an ineligible command while another dose is mixing."""
    repository = FakeSensorRepository()
    repository.system_settings["hitl_enabled"] = True
    repository.recent_logs = [
        SensorHistoryEntry(
            timestamp=datetime.now(timezone.utc) - timedelta(minutes=1),
            ph=5.3,
            ec=1.8,
            decision="wait_initial_confirmation",
            pump_activated="none",
        )
    ]
    repository.recent_or_active_pump_command = {
        "control_cycle_id": 987,
        "status": "mixing",
        "pump_activated": "ec_up",
        "dose_ml": 3.0,
        "duration_ms": 1800,
        "age_seconds": 20,
    }
    payload = SensorPayload(
        temperature=24.5,
        ph=5.3,
        ec=1.8,
        reservoir_volume_liters=20,
        ph_stable_for_seconds=30,
        ec_stable_for_seconds=30,
        control_strategy="agentic_ai",
    )

    response = receive_sensor_data(payload, repository)

    assert response.pump_activated == "none"
    assert response.decision.decision == "wait_for_mixing"
    assert "human_in_the_loop" not in response.decision.metadata
    assert response.decision.metadata["physical_guard"]["active_command"]["control_cycle_id"] == 987


def test_batch_source_filter_excludes_prior_scheduler_decisions() -> None:
    """Scheduled batch input should not include synthetic prior batch decisions."""
    scheduler_row = SensorHistoryEntry(
        timestamp=datetime.now(timezone.utc),
        ph=6.4,
        ec=1.5,
        decision_metadata={"triggered_by": "batch_scheduler"},
    )
    manual_row = SensorHistoryEntry(
        timestamp=datetime.now(timezone.utc),
        ph=6.4,
        ec=1.5,
        decision_metadata={"triggered_by": "manual_batch_trigger"},
    )
    collection_row = SensorHistoryEntry(
        timestamp=datetime.now(timezone.utc),
        ph=6.4,
        ec=1.5,
        decision_metadata={"triggered_by": "batch_mode_collection"},
    )

    assert _is_batch_source_reading(scheduler_row) is False
    assert _is_batch_source_reading(manual_row) is False
    assert _is_batch_source_reading(collection_row) is True


def test_batch_histories_exclude_all_maintenance_markers() -> None:
    """Maintenance samples must not influence a later scheduled dosing decision."""
    now = datetime.now(timezone.utc)
    maintenance_rows = [
        SensorHistoryEntry(
            timestamp=now - timedelta(minutes=5),
            ph=7.3,
            ec=0.02,
            decision="maintenance_mode",
        ),
        SensorHistoryEntry(
            timestamp=now - timedelta(minutes=4),
            ph=7.2,
            ec=0.03,
            status="maintenance_mode",
        ),
        SensorHistoryEntry(
            timestamp=now - timedelta(minutes=3),
            ph=7.1,
            ec=0.04,
            decision_metadata={"triggered_by": "maintenance_mode"},
        ),
        SensorHistoryEntry(
            timestamp=now - timedelta(minutes=2),
            ph=7.0,
            ec=0.05,
            decision_metadata={"maintenance_mode_enabled": True},
        ),
    ]
    source_row = SensorHistoryEntry(
        timestamp=now - timedelta(minutes=1),
        ph=6.4,
        ec=1.4,
        decision="within_range",
        decision_metadata={"triggered_by": "batch_mode_collection"},
    )

    raw_readings, control_history = _batch_histories(
        [source_row, *reversed(maintenance_rows)],
        source_limit=15,
        control_limit=180,
    )

    assert raw_readings == [source_row]
    assert control_history == [source_row]
    assert all(_is_batch_source_reading(row) is False for row in maintenance_rows)


def test_batch_histories_keep_prior_doses_for_response_adaptation() -> None:
    """Synthetic dose rows stay out of trends but remain in control history."""
    now = datetime.now(timezone.utc)
    dose_row = SensorHistoryEntry(
        timestamp=now - timedelta(minutes=20),
        ph=6.54,
        ec=1.45,
        decision="ph_high",
        pump_activated="ph_down",
        dose_ml=2.37,
        status="completed",
        decision_metadata={"triggered_by": "batch_scheduler"},
    )
    source_row = SensorHistoryEntry(
        timestamp=now - timedelta(minutes=1),
        ph=6.54,
        ec=1.45,
        decision="within_range",
        pump_activated="none",
        decision_metadata={"triggered_by": "batch_mode_collection"},
    )

    raw_readings, control_history = _batch_histories(
        [source_row, dose_row],
        source_limit=15,
        control_limit=180,
    )

    assert raw_readings == [source_row]
    assert control_history == [dose_row, source_row]


def test_batch_expiry_includes_dispatched_commands_that_never_started() -> None:
    """A lost ESP32 pickup response must not block future batch cycles forever."""
    source = inspect.getsource(PostgresSensorReadingRepository.expire_stale_batch_pending_commands)

    assert "control_cycles.status = 'batch_dispatched'" in source
    assert "control_cycles.action_started_at IS NULL" in source


def test_overview_summary_next_run_anchors_to_noon_manila(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Summary scheduling should use fixed Philippine-time anchors, not process uptime."""
    monkeypatch.setattr(settings, "app_timezone", "Asia/Manila")

    next_run = _next_summary_run_at(datetime(2026, 5, 31, 3, 30, tzinfo=timezone.utc))

    assert next_run == datetime(2026, 5, 31, 4, 0, tzinfo=timezone.utc)


def test_overview_summary_next_run_anchors_to_midnight_manila(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After local noon, the next Overview summary should wait for local midnight."""
    monkeypatch.setattr(settings, "app_timezone", "Asia/Manila")

    next_run = _next_summary_run_at(datetime(2026, 5, 31, 5, 0, tzinfo=timezone.utc))

    assert next_run == datetime(2026, 5, 31, 16, 0, tzinfo=timezone.utc)


def test_overview_summary_previous_run_anchors_to_midnight_manila(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup catch-up should know the most recent fixed local anchor."""
    monkeypatch.setattr(settings, "app_timezone", "Asia/Manila")

    previous_run = _previous_summary_run_at(datetime(2026, 5, 31, 17, 0, tzinfo=timezone.utc))

    assert previous_run == datetime(2026, 5, 31, 16, 0, tzinfo=timezone.utc)


def test_overview_summary_previous_run_anchors_to_noon_manila(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After local noon, startup catch-up should target the noon summary."""
    monkeypatch.setattr(settings, "app_timezone", "Asia/Manila")

    previous_run = _previous_summary_run_at(datetime(2026, 5, 31, 5, 0, tzinfo=timezone.utc))

    assert previous_run == datetime(2026, 5, 31, 4, 0, tzinfo=timezone.utc)


def test_overview_summary_metrics_include_reservoir_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The LLM overview payload should include reservoir/water-level context."""
    monkeypatch.setattr(settings, "default_reservoir_max_volume_liters", 70.0)
    monkeypatch.setattr(settings, "minimum_pumpable_reservoir_volume_liters", 20.0)
    readings = [
        SensorHistoryEntry(
            timestamp=datetime.now(timezone.utc) - timedelta(minutes=20),
            ph=6.2,
            ec=1.4,
            temperature=24.5,
            reservoir_volume_liters=68.0,
        ),
        SensorHistoryEntry(
            timestamp=datetime.now(timezone.utc) - timedelta(minutes=10),
            ph=6.25,
            ec=1.45,
            temperature=24.8,
            reservoir_volume_liters=65.5,
        ),
        SensorHistoryEntry(
            timestamp=datetime.now(timezone.utc),
            ph=6.3,
            ec=1.5,
            temperature=25.0,
            reservoir_volume_liters=63.0,
        ),
    ]

    metrics = _summary_metrics(readings, build_reference_range())

    assert metrics["reservoir_min_liters"] == 63.0
    assert metrics["reservoir_max_liters"] == 68.0
    assert metrics["reservoir_capacity_liters"] == 70.0
    assert metrics["latest"]["reservoir_liters"] == 63.0
    assert metrics["latest"]["reservoir_percent"] == 90
    assert metrics["reservoir_minimum_pumpable_liters"] == 20.0
    assert metrics["reservoir_pumpable_headroom_liters"] == 43.0
    assert metrics["reservoir_usable_remaining_percent"] == 86
    assert metrics["reservoir_recent_consumption_liters_per_hour"] == 15.0
    assert metrics["reservoir_estimated_hours_to_minimum"] == 2.9
    assert metrics["reservoir_operating_status"] == "warning"
    assert any("Reservoir Volume moved downward" in note for note in metrics["trend_notes"])

    narrative = overview_summarizer._deterministic_narrative(metrics)
    assert len(narrative.summary) <= 520
    assert "15.00 L/h" in narrative.summary


def test_overview_summary_excludes_maintenance_readings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Maintenance outliers must not enter recap metrics or narrative text."""
    now = datetime.now(timezone.utc)

    class Repo(FakeSensorRepository):
        def get_sensor_logs_since(self, *_args, **_kwargs) -> list[SensorHistoryEntry]:
            return [
                SensorHistoryEntry(
                    timestamp=now - timedelta(minutes=20),
                    ph=6.42,
                    ec=1.43,
                    temperature=25.8,
                    reservoir_volume_liters=50.6,
                ),
                SensorHistoryEntry(
                    timestamp=now - timedelta(minutes=10),
                    ph=7.38,
                    ec=0.02,
                    temperature=30.0,
                    reservoir_volume_liters=12.0,
                    decision="maintenance_mode",
                    status="maintenance_mode",
                    decision_metadata={
                        "triggered_by": "maintenance_mode",
                        "maintenance_mode_enabled": True,
                    },
                ),
                SensorHistoryEntry(
                    timestamp=now,
                    ph=6.44,
                    ec=1.42,
                    temperature=26.1,
                    reservoir_volume_liters=50.4,
                ),
            ]

    monkeypatch.setattr(settings, "openai_api_key", "")
    summary = _build_summary(Repo(), trigger_source="test", period_end=now)

    assert summary is not None
    assert summary.data_policy_version == overview_summarizer.OVERVIEW_SUMMARY_DATA_POLICY_VERSION
    assert summary.reading_count == 2
    assert summary.ph_min == 6.42
    assert summary.ph_max == 6.44
    assert summary.ec_min == 1.42
    assert summary.ec_max == 1.43
    assert summary.reservoir_min_liters == 50.4
    assert summary.reservoir_max_liters == 50.6
    assert "7.38" not in summary.summary
    assert "0.02" not in summary.summary


def test_overview_summary_returns_none_for_maintenance_only_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A maintenance-only period must not produce an operational recap."""
    now = datetime.now(timezone.utc)

    class Repo(FakeSensorRepository):
        def get_sensor_logs_since(self, *_args, **_kwargs) -> list[SensorHistoryEntry]:
            return [
                SensorHistoryEntry(
                    timestamp=now,
                    ph=7.38,
                    ec=0.02,
                    decision="maintenance_mode",
                    status="maintenance_mode",
                )
            ]

    monkeypatch.setattr(settings, "openai_api_key", "")

    assert _build_summary(Repo(), trigger_source="test", period_end=now) is None


def test_overview_reservoir_warns_from_pumpable_headroom_not_capacity_percent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A level near the pump minimum must not be called sufficient."""
    monkeypatch.setattr(settings, "default_reservoir_max_volume_liters", 70.0)
    monkeypatch.setattr(settings, "minimum_pumpable_reservoir_volume_liters", 20.0)
    now = datetime.now(timezone.utc)
    readings = [
        SensorHistoryEntry(
            timestamp=now - timedelta(minutes=20),
            ph=6.2,
            ec=1.4,
            temperature=24.5,
            reservoir_volume_liters=32.8,
        ),
        SensorHistoryEntry(
            timestamp=now,
            ph=6.2,
            ec=1.4,
            temperature=24.5,
            reservoir_volume_liters=32.8,
        ),
    ]

    metrics = _summary_metrics(readings, build_reference_range())
    narrative = overview_summarizer._deterministic_narrative(metrics)

    assert metrics["latest"]["reservoir_percent"] == 47
    assert metrics["reservoir_pumpable_headroom_liters"] == 12.8
    assert metrics["reservoir_usable_remaining_percent"] == 26
    assert metrics["reservoir_operating_status"] == "warning"
    assert narrative.title == "System needs attention"
    assert "12.8 L above the 20.0 L pumpable minimum" in narrative.summary
    assert "Plan a refill" in narrative.summary


def test_overview_reservoir_rate_requires_a_sustained_trend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One noisy endpoint must not create an urgent consumption forecast."""
    monkeypatch.setattr(settings, "default_reservoir_max_volume_liters", 70.0)
    monkeypatch.setattr(settings, "minimum_pumpable_reservoir_volume_liters", 20.0)
    now = datetime.now(timezone.utc)
    readings = [
        SensorHistoryEntry(
            timestamp=now - timedelta(minutes=20), ph=6.2, ec=1.4,
            reservoir_volume_liters=60.0,
        ),
        SensorHistoryEntry(
            timestamp=now - timedelta(minutes=10), ph=6.2, ec=1.4,
            reservoir_volume_liters=59.9,
        ),
        SensorHistoryEntry(
            timestamp=now, ph=6.2, ec=1.4,
            reservoir_volume_liters=60.0,
        ),
    ]

    metrics = _summary_metrics(readings, build_reference_range())

    assert metrics["reservoir_recent_consumption_liters_per_hour"] is None
    assert metrics["reservoir_estimated_hours_to_minimum"] is None
    assert metrics["reservoir_operating_status"] == "sufficient"


def test_overview_reservoir_rate_ignores_one_downward_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single low endpoint is not enough evidence for a depletion forecast."""
    monkeypatch.setattr(settings, "default_reservoir_max_volume_liters", 70.0)
    monkeypatch.setattr(settings, "minimum_pumpable_reservoir_volume_liters", 20.0)
    now = datetime.now(timezone.utc)
    readings = [
        SensorHistoryEntry(
            timestamp=now - timedelta(minutes=20), ph=6.2, ec=1.4,
            reservoir_volume_liters=60.0,
        ),
        SensorHistoryEntry(
            timestamp=now - timedelta(minutes=10), ph=6.2, ec=1.4,
            reservoir_volume_liters=60.0,
        ),
        SensorHistoryEntry(
            timestamp=now, ph=6.2, ec=1.4,
            reservoir_volume_liters=50.0,
        ),
    ]

    metrics = _summary_metrics(readings, build_reference_range())

    assert metrics["reservoir_recent_consumption_liters_per_hour"] is None
    assert metrics["reservoir_estimated_hours_to_minimum"] is None
    assert metrics["reservoir_operating_status"] == "sufficient"


def test_overview_reservoir_refill_resets_consumption_forecast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-refill decline must not contaminate the post-refill forecast."""
    monkeypatch.setattr(settings, "default_reservoir_max_volume_liters", 70.0)
    monkeypatch.setattr(settings, "minimum_pumpable_reservoir_volume_liters", 20.0)
    now = datetime.now(timezone.utc)
    volumes = [45.0, 43.0, 60.0, 59.9]
    readings = [
        SensorHistoryEntry(
            timestamp=now - timedelta(minutes=30 - index * 10),
            ph=6.2,
            ec=1.4,
            reservoir_volume_liters=volume,
        )
        for index, volume in enumerate(volumes)
    ]

    metrics = _summary_metrics(readings, build_reference_range())

    assert metrics["reservoir_recent_consumption_liters_per_hour"] is None
    assert metrics["reservoir_estimated_hours_to_minimum"] is None
    assert metrics["reservoir_operating_status"] == "sufficient"


def test_overview_reservoir_overfill_is_not_reported_as_sufficient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Historical over-capacity readings should remain visible as attention states."""
    monkeypatch.setattr(settings, "default_reservoir_max_volume_liters", 70.0)
    monkeypatch.setattr(settings, "minimum_pumpable_reservoir_volume_liters", 20.0)
    now = datetime.now(timezone.utc)
    readings = [
        SensorHistoryEntry(
            timestamp=now, ph=6.2, ec=1.4,
            reservoir_volume_liters=72.0,
        ),
    ]

    metrics = _summary_metrics(readings, build_reference_range())
    narrative = overview_summarizer._deterministic_narrative(metrics)

    assert metrics["reservoir_usable_remaining_percent"] == 100
    assert metrics["reservoir_operating_status"] == "overfilled"
    assert narrative.title == "System needs attention"
    assert "above the configured 70.0 L capacity" in narrative.summary


def test_overview_summary_combined_warning_stays_inside_schema_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Temperature, reservoir, and crop context must fit one valid recap."""
    monkeypatch.setattr(settings, "default_reservoir_max_volume_liters", 70.0)
    monkeypatch.setattr(settings, "minimum_pumpable_reservoir_volume_liters", 20.0)
    now = datetime.now(timezone.utc)
    readings = [
        SensorHistoryEntry(
            timestamp=now - timedelta(minutes=20), ph=7.2, ec=2.4,
            temperature=31.5, reservoir_volume_liters=68.0,
        ),
        SensorHistoryEntry(
            timestamp=now - timedelta(minutes=10), ph=7.2, ec=2.4,
            temperature=31.5, reservoir_volume_liters=65.5,
        ),
        SensorHistoryEntry(
            timestamp=now, ph=7.2, ec=2.4,
            temperature=31.5, reservoir_volume_liters=63.0,
        ),
    ]
    metrics = _summary_metrics(readings, build_reference_range())
    metrics["crop_lifecycle"] = {
        "age_days": 32,
        "stage_label": "Harvest window",
    }

    narrative = overview_summarizer._deterministic_narrative(metrics)

    assert len(narrative.summary) <= 520
    assert len(narrative.summary.split()) <= 95
    assert "15.00 L/h" in narrative.summary
    assert "Crop lifecycle" not in narrative.summary
    assert "temperature is high at 31.5°C" in narrative.summary


def test_overview_llm_cannot_call_warning_reservoir_sufficient() -> None:
    """Deterministic normalization must override an unsafe LLM reservoir label."""
    narrative = _SummaryNarrative(
        title="Reservoir sufficient",
        summary="pH and EC are on target. Reservoir volume is sufficient at 47% capacity.",
    )
    metrics = {
        "reservoir_operating_status": "warning",
        "reservoir_minimum_pumpable_liters": 20.0,
        "reservoir_pumpable_headroom_liters": 12.8,
        "reservoir_usable_remaining_percent": 26,
        "reservoir_recent_consumption_liters_per_hour": 1.6,
        "reservoir_estimated_hours_to_minimum": 8.0,
        "latest": {"reservoir_liters": 32.8},
        "crop_lifecycle": {
            "transplant_date": None,
            "age_days": None,
            "stage": "not_configured",
        },
    }

    normalized = _normalize_operator_narrative(narrative, metrics)

    assert normalized.title == "System needs attention"
    assert "sufficient" not in normalized.summary.lower()
    assert normalized.summary.startswith("pH and EC are on target.")
    assert "pumpable minimum" in normalized.summary
    assert "recent decline 1.60 L/h" in normalized.summary
    assert normalized.risks[0].startswith("Reservoir pumpable headroom")
    assert normalized.recommended_actions[0].startswith("Plan a reservoir refill")


def test_overview_summary_normalizes_generic_llm_wording() -> None:
    """Operator recap should not sound like a generic system explainer."""
    narrative = _SummaryNarrative(
        title="System stable",
        summary=(
            "The hydroponic system is operating within normal parameters, with pH and EC levels on target. "
            "Water temperature is currently low at 31.5°C, and reservoir levels remain steady."
        ),
    )

    normalized = _normalize_operator_narrative(narrative)

    assert "hydroponic system" not in normalized.summary.lower()
    assert "normal parameters" not in normalized.summary.lower()
    assert normalized.summary.startswith("pH and EC levels on target.")
    assert "Water temperature is currently high at 31.5°C" in normalized.summary


def test_overview_summary_hides_default_harvest_window_until_crop_age_is_set() -> None:
    """Default harvest days are configuration, not a real crop timeline."""
    repository = FakeSensorRepository()
    context = overview_summarizer._summary_crop_lifecycle_context(
        build_crop_lifecycle_context(repository, build_reference_range())
    )

    assert context["age_days"] is None
    assert context["transplant_date"] is None
    assert context["harvest_start_day"] is None
    assert context["harvest_end_day"] is None
    assert "do not infer" in context["stage_guidance"]


def test_overview_summary_removes_unconfigured_crop_timeline_claims() -> None:
    """LLM output cannot claim transplant or harvest timing without a date."""
    narrative = _SummaryNarrative(
        title="System stable",
        summary=(
            "pH and EC are on target. "
            "The crop is in the vegetative stage after transplanting, with harvest expected in 30-35 days. "
            "Reservoir volume is steady."
        ),
        highlights=[
            "pH and EC are on target.",
            "Harvest expected in 30-35 days.",
        ],
        recommended_actions=[
            "Set transplant date when the batch date is available.",
            "Prepare for harvest window day 30.",
        ],
    )
    metrics = {
        "crop_lifecycle": {
            "transplant_date": None,
            "age_days": None,
            "stage": "not_configured",
        }
    }

    normalized = _normalize_operator_narrative(narrative, metrics)

    assert "vegetative" not in normalized.summary.lower()
    assert "harvest" not in normalized.summary.lower()
    assert "after transplant" not in normalized.summary.lower()
    assert "Reservoir volume is steady." in normalized.summary
    assert normalized.highlights == ["pH and EC are on target."]
    assert normalized.recommended_actions == [
        "Set transplant date when the batch date is available."
    ]


def test_overview_summary_marks_fallback_when_configured_llm_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model failure produces an explicitly non-AI deterministic recap."""
    now = datetime.now(timezone.utc)

    class Repo:
        def get_sensor_logs_since(self, *_args, **_kwargs) -> list[SensorHistoryEntry]:
            return [
                SensorHistoryEntry(
                    timestamp=now,
                    ph=6.2,
                    ec=1.4,
                    temperature=24.0,
                    reservoir_volume_liters=69.0,
                )
            ]

        def get_active_reference_range(self, *_args, **_kwargs) -> ReferenceRange:
            return build_reference_range()

    monkeypatch.setattr(settings, "openai_api_key", "configured")
    monkeypatch.setattr("app.services.overview_summarizer._llm_narrative", lambda _metrics: None)

    summary = _build_summary(Repo(), trigger_source="test", period_end=now)
    assert summary is not None
    assert summary.llm_used is False
    assert summary.model is None


def test_overview_summary_is_stale_when_log_count_changes() -> None:
    """Persisted summaries should not survive a production system-log reset."""
    class Repo:
        def get_sensor_logs_since(self, *_args, **_kwargs) -> list[SensorHistoryEntry]:
            return [
                SensorHistoryEntry(
                    timestamp=datetime.now(timezone.utc),
                    ph=6.06,
                    ec=4.12,
                )
            ]

    summary = OverviewSummary(
        generated_at=datetime.now(timezone.utc),
        period_start=datetime.now(timezone.utc) - timedelta(hours=1),
        period_end=datetime.now(timezone.utc),
        title="Old summary",
        summary="Old summary",
        reading_count=16,
    )

    assert _summary_matches_current_logs(Repo(), summary) is False


def test_overview_summary_is_stale_when_only_newer_readings_exist() -> None:
    """If all current readings are newer than the summary, production logs were likely reset."""
    class Repo:
        def get_sensor_logs_since(self, *_args, **_kwargs) -> list[SensorHistoryEntry]:
            return [
                SensorHistoryEntry(
                    timestamp=datetime.now(timezone.utc),
                    ph=6.06,
                    ec=4.12,
                )
            ]

    summary = OverviewSummary(
        generated_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        period_start=datetime.now(timezone.utc) - timedelta(hours=1),
        period_end=datetime.now(timezone.utc) - timedelta(minutes=10),
        title="Old summary",
        summary="Old summary",
        reading_count=1,
    )

    assert _summary_matches_current_logs(Repo(), summary) is False


def test_overview_summary_remains_visible_when_new_readings_arrive() -> None:
    """A valid scheduled summary should remain visible while the next readings stream in."""
    generated_at = datetime.now(timezone.utc)

    class Repo:
        def get_sensor_logs_since(self, *_args, **_kwargs) -> list[SensorHistoryEntry]:
            return [
                SensorHistoryEntry(
                    timestamp=generated_at - timedelta(minutes=1),
                    ph=6.06,
                    ec=1.22,
                ),
                SensorHistoryEntry(
                    timestamp=generated_at + timedelta(minutes=10),
                    ph=6.08,
                    ec=1.24,
                ),
            ]

    summary = OverviewSummary(
        data_policy_version=overview_summarizer.OVERVIEW_SUMMARY_DATA_POLICY_VERSION,
        generated_at=generated_at,
        period_start=generated_at - timedelta(hours=12),
        period_end=generated_at,
        title="Scheduled summary",
        summary="Scheduled summary",
        reading_count=1,
    )

    assert _summary_matches_current_logs(Repo(), summary) is True


def test_overview_summary_invalidates_pre_filter_data_policy() -> None:
    """A cached recap made before maintenance filtering must not remain visible."""
    generated_at = datetime.now(timezone.utc)

    class Repo:
        def get_sensor_logs_since(self, *_args, **_kwargs) -> list[SensorHistoryEntry]:
            return [
                SensorHistoryEntry(
                    timestamp=generated_at - timedelta(minutes=1),
                    ph=6.42,
                    ec=1.43,
                )
            ]

    old_summary = OverviewSummary(
        generated_at=generated_at,
        period_start=generated_at - timedelta(hours=12),
        period_end=generated_at,
        title="Contaminated summary",
        summary="Includes maintenance readings.",
        reading_count=1,
    )

    assert old_summary.data_policy_version == 1
    assert _summary_matches_current_logs(Repo(), old_summary) is False


def test_summary_repository_query_excludes_maintenance_rows() -> None:
    """SQL filtering prevents maintenance rows from consuming the summary limit."""
    source = inspect.getsource(PostgresSensorReadingRepository.get_sensor_logs_since)

    assert "COALESCE(system_logs.decision, '') <> 'maintenance_mode'" in source
    assert "COALESCE(system_logs.status, '') <> 'maintenance_mode'" in source
    assert "system_logs.decision_metadata ->> 'triggered_by'" in source
    assert "system_logs.decision_metadata ->> 'maintenance_mode_enabled'" in source


def test_notification_history_is_limited_to_current_system_logs() -> None:
    """Notification history should not show orphaned rows after system_logs reset."""
    source = inspect.getsource(PostgresSensorReadingRepository.get_recent_notification_logs)

    assert "INNER JOIN {} AS system_logs" in source
    assert "system_logs.id = notification_logs.system_log_id" in source


def test_overview_summary_preserves_missing_temperature_instead_of_zero() -> None:
    readings=[SensorHistoryEntry(timestamp=datetime.now(timezone.utc),ph=6.0,ec=1.8,
        temperature=None,decision="within_range",pump_activated="none")]
    metrics=_summary_metrics(readings,build_reference_range())
    assert metrics["latest"]["temperature"] is None
    assert metrics["temperature_min"] is None
    assert metrics["temperature_max"] is None
    assert metrics["temperature_in_range_percent"] is None


def test_overview_summary_distinguishes_completed_doses_from_commands() -> None:
    statuses=["waiting","dosing","cancelled","rejected","failed",None,"completed","completed_estimated"]
    now=datetime.now(timezone.utc)
    readings=[SensorHistoryEntry(timestamp=now+timedelta(seconds=i),ph=6.0,ec=1.8,
        pump_activated="ph_up",dose_ml=2.0,status=status) for i,status in enumerate(statuses)]
    metrics=_summary_metrics(readings,build_reference_range())
    assert metrics["dose_command_count"]==8
    assert metrics["dosing_event_count"]==2
    assert metrics["total_dose_ml"]==4.0
    assert metrics["estimated_dosing_event_count"]==1
    assert metrics["unconfirmed_dose_command_count"]==3
    assert len(metrics["dosing_events"])==2
    assert "estimated completion" in metrics["dosing_events"][1]


def test_overview_summary_invalidates_previous_command_counting_policy() -> None:
    now=datetime.now(timezone.utc)
    old=OverviewSummary(data_policy_version=2,generated_at=now,period_start=now-timedelta(hours=1),
        period_end=now,title="Old summary",summary="Old command-counting summary",reading_count=1)
    assert _summary_matches_current_logs(object(),old) is False
