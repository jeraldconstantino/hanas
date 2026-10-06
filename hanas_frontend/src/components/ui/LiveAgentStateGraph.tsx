import { useEffect, useMemo, useRef, useState, type PointerEvent, type WheelEvent } from 'react'
import {
  Activity,
  Bot,
  Brain,
  CheckCircle2,
  ClipboardCheck,
  Database,
  GitBranch,
  History,
  Eye,
  EyeOff,
  Maximize2,
  Minimize2,
  Minus,
  Plus,
  UserCheck,
  Route,
  ShieldCheck,
  SlidersHorizontal,
  X,
} from 'lucide-react'
import type { ControlCycle, LatestLog } from '../../types'
import { formatDoseMl, formatPumpDuration, isHITLPending } from '../../utils'
import { PIPELINE_STAGE_META, type PipelineStageKey } from '../../pipeline'
import { formatDisplayText, formatEmbeddedDisplayText } from '../../text'

type Meta = Record<string, unknown>
type GraphStatus = 'completed' | 'active' | 'pending' | 'skipped' | 'blocked'
type GraphMode = 'live' | 'help'

type GraphNodeKey =
  | 'input_context'
  | 'orchestrator_agent'
  | 'monitoring_agent'
  | 'diagnostic_reasoning_agent'
  | 'decision_agent'
  | 'dose_planning_agent'
  | 'consistency_review'
  | 'safety_gate'
  | 'human_review_gate'
  | 'final_decision'
  | 'esp32_execution'
  | 'history_update'

type GraphNode = {
  key: GraphNodeKey
  title: string
  eyebrow: string
  summary: string
  detail: string
  status: GraphStatus
  icon: typeof Activity
}

type VisualNode = GraphNode & {
  x: number
  y: number
  w: number
  h: number
}

type GraphEdge = {
  from: GraphNodeKey
  to: GraphNodeKey
  kind?: 'main' | 'conditional' | 'feedback'
  label?: string
}

type NodeLayout = Record<GraphNodeKey, { x: number; y: number; w: number; h: number }>

type DragState = {
  key: GraphNodeKey
  lastX: number
  lastY: number
  startX: number
  startY: number
  moved: boolean
}

type PanState = {
  lastX: number
  lastY: number
}

const ORDER: GraphNodeKey[] = [
  'input_context',
  'orchestrator_agent',
  'monitoring_agent',
  'diagnostic_reasoning_agent',
  'decision_agent',
  'dose_planning_agent',
  'consistency_review',
  'safety_gate',
  'human_review_gate',
  'final_decision',
  'esp32_execution',
  'history_update',
]

const STAGE_TO_NODE: Partial<Record<PipelineStageKey, GraphNodeKey>> = {
  orchestrator_agent: 'orchestrator_agent',
  monitoring_agent: 'monitoring_agent',
  diagnostic_reasoning_agent: 'diagnostic_reasoning_agent',
  decision_agent: 'decision_agent',
  dose_planning_agent: 'dose_planning_agent',
  consistency_review: 'consistency_review',
  safety_gate: 'safety_gate',
  human_review_gate: 'human_review_gate',
  execution_agent: 'esp32_execution',
  completed: 'history_update',
}

const NODE_LAYOUT: Record<GraphNodeKey, { x: number; y: number; w: number; h: number }> = {
  input_context: { x: 70, y: 258, w: 76, h: 76 },
  orchestrator_agent: { x: 266, y: 238, w: 94, h: 94 },
  monitoring_agent: { x: 270, y: 80, w: 76, h: 76 },
  diagnostic_reasoning_agent: { x: 470, y: 106, w: 76, h: 76 },
  decision_agent: { x: 510, y: 258, w: 76, h: 76 },
  dose_planning_agent: { x: 470, y: 410, w: 76, h: 76 },
  consistency_review: { x: 680, y: 258, w: 76, h: 76 },
  safety_gate: { x: 870, y: 258, w: 76, h: 76 },
  human_review_gate: { x: 1040, y: 258, w: 76, h: 76 },
  final_decision: { x: 1040, y: 410, w: 76, h: 76 },
  esp32_execution: { x: 870, y: 410, w: 76, h: 76 },
  history_update: { x: 680, y: 410, w: 76, h: 76 },
}

const GRAPH_EDGES: GraphEdge[] = [
  { from: 'input_context', to: 'orchestrator_agent', kind: 'main' },
  { from: 'orchestrator_agent', to: 'monitoring_agent', kind: 'main' },
  { from: 'monitoring_agent', to: 'orchestrator_agent', kind: 'feedback', label: 'status feedback' },
  { from: 'orchestrator_agent', to: 'diagnostic_reasoning_agent', kind: 'conditional', label: 'next diagnosis' },
  { from: 'diagnostic_reasoning_agent', to: 'orchestrator_agent', kind: 'feedback', label: 'diagnosis feedback' },
  { from: 'orchestrator_agent', to: 'decision_agent', kind: 'conditional', label: 'next action' },
  { from: 'decision_agent', to: 'orchestrator_agent', kind: 'feedback', label: 'action feedback' },
  { from: 'orchestrator_agent', to: 'dose_planning_agent', kind: 'conditional', label: 'next dose plan' },
  { from: 'dose_planning_agent', to: 'orchestrator_agent', kind: 'feedback', label: 'plan feedback' },
  { from: 'orchestrator_agent', to: 'consistency_review', kind: 'conditional', label: 'next review' },
  { from: 'consistency_review', to: 'orchestrator_agent', kind: 'feedback', label: 'review feedback' },
  { from: 'orchestrator_agent', to: 'safety_gate', kind: 'conditional', label: 'safe handoff' },
  { from: 'safety_gate', to: 'human_review_gate', kind: 'main' },
  { from: 'human_review_gate', to: 'final_decision', kind: 'main' },
  { from: 'final_decision', to: 'esp32_execution', kind: 'main' },
  { from: 'esp32_execution', to: 'history_update', kind: 'main' },
  { from: 'history_update', to: 'input_context', kind: 'feedback', label: 'next cycle history' },
]

