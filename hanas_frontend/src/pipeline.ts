export type PipelineStageKey =
  | 'orchestrator_agent'
  | 'monitoring_agent'
  | 'diagnostic_reasoning_agent'
  | 'decision_agent'
  | 'dose_planning_agent'
  | 'consistency_review'
  | 'safety_gate'
  | 'human_review_gate'
  | 'execution_agent'
  | 'completed'

export const PIPELINE_STAGE_META: Record<PipelineStageKey, {
  label: string
  shortLabel: string
  liveSummary: string
  operatorSummary: string
}> = {
  orchestrator_agent: {
    label: 'Orchestrator agent',
    shortLabel: 'Routing',
    liveSummary: 'Managing the reasoning path and routing specialist feedback.',
    operatorSummary: 'HANAS is deciding the next safe stage from intake through specialist feedback.',
  },
  monitoring_agent: {
    label: 'Monitoring agent',
    shortLabel: 'Monitoring',
    liveSummary: 'Checking pH, EC, temperature, stability, and recent trend context.',
    operatorSummary: 'HANAS is validating whether the current readings are stable, drifting, recovering, or outside range.',
  },
  diagnostic_reasoning_agent: {
    label: 'Diagnostic reasoning agent',
    shortLabel: 'Diagnosis',
    liveSummary: 'Classifying the root condition and choosing the primary metric to correct first.',
    operatorSummary: 'HANAS is identifying whether pH, EC, or a combined disturbance is the main problem.',
  },
  decision_agent: {
    label: 'Decision agent',
    shortLabel: 'Decision',
    liveSummary: 'Selecting the action and pump strategy from the diagnosis.',
    operatorSummary: 'HANAS is choosing whether to wait, dose pH up/down, dose nutrients, or dilute.',
  },
  dose_planning_agent: {
    label: 'Dose planning agent',
    shortLabel: 'Dose plan',
    liveSummary: 'Sizing the dose, pump duration, and mixing window using history and safety limits.',
    operatorSummary: 'HANAS is calculating how much solution to dose and how long to mix before taking another reading.',
  },
  consistency_review: {
    label: 'Consistency review',
    shortLabel: 'Review',
    liveSummary: 'Cross-checking agent outputs for contradictions before hardware commands are allowed.',
    operatorSummary: 'HANAS is checking that the diagnosis, selected pump, and dose plan agree with each other.',
  },
  safety_gate: {
    label: 'Safety gate',
    shortLabel: 'Safety',
    liveSummary: 'Applying deterministic pump, dose, and duration caps.',
    operatorSummary: 'HANAS is enforcing hard safety limits before anything can reach the ESP32.',
  },
  human_review_gate: {
    label: 'Human review gate',
    shortLabel: 'HITL',
    liveSummary: 'Checking whether a bounded AI command must wait for operator review.',
    operatorSummary: 'HANAS is holding eligible agentic dosing commands when human-in-the-loop review is enabled.',
  },
  execution_agent: {
    label: 'ESP32 execution',
    shortLabel: 'Execution',
    liveSummary: 'Waiting for the embedded controller to actuate the pump and report completion.',
    operatorSummary: 'The ESP32 is responsible for physical pump actuation and post-dose timing.',
  },
  completed: {
    label: 'Pipeline complete',
    shortLabel: 'Complete',
    liveSummary: 'The decision cycle finished and the dashboard is showing the latest stored result.',
    operatorSummary: 'The latest AI decision is complete. HANAS is monitoring for the next sensor reading.',
  },
}

export function pipelineStageLabel(stage: string | null | undefined): string {
  if (!stage) return 'Latest decision'
  return PIPELINE_STAGE_META[stage as PipelineStageKey]?.label ?? formatDisplayText(stage)
}

export function pipelineStageSummary(stage: string | null | undefined): string {
  if (!stage) return 'Showing the latest completed decision while waiting for the next sensor cycle.'
  return PIPELINE_STAGE_META[stage as PipelineStageKey]?.operatorSummary ?? 'HANAS is processing the current AI control cycle.'
}

export function pipelineLiveSummary(stage: string | null | undefined): string {
  if (!stage) return 'Waiting for the next AI control cycle.'
  return PIPELINE_STAGE_META[stage as PipelineStageKey]?.liveSummary ?? 'Running this pipeline stage.'
}
import { formatDisplayText } from './text'
