import { isReadingStale, recordedDate } from '../readingFreshness'
import { Bot, BrainCircuit, CheckCircle2, Clock3, GitBranch, ShieldCheck } from 'lucide-react'
import type { LatestLog, ControlCycle, BatchStatus } from '../types'
import { deviationLabel, formatCountdown, formatDoseMl, formatDuration, formatPumpDuration, formatTime } from '../utils'
import { Pill } from '../components/ui/Pill'
import { Panel } from '../components/ui/Panel'
import { InfoList } from '../components/ui/InfoList'
import { LiveAgentStateGraph } from '../components/ui/LiveAgentStateGraph'
import { formatDisplayText } from '../text'

type Meta = Record<string, unknown>

const REASONING_STAGE_ORDER = [
  'orchestrator_agent',
  'monitoring_agent',
  'diagnostic_reasoning_agent',
  'decision_agent',
  'dose_planning_agent',
  'consistency_review',
  'safety_gate',
  'human_review_gate',
  'execution_agent',
] as const

function getMeta(log: LatestLog): Meta {
  return (log.decision_metadata ?? {}) as Meta
}

function getAgent<T = Meta>(meta: Meta, key: string): T | undefined {
  return meta[key] as T | undefined
}

function stageAtOrAfter(currentStage: string | null | undefined, stage: typeof REASONING_STAGE_ORDER[number]): boolean {
  if (!currentStage) return false
  return REASONING_STAGE_ORDER.indexOf(currentStage as typeof REASONING_STAGE_ORDER[number]) >= REASONING_STAGE_ORDER.indexOf(stage)
}

function commandPreview(meta: Meta): { pump: string; dose: number; durationMs: number; perComponent: boolean } {
  const dosePlan = getAgent<Meta>(meta, 'dose_planning_agent')
  const tools = getAgent<Meta>(meta, 'agentic_tool_results')
  const boundedDose = getAgent<Meta>(tools ?? {}, 'calculate_bounded_dose')
  return {
    pump: str(dosePlan?.pump_activated ?? boundedDose?.pump_activated, 'none'),
    dose: typeof meta.dose_ml_per_component === 'number'
      ? meta.dose_ml_per_component
      : typeof meta.requested_actuation_dose_ml === 'number'
        ? meta.requested_actuation_dose_ml
        : typeof boundedDose?.bounded_dose_ml === 'number'
          ? boundedDose.bounded_dose_ml
          : 0,
    durationMs: typeof meta.requested_actuation_duration_ms === 'number'
      ? meta.requested_actuation_duration_ms
      : typeof boundedDose?.duration_ms === 'number'
        ? boundedDose.duration_ms
        : 0,
    perComponent: typeof meta.dose_ml_per_component === 'number',
  }
}

function str(v: unknown, fallback = '—'): string {
  return typeof v === 'string' ? v : fallback
}

function label(v: unknown, fallback = '—'): string {
  const value = str(v, fallback)
  return value === fallback ? value : formatDisplayText(value)
}

function historySignalLabel(v: unknown): string {
  if (typeof v !== 'string') return 'No history signal'
  if (v === 'previous_same_pump_still_unresolved') return 'Previous same-pump command unresolved'
  if (v === 'previous_same_pump_overshot_opposite_direction') return 'Previous same-pump overshot opposite direction'
  if (v === 'no_previous_same_pump') return 'No same-pump history'
  return label(v)
}

function decisionLabel(v: unknown, fallback = '—'): string {
  const value = str(v, fallback)
  if (value === fallback) return value
  if (value === 'ph_high') return 'pH High'
  if (value === 'ph_low') return 'pH Low'
  if (value === 'ec_high') return 'EC High'
  if (value === 'ec_low') return 'EC Low'
  if (value === 'within_range') return 'Within Range'
  if (value === 'within_control_tolerance') return 'Within Control Tolerance'
  if (value === 'no_action') return 'No Action'
  return formatDisplayText(value)
}

