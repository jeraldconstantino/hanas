import { isReadingStale, recordedDate } from '../readingFreshness'
import {
  Activity,
  Bot,
  Brain,
  CalendarDays,
  CheckCircle2,
  ChevronRight,
  Circle,
  ClipboardCheck,
  Droplets,
  Gauge,
  GitBranch,
  Pencil,
  RefreshCw,
  Route,
  ShieldCheck,
  Sprout,
  Thermometer,
  Timer,
  UserCheck,
} from 'lucide-react'
import type { CSSProperties, ReactNode } from 'react'
import type {
  LatestLog,
  ControlCycle,
  Tone,
  Page,
  BatchStatus,
  BatchUnresolvedCommand,
  OverviewSummaryStatus,
  SensorHistoryEntry,
  SystemSettings,
} from '../types'
import { PH_TARGET, EC_TARGET } from '../constants'
import {
  phTone, ecTone, tempTone,
  phStatus, ecStatus, tempStatus,
  deviationLabel, isHITLState, isHITLPending, formatCountdown, formatDoseMl, formatDuration, formatPumpDuration, formatTime,
  formatTimeMinute,
  cropLifecycleFromSettings,
  type CropLifecycle,
} from '../utils'
import { PIPELINE_STAGE_META, pipelineStageLabel, pipelineStageSummary } from '../pipeline'
import { Pill } from '../components/ui/Pill'
import { Panel } from '../components/ui/Panel'
import { StatusHero } from '../components/ui/StatusHero'
import { formatDisplayText, formatStrategyLabel } from '../text'
import { heroContent, pumpLabel } from '../overviewHero'
import { overviewPipelineStagePresentation } from '../overviewPipeline'

function MetricCard({
  label,
  value,
  status,
  tone,
  target,
  deviation,
  footer,
  accent = false,
  fillPct,
  fillThresholdPct,
  fillLabel,
  icon,
}: {
  label: string
  value: string
  status: string
  tone: Tone
  target: string
  deviation: string
  footer: string
  accent?: boolean
  fillPct?: number
  fillThresholdPct?: number
  fillLabel?: string
  icon?: ReactNode
}) {
  const fillPercent = fillPct === undefined ? 0 : Math.min(Math.max(fillPct * 100, 0), 100)
  const thresholdPercent = fillThresholdPct === undefined
    ? 0
    : Math.min(Math.max(fillThresholdPct * 100, 0), 100)

  return (
    <article className={`metric-card ${tone} ${accent ? 'accent' : ''}`}>
      <div className="metric-top">
        <span className="metric-label">
          {icon && <i>{icon}</i>}
          {label}
        </span>
        <Pill label={status} tone={tone} />
      </div>
      <strong>{value}</strong>
      <p>{target}</p>
      <b>{deviation}</b>
      {fillPct !== undefined && (
        <div
          className="mini-fill reservoir-level-scale"
          role="img"
          aria-label={fillLabel}
          style={{
            '--fill-position': `${fillPercent}%`,
            '--threshold-position': `${thresholdPercent}%`,
          } as CSSProperties}
        >
          <span className="mini-fill-value" style={{ width: `${fillPercent}%` }} />
        </div>
      )}
      {footer && <><hr className="metric-sep" /><small>{footer}</small></>}
    </article>
  )
}

function QA({ q, a }: { q: string; a: string }) {
  return (
    <div className="qa-row">
      <span>{q}</span>
      <strong>{a}</strong>
    </div>
  )
}

function pumpDescription(pump: string): string {
  switch (pump) {
    case 'ph_up': return 'raises pH'
    case 'ph_down': return 'lowers pH'
    case 'ec_up': return 'increases nutrient concentration'
    case 'ec_down': return 'lowers EC by dilution'
    default: return 'adjusts nutrients'
  }
}

function reservoirStatus(
  volumeLiters: number,
  maxLiters: number,
  minimumPumpableLiters: number,
  consumptionLitersPerHour: number | null,
): {
  label: string
  tone: Tone
  description: string
  usablePercent: number
  pumpableHeadroom: number
} {
  const pct = maxLiters > 0 ? volumeLiters / maxLiters : 0
  const usableCapacity = Math.max(maxLiters - minimumPumpableLiters, 0)
  const pumpableHeadroom = Math.max(volumeLiters - minimumPumpableLiters, 0)
  const usablePercent = usableCapacity > 0
    ? Math.min(pumpableHeadroom / usableCapacity, 1)
    : 0
  const hoursToMinimum = consumptionLitersPerHour && consumptionLitersPerHour > 0
    ? pumpableHeadroom / consumptionLitersPerHour
    : null
  const timeToMinimum = hoursToMinimum == null ? null : formatHoursToMinimum(hoursToMinimum)
  if (pct > 1) {
    return {
      label: 'Overfilled',
      tone: 'warn',
      description: 'Above operating volume',
      usablePercent,
      pumpableHeadroom,
    }
  }
  if (volumeLiters <= minimumPumpableLiters) {
    return {
      label: 'Refill required',
      tone: 'danger',
      description: `At or below the ${minimumPumpableLiters.toFixed(1)}\u00a0L pumpable minimum`,
      usablePercent,
      pumpableHeadroom,
    }
  }
  if (usablePercent <= 0.3 || (hoursToMinimum != null && hoursToMinimum <= 24)) {
    return {
      label: 'Refill soon',
      tone: 'warn',
      description: hoursToMinimum != null
        ? `About ${timeToMinimum} until the ${minimumPumpableLiters.toFixed(1)}\u00a0L minimum at the recent usage rate`
        : `Near the ${minimumPumpableLiters.toFixed(1)}\u00a0L pumpable minimum`,
      usablePercent,
      pumpableHeadroom,
    }
  }
  return {
    label: 'Sufficient',
    tone: 'good',
    description: hoursToMinimum != null
      ? `About ${timeToMinimum} until the ${minimumPumpableLiters.toFixed(1)}\u00a0L minimum at the recent usage rate`
      : `Minimum ${minimumPumpableLiters.toFixed(1)}\u00a0L • capacity ${maxLiters.toFixed(1)}\u00a0L`,
    usablePercent,
    pumpableHeadroom,
  }
}

function formatHoursToMinimum(hours: number): string {
  if (!Number.isFinite(hours) || hours <= 0) return 'less than 1 hour'
  if (hours < 1) return 'less than 1 hour'
  if (hours < 72) {
    const roundedHours = Math.max(1, Math.round(hours))
    return `${roundedHours} ${roundedHours === 1 ? 'hour' : 'hours'}`
  }
  const days = hours / 24
  return `${days < 14 ? days.toFixed(1) : days.toFixed(0)} days`
}

function recentReservoirConsumptionRate(
  history: SensorHistoryEntry[],
  latestLog: LatestLog,
  maxLiters: number,
): number | null {
  const pointByTime = new Map<number, number>()
  history.forEach((entry) => {
    const timestamp = Date.parse(entry.timestamp)
    if (Number.isFinite(timestamp) && entry.reservoir_volume_liters != null) {
      pointByTime.set(timestamp, entry.reservoir_volume_liters)
    }
  })
  const latestTimestamp = Date.parse(latestLog.timestamp)
  if (Number.isFinite(latestTimestamp)) {
    pointByTime.set(latestTimestamp, latestLog.reservoir_volume_liters)
  }
  let points = [...pointByTime.entries()].sort(([a], [b]) => a - b)
  if (points.length < 2) return null

  const newestTimestamp = points[points.length - 1][0]
  points = points.filter(([timestamp]) => newestTimestamp - timestamp <= 3 * 60 * 60 * 1000)
  const refillJump = Math.max(0.5, maxLiters * 0.01)
  let lastRefillIndex = 0
  for (let index = 1; index < points.length; index += 1) {
    if (points[index][1] - points[index - 1][1] >= refillJump) lastRefillIndex = index
  }
  points = points.slice(lastRefillIndex)
  if (points.length < 3) return null

  const elapsedHours = (points[points.length - 1][0] - points[0][0]) / 3_600_000
  if (elapsedHours < 1 / 6) return null
  const declineIntervals = points.slice(1).filter(
    ([, volume], index) => points[index][1] - volume >= 0.05,
  ).length
  if (declineIntervals < 2) return null
  const elapsedSeconds = points.map(([timestamp]) => (timestamp - points[0][0]) / 1000)
  const volumes = points.map(([, volume]) => volume)
  const meanElapsed = elapsedSeconds.reduce((sum, value) => sum + value, 0) / elapsedSeconds.length
  const meanVolume = volumes.reduce((sum, value) => sum + value, 0) / volumes.length
  const variance = elapsedSeconds.reduce((sum, value) => sum + (value - meanElapsed) ** 2, 0)
  const covariance = elapsedSeconds.reduce(
    (sum, value, index) => sum + (value - meanElapsed) * (volumes[index] - meanVolume),
    0,
  )
  const slopeLitersPerSecond = variance > 0 ? covariance / variance : 0
  return Math.max(-slopeLitersPerSecond * 3600, 0)
}

