"""EC Up callback ordering, immutable clocks, and interruption regressions."""
from datetime import datetime, timedelta, timezone
import re

import pytest
from fastapi import HTTPException
from psycopg2 import sql

from app.api.routes.control_cycles import complete_control_cycle
from app.database.repositories.sensor_repository import (
    ControlCycleConflict, PostgresSensorReadingRepository,
)
from app.schemas.control_cycle import ControlCycleCompletionPayload


CLOCKS = ["action_started_at", "ec_up_a_started_at", "inter_dose_mixing_started_at",
          "ec_up_b_started_at", "action_completed_at", "completed_at"]


def render(query):
    if isinstance(query, sql.SQL):
        return query.string
    if isinstance(query, sql.Identifier):
        return '.'.join(f'"{part}"' for part in query.strings)
    return ''.join(render(part) for part in query.seq)


class ExecutionConnection:
    """In-memory row harness exercising the repository's actual SQL writes."""
    def __init__(self, pump="ec_up", status="batch_dispatched"):
        self.row = dict.fromkeys(CLOCKS)
        self.row.update(status=status, pump=pump, duration=1000)
        self.log_status = status
        self.now = datetime(2026, 10, 25, tzinfo=timezone.utc)
        self.commits = 0
        self.queries = []

    def cursor(self): return self
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def commit(self): self.commits += 1

    def execute(self, query, args):
        statement = render(query)
        self.queries.append(statement)
        if statement.lstrip().startswith('SELECT'):
            assert 'FOR UPDATE OF cycle' in statement
        elif 'COALESCE' in statement:
            for field in re.findall(r'"(\w+)" = COALESCE', statement):
                if self.row[field] is None:
                    self.row[field] = self.now
            self.row['status'] = args[0]
        else:
            self.log_status = args[0]

    def fetchone(self):
        return (self.row['status'], self.row['pump'], self.row['duration'],
                *(self.row[field] for field in CLOCKS))

    def advance(self, seconds=1): self.now += timedelta(seconds=seconds)


def callback(repo, stage):
    if stage in {'dosing', 'inter_dose_mixing', 'ec_up_b_dosing'}:
        repo.mark_control_cycle_action_started(1, stage)
    elif stage == 'mixing':
        repo.mark_control_cycle_action_completed(1)
    else:
        repo.complete_control_cycle(1, stage)


STAGES = ['dosing', 'inter_dose_mixing', 'ec_up_b_dosing', 'mixing', 'completed']


def test_success_and_duplicate_callbacks_preserve_every_timestamp():
    db = ExecutionConnection()
    repo = PostgresSensorReadingRepository(db)
    for stage in STAGES:
        callback(repo, stage)
        before = db.row.copy()
        db.advance(10)
        callback(repo, stage)
        assert db.row == before
        assert db.log_status == stage
    assert db.row['action_started_at'] == db.row['ec_up_a_started_at']
    assert db.row['action_started_at'] < db.row['inter_dose_mixing_started_at'] < db.row['ec_up_b_started_at']
    assert db.row['ec_up_b_started_at'] < db.row['action_completed_at'] < db.row['completed_at']
    # Delayed retries of earlier callbacks must not regress completed state.
    before = db.row.copy()
    for stage in STAGES:
        callback(repo, stage)
    assert db.row == before
    assert db.log_status == 'completed'


@pytest.mark.parametrize(('at_stage', 'event'), [
    (index, event) for index in range(4) for event in STAGES[index + 1:]
])
def test_cannot_skip_stages(at_stage, event):
    db = ExecutionConnection()
    repo = PostgresSensorReadingRepository(db)
    for stage in STAGES[:at_stage]:
        callback(repo, stage)
        db.advance()
    before = db.row.copy()
    commits = db.commits
    with pytest.raises(ControlCycleConflict): callback(repo, event)
    assert db.row == before
    assert db.commits == commits


@pytest.mark.parametrize('at_stage', range(5))
@pytest.mark.parametrize('failure', ['error', 'emergency_stopped'])
def test_interruption_never_becomes_success(at_stage, failure):
    db = ExecutionConnection()
    repo = PostgresSensorReadingRepository(db)
    for stage in STAGES[:at_stage]:
        callback(repo, stage)
        db.advance()
    action_done = db.row['action_completed_at']
    callback(repo, failure)
    before = db.row.copy()
    db.advance()
    callback(repo, failure)
    assert db.row == before
    assert db.row['action_completed_at'] == action_done
    for stage in STAGES:
        with pytest.raises(ControlCycleConflict): callback(repo, stage)
    assert db.row == before


@pytest.mark.parametrize('status', ['failed', 'cancelled', 'batch_expired', 'rejected', 'human_rejected', 'completed_estimated'])
def test_terminal_states_reject_success(status):
    db = ExecutionConnection(status=status)
    repo = PostgresSensorReadingRepository(db)
    for stage in STAGES:
        with pytest.raises(ControlCycleConflict): callback(repo, stage)
    assert db.commits == 0


def test_non_ec_pump_cannot_report_ec_stages():
    db = ExecutionConnection(pump='ph_down')
    repo = PostgresSensorReadingRepository(db)
    callback(repo, 'dosing')
    for stage in ['inter_dose_mixing', 'ec_up_b_dosing']:
        with pytest.raises(ControlCycleConflict): callback(repo, stage)
    callback(repo, 'mixing')
    callback(repo, 'completed')
    assert db.row['ec_up_a_started_at'] is None


def test_route_reports_invalid_completion_as_conflict():
    repo = PostgresSensorReadingRepository(ExecutionConnection())
    with pytest.raises(HTTPException) as error:
        complete_control_cycle(1, ControlCycleCompletionPayload(), repo)
    assert error.value.status_code == 409


def test_legacy_incomplete_ec_sequence_cannot_claim_success():
    db = ExecutionConnection(status='mixing')
    db.row['action_started_at'] = db.now
    db.row['action_completed_at'] = db.now
    repo = PostgresSensorReadingRepository(db)
    with pytest.raises(ControlCycleConflict): callback(repo, 'completed')
    assert db.row['completed_at'] is None


@pytest.mark.parametrize('method,args', [
    ('emergency_stop_active_commands', ('baseline',)),
    ('cancel_pending_commands', ('baseline', 'cancelled')),
    ('expire_stale_batch_pending_commands', ('baseline', 60)),
])
def test_backend_interruptions_do_not_fabricate_pump_completion(monkeypatch, method, args):
    class Cursor:
        rowcount = 1
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, query, params): self.statement = render(query)
    cursor = Cursor()
    class Connection:
        def cursor(self): return cursor
        def commit(self): pass
    repo = PostgresSensorReadingRepository(Connection())
    monkeypatch.setattr(repo, '_get_active_experiment_run_id', lambda *args: 1)
    getattr(repo, method)(*args)
    assert 'action_completed_at = ' not in cursor.statement
    assert 'completed_at = COALESCE' in cursor.statement