const NODE_SHORT_LABELS: Record<GraphNodeKey, string> = {
  input_context: 'Inputs',
  orchestrator_agent: 'Routing',
  monitoring_agent: 'Monitoring',
  diagnostic_reasoning_agent: 'Diagnosis',
  decision_agent: 'Decision',
  dose_planning_agent: 'Dose plan',
  consistency_review: 'Review',
  safety_gate: 'Safety',
  human_review_gate: 'Human review',
  final_decision: 'Final decision',
  esp32_execution: 'ESP32',
  history_update: 'History',
}

function metaOf(log: LatestLog): Meta {
  return (log.decision_metadata ?? {}) as Meta
}

function obj(meta: Meta, key: string): Meta | undefined {
  const value = meta[key]
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Meta : undefined
}

function skippedSet(orchestration: Meta | undefined): Set<string> {
  const skipped = Array.isArray(orchestration?.skipped_agents) ? orchestration.skipped_agents : []
  return new Set(skipped.filter((agent): agent is string => typeof agent === 'string'))
}

function text(value: unknown, fallback = 'Not recorded'): string {
  return typeof value === 'string' && value.trim().length > 0 ? value : fallback
}

function label(value: unknown, fallback = 'Not recorded'): string {
  const raw = text(value, fallback)
  return raw === fallback ? raw : formatDisplayText(raw)
}

function diagnosisDetail(value: unknown): string {
  return formatEmbeddedDisplayText(text(value, PIPELINE_STAGE_META.diagnostic_reasoning_agent.operatorSummary)
    .replace(
      /Both pH and EC are outside target range; agentic control will correct (ec_low|ec_high|ph_low|ph_high) first and defer the other correction\./g,
      (_, condition: string) => {
        const first = condition.startsWith('ec_') ? 'EC' : 'pH'
        const deferred = first === 'EC' ? 'pH' : 'EC'
        return `Both pH and EC are outside their target ranges. ${first} will be corrected first. ${deferred} correction is deferred.`
      },
    )
    .replace(/\bec_low\b/g, 'low EC')
    .replace(/\bec_high\b/g, 'high EC')
    .replace(/\bph_low\b/g, 'low pH')
    .replace(/\bph_high\b/g, 'high pH'))
}

function detailText(value: unknown, fallback: string): string {
  return formatEmbeddedDisplayText(text(value, fallback))
}

function agenticMode(meta: Meta): string {
  if (typeof meta.agentic_mode === 'string' && meta.agentic_mode.length > 0) return meta.agentic_mode
  if (meta.triggered_by === 'batch_mode_collection') return 'batch_collection'
  if (meta.triggered_by === 'batch_scheduler' || meta.triggered_by === 'manual_batch_trigger') return 'batch_llm'
  if (meta.triggered_by === 'emergency_guard') return 'emergency_guard'
  if (meta.triggered_by === 'emergency_guard_mixing_wait') return 'emergency_guard_mixing_wait'
  return 'per_reading_graph'
}

function agenticModeLabel(mode: string): string {
  if (mode === 'batch_collection') return 'Batch collection'
  if (mode === 'batch_llm') return 'Batch LLM'
  if (mode === 'emergency_guard') return 'Emergency guard'
  if (mode === 'emergency_guard_mixing_wait') return 'Emergency guard wait'
  if (mode === 'hitl_pending') return 'Held for human review'
  if (mode === 'human_approved_command') return 'Human-approved command'
  if (mode === 'human_override_command') return 'Human override'
  if (mode === 'per_reading_graph') return 'Per-reading graph'
  return formatDisplayText(mode)
}

function pct(value: unknown): string {
  return typeof value === 'number' ? `${Math.round(value * 100)}%` : 'Not recorded'
}

function liveStatus(key: GraphNodeKey, currentStage: string | null | undefined): GraphStatus | null {
  if (!currentStage) return null
  const activeKey = STAGE_TO_NODE[currentStage as PipelineStageKey]
  if (!activeKey) return null
  const activeIndex = ORDER.indexOf(activeKey)
  const index = ORDER.indexOf(key)
  if (index < 0 || activeIndex < 0) return null
  if (index < activeIndex) return 'completed'
  if (index === activeIndex) return 'active'
  return 'pending'
}

function savedStatus(done: boolean, skipped = false, blocked = false): GraphStatus {
  if (blocked) return 'blocked'
  if (done) return 'completed'
  if (skipped) return 'skipped'
  return 'pending'
}

function visualNodes(nodes: GraphNode[], layout: NodeLayout): VisualNode[] {
  return nodes.map((node) => ({ ...node, ...layout[node.key] }))
}

function center(node: VisualNode): { x: number; y: number } {
  return { x: node.x + node.w / 2, y: node.y + node.h / 2 }
}

function nodeRadius(node: VisualNode): number {
  return node.key === 'orchestrator_agent' ? 49 : 41
}

function pointToward(from: { x: number; y: number }, to: { x: number; y: number }, distance: number): { x: number; y: number } {
  const dx = to.x - from.x
  const dy = to.y - from.y
  const length = Math.hypot(dx, dy) || 1
  return {
    x: from.x + (dx / length) * distance,
    y: from.y + (dy / length) * distance,
  }
}

function edgePath(from: VisualNode, to: VisualNode, kind: GraphEdge['kind']): string {
  const fromCenter = center(from)
  const toCenter = center(to)
  const a = pointToward(fromCenter, toCenter, nodeRadius(from))
  const b = pointToward(toCenter, fromCenter, nodeRadius(to) + 8)
  if (kind === 'feedback') {
    const dx = b.x - a.x
    const dy = b.y - a.y
    const distance = Math.hypot(dx, dy) || 1
    const arc = Math.abs(dy) < 70 ? 58 : 42
    const normalX = -dy / distance
    const normalY = dx / distance
    const midX = (a.x + b.x) / 2 + normalX * arc
    const midY = (a.y + b.y) / 2 + normalY * arc
    return `M ${a.x} ${a.y} Q ${midX} ${midY}, ${b.x} ${b.y}`
  }
  if (kind === 'conditional') {
    return `M ${a.x} ${a.y} C ${(a.x + b.x) / 2} ${a.y}, ${(a.x + b.x) / 2} ${b.y}, ${b.x} ${b.y}`
  }
  return `M ${a.x} ${a.y} L ${b.x} ${b.y}`
}