const HITL_STATUS_LABELS: Record<string, string> = {
  wait_human_review: 'Awaiting operator review',
  human_approved_pending_execution: 'Approved: ESP32 executing',
  human_override_pending_execution: 'Override set: ESP32 executing',
  human_command_dispatched: 'Command dispatched to device',
  human_rejected: 'Rejected: no action taken',
}

function HITLAlert({
  cycle,
  decision,
  onNavigate,
}: {
  cycle: ControlCycle
  decision: string
  onNavigate?: (p: Page) => void
}) {
  const isWaiting = isHITLPending(cycle.status, decision)
  const label = isWaiting
    ? HITL_STATUS_LABELS.wait_human_review
    : HITL_STATUS_LABELS[cycle.status] ?? cycle.status
  return (
    <div className={`hitl-overview-alert ${isWaiting ? 'warn' : 'info'}`}>
      <Pill label="HITL" tone={isWaiting ? 'warn' : 'info'} />
      <div className="hitl-overview-alert-copy">
        <strong>{label}</strong>
        <span>Cycle #{cycle.id}</span>
      </div>
      {isWaiting && onNavigate && (
        <button type="button" onClick={() => onNavigate('Dosing')}>
          Review in Dosing →
        </button>
      )}
    </div>
  )
}

function CyclePanel({ cycle, latestLog, wide = false }: { cycle: ControlCycle; latestLog: LatestLog; wide?: boolean }) {
  const meta = latestLog.decision_metadata as Record<string, unknown>
  const baseline = meta?.baseline_shadow as Record<string, unknown> | undefined
  const baselineDose = typeof baseline?.dose_ml === 'number' ? baseline.dose_ml : null
  const factor = typeof meta?.applied_dose_adjustment_factor === 'number'
    ? meta.applied_dose_adjustment_factor
    : null

  const note = baselineDose != null && baselineDose !== cycle.dose_ml
    ? `Baseline comparison: rule-based would have dosed ${formatDoseMl(baselineDose)}. Agentic AI reduced to ${formatDoseMl(cycle.dose_ml)}${factor != null ? ` (×${factor})` : ''} based on history.`
    : null
  const status = cycle.status.toLowerCase()
  const isMixing = status === 'mixing'
  const isComplete = status === 'completed' || status === 'complete' || status === 'done'
  const title = isMixing
    ? `Mixing after ${pumpLabel(cycle.pump_activated)}`
    : isComplete
      ? `${pumpLabel(cycle.pump_activated)} cycle complete`
      : `Dosing ${pumpLabel(cycle.pump_activated)}`

  return (
    <Panel
      title={title}
      eyebrow="Current cycle"
      className={`${wide ? 'span-12' : 'span-6'} cycle-panel`}
      badge={`Cycle #${cycle.id}`}
    >
      <div className="cycle-strip">
        <span className="done"><CheckCircle2 size={15} /> Received</span>
        <span className={isMixing || isComplete ? 'done' : 'active'}>
          {isMixing || isComplete ? <CheckCircle2 size={15} /> : <Activity size={15} />}
          Dosing {formatPumpDuration(cycle.duration_ms, { compact: true })}
        </span>
        <span className={isMixing ? 'active' : isComplete ? 'done' : 'pending'}>
          {isComplete ? <CheckCircle2 size={15} /> : <Timer size={15} />}
          Mixing {formatDuration(cycle.mixing_duration_seconds, { compact: true })}
        </span>
        <span className={isComplete ? 'done' : 'pending'}>
          {isComplete ? <CheckCircle2 size={15} /> : <Circle size={15} />}
          Done
        </span>
      </div>
      {note && (
        <p className="cycle-note">
          <GitBranch className="cycle-note-icon" size={15} strokeWidth={2.2} aria-hidden="true" />
          <span>{note}</span>
        </p>
      )}
    </Panel>
  )
}

function WhyPanel({ latestLog, wide = false }: { latestLog: LatestLog; wide?: boolean }) {
  const meta = latestLog.decision_metadata as Record<string, unknown>
  const dosePlan = meta?.dose_planning_agent as Record<string, unknown> | undefined
  const monitoring = meta?.monitoring_agent as Record<string, unknown> | undefined

  const explanation =
    typeof dosePlan?.reason === 'string'
      ? dosePlan.reason
      : typeof monitoring?.summary === 'string'
        ? monitoring.summary
        : `pH (${latestLog.ph.toFixed(2)}) or EC (${latestLog.ec.toFixed(2)}) is outside the target range. HANAS is correcting.`

  return (
    <Panel title="Operator summary" eyebrow="AI decision summary" className={`${wide ? 'span-12' : 'span-6'} why-panel`}>
      <div className="explain-card">{explanation}</div>
      <QA q="Selected pump" a={`${formatDisplayText(latestLog.pump_activated)}: ${pumpDescription(latestLog.pump_activated)}`} />
      <QA q="Is it safe?" a="Yes. Safety gate confirmed bounds." />
      <QA q="What's next?" a="Wait for mixing to finish, then re-measure" />
    </Panel>
  )
}

function OverviewAIContext({
  latestLog,
  cycle,
  currentStage,
  onNavigate,
  correctionTitle,
}: {
  latestLog: LatestLog
  cycle: ControlCycle
  currentStage?: string | null
  onNavigate?: (page: Page) => void
  correctionTitle?: string
}) {
  const meta = latestLog.decision_metadata as Record<string, unknown>
  const monitoring = meta?.monitoring_agent as Record<string, unknown> | undefined
  const diagnostic = meta?.diagnostic_reasoning_agent as Record<string, unknown> | undefined
  const dosePlan = meta?.dose_planning_agent as Record<string, unknown> | undefined
  const decisionAgent = meta?.decision_agent as Record<string, unknown> | undefined
  const samePump = meta?.same_pump_response as Record<string, unknown> | undefined
  const consistency = meta?.consistency_review as Record<string, unknown> | undefined
  const isLive = Boolean(currentStage)
  const stageLabel = pipelineStageLabel(currentStage)
  const stageSummary = pipelineStageSummary(currentStage)
  const context = decisionContextSummary({
    latestLog,
    cycle,
    monitoring,
    diagnostic,
    dosePlan,
    decisionAgent,
    samePump,
    consistency,
  })

  return (
    <section className={`overview-ai-context ${isLive ? 'live' : 'idle'}${correctionTitle ? ' merged-correction' : ''}`}>
      <div className="overview-ai-context-main">
        <div className="overview-ai-context-head">
          <span>
            <Brain size={14} strokeWidth={2.2} aria-hidden="true" />
            {correctionTitle
              ? 'AI decision context · Dosing in progress'
              : isLive
                ? 'Live AI stage'
                : 'AI decision context'}
          </span>
        </div>
        <h2>{isLive && !correctionTitle ? stageLabel : context.title}</h2>
        <p>{isLive && !correctionTitle ? stageSummary : context.body}</p>
        {onNavigate && (
          <div className="overview-ai-context-actions">
            <button type="button" className="overview-ai-reasoning-link" onClick={() => onNavigate('AI Reasoning')}>
              <span>View full reasoning</span>
              <ChevronRight size={14} strokeWidth={2.2} aria-hidden="true" />
            </button>
          </div>
        )}
      </div>
    </section>
  )
}