function comparisonValue(value: string): string {
  if (value === 'within_range') return 'Within Range'
  if (value === 'within_control_tolerance') return 'Within Control Tolerance'
  if (value === 'no_action') return 'No Action'
  if (value.includes('_')) return formatDisplayText(value)
  return value
}

function reasoningHeading(log: LatestLog, meta: Meta): string {
  const dosePlan = getAgent<Meta>(meta, 'dose_planning_agent')
  const plannedPump = str(dosePlan?.pump_activated, 'none')
  const displayedPump = log.pump_activated !== 'none' ? log.pump_activated : plannedPump
  if (displayedPump === 'none') {
    return 'Why HANAS did not dose'
  }
  return `Why HANAS chose ${label(displayedPump)}`
}

function pct(v: unknown, fallback = '—'): string {
  return typeof v === 'number' ? `${Math.round(v * 100)}%` : fallback
}

function factor(v: unknown): string {
  return typeof v === 'number' ? `×${v}` : 'Not applied'
}

function doseFactorLabel(v: unknown, fallback = '×1.00'): string {
  return typeof v === 'number' ? `×${v.toFixed(2)}` : fallback
}

function AgentPanel({
  title,
  items,
  className = 'span-4',
}: {
  title: string
  items: Array<[string, string]>
  className?: string
}) {
  return (
    <Panel title={title} eyebrow="Agent" className={`${className} agent-panel`}>
      <InfoList items={items} />
    </Panel>
  )
}

function ResearchResultStrip({
  latestLog,
  cycle,
  confidence,
  batchStatus,
  isBatchCollection,
  currentStage,
}: {
  latestLog: LatestLog
  cycle: ControlCycle
  confidence: string
  batchStatus?: BatchStatus | null
  isBatchCollection: boolean
  currentStage?: string | null
}) {
  const meta = getMeta(latestLog)
  const consistency = getAgent<Meta>(meta, 'consistency_review')
  const humanReviewGate = getAgent<Meta>(meta, 'human_review_gate')
  const preview = commandPreview(meta)
  const reviewPass = str(consistency?.review_status, 'pass') === 'pass'
  const diagnostic = getAgent<Meta>(meta, 'diagnostic_reasoning_agent')
  const primaryMetric = label(diagnostic?.primary_metric, 'Primary metric recorded')
  const primaryMetricDetail = ['Primary metric recorded', 'None', '—'].includes(primaryMetric)
    ? 'No actionable metric'
    : `Primary metric: ${primaryMetric}`
  const showPlannedCommand = stageAtOrAfter(currentStage, 'dose_planning_agent')
    && preview.pump !== 'none'
    && preview.dose > 0
    && preview.durationMs > 0
  const showSafetyResult = currentStage === 'safety_gate'
    && showPlannedCommand
    && reviewPass
  const showDispatchedCommand = currentStage === 'execution_agent' && showPlannedCommand && reviewPass
  const requiresHumanReview = humanReviewGate?.review_required === true
  const showHumanReview = currentStage === 'human_review_gate' && showPlannedCommand && requiresHumanReview
  const safetyReached = !currentStage || stageAtOrAfter(currentStage, 'safety_gate')
  const displayedPump = showPlannedCommand ? preview.pump : latestLog.pump_activated
  const doseLabel = showPlannedCommand
    ? `${formatDoseMl(preview.dose)}${preview.perComponent ? ' per component' : ''} · ${formatPumpDuration(preview.durationMs)}${showSafetyResult && requiresHumanReview ? ' · human review next' : showHumanReview ? ' · operator action required' : ''}`
    : latestLog.dose_ml > 0
      ? `${formatDoseMl(latestLog.dose_ml)}${preview.perComponent ? ' per component' : ''} · ${Number((cycle.duration_ms / 1000).toFixed(3))} s`
    : 'No pump actuation'
  const latestBatch = batchStatus?.batch_latest_analysis
  const nextBatch = batchStatus?.batch_scheduler_next_run_seconds
  const resultCards = isBatchCollection ? [
    {
      label: 'Reading status',
      value: 'Saved',
      detail: 'Sensor row recorded',
      icon: CheckCircle2,
    },
    {
      label: 'AI review',
      value: 'Awaiting batch',
      detail: nextBatch != null ? `Scheduled in ${formatCountdown(nextBatch)}` : 'Scheduled batch pending',
      icon: Clock3,
    },
    {
      label: 'Latest completed batch',
      value: latestBatch ? `Cycle #${latestBatch.control_cycle_id}` : 'None yet',
      detail: latestBatch
        ? `${label(latestBatch.status)}${latestBatch.timestamp ? ` · ${formatTime(latestBatch.timestamp)}` : ''}`
        : 'Awaiting the first completed run',
      icon: BrainCircuit,
    },
    {
      label: 'Pump command',
      value: 'None',
      detail: 'Collection rows cannot actuate pumps',
      icon: ShieldCheck,
    },
  ] : [
    {
      label: 'Detected condition',
      value: diagnostic?.classification === 'combined_disturbance'
        ? 'Combined disturbance'
        : decisionLabel(latestLog.decision),
      detail: primaryMetricDetail,
      icon: CheckCircle2,
    },
    {
      label: showHumanReview
        ? 'Awaiting human review'
        : showDispatchedCommand
        ? 'Command sent to ESP32'
        : showSafetyResult
          ? requiresHumanReview ? 'Validated proposal' : 'Validated command'
          : showPlannedCommand ? 'Proposed command' : 'Corrective command',
      value: label(displayedPump),
      detail: doseLabel,
      icon: ShieldCheck,
    },
    {
      label: 'Confidence',
      value: confidence,
      icon: BrainCircuit,
    },
    {
      label: 'Safety trace',
      value: !safetyReached ? 'Pending' : reviewPass ? 'Passed' : 'Needs review',
      detail: !safetyReached ? 'Final deterministic gate not reached yet' : reviewPass ? undefined : 'Review required before actuation',
      icon: GitBranch,
    },
  ]

  return (
    <section className="research-result-strip" aria-label="Agentic AI research result summary">
      {resultCards.map(({ label: itemLabel, value, detail, icon: Icon }) => (
        <div className="research-result-card" key={itemLabel}>
          <span className="research-result-icon" aria-hidden="true">
            <Icon size={18} strokeWidth={2.2} />
          </span>
          <div>
            <span>{itemLabel}</span>
            <strong>{value}</strong>
            {detail && <small>{detail}</small>}
          </div>
        </div>
      ))}
    </section>
  )
}