function edgeIsComplete(edge: GraphEdge, nodesByKey: Map<GraphNodeKey, VisualNode>): boolean {
  const from = nodesByKey.get(edge.from)
  const to = nodesByKey.get(edge.to)
  if (!from || !to) return false
  return ['completed', 'active'].includes(from.status) && ['completed', 'active', 'blocked'].includes(to.status)
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value))
}

function helpNodes(): GraphNode[] {
  return [
    {
      key: 'input_context',
      title: 'Inputs & context',
      eyebrow: 'Context',
      summary: 'Sensor values plus decision context enter the backend.',
      detail: 'Includes pH, EC, temperature, reservoir volume, stability indicators, reference ranges, recent history, baseline shadow decision, and crop lifecycle context.',
      status: 'completed',
      icon: Database,
    },
    {
      key: 'orchestrator_agent',
      title: PIPELINE_STAGE_META.orchestrator_agent.label,
      eyebrow: 'Agent',
      summary: PIPELINE_STAGE_META.orchestrator_agent.operatorSummary,
      detail: 'Routes ordinary cycles into monitoring, receives specialist feedback, and selects the next specialist or Safety Gate.',
      status: 'completed',
      icon: Route,
    },
    {
      key: 'monitoring_agent',
      title: PIPELINE_STAGE_META.monitoring_agent.label,
      eyebrow: 'Agent',
      summary: PIPELINE_STAGE_META.monitoring_agent.operatorSummary,
      detail: 'Checks range status, stability, trends, recovery behavior, and possible sensor anomalies.',
      status: 'completed',
      icon: Activity,
    },
    {
      key: 'diagnostic_reasoning_agent',
      title: PIPELINE_STAGE_META.diagnostic_reasoning_agent.label,
      eyebrow: 'Agent',
      summary: PIPELINE_STAGE_META.diagnostic_reasoning_agent.operatorSummary,
      detail: 'Classifies pH/EC deviations and chooses the primary correction metric.',
      status: 'completed',
      icon: Brain,
    },
    {
      key: 'decision_agent',
      title: PIPELINE_STAGE_META.decision_agent.label,
      eyebrow: 'Agent',
      summary: PIPELINE_STAGE_META.decision_agent.operatorSummary,
      detail: 'Chooses dose, wait, monitor, mix longer, or fallback-to-baseline action.',
      status: 'completed',
      icon: GitBranch,
    },
    {
      key: 'dose_planning_agent',
      title: PIPELINE_STAGE_META.dose_planning_agent.label,
      eyebrow: 'Agent',
      summary: PIPELINE_STAGE_META.dose_planning_agent.operatorSummary,
      detail: 'Selects pump action and recommends dose and mixing adjustment factors.',
      status: 'completed',
      icon: ClipboardCheck,
    },
    {
      key: 'consistency_review',
      title: PIPELINE_STAGE_META.consistency_review.label,
      eyebrow: 'Deterministic',
      summary: PIPELINE_STAGE_META.consistency_review.operatorSummary,
      detail: 'Cross-checks agent outputs before the final safety gate.',
      status: 'completed',
      icon: SlidersHorizontal,
    },
    {
      key: 'safety_gate',
      title: PIPELINE_STAGE_META.safety_gate.label,
      eyebrow: 'Deterministic',
      summary: PIPELINE_STAGE_META.safety_gate.operatorSummary,
      detail: 'Applies dose caps, pump duration limits, mixing bounds, and operational safety policies.',
      status: 'completed',
      icon: ShieldCheck,
    },
    {
      key: 'human_review_gate',
      title: PIPELINE_STAGE_META.human_review_gate.label,
      eyebrow: 'Operator',
      summary: PIPELINE_STAGE_META.human_review_gate.operatorSummary,
      detail: 'When HITL is enabled and Safety Gate approves an agentic dose, the command is persisted for operator approval before ESP32 actuation.',
      status: 'completed',
      icon: UserCheck,
    },
    {
      key: 'final_decision',
      title: 'Final backend decision',
      eyebrow: 'Output',
      summary: 'The backend returns a bounded command or a wait/no-action decision.',
      detail: 'The decision record stores command fields, reasoning metadata, and control-cycle status.',
      status: 'completed',
      icon: CheckCircle2,
    },
    {
      key: 'esp32_execution',
      title: 'ESP32 execution',
      eyebrow: 'Embedded',
      summary: 'The ESP32 executes accepted pump commands and reports status.',
      detail: 'Human review may hold a command before actuation when HITL is enabled.',
      status: 'completed',
      icon: Bot,
    },
    {
      key: 'history_update',
      title: 'Next reading / history',
      eyebrow: 'Feedback',
      summary: 'The next sensor reading becomes context for later decisions.',
      detail: 'Post-dose mixing and observed response are persisted as recent history.',
      status: 'completed',
      icon: History,
    },
  ]
}