function decisionContextSummary({
  latestLog,
  cycle,
  monitoring,
  diagnostic,
  dosePlan,
  decisionAgent,
  samePump,
  consistency,
}: {
  latestLog: LatestLog
  cycle: ControlCycle
  monitoring?: Record<string, unknown>
  diagnostic?: Record<string, unknown>
  dosePlan?: Record<string, unknown>
  decisionAgent?: Record<string, unknown>
  samePump?: Record<string, unknown>
  consistency?: Record<string, unknown>
}): { title: string; body: string } {
  const pump = formatDisplayText(latestLog.pump_activated)
  const decision = formatDisplayText(latestLog.decision)
  const primaryMetric = metricLabel(textValue(diagnostic?.primary_metric))
  const action = textValue(decisionAgent?.action)
  const safety = safetyLabel(consistency, latestLog.decision_metadata as Record<string, unknown>)
  const responseContext = samePumpLabel(samePump)
  const nextStep = nextStepLabel(cycle)
  const rationale = firstMeaningfulText(
    dosePlan?.reason,
    decisionAgent?.reason,
    diagnostic?.summary,
    monitoring?.summary,
    latestLog.decision === 'within_range' || latestLog.decision === 'within_control_tolerance'
      ? 'Both readings are within the configured target range, so no dosing is needed.'
      : '',
  )

  if (latestLog.pump_activated !== 'none' && latestLog.dose_ml > 0) {
    const title = `Why HANAS selected ${pump}`
    const pendingApproval = isHITLPending(cycle.status, latestLog.decision)
    const metric = primaryMetric || (latestLog.decision.startsWith('ph_') ? 'pH' : 'EC')
    const rawAdjustmentFactor = (
      latestLog.decision_metadata as Record<string, unknown>
    )?.applied_dose_adjustment_factor
    const adjustmentFactor = typeof rawAdjustmentFactor === 'number' ? rawAdjustmentFactor : null
    const actionContext = pendingApproval
      ? `HANAS proposed a ${formatDoseMl(latestLog.dose_ml)} ${pump} dose for operator review`
      : safety === 'safety review passed'
      ? `After the stable reading passed the safety review, HANAS started a ${formatDoseMl(latestLog.dose_ml)} ${pump} dose`
      : `HANAS selected a ${formatDoseMl(latestLog.dose_ml)} ${pump} dose${safety ? ` after the ${safety}` : ''}`
    const adjustmentContext = adjustmentFactor != null && adjustmentFactor < 1
      ? `A ${adjustmentFactor.toFixed(2)} crop-stage factor keeps this correction conservative`
      : responseContext
    const followUp = pendingApproval
      ? 'The pump remains off until an operator approves or modifies the proposal'
      : cycle.status === 'dosing'
      ? `After dosing, allow the ${formatNarrativeDuration(cycle.mixing_duration_seconds)} mixing interval to finish, then confirm the next stable ${metric} reading`
      : cycle.status === 'mixing'
        ? `Wait for mixing to finish, then confirm the next stable ${metric} reading`
        : nextStep
    return {
      title,
      body: joinSentences(
        decisionReadingContext(latestLog),
        actionContext,
        adjustmentContext,
        followUp,
      ),
    }
  }

  if (action === 'wait' || latestLog.decision.startsWith('wait_')) {
    return {
      title: `Why: ${formatDisplayText(latestLog.decision)}`,
      body: joinSentences(
        rationale || 'HANAS is holding the command until readings or safety context are ready.',
        nextStep,
      ),
    }
  }

  return {
    title: `Why: ${decision}`,
    body: compactSentence(
      rationale ||
        `pH ${latestLog.ph.toFixed(2)} and EC ${latestLog.ec.toFixed(2)} were checked against target before choosing ${decision}.`,
    ),
  }
}

function decisionReadingContext(latestLog: LatestLog): string {
  if (latestLog.decision === 'ph_high') {
    return `pH reached ${latestLog.ph.toFixed(2)}, which is ${(latestLog.ph - PH_TARGET.max).toFixed(2)} above the configured upper limit of ${PH_TARGET.max.toFixed(2)}`
  }
  if (latestLog.decision === 'ph_low') {
    return `pH reached ${latestLog.ph.toFixed(2)}, which is ${(PH_TARGET.min - latestLog.ph).toFixed(2)} below the configured lower limit of ${PH_TARGET.min.toFixed(2)}`
  }
  if (latestLog.decision === 'ec_high') {
    return `EC reached ${latestLog.ec.toFixed(2)} mS/cm, which is ${(latestLog.ec - EC_TARGET.max).toFixed(2)} above the configured upper limit of ${EC_TARGET.max.toFixed(2)} mS/cm`
  }
  if (latestLog.decision === 'ec_low') {
    return `EC reached ${latestLog.ec.toFixed(2)} mS/cm, which is ${(EC_TARGET.min - latestLog.ec).toFixed(2)} below the configured lower limit of ${EC_TARGET.min.toFixed(2)} mS/cm`
  }
  return ''
}

function formatNarrativeDuration(seconds: number): string {
  const safeSeconds = Math.max(0, Math.round(seconds))
  const minutes = Math.floor(safeSeconds / 60)
  const remainingSeconds = safeSeconds % 60
  if (minutes > 0 && remainingSeconds > 0) return `${minutes}-minute ${remainingSeconds}-second`
  if (minutes > 0) return `${minutes}-minute`
  return `${remainingSeconds}-second`
}

function safetyLabel(
  consistency?: Record<string, unknown>,
  meta?: Record<string, unknown>,
): string {
  const reviewStatus = textValue(consistency?.review_status).toLowerCase()
  if (reviewStatus === 'pass') return 'safety review passed'
  if (reviewStatus) return `safety review ${formatDisplayText(reviewStatus)}`
  if (textValue(meta?.safety_gate_source)) return 'safety gate checked limits'
  return ''
}

function samePumpLabel(samePump?: Record<string, unknown>): string {
  const interpretation = textValue(samePump?.interpretation)
  if (!interpretation) return ''
  const labels: Record<string, string> = {
    previous_same_pump_still_unresolved: 'previous same-pump dose is still settling',
    previous_same_pump_overshot_opposite_direction: 'previous same-pump dose overshot',
    previous_same_pump_resolved_or_changed_condition: 'previous same-pump dose resolved or condition changed',
  }
  return labels[interpretation] ?? formatDisplayText(interpretation)
}

function nextStepLabel(cycle: ControlCycle): string {
  if (cycle.status === 'mixing') return 'wait for mixing, then re-measure'
  if (cycle.status === 'dosing') return 'pump command is in progress'
  if (cycle.status === 'completed') return 'cycle completed, watch the next reading'
  if (cycle.status === 'queued' || cycle.status === 'pending_dispatch') return 'waiting for ESP32 pickup'
  return ''
}

function metricLabel(value: string): string {
  const normalized = value.toLowerCase()
  if (normalized === 'ph') return 'pH'
  if (normalized === 'ec') return 'EC'
  return value
}

function firstMeaningfulText(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  return ''
}

