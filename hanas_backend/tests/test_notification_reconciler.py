"""Tests for asynchronous Semaphore status reconciliation."""

from contextlib import contextmanager
import inspect

from app.core.config import settings
from app.database.repositories.sensor_repository import PostgresSensorReadingRepository
from app.services import notification_reconciler


class _FakeCursor:
    def __init__(self) -> None:
        self.statement = ""

    def __enter__(self) -> "_FakeCursor":
        """Enter the fake cursor context."""
        return self

    def __exit__(self, *_args: object) -> None:
        """Exit the fake cursor context."""
        return None

    def execute(self, statement: str, _parameters: object) -> None:
        """Record the last SQL statement."""
        self.statement = statement

    def fetchone(self) -> tuple[bool]:
        """Report successful advisory-lock acquisition."""
        return (True,)


class _FakeConnection:
    def cursor(self) -> _FakeCursor:
        """Return a fake database cursor."""
        return _FakeCursor()


class _FakeRepository:
    def __init__(self) -> None:
        self.updated: list[tuple[int, str, str]] = []
        self.failed: list[int] = []
        self.pending = [(7, 278522315)]

    def get_pending_notification_status_checks(
        self, *, limit: int, max_age_days: int, max_checks: int, min_check_interval_seconds: int
    ) -> list[tuple[int, int]]:
        """Return configured pending notification records."""
        assert limit == 20
        assert max_age_days == 30
        assert max_checks == 180
        assert min_check_interval_seconds == 60
        return self.pending

    def update_notification_provider_status(
        self, notification_log_id: int, *, status: str, provider_response: str
    ) -> None:
        """Record a successful provider status update."""
        self.updated.append((notification_log_id, status, provider_response))

    def record_notification_status_check_failure(self, notification_log_id: int) -> None:
        """Record a failed provider lookup."""
        self.failed.append(notification_log_id)


@contextmanager
def _fake_connection_context():
    """Yield a fake database connection."""
    yield _FakeConnection()


def _configure(monkeypatch) -> None:
    monkeypatch.setattr(settings, "semaphore_api_key", "secret")
    monkeypatch.setattr(settings, "sms_status_poll_batch_size", 20)
    monkeypatch.setattr(settings, "sms_status_lookback_days", 30)
    monkeypatch.setattr(settings, "sms_status_max_checks", 180)


def test_pending_semaphore_message_is_updated_to_sent(monkeypatch) -> None:
    """A terminal provider response replaces the pending local state."""
    _configure(monkeypatch)
    repository = _FakeRepository()
    response = '[{"message_id": 278522315, "status": "Sent"}]'
    monkeypatch.setattr(notification_reconciler, "get_db_connection", _fake_connection_context)
    monkeypatch.setattr(
        notification_reconciler,
        "PostgresSensorReadingRepository",
        lambda _connection: repository,
    )
    monkeypatch.setattr(
        notification_reconciler,
        "_fetch_semaphore_message",
        lambda _message_id: response,
    )

    assert notification_reconciler.reconcile_pending_notification_statuses() == 1
    assert repository.updated == [(7, "sent", response)]
    assert not repository.failed


def test_failed_status_lookup_is_recorded_for_bounded_retry(monkeypatch) -> None:
    """A provider lookup failure is counted without changing the known status."""
    _configure(monkeypatch)
    repository = _FakeRepository()
    monkeypatch.setattr(notification_reconciler, "get_db_connection", _fake_connection_context)
    monkeypatch.setattr(
        notification_reconciler,
        "PostgresSensorReadingRepository",
        lambda _connection: repository,
    )

    def fail_lookup(_message_id: int) -> str:
        raise OSError("provider unavailable")

    monkeypatch.setattr(notification_reconciler, "_fetch_semaphore_message", fail_lookup)

    assert notification_reconciler.reconcile_pending_notification_statuses() == 0
    assert not repository.updated
    assert repository.failed == [7]


def test_pending_status_query_spaces_checks_across_workers() -> None:
    """A second worker must not immediately poll messages checked by the first."""
    source = inspect.getsource(PostgresSensorReadingRepository.get_pending_notification_status_checks)

    assert "status_checked_at <= CURRENT_TIMESTAMP" in source
    assert "min_check_interval_seconds" in source
    assert "status_checked_at ASC NULLS FIRST" in source