function liveNodes(log: LatestLog, cycle: ControlCycle, currentStage: string | null | undefined): GraphNode[] {
  const meta = metaOf(log)
  const monitoring = obj(meta, 'monitoring_agent')
  const diagnostic = obj(meta, 'diagnostic_reasoning_agent')
  const decision = obj(meta, 'decision_agent')
  const dosePlan = obj(meta, 'dose_planning_agent')
  const consistency = obj(meta, 'consistency_review')
  const orchestration = obj(meta, 'orchestrator_agent')
  const humanInTheLoop = obj(meta, 'human_in_the_loop')
  const humanReviewGate = obj(meta, 'human_review_gate') ?? humanInTheLoop
  const pendingDecision = obj(humanInTheLoop ?? {}, 'pending_decision')
  const toolResults = obj(meta, 'agentic_tool_results')
  const boundedDose = obj(toolResults ?? {}, 'calculate_bounded_dose')
  const mode = agenticMode(meta)
  const skipped = skippedSet(orchestration)

  const hasAnyTrace = Object.keys(meta).length > 0
  const hasMonitoring = !!monitoring
  const hasDiagnostic = !!diagnostic && !skipped.has('diagnostic_reasoning_agent')
  const hasDecision = (!!decision || meta.confidence != null) && !skipped.has('decision_agent')
  const hasDosePlan = !!dosePlan && !skipped.has('dose_planning_agent')
  const hasConsistency = !!consistency && !skipped.has('consistency_review')
  const reviewStatus = text(consistency?.review_status, 'pass')
  const blocked = reviewStatus === 'block' || log.decision === 'wait_consistency_review' || log.decision === 'wait_safety_gate'
  const terminalAtMonitoring = skipped.has('diagnostic_reasoning_agent') || (hasMonitoring && !hasDiagnostic)
  const terminalAtDecision = skipped.has('dose_planning_agent') || (hasDecision && !hasDosePlan && ['wait', 'mix_longer', 'no_action'].includes(text(decision?.action, '')))
  const noHardwareCommand = log.pump_activated === 'none' || log.dose_ml <= 0 || log.duration_ms <= 0
  const reviewRequired = humanReviewGate?.review_required === true
  const hitlPending = isHITLPending(cycle.status, log.decision)
    || (['safety_gate', 'human_review_gate'].includes(currentStage ?? '') && reviewRequired)
  const candidatePump = text(pendingDecision?.pump_activated ?? dosePlan?.pump_activated ?? boundedDose?.pump_activated, 'none')
  const hasDoseCandidate = candidatePump !== 'none'
  const candidateDose = typeof pendingDecision?.dose_ml === 'number'
    ? pendingDecision.dose_ml
    : typeof meta.requested_actuation_dose_ml === 'number'
      ? meta.requested_actuation_dose_ml
      : typeof boundedDose?.bounded_dose_ml === 'number'
        ? boundedDose.bounded_dose_ml
        : null
  const candidateDurationMs = typeof pendingDecision?.duration_ms === 'number'
    ? pendingDecision.duration_ms
    : typeof meta.requested_actuation_duration_ms === 'number'
      ? meta.requested_actuation_duration_ms
      : typeof boundedDose?.duration_ms === 'number'
        ? boundedDose.duration_ms
        : null
  const componentDose = typeof meta.dose_ml_per_component === 'number'
  const safetyGateValidated = currentStage === 'safety_gate'
    && hasDoseCandidate
    && candidateDose != null
    && candidateDurationMs != null
    && reviewStatus === 'pass'
  const safetyGateActive = currentStage === 'safety_gate'
  const executionCommandReady = currentStage === 'execution_agent'
    && hasDoseCandidate
    && candidateDose != null
    && candidateDurationMs != null
    && reviewStatus === 'pass'
    && !reviewRequired
  const esp32Active = cycle.status === 'dosing' || cycle.status === 'human_command_dispatched'
  const esp32Done = ['completed', 'mixing'].includes(cycle.status) || (hasAnyTrace && noHardwareCommand)

  const base: GraphNode[] = [
    {
      key: 'input_context',
      title: 'Inputs & context',
      eyebrow: agenticModeLabel(mode),
      summary: `pH ${log.ph.toFixed(2)}, EC ${log.ec.toFixed(2)}, ${log.temperature.toFixed(1)}°C, ${log.reservoir_volume_liters.toFixed(1)} L`,
      detail: `Mode: ${agenticModeLabel(mode)}. Stability: pH ${log.ph_stable_for_seconds ?? 'n/a'}s, EC ${log.ec_stable_for_seconds ?? 'n/a'}s. Includes active reference range, recent history, baseline shadow, and crop lifecycle context when available.`,
      status: hasAnyTrace ? 'completed' : 'pending',
      icon: Database,
    },
    {
      key: 'orchestrator_agent',
      title: 'Orchestrator agent',
      eyebrow: 'Agent',
      summary: orchestration
        ? `${label(orchestration.action, 'Routed')} to ${label(orchestration.route, 'next stage')}`
        : 'Waiting for routing details',
      detail: detailText(orchestration?.reason, PIPELINE_STAGE_META.orchestrator_agent.operatorSummary),
      status: savedStatus(hasAnyTrace),
      icon: Route,
    },
    {
      key: 'monitoring_agent',
      title: 'Monitoring agent',
      eyebrow: 'Agent',
      summary: monitoring
        ? `${label(monitoring.status)} · ${monitoring.is_stable ? 'stable' : 'not stable'}`
        : 'Waiting for monitoring output',
      detail: detailText(monitoring?.summary, PIPELINE_STAGE_META.monitoring_agent.operatorSummary),
      status: savedStatus(hasMonitoring),
      icon: Activity,
    },
    {
      key: 'diagnostic_reasoning_agent',
      title: 'Diagnostic reasoning agent',
      eyebrow: 'Agent',
      summary: hasDiagnostic && diagnostic
        ? `${label(diagnostic.classification)} · ${label(diagnostic.primary_metric)} prioritized`
        : 'Not reached for this cycle',
      detail: hasDiagnostic
        ? diagnosisDetail(diagnostic?.summary)
        : terminalAtMonitoring ? 'Skipped because Monitoring feedback routed directly to Safety Gate.' : PIPELINE_STAGE_META.diagnostic_reasoning_agent.operatorSummary,
      status: savedStatus(hasDiagnostic, terminalAtMonitoring),
      icon: Brain,
    },
    {
      key: 'decision_agent',
      title: 'Decision agent',
      eyebrow: 'Agent',
      summary: hasDecision
        ? `${label(decision?.action ?? log.decision)} · confidence ${pct(meta.confidence)}`
        : 'Not reached for this cycle',
      detail: hasDecision
        ? detailText(decision?.reason, PIPELINE_STAGE_META.decision_agent.operatorSummary)
        : terminalAtMonitoring ? 'Skipped because no downstream decision was required.' : PIPELINE_STAGE_META.decision_agent.operatorSummary,
      status: savedStatus(hasDecision, terminalAtMonitoring),
      icon: GitBranch,
    },
    {
      key: 'dose_planning_agent',
      title: 'Dose planning agent',
      eyebrow: 'Agent',
      summary: hasDosePlan && dosePlan
        ? `${label(dosePlan.pump_activated)} · dose ${typeof dosePlan.dose_adjustment_factor === 'number' ? `x${dosePlan.dose_adjustment_factor.toFixed(2)}` : 'not set'}`
        : 'No dose plan needed',
      detail: hasDosePlan
        ? detailText(dosePlan?.reason, PIPELINE_STAGE_META.dose_planning_agent.operatorSummary)
        : terminalAtDecision || terminalAtMonitoring ? 'Skipped because the selected action did not require dosing.' : PIPELINE_STAGE_META.dose_planning_agent.operatorSummary,
      status: savedStatus(hasDosePlan, terminalAtMonitoring || terminalAtDecision),
      icon: ClipboardCheck,
    },
    {
      key: 'consistency_review',
      title: 'Consistency review',
      eyebrow: 'Deterministic',
      summary: hasConsistency && consistency ? `${label(consistency.review_status, 'Pass')} · ${Array.isArray(consistency.issues) ? consistency.issues.length : 0} issue(s)` : 'Not reached for this cycle',
      detail: hasConsistency
        ? detailText(consistency?.reason, PIPELINE_STAGE_META.consistency_review.operatorSummary)
        : 'Skipped when no candidate dose plan needed cross-checking.',
      status: savedStatus(hasConsistency, !hasDosePlan && hasAnyTrace, blocked),
      icon: SlidersHorizontal,
    },
    {
      key: 'safety_gate',
      title: 'Deterministic safety gate',
      eyebrow: 'Safety',
      summary: safetyGateValidated
        ? `${label(candidatePump)} · ${formatDoseMl(candidateDose)}${componentDose ? ' per component' : ''} · ${formatPumpDuration(candidateDurationMs)} · ${reviewRequired ? 'validated for review' : 'validated'}`
        : safetyGateActive && hasDoseCandidate
          ? `${label(candidatePump)} candidate · validating`
        : hitlPending && hasDoseCandidate
          ? `${label(candidatePump)}${candidateDose != null ? ` · ${formatDoseMl(candidateDose)}` : ''} · cleared for review`
          : noHardwareCommand
            ? `${label(log.decision)} · no pump command`
        : `${label(log.pump_activated)} · ${formatDoseMl(log.dose_ml)} · ${formatPumpDuration(log.duration_ms)}`,
      detail: blocked
        ? 'The command was blocked or converted to a wait condition before hardware execution.'
        : safetyGateValidated
          ? `${reviewRequired ? 'Validated proposal' : 'Validated command'}: ${label(candidatePump)}, ${formatDoseMl(candidateDose)}${componentDose ? ' per component' : ''}, with a ${formatPumpDuration(candidateDurationMs)} runtime. Dose, duration, mixing, confirmation, and operational safety checks passed. ${reviewRequired ? 'Human review is required before ESP32 execution.' : 'The physical pump remains off until execution.'}`
        : safetyGateActive && hasDoseCandidate
          ? 'Validating the candidate pump, dose, duration, mixing window, confirmation state, and operational safety constraints. The physical pump remains off.'
        : 'Final validation applied dose, duration, mixing, confirmation, and operational safety constraints.',
      status: savedStatus(hasAnyTrace, false, blocked),
      icon: ShieldCheck,
    },
    {
      key: 'human_review_gate',
      title: 'Human review gate',
      eyebrow: 'Operator',
      summary: hitlPending
        ? 'Held for operator review'
        : noHardwareCommand
          ? 'No command to review'
          : 'No HITL hold active',
      detail: hitlPending
        ? 'The bounded AI command is stored and must be approved, rejected, or overridden before ESP32 execution.'
        : detailText(humanReviewGate?.summary, PIPELINE_STAGE_META.human_review_gate.operatorSummary),
      status: hitlPending ? 'active' : savedStatus(hasAnyTrace),
      icon: UserCheck,
    },
    {
      key: 'final_decision',
      title: 'Final backend decision',
      eyebrow: 'Output',
      summary: safetyGateValidated
        ? reviewRequired ? 'Bounded proposal awaiting human review' : 'Bounded pump command ready'
        : hitlPending ? 'Bounded proposal awaiting human review'
          : noHardwareCommand ? 'Wait / monitor decision stored' : 'Bounded pump command stored',
      detail: `Decision ${formatDisplayText(log.decision)} with control cycle #${log.control_cycle_id}.`,
      status: savedStatus(hasAnyTrace),
      icon: CheckCircle2,
    },
    {
      key: 'esp32_execution',
      title: 'ESP32 execution',
      eyebrow: 'Embedded',
      summary: hitlPending
        ? 'Held for human review'
        : executionCommandReady
          ? `${label(candidatePump)} command handed to ESP32`
        : esp32Active
          ? 'Pump actuation in progress'
          : noHardwareCommand
            ? 'No physical actuation'
            : esp32Done
              ? 'Execution completed'
              : 'Waiting for execution status',
      detail: hitlPending
        ? 'Operator approval, rejection, or override is required before actuation.'
        : executionCommandReady
          ? `${formatDoseMl(candidateDose)}${componentDose ? ' per component' : ''} at ${formatPumpDuration(candidateDurationMs)} was dispatched to the embedded controller; pump progress appears on Dosing.`
        : noHardwareCommand
          ? 'The embedded controller remains idle because the backend returned no pump command.'
          : `Pump ${formatDisplayText(log.pump_activated)} is executed by the ESP32, then the cycle status is reported back.`,
      status: hitlPending ? 'active' : esp32Active ? 'active' : esp32Done || noHardwareCommand ? 'completed' : 'pending',
      icon: Bot,
    },
    {
      key: 'history_update',
      title: 'Next reading / history',
      eyebrow: 'Feedback',
      summary: cycle.status === 'mixing' ? 'Post-dose mixing window active' : 'Latest cycle available as history',
      detail: 'The next stable pH/EC reading and control-cycle status become recent history for later agentic decisions.',
      status: esp32Done || noHardwareCommand ? 'completed' : 'pending',
      icon: History,
    },
  ]

  return base.map((node) => {
    const live = liveStatus(node.key, currentStage)
    return live ? { ...node, status: live } : node
  })
}