function textValue(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

function compactSentence(value: string): string {
  const compact = value.replace(/\s+/g, ' ').trim()
  if (!compact) return ''
  return compact.endsWith('.') ? compact : `${compact}.`
}

function joinSentences(...values: string[]): string {
  return values
    .map(compactSentence)
    .filter(Boolean)
    .join(' ')
}

function batchSourceLabel(source: string | null | undefined, mode?: string | null): string {
  if (source === 'manual_batch_trigger' || mode === 'manual') return 'Manual batch'
  if (source === 'batch_scheduler') return 'Scheduled batch'
  if (source === 'batch_mode_collection') return 'One-minute collection'
  return source ? formatDisplayText(source) : 'No batch yet'
}

function BatchCommandSummary({ command }: { command: BatchUnresolvedCommand }) {
  return (
    <>
      Cycle #{command.control_cycle_id}, {pumpLabel(command.pump_activated)}, {formatDoseMl(command.dose_ml)}
    </>
  )
}

function OverviewBatchGuard({
  batchStatus,
}: {
  batchStatus: BatchStatus | null
}) {
  if (!batchStatus?.batch_analysis_enabled) {
    return (
      <section className="overview-batch-guard neutral">
        <div className="overview-batch-guard-main">
          <span>Agentic batch guard</span>
          <strong>Batch analysis disabled</strong>
          <p>The backend is storing sensor readings, but the 10-minute LLM batch is not scheduled.</p>
        </div>
        <div className="overview-batch-guard-meta">
          <div>
            <span>Scheduler</span>
            <strong>Not scheduled</strong>
            <p>Enable Phase 3 batch mode in backend config.</p>
          </div>
          <div>
            <span>Next safety check</span>
            <strong>—</strong>
            <p>No timer is active.</p>
          </div>
        </div>
      </section>
    )
  }

  const unresolved = batchStatus.batch_unresolved_command
  const physical = batchStatus.batch_recent_or_active_pump_command
  const latest = batchStatus.batch_latest_analysis
  const interval = Math.round(batchStatus.batch_analysis_interval_seconds / 60)
  const window = batchStatus.batch_analysis_window_readings
  const guard = Math.round(batchStatus.batch_physical_guard_window_seconds / 60)
  const physicalGuardRemainingSeconds = physical?.action_completed_at
    ? Math.max(0, batchStatus.batch_physical_guard_window_seconds - physical.age_seconds)
    : null
  const physicalPumpRemainingSeconds = physical && !physical.action_completed_at
    ? Math.max(0, Math.ceil(physical.duration_ms / 1000) - physical.age_seconds)
    : null
  const physicalCompletionOverdue = Boolean(
    physical &&
    !physical.action_completed_at &&
    physicalPumpRemainingSeconds === 0,
  )
  const blocked = Boolean(unresolved || physical)
  const schedulerRunning = batchStatus.batch_scheduler_running
  const nextRunLabel = schedulerRunning
    ? formatCountdown(batchStatus.batch_scheduler_next_run_seconds)
    : 'Not running'
  const title = unresolved
    ? 'Additional dosing locked while the current cycle resolves'
    : physical
      ? 'Additional dosing locked during the active pump window'
      : schedulerRunning
        ? 'Scheduled batch is active'
        : 'Batch is enabled but scheduler is not running'
  const body = unresolved
    ? `The scheduler will evaluate again in ${nextRunLabel}, but HANAS will not queue another AI dose until the previous command is completed, rejected, or expired.`
    : physical
      ? physical.action_completed_at
        ? `The pump is recorded as ${formatDisplayText(physical.status)}. The duplicate-dose guard clears in ${formatCountdown(physicalGuardRemainingSeconds)}.`
        : physicalCompletionOverdue
          ? 'The expected pump runtime has elapsed, but shutdown was not confirmed. Keep automatic dosing paused and inspect the pump or controller connection.'
          : `The pump action has ${formatCountdown(physicalPumpRemainingSeconds)} remaining, followed by a ${guard}-minute duplicate-dose guard.`
      : schedulerRunning
        ? `Next scheduled evaluation is in ${nextRunLabel}. HANAS reviews the latest ${window} readings before deciding whether AI dosing is needed.`
        : 'The config says batch mode is enabled, but this backend process has not started the scheduler loop. Restart the backend or check startup logs.'

  return (
    <section className={`overview-batch-guard ${blocked || !schedulerRunning ? 'blocked' : 'clear'}`}>
      <div className="overview-batch-guard-main">
        <span>Phase 3 batch safety</span>
        <strong>{title}</strong>
        <p>{body}</p>
      </div>
      <div className="overview-batch-guard-meta">
        <div>
          <span>Latest batch</span>
          <strong>{latest ? `${batchSourceLabel(latest.triggered_by, latest.batch_trigger_mode)}, ${formatDisplayText(latest.status)}` : 'No run yet'}</strong>
          <p>{latest?.timestamp ? formatTime(latest.timestamp) : `Every ${interval} min`}</p>
        </div>
        <div>
          <span>Scheduler</span>
          <strong>{schedulerRunning ? 'Scheduled' : 'Not running'}</strong>
          <p>
            {schedulerRunning
              ? `Next safety check ${nextRunLabel}${batchStatus.batch_scheduler_next_run_at ? `, ${formatTime(batchStatus.batch_scheduler_next_run_at)}` : ''}`
              : batchStatus.batch_scheduler_last_message}
          </p>
        </div>
        <div>
          <span>Blocker</span>
          <strong>
            {unresolved
              ? <BatchCommandSummary command={unresolved} />
              : physical
                ? <BatchCommandSummary command={physical} />
                : 'None'}
          </strong>
          <p>
            {physicalGuardRemainingSeconds != null
              ? `Guard clears in ${formatCountdown(physicalGuardRemainingSeconds)}`
              : physicalCompletionOverdue
                ? 'Completion callback overdue'
                : blocked
                  ? 'Duplicate-dose guard active'
                  : `${window} readings per batch`}
          </p>
        </div>
      </div>
    </section>
  )
}

function OverviewDailySummary({
  overviewSummary,
  onGenerateSummary,
  isGenerating = false,
  generationError,
}: {
  overviewSummary: OverviewSummaryStatus | null
  onGenerateSummary?: () => Promise<void>
  isGenerating?: boolean
  generationError?: string | null
}) {
  if (!overviewSummary?.overview_summary_enabled) return null

  const summary = overviewSummary.latest_summary
  const nextRunAt = overviewSummary.overview_summary_scheduler_next_run_at
  const nextRunTime = nextRunAt ? formatTime(nextRunAt).replace(/:\d{2}\s/, ' ') : '12:00 AM / 12:00 PM'
  const nextRunText = nextRunAt ? `${nextRunTime} PHT` : '12:00 AM or 12:00 PM PHT'
  const lastRunFinishedAt = overviewSummary.overview_summary_scheduler_last_run_finished_at
  const lastRunText = lastRunFinishedAt
    ? ` Last run: ${formatDisplayText(overviewSummary.overview_summary_scheduler_last_status)} at ${formatTime(lastRunFinishedAt)}.`
    : ''
  const generateButton = onGenerateSummary ? (
    <button
      type="button"
      className={`overview-summary-generate${summary ? ' subtle' : ''}`}
      aria-label={summary ? 'Refresh 12-hour summary' : undefined}
      onClick={() => {
        void onGenerateSummary().catch(() => undefined)
      }}
      disabled={isGenerating}
    >
      <RefreshCw size={14} strokeWidth={2.2} />
      {isGenerating ? 'Refreshing' : summary ? 'Refresh' : 'Generate summary'}
    </button>
  ) : null

  if (!summary) {
    return (
      <section className="overview-daily-summary pending">
        <div className="overview-daily-summary-main">
          <div className="overview-daily-summary-head">
            <span className="overview-summary-eyebrow">
              <Bot size={14} strokeWidth={2.2} />
              Latest 12-hour summary
            </span>
            {generateButton}
          </div>
          <strong>No summary generated yet</strong>
          <p>
            The first summary will appear here after the scheduled run.
            {overviewSummary.overview_summary_scheduler_running
              ? ` Next run: ${nextRunText}.`
              : ' Scheduler is not running in this backend process.'}
            {lastRunText}
          </p>
          {generationError && <p className="overview-summary-error">{generationError}</p>}
        </div>
      </section>
    )
  }

  const generated = formatTimeMinute(summary.generated_at)
  const cleanTitle = formatDisplayText(summary.title.replace(/^latest summary:\s*/i, ''))
  const needsAttention = /attention|review|unavailable|issue|warning/i.test(cleanTitle)
  return (
    <section className={`overview-daily-summary ${needsAttention ? 'attention' : 'stable'}`}>
      <div className="overview-daily-summary-main">
        <div className="overview-daily-summary-head">
          <span className="overview-summary-eyebrow">
            <Bot size={14} strokeWidth={2.2} />
            Latest 12-hour summary
          </span>
        </div>
        <strong>{cleanTitle}</strong>
        <p>{summary.summary}</p>
        {generationError && <p className="overview-summary-error">{generationError}</p>}
        <div className="overview-daily-summary-meta">
          <span>Generated {generated}</span>
          <span>Next summary {nextRunText}</span>
          {generateButton}
        </div>
      </div>
    </section>
  )
}

type CropLifecycleDaySummary = {
  readingCount: number
  avgPh: number
  avgEc: number
  avgTemp: number | null
  avgReservoir: number | null
  totalDoseMl: number
}

function parseCropLifecycleDate(value: string | null): Date | null {
  if (!value) return null
  const [year, month, day] = value.split('-').map(Number)
  if (!year || !month || !day) return null
  return new Date(year, month - 1, day)
}

function cropDayFromTimestamp(transplantDate: Date, timestamp: string): number | null {
  const readingDate = new Date(timestamp)
  if (Number.isNaN(readingDate.getTime())) return null
  const start = new Date(transplantDate.getFullYear(), transplantDate.getMonth(), transplantDate.getDate())
  const readingDay = new Date(readingDate.getFullYear(), readingDate.getMonth(), readingDate.getDate())
  return Math.floor((readingDay.getTime() - start.getTime()) / 86_400_000)
}

function average(values: number[]): number | null {
  if (!values.length) return null
  return values.reduce((sum, value) => sum + value, 0) / values.length
}

function cropLifecycleSummariesByDay(
  lifecycle: CropLifecycle,
  history: SensorHistoryEntry[],
): Map<number, CropLifecycleDaySummary> {
  const transplantDate = parseCropLifecycleDate(lifecycle.transplantDate)
  if (!transplantDate || !history.length) return new Map()

  const buckets = new Map<number, {
    ph: number[]
    ec: number[]
    temp: number[]
    reservoir: number[]
    totalDoseMl: number
    doseKeys: Set<string>
  }>()

  history.forEach((entry, index) => {
    const day = cropDayFromTimestamp(transplantDate, entry.timestamp)
    if (day == null || day < 0 || day > lifecycle.harvestEndDay) return
    const bucket = buckets.get(day) ?? {
      ph: [],
      ec: [],
      temp: [],
      reservoir: [],
      totalDoseMl: 0,
      doseKeys: new Set<string>(),
    }
    if (Number.isFinite(entry.ph)) bucket.ph.push(entry.ph)
    if (Number.isFinite(entry.ec)) bucket.ec.push(entry.ec)
    if (entry.temperature != null && Number.isFinite(entry.temperature)) bucket.temp.push(entry.temperature)
    if (entry.reservoir_volume_liters != null && Number.isFinite(entry.reservoir_volume_liters)) {
      bucket.reservoir.push(entry.reservoir_volume_liters)
    }
    if (entry.dose_ml != null && entry.dose_ml > 0 && entry.pump_activated && entry.pump_activated !== 'none') {
      const doseKey = entry.control_cycle_id == null
        ? `${entry.timestamp}-${entry.pump_activated}-${index}`
        : `${entry.control_cycle_id}-${entry.pump_activated}`
      if (!bucket.doseKeys.has(doseKey)) {
        bucket.doseKeys.add(doseKey)
        bucket.totalDoseMl += entry.dose_ml
      }
    }
    buckets.set(day, bucket)
  })

  const summaries = new Map<number, CropLifecycleDaySummary>()
  buckets.forEach((bucket, day) => {
    const avgPh = average(bucket.ph)
    const avgEc = average(bucket.ec)
    if (avgPh == null || avgEc == null) return
    summaries.set(day, {
      readingCount: bucket.ph.length,
      avgPh,
      avgEc,
      avgTemp: average(bucket.temp),
      avgReservoir: average(bucket.reservoir),
      totalDoseMl: bucket.totalDoseMl,
    })
  })
  return summaries
}

function CropLifecycleStrip({
  lifecycle,
  history,
  onNavigate,
}: {
  lifecycle: CropLifecycle
  history: SensorHistoryEntry[]
  onNavigate?: (page: Page) => void
}) {
  const milestoneMarkers = [
    {
      label: 'Transplant',
      day: 0,
      title: 'Transplant day',
      detail: 'Seedlings enter the hydroponic system; keep the environment steady.',
      current: false,
    },
    {
      label: 'Day 7',
      day: 7,
      title: 'Establishment checkpoint',
      detail: 'Roots should be recovering; avoid aggressive correction unless readings drift.',
      current: false,
    },
    {
      label: 'Day 20',
      day: 20,
      title: 'Vegetative growth',
      detail: 'Maintain stable pH, EC, temperature, and water level as biomass increases.',
      current: false,
    },
    {
      label: `Day ${lifecycle.harvestStartDay}`,
      day: lifecycle.harvestStartDay,
      title: 'Harvest window opens',
      detail: 'Start inspecting crop size, roots, and quality before final harvest.',
      current: false,
    },
    {
      label: `Day ${lifecycle.harvestEndDay}`,
      day: lifecycle.harvestEndDay,
      title: 'Harvest window closes',
      detail: 'Crop may be overdue after this point; validate quality and reset lifecycle after harvest.',
      current: false,
    },
  ]
  const hasCurrentDayMarker = lifecycle.ageDays != null && milestoneMarkers.some((marker) => marker.day === lifecycle.ageDays)
  const markers = lifecycle.configured && lifecycle.ageDays != null && !hasCurrentDayMarker
    ? [
      ...milestoneMarkers,
      {
        label: 'Today',
        day: lifecycle.ageDays,
        title: 'Current crop day',
        detail: `${lifecycle.stageLabel}: ${lifecycle.stageNote}`,
        current: true,
      },
    ].sort((a, b) => a.day - b.day)
    : milestoneMarkers
  const summariesByDay = cropLifecycleSummariesByDay(lifecycle, history)
  const markerContext = (marker: (typeof markers)[number]) => {
    if (!lifecycle.configured || lifecycle.ageDays == null) return 'Planned milestone'
    if (marker.day === lifecycle.ageDays) return 'Current crop day'
    if (marker.day < lifecycle.ageDays) return 'Past milestone'
    return 'Upcoming milestone'
  }
  const noDataMessage = (marker: (typeof markers)[number]) => {
    if (!lifecycle.configured) return 'Set a transplant date to connect readings and doses to crop age.'
    if (lifecycle.ageDays != null && marker.day > lifecycle.ageDays) {
      return 'Daily values will appear after this crop day has readings.'
    }
    return 'No readings for this crop day in the current history window.'
  }
  const renderMarker = (
    marker: (typeof markers)[number],
    index: number,
  ) => {
    const summary = summariesByDay.get(marker.day)
    const markerPct = Math.min(100, (marker.day / lifecycle.harvestEndDay) * 100)
    const edgeClass = markerPct < 14 ? 'edge-left' : markerPct > 86 ? 'edge-right' : ''
    const stateClass = marker.current
      ? 'current'
      : lifecycle.ageDays != null && marker.day < lifecycle.ageDays
        ? 'completed'
        : 'upcoming'
    return (
      <button
        key={`${marker.label}-${marker.day}`}
        type="button"
        className={`crop-lifecycle-marker ${stateClass} ${edgeClass} ${index === 0 ? 'first' : ''} ${index === markers.length - 1 ? 'last' : ''}`}
        style={{ left: `${markerPct}%` }}
        aria-label={`${marker.title}. Day ${marker.day}. ${marker.detail}`}
      >
        <i />
        <b>{marker.label}</b>
        <span className="crop-lifecycle-tooltip" role="tooltip">
          <strong>{marker.title}</strong>
          <em>{markerContext(marker)} · Day {marker.day}</em>
          <span>{marker.detail}</span>
          {summary ? (
            <dl className="crop-lifecycle-tooltip-stats" aria-label={`Day ${marker.day} operating values`}>
              <div>
                <dt>Avg pH</dt>
                <dd>{summary.avgPh.toFixed(2)}</dd>
              </div>
              <div>
                <dt>Avg EC</dt>
                <dd>{summary.avgEc.toFixed(2)} mS/cm</dd>
              </div>
              <div>
                <dt>Avg temp</dt>
                <dd>{summary.avgTemp == null ? 'Not recorded' : `${summary.avgTemp.toFixed(1)}°C`}</dd>
              </div>
              <div>
                <dt>Dose total</dt>
                <dd>{formatDoseMl(summary.totalDoseMl)}</dd>
              </div>
              <div>
                <dt>Avg water</dt>
                <dd>{summary.avgReservoir == null ? 'Not recorded' : `${summary.avgReservoir.toFixed(1)} L`}</dd>
              </div>
              <div>
                <dt>Readings</dt>
                <dd>{summary.readingCount}</dd>
              </div>
            </dl>
          ) : (
            <span className="crop-lifecycle-tooltip-muted">{noDataMessage(marker)}</span>
          )}
        </span>
      </button>
    )
  }

  if (!lifecycle.configured) {
    return (
      <section className="crop-lifecycle-strip not_configured">
        <div className="crop-lifecycle-main">
          <div className="crop-lifecycle-head">
            <div className="crop-lifecycle-heading">
              <span>
                <Sprout size={13} strokeWidth={2.4} aria-hidden="true" />
                Crop lifecycle
              </span>
              <strong>Crop age not configured</strong>
            </div>
            <div className="crop-lifecycle-head-action setup">
              <span className="crop-lifecycle-status">
                <CalendarDays size={14} strokeWidth={2.2} aria-hidden="true" />
                Crop age not set
              </span>
              {onNavigate && (
                <button type="button" onClick={() => onNavigate('Settings')}>
                  <Pencil size={13} strokeWidth={2.3} aria-hidden="true" />
                  Set date
                </button>
              )}
            </div>
          </div>
          <p>
            Save the transplant date so HANAS can use {lifecycle.cropVariety || lifecycle.cropType} crop age in
            Agentic AI dosing and recaps.
          </p>
          <div className="crop-lifecycle-rail crop-lifecycle-rail-preview" aria-label="Planned crop lifecycle journey">
            {markers.map(renderMarker)}
          </div>
          <div className="crop-lifecycle-empty-facts" aria-label="Crop lifecycle setup details">
            <span>
              <b>Journey preview</b>
              <strong>Day 0 to {lifecycle.harvestEndDay}</strong>
            </span>
            <span>
              <b>Harvest window</b>
              <strong>Day {lifecycle.harvestStartDay}-{lifecycle.harvestEndDay}</strong>
            </span>
          </div>
        </div>
      </section>
    )
  }

  const harvestNote = lifecycle.stageKey === 'harvest_window'
    ? 'Ready to inspect for harvest'
    : lifecycle.stageKey === 'overdue'
      ? `${lifecycle.daysPastHarvestWindow} day${lifecycle.daysPastHarvestWindow === 1 ? '' : 's'} past window`
      : `${lifecycle.daysUntilHarvestWindow} day${lifecycle.daysUntilHarvestWindow === 1 ? '' : 's'} to harvest window`

  return (
    <section className={`crop-lifecycle-strip ${lifecycle.stageKey}`}>
      <div className="crop-lifecycle-main">
        <div className="crop-lifecycle-head">
          <div className="crop-lifecycle-heading">
            <span>
              <Sprout size={13} strokeWidth={2.4} aria-hidden="true" />
              Crop lifecycle
            </span>
            <strong>Day {lifecycle.ageDays} · {lifecycle.stageLabel}</strong>
          </div>
          <div className="crop-lifecycle-head-action">
            <span className="crop-lifecycle-status">
              <CalendarDays size={14} strokeWidth={2.2} aria-hidden="true" />
              {harvestNote}
            </span>
            {onNavigate && (
              <button type="button" className="compact-card-action" onClick={() => onNavigate('Settings')}>
                <Pencil size={13} strokeWidth={2.3} aria-hidden="true" />
                Edit crop
              </button>
            )}
          </div>
        </div>
        <p>
          {lifecycle.cropVariety || lifecycle.cropType} · Harvest window day {lifecycle.harvestStartDay}-{lifecycle.harvestEndDay}. {lifecycle.stageNote}
        </p>
        <div className="crop-lifecycle-rail" aria-label="Crop lifecycle progress">
          <span className="crop-lifecycle-fill" style={{ width: `${lifecycle.progressPct}%` }} />
          {markers.map(renderMarker)}
        </div>
      </div>
    </section>
  )
}

const OVERVIEW_PIPELINE_STAGES = [
  'orchestrator_agent',
  'monitoring_agent',
  'diagnostic_reasoning_agent',
  'decision_agent',
  'dose_planning_agent',
  'consistency_review',
  'safety_gate',
  'human_review_gate',
] as const

function safetyReviewLabel(value: unknown): string {
  if (value === 'pass') return 'Safety gate passed'
  if (value === 'block') return 'Safety gate blocked'
  if (typeof value === 'string') return formatDisplayText(value)
  return 'Within safety bounds'
}

const OVERVIEW_PIPELINE_ICONS = {
  orchestrator_agent: Route,
  monitoring_agent: Activity,
  diagnostic_reasoning_agent: Brain,
  decision_agent: GitBranch,
  dose_planning_agent: ClipboardCheck,
  consistency_review: ClipboardCheck,
  safety_gate: ShieldCheck,
  human_review_gate: UserCheck,
} as const

function OverviewPipelineCard({
  latestLog,
  cycle,
  currentStage,
  batchStatus,
  cropLifecycle,
  onNavigate,
  fullAgenticMode = false,
}: {
  latestLog: LatestLog
  cycle: ControlCycle
  currentStage?: string | null
  batchStatus?: BatchStatus | null
  cropLifecycle: CropLifecycle
  onNavigate?: (page: Page) => void
  fullAgenticMode?: boolean
}) {
  const meta = latestLog.decision_metadata as Record<string, unknown>
  const triggeredBy = typeof meta.triggered_by === 'string' ? meta.triggered_by : ''
  const isCollectionRow = triggeredBy === 'batch_mode_collection'
  const monitoring = meta.monitoring_agent as Record<string, unknown> | undefined
  const diagnostic = meta.diagnostic_reasoning_agent as Record<string, unknown> | undefined
  const dosePlan = meta.dose_planning_agent as Record<string, unknown> | undefined
  const consistency = meta.consistency_review as Record<string, unknown> | undefined
  const humanReviewGate = (meta.human_review_gate ?? meta.human_in_the_loop) as Record<string, unknown> | undefined
  const orchestration = meta.orchestrator_agent as Record<string, unknown> | undefined
  const skippedAgents = new Set(
    Array.isArray(orchestration?.skipped_agents)
      ? orchestration.skipped_agents.filter((agent): agent is string => typeof agent === 'string')
      : [],
  )
  const waitingForConfirmation = latestLog.status.toLowerCase() === 'confirming'
    || latestLog.decision.toLowerCase() === 'wait_initial_confirmation'
  const decisionSummary = latestLog.pump_activated === 'none' || latestLog.dose_ml <= 0
    ? `No dosing needed${typeof meta.confidence === 'number' ? `, ${Math.round(meta.confidence * 100)}% confidence` : ''}`
    : `${formatDisplayText(latestLog.pump_activated)}, ${typeof meta.confidence === 'number' ? `${Math.round(meta.confidence * 100)}% confidence` : 'confidence pending'}`
  const summaries: Record<(typeof OVERVIEW_PIPELINE_STAGES)[number], string> = {
    orchestrator_agent: monitoring ? 'Reading routed into this control cycle' : 'Reading captured',
    monitoring_agent: isCollectionRow
      ? 'Waiting for the scheduled 10-minute AI review'
      : waitingForConfirmation
        ? 'Waiting for one matching stable reading'
      : `pH ${latestLog.ph.toFixed(2)}, EC ${latestLog.ec.toFixed(2)} mS/cm`,
    diagnostic_reasoning_agent: typeof diagnostic?.classification === 'string'
      && !skippedAgents.has('diagnostic_reasoning_agent')
      ? formatDisplayText(`${diagnostic.classification}`)
      : skippedAgents.has('diagnostic_reasoning_agent') ? 'Not needed for this decision' : formatDisplayText(latestLog.decision),
    decision_agent: isCollectionRow
      ? 'Pending batch decision'
      : skippedAgents.has('decision_agent')
        ? 'Not needed for this decision'
      : decisionSummary,
    dose_planning_agent: isCollectionRow
      ? 'Runs only if batch finds a dose-worthy trend'
      : skippedAgents.has('dose_planning_agent')
        ? 'No dosing action required'
      : cycle.dose_ml > 0
      ? `${formatDoseMl(cycle.dose_ml)}, ${formatPumpDuration(cycle.duration_ms)}`
      : typeof dosePlan?.reason === 'string'
        ? 'No dose planned'
        : 'No physical action',
    consistency_review: isCollectionRow
      ? 'Runs only if the batch recommends a pump command'
      : skippedAgents.has('consistency_review')
        ? 'No dose plan to review'
      : typeof consistency?.review_status === 'string'
      ? formatDisplayText(`${consistency.review_status}`)
      : 'No contradictions found',
    safety_gate: isCollectionRow
      ? 'Runs only if the batch recommends a pump command'
      : typeof consistency?.review_status === 'string'
      ? formatDisplayText(`${consistency.review_status}`)
      : 'Within safety bounds',
    human_review_gate: isCollectionRow
      ? 'No per-reading command to hold'
      : typeof humanReviewGate?.summary === 'string'
      ? humanReviewGate.summary
      : cycle.status === 'wait_human_review'
        ? 'Held for operator approval'
        : 'No operator review required',
  }
  const activeIndex = currentStage
    ? OVERVIEW_PIPELINE_STAGES.findIndex((stage) => stage === currentStage)
    : -1
  const confidence = typeof meta.confidence === 'number'
    ? `${Math.round(meta.confidence * 100)}%`
    : 'pending'
  const safetyLabel = safetyReviewLabel(consistency?.review_status)
  const latestBatch = batchStatus?.batch_latest_analysis
  const footerItems = isCollectionRow
    ? [
      {
        label: 'This reading',
        value: 'Logged for AI review',
        note: 'No pump command was sent by this one-minute reading.',
      },
      {
        label: 'Next review',
        value: batchStatus ? `Every ${Math.round(batchStatus.batch_analysis_interval_seconds / 60)} min` : 'Every 10 min',
        note: 'Agentic AI reviews recent readings as a batch.',
      },
      {
        label: 'Last AI result',
        value: latestBatch ? `${batchSourceLabel(latestBatch.triggered_by, latestBatch.batch_trigger_mode)} · ${formatDisplayText(latestBatch.status)}` : 'No completed batch yet',
        note: latestBatch ? `Cycle #${latestBatch.control_cycle_id}` : 'Will appear after the first scheduled batch.',
      },
      {
        label: 'Crop age',
        value: cropLifecycle.configured ? `Day ${cropLifecycle.ageDays}` : 'Not set',
        note: cropLifecycle.stageLabel,
      },
    ]
    : [
    {
      label: 'Control mode',
      value: formatStrategyLabel(latestLog.control_strategy),
      note: latestLog.control_strategy === 'agentic_ai'
        ? 'Agentic AI decision with deterministic safety checks.'
        : 'Rule-based control decision.',
    },
    {
      label: 'Confidence',
      value: confidence,
      note: 'Confidence reported by the decision stage.',
    },
    {
      label: 'Safety review',
      value: safetyLabel,
      note: 'Final gate before a pump command is allowed.',
    },
    {
      label: 'Crop age',
      value: cropLifecycle.configured ? `Day ${cropLifecycle.ageDays}` : 'Not set',
      note: cropLifecycle.stageLabel,
    },
  ]

  return (
    <section className="overview-pipeline-card">
      <div className="overview-pipeline-header">
        <div>
          <span className="overview-pipeline-eyebrow">
            Agentic AI Pipeline
          </span>
          <h2>{currentStage ? pipelineStageLabel(currentStage) : isCollectionRow ? fullAgenticMode ? 'Full agentic processing enabled' : 'Waiting for 10-minute AI batch' : 'Latest decision trace'}</h2>
          <p>
            {currentStage
              ? pipelineStageSummary(currentStage)
              : isCollectionRow
                ? fullAgenticMode
                  ? 'This row was captured before the mode change. The next 60-second sensor upload will run the complete LLM pipeline.'
                  : 'This one-minute reading is saved now. The complete LLM pipeline runs on the scheduled batch, using recent readings together.'
              : 'Latest agent outputs from reading intake through safety review.'}
          </p>
        </div>
        {onNavigate && (
          <div className="overview-pipeline-actions">
            <button
              type="button"
              className="overview-pipeline-help-link"
              onClick={() => onNavigate('Help')}
            >
              How it works
            </button>
            <button type="button" onClick={() => onNavigate('AI Reasoning')}>
              Full trace
            </button>
          </div>
        )}
      </div>

      <div className="overview-pipeline-steps" aria-label="Agentic AI pipeline summary">
        {OVERVIEW_PIPELINE_STAGES.map((stage, index) => {
          const meta = PIPELINE_STAGE_META[stage]
          const Icon = OVERVIEW_PIPELINE_ICONS[stage]
          const summary = summaries[stage]
          const presentation = overviewPipelineStagePresentation({
            stage,
            index,
            isCollectionRow,
            activeIndex,
            skipped: skippedAgents.has(stage) || summary.toLowerCase().startsWith('skipped'),
            waitingForConfirmation,
          })

          return (
            <div key={stage} className={`overview-pipeline-step ${presentation.status}`}>
              <div className="overview-pipeline-step-head">
                <span
                  className="overview-pipeline-step-number"
                  aria-label={`Step ${index + 1} of ${OVERVIEW_PIPELINE_STAGES.length}`}
                >
                  {index + 1}
                </span>
                <span className="overview-pipeline-step-state">{presentation.label}</span>
              </div>
              <span className="overview-pipeline-step-icon">
                <Icon size={18} strokeWidth={2.2} />
              </span>
              <strong>{meta.shortLabel}</strong>
              <p>{summary}</p>
            </div>
          )
        })}
      </div>

      <div className="overview-pipeline-footer" aria-label="Agentic AI decision metadata">
        {footerItems.map(({ label, value, note }) => (
          <div key={label}>
            <span>{label}</span>
            <strong>{value}</strong>
            <p>{note}</p>
          </div>
        ))}
      </div>
    </section>
  )
}


export function OverviewPage({
  latestLog,
  cycle,
  history,
  reservoirMaxLiters,
  phTarget,
  ecTarget,
  onNavigate,
  currentStage,
  batchStatus,
  overviewSummary,
  onGenerateOverviewSummary,
  overviewSummaryGenerating,
  overviewSummaryError,
  systemSettings,
  referenceTime,
}: {
  latestLog: LatestLog
  cycle: ControlCycle
  history: SensorHistoryEntry[]
  reservoirMaxLiters: number
  phTarget: { min: number; max: number }
  ecTarget: { min: number; max: number }
  onNavigate?: (page: Page) => void
  currentStage?: string | null
  batchStatus?: BatchStatus | null
  overviewSummary?: OverviewSummaryStatus | null
  onGenerateOverviewSummary?: () => Promise<void>
  overviewSummaryGenerating?: boolean
  overviewSummaryError?: string | null
  systemSettings?: SystemSettings | null
  referenceTime?: string
}) {
  const minimumPumpableLiters = systemSettings?.minimum_pumpable_reservoir_volume_liters
    ?? (reservoirMaxLiters >= 40 ? 20 : reservoirMaxLiters * 0.25)
  const consumptionRate = recentReservoirConsumptionRate(history, latestLog, reservoirMaxLiters)
  const reservoir = reservoirStatus(
    latestLog.reservoir_volume_liters,
    reservoirMaxLiters,
    minimumPumpableLiters,
    consumptionRate,
  )
  const isWithinRange =
    latestLog.decision === 'within_range' ||
    latestLog.decision === 'within_control_tolerance' ||
    cycle.status === 'within_range'
  const isMonitoringOnly =
    systemSettings?.monitoring_mode_enabled === true ||
    latestLog.decision === 'monitoring_mode' ||
    latestLog.status === 'monitoring_mode'
  const hasDose =
    cycle.pump_activated !== 'none' ||
    latestLog.pump_activated !== 'none' ||
    cycle.dose_ml > 0 ||
    latestLog.dose_ml > 0
  const showAiContext = !isMonitoringOnly && latestLog.control_strategy === 'agentic_ai' && (Boolean(currentStage) || !isWithinRange)
  const showDecisionPanels = !isMonitoringOnly && (hasDose || !isWithinRange)
  const cropReferenceDate = referenceTime ? new Date(referenceTime) : undefined
  const cropLifecycle = cropLifecycleFromSettings(systemSettings, cropReferenceDate)
  const staleReading = isReadingStale(latestLog.timestamp)
  const hero = staleReading && !systemSettings?.emergency_stop_enabled ? { tone: 'warn' as const, title: 'Latest recorded reading', body: `Recorded ${recordedDate(latestLog.timestamp)}. Waiting for fresh sensor data; this saved decision does not establish current hardware status.`, icon: 'i' } : heroContent(
    latestLog,
    cycle,
    systemSettings?.emergency_stop_enabled === true,
    isMonitoringOnly,
    { phTarget, ecTarget, reservoirCapacity: reservoirMaxLiters, minimumPumpable: minimumPumpableLiters },
  )
  const heroStatus = latestLog.status || cycle.status
  const isPendingHITL = isHITLPending(cycle.status, latestLog.decision)
  const mergeCorrectionContext = !staleReading && showAiContext && heroStatus.toLowerCase() === 'dosing'
  const showHeroStatus = !staleReading && heroStatus.toLowerCase() !== 'dosing'

  return (
    <section className="page-content overview-page">
      {isHITLState(cycle.status, latestLog.decision) && !isPendingHITL && (
        <HITLAlert cycle={cycle} decision={latestLog.decision} onNavigate={onNavigate} />
      )}
      {(!staleReading || systemSettings?.emergency_stop_enabled) && !mergeCorrectionContext && !isPendingHITL && (
        <StatusHero
          tone={hero.tone}
          title={hero.title}
          body={hero.body}
          icon={hero.icon}
          pills={showHeroStatus ? [{ label: heroStatus, tone: hero.tone }] : []}
        />
      )}

      {showAiContext && !staleReading && (
        <OverviewAIContext
          latestLog={latestLog}
          cycle={cycle}
          currentStage={currentStage}
          onNavigate={onNavigate}
          correctionTitle={mergeCorrectionContext ? hero.title : undefined}
        />
      )}

      <div className="grid-12 overview-metrics-grid">
        <MetricCard
          label="pH"
          value={latestLog.ph.toFixed(2)}
          status={phStatus(latestLog.ph, phTarget)}
          tone={phTone(latestLog.ph, phTarget)}
          target={`Target ${phTarget.min} - ${phTarget.max}`}
          deviation={deviationLabel(latestLog.ph, latestLog.ph_deviation, phTarget)}
          footer={staleReading ? 'Historical reading' : 'Recorded stability confirmed'}
          accent
          icon={<Activity size={15} strokeWidth={2.2} />}
        />
        <MetricCard
          label="EC"
          value={latestLog.ec.toFixed(2)}
          status={ecStatus(latestLog.ec, ecTarget)}
          tone={ecTone(latestLog.ec, ecTarget)}
          target={`Target ${ecTarget.min} - ${ecTarget.max} mS/cm`}
          deviation={deviationLabel(latestLog.ec, latestLog.ec_deviation, ecTarget, ' mS/cm')}
          footer={staleReading ? 'Historical reading' : 'Recorded stability confirmed'}
          accent
          icon={<Gauge size={15} strokeWidth={2.2} />}
        />
        <MetricCard
          label="Water Temperature"
          value={`${latestLog.temperature.toFixed(1)}°C`}
          status={tempStatus(latestLog.temperature)}
          tone={tempTone(latestLog.temperature)}
          target="Optimal range 18 - 26°C"
          deviation={tempTone(latestLog.temperature) === 'good' ? 'Within optimal range' : 'Higher temps reduce dissolved O₂'}
          footer={staleReading ? 'Historical temperature' : tempTone(latestLog.temperature) === 'good' ? 'Latest recorded temperature' : 'Informational warning'}
          icon={<Thermometer size={15} strokeWidth={2.2} />}
        />
        <MetricCard
          label="Reservoir"
          value={`${latestLog.reservoir_volume_liters.toFixed(1)} L`}
          status={reservoir.label}
          tone={reservoir.tone}
          target={`${reservoir.pumpableHeadroom.toFixed(1)}\u00a0L pumpable • ${Math.round(reservoir.usablePercent * 100)}% usable`}
          deviation={reservoir.description}
          footer=""
          accent={reservoir.tone === 'good'}
          fillPct={Math.min(1, latestLog.reservoir_volume_liters / reservoirMaxLiters)}
          fillThresholdPct={minimumPumpableLiters / reservoirMaxLiters}
          fillLabel={
            `${latestLog.reservoir_volume_liters.toFixed(1)} L total • `
            + `${minimumPumpableLiters.toFixed(1)} L pumpable minimum • `
            + `${reservoirMaxLiters.toFixed(1)} L capacity`
          }
          icon={<Droplets size={15} strokeWidth={2.2} />}
        />
      </div>

      <CropLifecycleStrip lifecycle={cropLifecycle} history={history} onNavigate={onNavigate} />

      <OverviewDailySummary
        overviewSummary={overviewSummary ?? null}
        onGenerateSummary={onGenerateOverviewSummary}
        isGenerating={overviewSummaryGenerating}
        generationError={overviewSummaryError}
      />

      {!isMonitoringOnly && !systemSettings?.full_agentic_mode_enabled && (
        <OverviewBatchGuard batchStatus={batchStatus ?? null} />
      )}

      {latestLog.control_strategy === 'agentic_ai' && !isMonitoringOnly && (
        <OverviewPipelineCard
          latestLog={latestLog}
          cycle={cycle}
          currentStage={currentStage}
          batchStatus={batchStatus}
          cropLifecycle={cropLifecycle}
          onNavigate={onNavigate}
          fullAgenticMode={systemSettings?.full_agentic_mode_enabled}
        />
      )}

      {showDecisionPanels && (
        <div className="grid-12">
          {hasDose && <CyclePanel cycle={cycle} latestLog={latestLog} wide={isWithinRange} />}
          {!isWithinRange && <WhyPanel latestLog={latestLog} wide={!hasDose} />}
        </div>
      )}

    </section>
  )
}