function BatchRunSummary({ batch }: { batch: BatchStatus['batch_latest_analysis'] }) {
  if (!batch) return 'No completed batch yet'
  const time = batch.timestamp ? formatTime(batch.timestamp) : 'No timestamp'
  return `Cycle #${batch.control_cycle_id}, ${label(batch.status)}, ${time}`
}

function agenticMode(meta: Meta): string {
  if (typeof meta.agentic_mode === 'string' && meta.agentic_mode.length > 0) return meta.agentic_mode
  if (typeof meta.triggered_by === 'string') {
    if (meta.triggered_by === 'batch_mode_collection') return 'batch_collection'
    if (meta.triggered_by === 'batch_scheduler' || meta.triggered_by === 'manual_batch_trigger') return 'batch_llm'
    if (meta.triggered_by === 'emergency_guard') return 'emergency_guard'
    if (meta.triggered_by === 'emergency_guard_mixing_wait') return 'emergency_guard_mixing_wait'
  }
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

function intervalLabel(seconds: number): string {
  if (seconds > 0 && seconds % 60 === 0) return `${seconds / 60} min`
  return formatDuration(seconds, { compact: true })
}

function Phase3OperatingModel({
  batchStatus,
  isBatchCollection,
}: {
  batchStatus?: BatchStatus | null
  isBatchCollection: boolean
}) {
  const interval = batchStatus?.batch_analysis_interval_seconds ?? 600
  const fullAgenticMode = batchStatus?.full_agentic_mode_enabled ?? false
  const batchInterval = intervalLabel(interval)
  const nextRunLabel = fullAgenticMode
    ? 'Runs on next sensor reading'
    : !batchStatus?.batch_analysis_enabled
    ? 'Batch disabled'
    : batchStatus.batch_scheduler_running
      ? `Next batch in ${formatCountdown(batchStatus.batch_scheduler_next_run_seconds)}`
      : 'Scheduler not running'
  const lastRunLabel = BatchRunSummary({ batch: batchStatus?.batch_latest_analysis ?? null })
  const schedulerState = fullAgenticMode
    ? 'Full Agentic'
    : !batchStatus?.batch_analysis_enabled
    ? 'Disabled'
    : batchStatus.batch_scheduler_running
      ? 'Scheduled'
      : 'Not running'
  const schedulerTone = schedulerState === 'Scheduled' || schedulerState === 'Full Agentic'
    ? 'good'
    : schedulerState === 'Disabled'
      ? 'neutral'
      : 'warn'

  return (
    <section className="phase3-model">
      <div className="phase3-model-header">
        <div>
          <span>Control operating model</span>
          <strong>{fullAgenticMode ? 'Full Agentic AI every minute with deterministic safety' : 'Deterministic safety every minute, Agentic AI on batch'}</strong>
        </div>
        <Pill label={schedulerState} tone={schedulerTone} />
      </div>
      <div className="phase3-model-grid">
        <div className="phase3-model-card">
          <span className="phase3-model-icon safety">
            <ShieldCheck size={18} strokeWidth={2.2} />
          </span>
          <div>
            <strong>{fullAgenticMode ? 'Full agentic pipeline' : 'Per-minute deterministic guard'}</strong>
            <p>{fullAgenticMode ? 'Every ESP32 reading runs the complete LLM pipeline using recent history and crop context.' : 'Every ESP32 reading is checked for emergency pH/EC thresholds, active pump commands, and mixing guard conditions.'}</p>
          </div>
          <Pill label="Every 1 min" tone="info" />
        </div>
        <div className="phase3-model-card primary">
          <span className="phase3-model-icon agent">
            <Bot size={18} strokeWidth={2.2} />
          </span>
          <div>
            <strong>{fullAgenticMode ? 'Deterministic safety gate' : 'Agentic AI batch pipeline'}</strong>
            <p>{fullAgenticMode ? 'Pump limits, active-command checks, mixing locks, and emergency stop still gate every AI recommendation.' : 'Full LLM reasoning runs on the scheduled batch using recent saved readings and safety context.'}</p>
          </div>
          <Pill label={fullAgenticMode ? 'Every cycle' : `Every ${batchInterval}`} tone="info" />
        </div>
        <div className="phase3-model-card">
          <span className="phase3-model-icon clock">
            <Clock3 size={18} strokeWidth={2.2} />
          </span>
          <div>
            <strong>{fullAgenticMode ? 'Scheduled batch paused' : isBatchCollection ? 'Current row saved for batch' : 'Latest Agentic AI run'}</strong>
            <p>Last run: {lastRunLabel}</p>
          </div>
          <Pill label={nextRunLabel} tone="neutral" />
        </div>
      </div>
    </section>
  )
}

function BaselinePanel({ cycle, latestLog }: { cycle: ControlCycle; latestLog: LatestLog }) {
  const meta = getMeta(latestLog)
  const preview = commandPreview(meta)
  const baseline = getAgent<Meta>(meta, 'baseline_shadow')
  const mixingWindow = getAgent<Meta>(meta, 'mixing_window')
  const mixingBase = typeof mixingWindow?.base_seconds === 'number'
    ? mixingWindow.base_seconds
    : Math.round(cycle.mixing_duration_seconds / 2)
  const adjustmentFactor = typeof meta.applied_dose_adjustment_factor === 'number'
    ? meta.applied_dose_adjustment_factor
    : null
  const agentDose = cycle.dose_ml > 0 ? cycle.dose_ml : preview.dose
  const agentDurationMs = cycle.duration_ms > 0 ? cycle.duration_ms : preview.durationMs
  const rows = [
    {
      label: 'Action',
      rule: decisionLabel(baseline?.decision, latestLog.decision),
      ai: decisionLabel(latestLog.decision),
      same: baseline?.decision === latestLog.decision,
    },
    {
      label: 'Amount',
      rule: baseline?.dose_ml != null ? formatDoseMl(baseline.dose_ml as number) : formatDoseMl(0),
      ai: `${formatDoseMl(agentDose)}${preview.perComponent ? ' per component' : ''}`,
      same: baseline?.dose_ml === agentDose,
    },
    {
      label: 'Duration',
      rule: baseline?.duration_ms != null ? formatPumpDuration(baseline.duration_ms as number) : '0 s',
      ai: formatPumpDuration(agentDurationMs),
      same: baseline?.duration_ms === agentDurationMs,
    },
    {
      label: 'Mixing',
      rule: formatDuration(mixingBase),
      ai: formatDuration(cycle.mixing_duration_seconds),
      same: mixingBase === cycle.mixing_duration_seconds,
    },
    {
      label: 'History',
      rule: 'Not used',
      ai: adjustmentFactor != null ? `Used ×${adjustmentFactor}` : 'Checked',
      same: false,
    },
  ]

  return (
    <Panel title="Agent decision audit" eyebrow="Decision review" className="span-12 comparison-panel">
      <div className="comparison-header" aria-hidden="true">
        <span>Check</span>
        <span>Safety rule</span>
        <span>Agent decision</span>
      </div>
      <div className="comparison-rows">
        {rows.map((row) => (
          <div className={row.same ? 'same' : 'changed'} key={row.label}>
            <span className="comparison-label">{row.label}</span>
            <span className="comparison-value rule">
              <b>Safety rule</b>
              <strong>{comparisonValue(row.rule)}</strong>
            </span>
            <span className="comparison-value ai">
              <b>Agent decision</b>
              <strong>{comparisonValue(row.ai)}</strong>
            </span>
          </div>
        ))}
      </div>
      {typeof baseline?.reason === 'string' && (
        <p className="comparison-note">Safety rule context: {baseline.reason}</p>
      )}
    </Panel>
  )
}

function SafetyChecklist({
  latestLog,
  isBatchCollection,
  currentStage,
}: {
  latestLog: LatestLog
  isBatchCollection: boolean
  currentStage?: string | null
}) {
  const meta = getMeta(latestLog)
  const preview = commandPreview(meta)
  const consistency = getAgent<Meta>(meta, 'consistency_review')
  const reviewPass = str(consistency?.review_status, 'pass') === 'pass'
  const hasDose = (latestLog.pump_activated !== 'none' && latestLog.dose_ml > 0)
    || (preview.pump !== 'none' && preview.dose > 0)
  const safetyReached = !currentStage || stageAtOrAfter(currentStage, 'safety_gate')
  const checks = isBatchCollection
    ? [
        { label: 'Reading stored', passed: true },
        { label: 'Pumps remained off', passed: true },
        { label: 'No sensor anomaly detected', passed: true },
        { label: 'Awaiting scheduled AI review', passed: true },
      ]
    : !safetyReached
      ? [
        { label: hasDose ? 'Candidate dose prepared' : 'No dose candidate', passed: true },
        { label: hasDose ? 'Pump duration calculated' : 'Pumps remain off', passed: true },
        { label: 'Consistency review precedes safety', passed: true },
        { label: 'Final safety validation pending', passed: true },
      ]
      : [
        { label: hasDose ? 'Dose within configured cap' : 'No dose command issued', passed: true },
        { label: hasDose ? 'Pump duration within limit' : 'Pumps remained off', passed: true },
        { label: `Decision review ${reviewPass ? 'passed' : 'failed'}`, passed: reviewPass },
        { label: 'No sensor anomaly detected', passed: true },
      ]

  return (
    <section className={`safety-checklist ${isBatchCollection || !safetyReached ? 'pending' : reviewPass ? 'passed' : 'failed'} span-6`}>
      <div className="context-card-title">
        <span className="context-icon">
          <ShieldCheck size={18} strokeWidth={2.2} />
        </span>
        <div>
          <span>{isBatchCollection ? 'Collection safety' : 'Safety gate'}</span>
          <strong>{isBatchCollection ? 'No actuation' : !safetyReached ? 'Pending' : reviewPass ? 'Passed' : 'Needs review'}</strong>
        </div>
      </div>
      <div className="safety-check-grid">
        {checks.map((check) => (
          <span key={check.label} className={check.passed ? undefined : 'failed'}>
            <b aria-hidden="true">{check.passed ? '✓' : '!'}</b>
            {check.label}
          </span>
        ))}
      </div>
    </section>
  )
}

function HistoryContext({ latestLog }: { latestLog: LatestLog }) {
  const meta = getMeta(latestLog)
  const preview = commandPreview(meta)
  const samePane = getAgent<Meta>(meta, 'same_pump_response')
  const appliedFactor = typeof meta.applied_dose_adjustment_factor === 'number'
    ? meta.applied_dose_adjustment_factor
    : null
  const hasHistorySignal = typeof samePane?.interpretation === 'string'
  const historySignal = typeof samePane?.interpretation === 'string'
    ? historySignalLabel(samePane.interpretation)
    : 'No history signal'
  const historyAdjusted = hasHistorySignal && (
    samePane?.interpretation === 'previous_same_pump_still_unresolved' ||
    samePane?.interpretation === 'previous_same_pump_overshot_opposite_direction' ||
    (appliedFactor != null && Math.abs(appliedFactor - 1) > 0.001)
  )
  const recommended = doseFactorLabel(samePane?.recommended_dose_factor)
  const applied = doseFactorLabel(appliedFactor)
  const hasDose = (latestLog.pump_activated !== 'none' && latestLog.dose_ml > 0)
    || (preview.pump !== 'none' && preview.dose > 0)
  const displayHistorySignal = !hasDose
    ? 'Not used for this decision'
    : historyAdjusted
      ? historySignal
      : 'No adjustment applied'
  const previousDosesUsed = samePane?.interpretation === 'no_previous_same_pump' || !hasHistorySignal
    ? 'None'
    : 'Reviewed'

  return (
    <section className={`history-context ${hasDose && historyAdjusted ? 'used' : 'idle'} span-6`}>
      <div className="context-card-title">
        <span className="context-icon history">
          <Clock3 size={18} strokeWidth={2.2} />
        </span>
        <div>
          <span>Dose history</span>
          <strong>{displayHistorySignal}</strong>
        </div>
      </div>
      <div className="history-factor-grid">
        {hasDose && historyAdjusted ? (
          <>
            <div>
              <span>Recommended factor</span>
              <strong>{recommended}</strong>
            </div>
            <div>
              <span>Applied factor</span>
              <strong>{applied}</strong>
            </div>
          </>
        ) : (
          <>
            <div>
              <span>Previous doses used</span>
              <strong>{hasDose ? previousDosesUsed : 'None'}</strong>
            </div>
            <div>
              <span>Adjustment factor</span>
              <strong>{hasDose ? applied : 'Not applied'}</strong>
            </div>
          </>
        )}
      </div>
    </section>
  )
}

export function AIReasoningPage({
  latestLog,
  cycle,
  batchStatus,
  currentStage = null,
  phTarget,
  ecTarget,
}: {
  latestLog: LatestLog
  cycle: ControlCycle
  batchStatus?: BatchStatus | null
  currentStage?: string | null
  phTarget: { min: number; max: number }
  ecTarget: { min: number; max: number }
}) {
  const meta = getMeta(latestLog)
  const monitoring = getAgent<Meta>(meta, 'monitoring_agent')
  const diagnostic = getAgent<Meta>(meta, 'diagnostic_reasoning_agent')
  const dosePlan = getAgent<Meta>(meta, 'dose_planning_agent')
  const decision = getAgent<Meta>(meta, 'decision_agent')
  const preview = commandPreview(meta)
  const confidence = pct(meta.confidence)
  const mode = agenticMode(meta)

  const isMonitoringOnly =
    latestLog.decision === 'monitoring_mode' ||
    latestLog.status === 'monitoring_mode' ||
    meta.monitoring_mode_enabled === true
  const isMaintenanceMode = latestLog.decision === 'maintenance_mode'
    || latestLog.status === 'maintenance_mode'
    || meta.maintenance_mode_enabled === true
  const isEmergencyStopped = latestLog.decision === 'emergency_stop'
    || latestLog.status === 'emergency_stopped'
    || meta.emergency_stop_enabled === true
  const isAgentic = latestLog.control_strategy === 'agentic_ai'
  const isBatchCollection = mode === 'batch_collection'
  const isEmergencyGuard = mode === 'emergency_guard' || mode === 'emergency_guard_mixing_wait'
  const latestBatch = batchStatus?.batch_latest_analysis
  const nextBatchText = batchStatus?.batch_scheduler_next_run_seconds != null
    ? formatCountdown(batchStatus.batch_scheduler_next_run_seconds)
    : null

  const summaryText = typeof dosePlan?.reason === 'string'
    ? dosePlan.reason
    : typeof monitoring?.summary === 'string'
      ? monitoring.summary
      : `pH (${latestLog.ph.toFixed(2)}) and EC (${latestLog.ec.toFixed(2)}) were observed. Decision: ${label(latestLog.pump_activated)}.`

  if (isMonitoringOnly || isMaintenanceMode || isEmergencyStopped) {
    const pauseTitle = isEmergencyStopped
      ? 'Control-agent execution locked by Emergency Stop'
      : isMaintenanceMode
        ? 'Control-agent analysis paused for maintenance'
        : 'Control-agent analysis paused'
    const pauseEyebrow = isEmergencyStopped ? 'Emergency Stop' : isMaintenanceMode ? 'Maintenance Mode' : 'Monitoring Only'
    const pauseDescription = isEmergencyStopped
      ? 'The safety interlock stopped pump execution. This reading remains available for audit, but no new AI command can run until the physical system is inspected and Emergency Stop is cleared.'
      : isMaintenanceMode
        ? 'The reading is retained in the System Log, while control-agent analysis and pump commands remain paused until maintenance is cleared.'
        : 'Monitoring Only stored this live reading without running the control-agent pipeline or issuing a new pump command. Resume automatic control from Settings when the system is ready.'
    return (
      <section className="page-content ai-reasoning-page">
        <section className="ai-summary batch-collection">
          <div className="ai-icon" aria-hidden="true">
            <ShieldCheck size={24} strokeWidth={2.1} />
          </div>
          <div>
            <h2>{pauseTitle}</h2>
            <p>{pauseDescription}</p>
          </div>
        </section>
        <div className="grid-12">
          <Panel title={isMonitoringOnly ? 'Latest monitored reading' : 'Latest retained reading'} eyebrow="Live data" className="span-6">
            <InfoList items={[
              ['pH', latestLog.ph.toFixed(2)],
              ['EC', `${latestLog.ec.toFixed(2)} mS/cm`],
              ['Water temperature', `${latestLog.temperature.toFixed(1)}°C`],
              ['Reservoir volume', `${latestLog.reservoir_volume_liters.toFixed(1)} L`],
            ]} />
          </Panel>
          <Panel title="Control state" eyebrow={pauseEyebrow} className="span-6">
            <InfoList items={[
              ['Control-agent pipeline', isEmergencyStopped ? 'Locked' : 'Paused'],
              ['Pump command', 'None'],
              ['Reading storage', 'Completed'],
              [isEmergencyStopped ? 'Clear only after' : 'Resume from', isEmergencyStopped ? 'Physical inspection' : 'Settings'],
            ]} />
          </Panel>
        </div>
      </section>
    )
  }

  return (
    <section className="page-content ai-reasoning-page">
      <section className={`ai-summary ${isBatchCollection ? 'batch-collection' : isEmergencyGuard ? 'emergency-guard' : 'decision-summary'}`}>
        <div className="ai-icon" aria-hidden="true">
          {isAgentic
            ? <BrainCircuit size={24} strokeWidth={2.1} />
            : <ShieldCheck size={24} strokeWidth={2.1} />}
        </div>
        <div>
          <h2>
            {isBatchCollection
              ? 'Reading queued for scheduled AI review'
              : isEmergencyGuard
              ? 'Deterministic emergency guard handled this reading'
              : isAgentic
              ? isReadingStale(latestLog.timestamp) ? `Saved decision · ${recordedDate(latestLog.timestamp)}` : reasoningHeading(latestLog, meta)
              : `Rule-based decision: ${label(latestLog.decision)}`}
          </h2>
          <p>
            {isBatchCollection
              ? `This one-minute sensor row is saved, but it has not been reviewed by the LLM.${nextBatchText ? ` The next scheduled AI batch runs in ${nextBatchText}.` : ''}${latestBatch ? ` Latest completed batch: cycle #${latestBatch.control_cycle_id} (${label(latestBatch.status)}).` : ''}`
              : isEmergencyGuard
                ? `This command came from the Phase 3 deterministic guard, not the scheduled LLM batch. Mode: ${agenticModeLabel(mode)}.`
              : summaryText}
          </p>
        </div>
      </section>

      {isAgentic ? (
        <>
          <ResearchResultStrip
            latestLog={latestLog}
            cycle={cycle}
            confidence={confidence}
            batchStatus={batchStatus}
            isBatchCollection={isBatchCollection}
            currentStage={currentStage}
          />

          <LiveAgentStateGraph
            latestLog={latestLog}
            cycle={cycle}
            currentStage={currentStage}
            defaultExpanded
          />

          <div className="grid-12">
            <AgentPanel
              title="Monitoring & diagnostic"
              className="span-6"
              items={[
                ['Classification', label(diagnostic?.classification)],
                ['pH status', deviationLabel(latestLog.ph, latestLog.ph_deviation, phTarget)],
                ['EC status', deviationLabel(latestLog.ec, latestLog.ec_deviation, ecTarget)],
                ['Is recovering', monitoring?.is_recovering ? 'Yes' : 'No'],
                ['Primary metric', label(diagnostic?.primary_metric)],
                ['Recommended pump', label(preview.pump)],
              ]}
            />
            <AgentPanel
              title="Decision & dose plan"
              className="span-6"
              items={[
                ['Action chosen', label(decision?.action, decisionLabel(latestLog.decision))],
                ['Pump selected', label(preview.pump)],
                ['Dose factor', factor(meta.applied_dose_adjustment_factor)],
                ['Mixing factor', factor((dosePlan as Meta | undefined)?.mixing_adjustment_factor)],
                ['History signal', historySignalLabel((getAgent<Meta>(meta, 'same_pump_response') as Meta | undefined)?.interpretation)],
              ]}
            />
          </div>

          <BaselinePanel cycle={cycle} latestLog={latestLog} />

          <div className="grid-12 decision-context-grid">
            <SafetyChecklist latestLog={latestLog} isBatchCollection={isBatchCollection} currentStage={currentStage} />
            <HistoryContext latestLog={latestLog} />
          </div>

          <Phase3OperatingModel
            batchStatus={batchStatus}
            isBatchCollection={isBatchCollection}
          />
        </>
      ) : (
        <>
          <div className="grid-12">
            <Panel title="Decision details" eyebrow="Rule-based" className="span-6">
              <InfoList items={[
                ['Decision', latestLog.decision],
                ['Pump activated', latestLog.pump_activated],
                ['Dose', formatDoseMl(latestLog.dose_ml)],
                ['Duration', formatPumpDuration(latestLog.duration_ms)],
                ['pH status', deviationLabel(latestLog.ph, latestLog.ph_deviation, phTarget)],
                ['EC status', deviationLabel(latestLog.ec, latestLog.ec_deviation, ecTarget)],
              ]} />
            </Panel>
            <SafetyChecklist latestLog={latestLog} isBatchCollection={false} />
          </div>
          <p className="muted-line" style={{ padding: '8px 0' }}>
            LLM agent pipeline is only active when control strategy is <strong>Agentic AI</strong>.
            Current strategy: <strong>Baseline</strong> (rule-based dosing).
          </p>
        </>
      )}
    </section>
  )
}