export function LiveAgentStateGraph({
  latestLog,
  cycle,
  currentStage = null,
  mode = 'live',
  defaultExpanded,
}: {
  latestLog?: LatestLog
  cycle?: ControlCycle
  currentStage?: string | null
  mode?: GraphMode
  defaultExpanded?: boolean
}) {
  const nodes = useMemo(
    () => mode === 'help' || !latestLog || !cycle ? helpNodes() : liveNodes(latestLog, cycle, currentStage),
    [cycle, currentStage, latestLog, mode],
  )
  const [selectedKey, setSelectedKey] = useState<GraphNodeKey | null>(null)
  const [hoveredKey, setHoveredKey] = useState<GraphNodeKey | null>(null)
  const [nodeLayout, setNodeLayout] = useState<NodeLayout>(NODE_LAYOUT)
  const [draggedKey, setDraggedKey] = useState<GraphNodeKey | null>(null)
  const [isPanning, setIsPanning] = useState(false)
  const [isExpanded, setIsExpanded] = useState(defaultExpanded ?? mode === 'help')
  const [isFullscreen, setIsFullscreen] = useState(false)
  const [useViewportSelection, setUseViewportSelection] = useState(
    () => typeof window !== 'undefined' && window.matchMedia('(max-width: 900px)').matches,
  )
  const [zoom, setZoom] = useState(1)
  const selected = selectedKey ? nodes.find((node) => node.key === selectedKey) : null
  const activeNode = nodes.find((node) => node.status === 'active')
  const highlightedKey = hoveredKey ?? selectedKey
  const graphTone = activeNode ? 'live' : mode === 'help' ? 'guide' : 'idle'
  const positioned = useMemo(() => visualNodes(nodes, nodeLayout), [nodeLayout, nodes])
  const nodesByKey = useMemo(
    () => new Map(positioned.map((node) => [node.key, node])),
    [positioned],
  )
  const canvasWidth = 1160
  const canvasHeight = 560
  const selectionWidth = 400
  const selectionHeight = 188
  const scaleOriginX = (canvasWidth - canvasWidth * zoom) / 2
  const scaleOriginY = (canvasHeight - canvasHeight * zoom) / 2
  const selectedVisual = selected ? nodesByKey.get(selected.key) : null
  const selectedOverlay = selectedVisual ? (() => {
    const nodeLeft = scaleOriginX + selectedVisual.x * zoom
    const nodeRight = scaleOriginX + (selectedVisual.x + selectedVisual.w) * zoom
    const nodeCenterY = scaleOriginY + (selectedVisual.y + selectedVisual.h / 2) * zoom
    const gap = 18
    const rightFits = nodeRight + gap + selectionWidth <= canvasWidth - 14
    return {
      x: clamp(
        rightFits ? nodeRight + gap : nodeLeft - gap - selectionWidth,
        14,
        canvasWidth - selectionWidth - 14,
      ),
      y: clamp(nodeCenterY - selectionHeight / 2, 14, canvasHeight - selectionHeight - 14),
    }
  })() : null
  const svgRef = useRef<SVGSVGElement>(null)
  const canvasRef = useRef<HTMLDivElement>(null)
  const graphRef = useRef<HTMLElement>(null)
  const dragRef = useRef<DragState | null>(null)
  const panRef = useRef<PanState | null>(null)

  useEffect(() => {
    const media = window.matchMedia('(max-width: 900px)')
    const updateSelectionLayout = () => setUseViewportSelection(media.matches)
    updateSelectionLayout()
    media.addEventListener('change', updateSelectionLayout)
    return () => media.removeEventListener('change', updateSelectionLayout)
  }, [])

  useEffect(() => {
    if (!isFullscreen) return
    document.body.classList.add('live-agent-graph-focus-open')
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setIsFullscreen(false)
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.body.classList.remove('live-agent-graph-focus-open')
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [isFullscreen])

  useEffect(() => {
    const dismissFromOutside = (event: globalThis.PointerEvent) => {
      const graph = graphRef.current
      const target = event.target
      if (graph && target instanceof Node && !graph.contains(target)) {
        setSelectedKey(null)
      }
    }
    const dismissFromEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setSelectedKey(null)
    }

    document.addEventListener('pointerdown', dismissFromOutside)
    document.addEventListener('keydown', dismissFromEscape)
    return () => {
      document.removeEventListener('pointerdown', dismissFromOutside)
      document.removeEventListener('keydown', dismissFromEscape)
    }
  }, [])

  const dismissSelectedStage = (event: PointerEvent<HTMLElement>) => {
    const target = event.target
    if (!(target instanceof Element)) return
    if (target.closest('.live-agent-svg-node, .live-agent-graph-selection')) return
    setSelectedKey(null)
  }

  const pointFromEvent = (event: PointerEvent): { x: number; y: number } => {
    const svg = svgRef.current
    if (!svg) return { x: 0, y: 0 }
    const rect = svg.getBoundingClientRect()
    const viewX = ((event.clientX - rect.left) / rect.width) * canvasWidth
    const viewY = ((event.clientY - rect.top) / rect.height) * canvasHeight
    return {
      x: (viewX - scaleOriginX) / zoom,
      y: (viewY - scaleOriginY) / zoom,
    }
  }

  const resetGraphView = () => {
    setZoom(1)
    setNodeLayout(NODE_LAYOUT)
    setDraggedKey(null)
    setIsPanning(false)
    setHoveredKey(null)
    setSelectedKey(null)
    dragRef.current = null
    panRef.current = null
    requestAnimationFrame(() => {
      canvasRef.current?.scrollTo({ left: 0, top: 0, behavior: 'smooth' })
    })
  }

  const toggleFullscreen = () => {
    setIsExpanded(true)
    setIsFullscreen((value) => !value)
  }

  const panGraphCanvas = (event: WheelEvent<HTMLDivElement>) => {
    const canvas = canvasRef.current
    if (!canvas) return
    const hasHorizontalOverflow = canvas.scrollWidth > canvas.clientWidth
    const hasVerticalOverflow = canvas.scrollHeight > canvas.clientHeight
    if (!hasHorizontalOverflow && !hasVerticalOverflow) return

    const xDelta = event.shiftKey || Math.abs(event.deltaX) > Math.abs(event.deltaY)
      ? event.deltaX || event.deltaY
      : event.deltaY
    canvas.scrollLeft += xDelta
    if (Math.abs(event.deltaX) > Math.abs(event.deltaY) && hasVerticalOverflow) {
      canvas.scrollTop += event.deltaY
    }
    event.preventDefault()
  }

  const moveDraggedNode = (event: PointerEvent) => {
    const drag = dragRef.current
    const pan = panRef.current
    if (!drag && !pan) return
    event.preventDefault()
    if (pan) {
      const canvas = canvasRef.current
      if (!canvas) return
      const dx = event.clientX - pan.lastX
      const dy = event.clientY - pan.lastY
      pan.lastX = event.clientX
      pan.lastY = event.clientY
      canvas.scrollLeft += dx
      canvas.scrollTop += dy
      return
    }
    if (!drag) return
    const point = pointFromEvent(event)
    const dx = point.x - drag.lastX
    const dy = point.y - drag.lastY
    drag.lastX = point.x
    drag.lastY = point.y
    drag.moved = drag.moved || Math.abs(point.x - drag.startX) > 2 || Math.abs(point.y - drag.startY) > 2
    setNodeLayout((current) => {
      const node = current[drag.key]
      return {
        ...current,
        [drag.key]: {
          ...node,
          x: clamp(node.x + dx, 18, canvasWidth - node.w - 18),
          y: clamp(node.y + dy, 34, canvasHeight - node.h - 42),
        },
      }
    })
  }

  const finishDrag = (event: PointerEvent) => {
    const drag = dragRef.current
    const pan = panRef.current
    if (!drag && !pan) return
    event.preventDefault()
    if (drag && !drag.moved) setSelectedKey(drag.key)
    dragRef.current = null
    panRef.current = null
    setDraggedKey(null)
    setIsPanning(false)
  }

  const startCanvasPan = (event: PointerEvent) => {
    if (event.button !== 0) return
    const target = event.target
    if (target instanceof Element && target.closest('.live-agent-svg-node, .live-agent-graph-selection')) return
    const canvas = canvasRef.current
    if (!canvas) return
    const canPan = canvas.scrollWidth > canvas.clientWidth || canvas.scrollHeight > canvas.clientHeight
    if (!canPan) return
    event.preventDefault()
    panRef.current = {
      lastX: event.clientX,
      lastY: event.clientY,
    }
    setIsPanning(true)
    event.currentTarget.setPointerCapture(event.pointerId)
  }

  return (
    <section
      ref={graphRef}
      className={`live-agent-graph ${graphTone}${isExpanded ? ' expanded' : ' collapsed'}${isFullscreen ? ' fullscreen' : ''}`}
      aria-label="Live Agentic AI state graph"
      role={isFullscreen ? 'dialog' : undefined}
      aria-modal={isFullscreen || undefined}
      onPointerDownCapture={dismissSelectedStage}
    >
      <div className="live-agent-graph-head">
        <div>
          <span>
            {mode === 'help'
              ? 'Interactive Guide'
              : activeNode
                ? 'Live State Graph · Live'
                : 'Live State Graph · Latest Trace'}
          </span>
          <h2>{activeNode
            ? activeNode.key === 'esp32_execution'
              ? activeNode.title
              : `${activeNode.title} is running`
            : mode === 'help'
              ? 'How Agentic AI moves through a decision'
              : 'Latest agentic decision path'}</h2>
          <p>
            {activeNode
              ? activeNode.summary
              : mode === 'help'
                ? 'Select a stage to learn what it contributes before a command can reach the ESP32.'
                : 'Select a stage to inspect the latest saved reasoning, safety checks, and execution status.'}
          </p>
        </div>
        <div className="live-agent-graph-actions">
          <button
            type="button"
            className="live-agent-graph-toggle"
            onClick={toggleFullscreen}
            aria-label={isFullscreen ? 'Exit graph full screen' : 'Open graph full screen'}
          >
            {isFullscreen
              ? <Minimize2 size={15} strokeWidth={2.2} aria-hidden="true" />
              : <Maximize2 size={15} strokeWidth={2.2} aria-hidden="true" />}
            {isFullscreen ? 'Exit full screen' : 'Full screen'}
          </button>
          {!isFullscreen && (
          <button
            type="button"
            className="live-agent-graph-toggle"
            aria-expanded={isExpanded}
            onClick={() => setIsExpanded((value) => !value)}
          >
            {isExpanded
              ? <EyeOff size={15} strokeWidth={2.2} aria-hidden="true" />
              : <Eye size={15} strokeWidth={2.2} aria-hidden="true" />}
            {isExpanded ? 'Hide graph' : 'Show graph'}
          </button>
          )}
        </div>
      </div>

      {isExpanded && (
        <div className="live-agent-graph-layout">
          <div className="live-agent-graph-legend" aria-label="Graph legend">
            <span><i className="completed" />Completed</span>
            <span><i className="active" />Running</span>
            <span><i className="pending" />Waiting</span>
            <span><i className="skipped" />Skipped</span>
            <span><i className="blocked" />Blocked</span>
            <span className="edge-key main">Process flow</span>
            <span className="edge-key routing">Agent routing</span>
          </div>
          <div
            className={`live-agent-graph-canvas${isPanning ? ' panning' : ''}`}
            ref={canvasRef}
            onWheel={panGraphCanvas}
          >
            <div className="live-agent-graph-tools" aria-label="Graph zoom controls">
              <button
                type="button"
                title="Zoom in"
                aria-label="Zoom in"
                onClick={() => setZoom((value) => Math.min(1.35, value + 0.1))}
              >
                <Plus size={15} strokeWidth={2.4} aria-hidden="true" />
              </button>
              <button
                type="button"
                title="Zoom out"
                aria-label="Zoom out"
                onClick={() => setZoom((value) => Math.max(0.78, value - 0.1))}
              >
                <Minus size={15} strokeWidth={2.4} aria-hidden="true" />
              </button>
              <button type="button" onClick={resetGraphView}>Reset view</button>
            </div>
            <svg
              ref={svgRef}
              className="live-agent-svg"
              viewBox={`0 0 ${canvasWidth} ${canvasHeight}`}
              role="img"
              aria-label="Interactive Agentic AI state graph"
              onPointerDown={startCanvasPan}
              onPointerMove={moveDraggedNode}
              onPointerUp={finishDrag}
              onPointerCancel={finishDrag}
            >
              <defs>
                <marker id="live-agent-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                  <path d="M 0 0 L 10 5 L 0 10 z" fill="context-stroke" />
                </marker>
              </defs>
              <g transform={`translate(${scaleOriginX} ${scaleOriginY}) scale(${zoom})`}>
              <g className="live-agent-edges">
                {GRAPH_EDGES.map((edge) => {
                  const from = nodesByKey.get(edge.from)
                  const to = nodesByKey.get(edge.to)
                  if (!from || !to) return null
                  const complete = edgeIsComplete(edge, nodesByKey)
                  const active = from.status === 'active' || to.status === 'active'
                  const related = highlightedKey != null && (edge.from === highlightedKey || edge.to === highlightedKey)
                  return (
                    <path
                      key={`${edge.from}-${edge.to}-${edge.kind ?? 'main'}`}
                      className={`live-agent-edge ${edge.kind ?? 'main'}${complete ? ' complete' : ''}${active ? ' active' : ''}${related ? ' related' : ''}`}
                      d={edgePath(from, to, edge.kind)}
                      markerEnd="url(#live-agent-arrow)"
                    />
                  )
                })}
              </g>
              <g className="live-agent-nodes">
                {positioned.map((node) => {
                  const isSelected = selected?.key === node.key
                  const isHovered = hoveredKey === node.key
                  const NodeIcon = node.icon
                  return (
                    <g
                      key={node.key}
                      className={`live-agent-svg-node ${node.status}${isSelected ? ' selected' : ''}${isHovered ? ' hovered' : ''}${draggedKey === node.key ? ' dragging' : ''}`}
                      transform={`translate(${node.x} ${node.y})`}
                      onPointerEnter={() => setHoveredKey(node.key)}
                      onPointerLeave={() => setHoveredKey((key) => key === node.key ? null : key)}
                      onPointerDown={(event) => {
                        event.preventDefault()
                        const point = pointFromEvent(event)
                        dragRef.current = {
                          key: node.key,
                          lastX: point.x,
                          lastY: point.y,
                          startX: point.x,
                          startY: point.y,
                          moved: false,
                        }
                        setDraggedKey(node.key)
                        setSelectedKey(node.key)
                        event.currentTarget.setPointerCapture(event.pointerId)
                      }}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter' || event.key === ' ') {
                          event.preventDefault()
                          setSelectedKey(node.key)
                        }
                      }}
                      role="button"
                      tabIndex={0}
                      aria-label={`${node.title}: ${node.summary}`}
                    >
                      <circle className="live-agent-node-ring" cx={node.w / 2} cy={node.h / 2} r={node.key === 'orchestrator_agent' ? 39 : 31} />
                      <circle className="live-agent-node-orb" cx={node.w / 2} cy={node.h / 2} r={node.key === 'orchestrator_agent' ? 25 : 20} />
                      <foreignObject
                        className="live-agent-node-icon-fo"
                        x={node.w / 2 - 13}
                        y={node.h / 2 - 13}
                        width="26"
                        height="26"
                      >
                        <div className="live-agent-node-svg-icon">
                          <NodeIcon size={18} strokeWidth={2.4} />
                        </div>
                      </foreignObject>
                      <circle className="live-agent-node-status" cx={node.w - 9} cy="10" r="5" />
                      <text
                        className="live-agent-node-label"
                        x={node.w / 2}
                        y={node.h / 2 + (node.key === 'orchestrator_agent' ? 57 : 49)}
                        textAnchor="middle"
                      >
                        {NODE_SHORT_LABELS[node.key]}
                      </text>
                    </g>
                  )
                })}
              </g>
              </g>
              {selected && selectedOverlay && !useViewportSelection && (
                <foreignObject
                  className="live-agent-selection-fo"
                  x={selectedOverlay.x}
                  y={selectedOverlay.y}
                  width={selectionWidth}
                  height={selectionHeight}
                >
                  <aside
                    className={`live-agent-graph-selection ${selected.status}`}
                    aria-label={`${selected.title} stage details`}
                  >
                    <div className="live-agent-graph-selection-copy">
                      <span>{selected.eyebrow} · {formatDisplayText(selected.status)}</span>
                      <strong>{selected.title}</strong>
                      <p>{selected.summary}</p>
                      <small>{selected.detail}</small>
                    </div>
                    <div className="live-agent-graph-selection-meta">
                      <span>Stage {ORDER.indexOf(selected.key) + 1} of {ORDER.length}</span>
                      <button
                        type="button"
                        onClick={() => setSelectedKey(null)}
                        aria-label="Close stage details"
                        title="Close stage details"
                      >
                        <X size={16} strokeWidth={2.2} aria-hidden="true" />
                      </button>
                    </div>
                  </aside>
                </foreignObject>
              )}
            </svg>
          </div>
          {selected && useViewportSelection && (
            <aside
              className={`live-agent-graph-selection mobile-viewport ${selected.status}`}
              aria-label={`${selected.title} stage details`}
            >
              <div className="live-agent-graph-selection-copy">
                <span>{selected.eyebrow} · {formatDisplayText(selected.status)}</span>
                <strong>{selected.title}</strong>
                <p>{selected.summary}</p>
                <small>{selected.detail}</small>
              </div>
              <div className="live-agent-graph-selection-meta">
                <span>Stage {ORDER.indexOf(selected.key) + 1} of {ORDER.length}</span>
                <button
                  type="button"
                  onClick={() => setSelectedKey(null)}
                  aria-label="Close stage details"
                  title="Close stage details"
                >
                  <X size={16} strokeWidth={2.2} aria-hidden="true" />
                </button>
              </div>
            </aside>
          )}
        </div>
      )}
    </section>
  )
}
