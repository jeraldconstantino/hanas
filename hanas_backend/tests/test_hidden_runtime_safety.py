"""Delayed analysis, stale source data, and invalid callback regressions."""
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.core.config import settings
from app.schemas.sensor import SensorHistoryEntry, SensorPayload
from app.schemas.control_cycle import (
    ControlCycleCompletionPayload, ControlCycleActionStartedPayload,
    ControlCycleActionCompletedPayload,
)
from app.services import batch_scheduler
from app.services.baseline.dosing_rules import evaluate_dosing_decision
from app.api.routes.sensors import _persist_sensor_decision
from app.database.repositories.sensor_repository import ControlCycleConflict, PostgresSensorReadingRepository
from tests.factories import build_reference_range
from tests.test_api import FakeSensorRepository, _FakeConnectionContext


@pytest.mark.parametrize('mode,decision', [('emergency_stop_enabled','emergency_stop'),
    ('maintenance_mode_enabled','maintenance_mode'), ('monitoring_mode_enabled','monitoring_mode')])
def test_operator_hold_is_rechecked_after_analysis(mode, decision):
    repo = FakeSensorRepository()
    payload = SensorPayload(temperature=24,ph=5,ec=1.5,reservoir_volume_liters=20,control_strategy='baseline')
    proposed = evaluate_dosing_decision(payload,build_reference_range())
    assert proposed.pump_activated != 'none'
    repo.system_settings[mode] = True
    response = _persist_sensor_decision(payload,payload,proposed,{},repo)
    assert response.pump_activated == 'none'
    assert response.duration_ms == 0
    assert response.decision.decision == decision


@pytest.fixture
def batch_repo(monkeypatch):
    repo = FakeSensorRepository()
    repo.recent_logs = [SensorHistoryEntry(timestamp=datetime.now(timezone.utc),ph=5,ec=1.5,temperature=24,reservoir_volume_liters=70)]
    monkeypatch.setattr(settings,'openai_api_key','fake-test-key')
    monkeypatch.setattr(settings,'force_fixed_reservoir_volume',False)
    monkeypatch.setattr(settings,'default_reservoir_max_volume_liters',70)
    monkeypatch.setattr(batch_scheduler,'get_db_connection',_FakeConnectionContext)
    monkeypatch.setattr(batch_scheduler,'_try_acquire_batch_lock',lambda _:True)
    monkeypatch.setattr(batch_scheduler,'_release_batch_lock',lambda _:None)
    monkeypatch.setattr(batch_scheduler,'PostgresSensorReadingRepository',lambda _:repo)
    monkeypatch.setattr(batch_scheduler,'OpenAIJsonLLM',lambda *a,**kw:object())
    return repo


@pytest.mark.parametrize('offset',[-3600,60])
def test_batch_rejects_stale_or_future_source_before_model_call(batch_repo,monkeypatch,offset):
    batch_repo.recent_logs[0] = batch_repo.recent_logs[0].model_copy(update={'timestamp':datetime.now(timezone.utc)+timedelta(seconds=offset)})
    monkeypatch.setattr(batch_scheduler,'evaluate_agentic_decision',lambda *a,**kw:pytest.fail('Model called for unusable reading'))
    assert batch_scheduler.run_batch_cycle(force=True) is None
    assert 'stale or future-dated' in batch_scheduler.get_last_batch_skip_message()


@pytest.mark.parametrize('mode',['emergency_stop_enabled','maintenance_mode_enabled','monitoring_mode_enabled'])
def test_batch_discards_proposal_if_hold_changes_during_analysis(batch_repo,monkeypatch,mode):
    def evaluate(payload,reference,*args,**kwargs):
        batch_repo.system_settings[mode] = True
        return evaluate_dosing_decision(payload,reference)
    monkeypatch.setattr(batch_scheduler,'evaluate_agentic_decision',evaluate)
    monkeypatch.setattr(batch_repo,'create_sensor_log',lambda *a:pytest.fail('Held command was saved'))
    assert batch_scheduler.run_batch_cycle(force=True) is None
    assert 'during analysis' in batch_scheduler.get_last_batch_skip_message()


@pytest.mark.parametrize('model,status',[(ControlCycleCompletionPayload,'mixing'),
    (ControlCycleCompletionPayload,'dosing'),(ControlCycleActionStartedPayload,'completed'),
    (ControlCycleActionCompletedPayload,'completed'),(ControlCycleActionCompletedPayload,'anything')])
def test_callback_cannot_supply_status_from_another_stage(model,status):
    with pytest.raises(ValidationError):model(status=status)


@pytest.mark.parametrize('method',['mark_control_cycle_action_started','mark_control_cycle_action_completed','complete_control_cycle'])
def test_rejected_callback_does_not_update_logs_or_commit(method):
    class Cursor:
        calls=[]
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def execute(self,query,args):self.calls.append((query,args))
        def fetchone(self):return None
    cursor=Cursor()
    class Connection:
        def cursor(self):return cursor
        def commit(self):pytest.fail('Rejected callback committed')
    repo=PostgresSensorReadingRepository(Connection())
    with pytest.raises(ControlCycleConflict):getattr(repo,method)(1)
    assert len(cursor.calls)==1
    assert 'emergency_stopped' in str(cursor.calls[0][0])
    assert 'batch_expired' in str(cursor.calls[0][0])


@pytest.mark.parametrize('status',['cancelled','emergency_stopped','human_rejected','completed'])
def test_old_review_metadata_cannot_revive_resolved_cycle(status):
    from app.database.repositories.sensor_repository import InvalidHumanReview
    from app.schemas.control_cycle import HumanReviewPayload
    class Cursor:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def execute(self,*a):pass
        def fetchone(self):return (1,{'human_in_the_loop':{'review_required':True}},status)
    class Connection:
        def cursor(self):return Cursor()
        def commit(self):pytest.fail('Resolved cycle revived')
    with pytest.raises(InvalidHumanReview):
        PostgresSensorReadingRepository(Connection()).apply_human_review(1,HumanReviewPayload(action='approve'))


def test_approval_rechecks_current_caps(monkeypatch):
    from app.database.repositories.sensor_repository import InvalidHumanReview
    from app.schemas.control_cycle import HumanReviewPayload
    class Cursor:
        calls=0
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def execute(self,*a):self.calls+=1
        def fetchone(self):return (1,{'human_in_the_loop':{'review_required':True,
            'pending_decision':{'pump_activated':'ph_down','dose_ml':20,'duration_ms':9917}}},'wait_human_review')
    cursor=Cursor()
    class Connection:
        def cursor(self):return cursor
        def commit(self):pytest.fail('Approval bypassed current cap')
    repo=PostgresSensorReadingRepository(Connection())
    monkeypatch.setattr(repo,'get_active_reference_range',lambda *_:build_reference_range())
    with pytest.raises(InvalidHumanReview):repo.apply_human_review(1,HumanReviewPayload(action='approve'))
    assert cursor.calls==1


def test_pending_dispatch_locks_shared_pumps_before_availability_check(monkeypatch):
    calls = []

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, query, args): calls.append((str(query), args))
        def fetchone(self): return None

    class Connection:
        def cursor(self): return Cursor()

    repo = PostgresSensorReadingRepository(Connection())
    monkeypatch.setattr(repo, 'expire_stale_batch_pending_commands', lambda *args: None)
    response = repo.get_pending_human_review_command()
    assert not response.has_command
    assert 'pg_advisory_xact_lock' in calls[0][0]
    assert 'NOT EXISTS' in calls[1][0]
    assert 'FOR UPDATE' in calls[1][0]
