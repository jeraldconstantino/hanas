"""Regression coverage for recap facts and operator override actuation bounds."""
from datetime import datetime, timedelta, timezone
import re

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.routes.control_cycles import apply_human_review
from app.core.config import settings
from app.database.repositories.sensor_repository import (
    InvalidHumanReview, PostgresSensorReadingRepository, _validate_override,
)
from app.schemas.control_cycle import HumanReviewPayload
from app.schemas.sensor import SensorHistoryEntry
from app.services.overview_summarizer import (
    _SummaryNarrative, _summary_metrics, _deterministic_narrative, _normalize_operator_narrative,
)
from tests.factories import build_reference_range


@pytest.fixture
def metrics(monkeypatch):
    monkeypatch.setattr(settings, 'default_reservoir_max_volume_liters', 70.0)
    monkeypatch.setattr(settings, 'minimum_pumpable_reservoir_volume_liters', 20.0)
    now = datetime.now(timezone.utc)
    rows = [SensorHistoryEntry(
        timestamp=now - timedelta(minutes=20-i), ph=6, ec=1.5, temperature=24,
        reservoir_volume_liters=70, decision='within_range', status='completed',
    ) for i in range(20)]
    return rows


@pytest.mark.parametrize('field,value,label', [('ph',7,'pH is high at 7'),
    ('ec',2.5,'EC is high at 2.5'), ('temperature',31.5,'temperature is high at 31.5'),
    ('temperature',17,'temperature is low at 17')])
def test_latest_excursion_overrides_window_majority(metrics, field, value, label):
    metrics[-1] = metrics[-1].model_copy(update={field:value})
    data = _summary_metrics(metrics, build_reference_range())
    recap = _deterministic_narrative(data)
    assert recap.title == 'System needs attention'
    assert label in recap.summary
    wrong = _SummaryNarrative(title='System stable', summary='pH and EC are on target. All readings are within range.')
    normalized = _normalize_operator_narrative(wrong, data)
    assert normalized.title == 'System needs attention'
    assert label in normalized.summary
    assert 'All readings are within range' not in normalized.summary


def test_newest_reading_selected_even_with_reversed_history(metrics):
    metrics[-1] = metrics[-1].model_copy(update={'ph':7})
    data = _summary_metrics(list(reversed(metrics)), build_reference_range())
    assert data['latest']['ph'] == 7


def test_stable_title_needs_explicit_stability(metrics):
    data = _summary_metrics(metrics, build_reference_range())
    assert _deterministic_narrative(data).title != 'System stable'
    metrics[-1] = metrics[-1].model_copy(update={'decision_metadata':{'monitoring_agent':{'is_stable':True}}})
    assert _deterministic_narrative(_summary_metrics(metrics, build_reference_range())).title == 'System stable'


def test_valid_llm_narrative_retains_its_useful_text(metrics):
    data = _summary_metrics(metrics, build_reference_range())
    narrative = _SummaryNarrative(title='System needs attention', summary='Recorded pH and EC are on target. Validate reading stability before acting.')
    assert _normalize_operator_narrative(narrative, data).summary == narrative.summary


def test_missing_latest_temperature_stays_historical(metrics):
    metrics[-1] = metrics[-1].model_copy(update={'temperature':None})
    recap = _deterministic_narrative(_summary_metrics(metrics, build_reference_range()))
    assert 'Historical temperature' in recap.summary
    assert recap.title != 'System stable'


@pytest.mark.parametrize('volume', [0,20,32.8,72])
@pytest.mark.parametrize('anomaly', [False,True])
def test_multiple_concerns_fit_recap_without_hiding_validation(metrics, volume, anomaly):
    metrics[-1] = metrics[-1].model_copy(update={
        'ph':7.2,'ec':2.4,'temperature':31.5,'reservoir_volume_liters':volume,
        'decision':'sensor_anomaly' if anomaly else 'ph_high',
    })
    recap = _deterministic_narrative(_summary_metrics(metrics, build_reference_range()))
    assert len(recap.summary) <= 520
    assert len(recap.summary.split()) <= 95
    assert 2 <= len(re.findall(r'[.!?](?:\s|$)', recap.summary)) <= 4
    assert '31.5' in recap.summary and 'high' in recap.summary
    assert 'level' in recap.summary
    if volume > 70:
        assert 'refill' not in recap.summary.lower()
    if volume <= 20:
        assert '0.0 L pumpable headroom' in recap.summary
        assert 'refill promptly' in recap.summary.lower()


