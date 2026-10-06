"""Validate model routing and the monitoring API contract without network calls."""
import json
from types import SimpleNamespace

from app.services.agentic_ai.llm import OpenAIJsonLLM
from app.services.agentic_ai.schemas import MonitoringResult, OrchestratorResult


def test_monitoring_model_and_required_nullable_fields(monkeypatch):
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        output = ({'status':'in_range','is_stable':True,'is_recovering':False,'risk_flags':[],
                   'summary':'Both metrics are in range.', 'recovery_assessment':None}
                  if kwargs['model'] == 'gpt-4.1-mini' else
                  {'route':'monitoring_agent','action':'proceed','confidence':0.9,'reason':'Classify the reading.'})
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(output)))])
    import openai
    monkeypatch.setattr(openai, 'OpenAI', lambda **kwargs: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    llm = OpenAIJsonLLM('test-key','gpt-4.1-nano','gpt-4.1-mini',monitoring_model='gpt-4.1-mini')
    assert llm.complete_json('Monitoring Agent','prompt',{},MonitoringResult).model == 'gpt-4.1-mini'
    assert llm.complete_json('Orchestrator Agent','prompt',{},OrchestratorResult).model == 'gpt-4.1-nano'
    schema = calls[0]['response_format']['json_schema']['schema']
    assert calls[0]['response_format']['json_schema']['strict'] is True
    assert 'is_recovering' in schema['required']
    assert 'recovery_assessment' in schema['required']
    assert schema['additionalProperties'] is False
    assert schema['$defs']['RecoveryAssessment']['additionalProperties'] is False
    assert set(schema['$defs']['RecoveryAssessment']['required']) == set(schema['$defs']['RecoveryAssessment']['properties'])
    assert list(schema['properties']).index('recovery_assessment') < list(schema['properties']).index('is_recovering')
    assert calls[1]['response_format'] == {'type':'json_object'}
    assert llm._models_to_try('Monitoring Agent') == ['gpt-4.1-mini']


def test_diagnostic_model_routing_preserves_other_stage_models(monkeypatch):
    from app.services.agentic_ai.schemas import DiagnosticResult
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        output = {'classification':'combined_disturbance','primary_metric':'ec',
                  'summary':'Both metrics deviate; select EC for action review.'}
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(output)))])
    import openai
    monkeypatch.setattr(openai, 'OpenAI', lambda **kwargs: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    llm = OpenAIJsonLLM('test-key','gpt-4.1-nano','gpt-4.1-mini',
                       monitoring_model='gpt-4.1-mini', diagnostic_model='gpt-4.1-mini')
    result=llm.complete_json('Diagnostic Reasoning Agent','prompt',{},DiagnosticResult)
    assert result.model=='gpt-4.1-mini'
    assert result.output.primary_metric=='ec'
    assert calls[0]['response_format']=={'type':'json_object'}
    assert llm._models_to_try('Diagnostic Reasoning Agent')==['gpt-4.1-mini']
    assert llm._models_to_try('Decision Agent')==['gpt-4.1-nano','gpt-4.1-mini']
    assert OpenAIJsonLLM('test-key','gpt-4.1-nano')._models_to_try('Diagnostic Reasoning Agent')==['gpt-4.1-nano']


def test_dose_planning_model_routing_and_bounds(monkeypatch):
    from app.services.agentic_ai.schemas import DosePlanResult
    calls=[]
    def create(**kwargs):
        calls.append(kwargs)
        output={'pump_activated':'ph_up','dose_adjustment_factor':1.0,
                'mixing_adjustment_factor':1.25,'reason':'Plan the selected bounded correction.'}
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(output)))])
    import openai
    monkeypatch.setattr(openai,'OpenAI',lambda **kwargs:SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    llm=OpenAIJsonLLM('test-key','gpt-4.1-nano','gpt-4.1-mini',dose_planning_model='gpt-4.1-mini')
    result=llm.complete_json('Dose Planning Agent','prompt',{},DosePlanResult)
    assert result.model=='gpt-4.1-mini'
    assert result.output.pump_activated=='ph_up'
    assert llm._models_to_try('Dose Planning Agent')==['gpt-4.1-mini']
    assert llm._models_to_try('Decision Agent')==['gpt-4.1-nano','gpt-4.1-mini']
    assert calls[0]['response_format']=={'type':'json_object'}
    assert OpenAIJsonLLM('test-key','gpt-4.1-nano')._models_to_try('Dose Planning Agent')==['gpt-4.1-nano']