def test_estimated_completed_dose_stays_qualified(metrics):
    metrics[-1] = metrics[-1].model_copy(update={'pump_activated':'ph_down', 'dose_ml':4,
        'duration_ms':1983, 'status':'completed_estimated'})
    recap = _deterministic_narrative(_summary_metrics(metrics, build_reference_range()))
    assert 'estimated completion' in recap.dosing_events[0]
    assert 'ph' in recap.dosing_events[0].lower() and '4.00 mL' in recap.dosing_events[0]


@pytest.mark.parametrize('pump,flow', [('ph_up',121),('ph_down',121),('ec_up',125),('ec_down',125)])
def test_calibrated_override_is_allowed(pump, flow):
    _validate_override(HumanReviewPayload(action='override', pump_activated=pump,
        dose_ml=round(flow/60,2), duration_ms=1000), build_reference_range())


@pytest.mark.parametrize('changes', [
    {'dose_ml':10000,'duration_ms':3600000},
    {'dose_ml':11,'duration_ms':5455},
    {'dose_ml':10,'duration_ms':10001},
    {'dose_ml':10,'duration_ms':4964},
    {'dose_ml':4,'duration_ms':1000},
    {'dose_ml':0,'duration_ms':1000},
    {'dose_ml':1,'duration_ms':0},
    {'mixing_time_ms':0}, {'mixing_time_ms':450001},
    {'pump_activated':'none'},
])
def test_invalid_override_is_rejected(changes):
    values = dict(action='override',pump_activated='ph_down',dose_ml=2.02,duration_ms=1000)
    values.update(changes)
    with pytest.raises(InvalidHumanReview):
        _validate_override(HumanReviewPayload(**values), build_reference_range())


@pytest.mark.parametrize('dose',[float('nan'),float('inf')])
def test_nonfinite_override_rejected_by_schema(dose):
    with pytest.raises(ValidationError):
        HumanReviewPayload(action='override',dose_ml=dose)


def test_repository_rejects_override_before_writing(monkeypatch):
    class Cursor:
        queries = []
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def execute(self,query,args): self.queries.append(query)
        def fetchone(self): return (1,{'human_in_the_loop':{'review_required':True,'sensor_snapshot':{'control_strategy':'agentic_ai'}}},'wait_human_review')
    class Connection:
        def cursor(self): return cursor
        def commit(self): pytest.fail('Unsafe override committed')
    cursor = Cursor()
    repo = PostgresSensorReadingRepository(Connection())
    monkeypatch.setattr(repo,'get_active_reference_range',lambda *_:build_reference_range())
    with pytest.raises(InvalidHumanReview):
        repo.apply_human_review(1,HumanReviewPayload(action='override',pump_activated='ph_down',dose_ml=10000,duration_ms=3600000))
    assert len(cursor.queries) == 1


def test_invalid_override_returns_actionable_422():
    class Repo:
        def get_system_setting(self,key): return False
        def apply_human_review(self,*args): raise InvalidHumanReview('Override exceeds configured pump limit.')
    with pytest.raises(HTTPException) as error:
        apply_human_review(1,HumanReviewPayload(action='override'),Repo())
    assert error.value.status_code == 422
    assert 'pump limit' in error.value.detail


@pytest.mark.parametrize('changes', [{'ph':6.504}, {'ec':2.004}, {'temperature':26.04}, {'reservoir_volume_liters':70.04}])
def test_rounding_does_not_hide_boundary_excursions(metrics, changes):
    metrics[-1] = metrics[-1].model_copy(update=changes)
    data = _summary_metrics(metrics, build_reference_range())
    assert _deterministic_narrative(data).title == 'System needs attention'
    if 'reservoir_volume_liters' in changes:
        assert data['reservoir_operating_status'] == 'overfilled'
    else:
        assert 'high' in _deterministic_narrative(data).summary
