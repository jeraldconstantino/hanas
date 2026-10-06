import { type CSSProperties } from 'react'
import { CheckCircle2, Circle, Loader2, X } from 'lucide-react'
import type { LatestLog, ControlCycle, BatchStatus } from '../../types'
import { formatDoseMl, formatDuration, formatPumpDuration, isHITLPending } from '../../utils'
import { pipelineLiveSummary, pipelineStageLabel } from '../../pipeline'
import { Pill } from './Pill'
import { formatDisplayText, formatEmbeddedDisplayText } from '../../text'

type StageStatus = 'completed' | 'active' | 'pending'
type StageSource = 'llm' | 'deterministic' | 'operator' | 'embedded'

interface PipelineStage {
  name: string
  source: StageSource
  status: StageStatus
  summary: string
}

// Maps backend stage key → index in the pipeline order
const STAGE_ORDER: Record<string, number> = {
  orchestrator_agent: 0,
  monitoring_agent: 1,
  diagnostic_reasoning_agent: 2,
  decision_agent: 3,
  dose_planning_agent: 4,
  consistency_review: 5,
  safety_gate: 6,
  human_review_gate: 7,
  execution_agent: 8,
}

const EMPTY_PIPELINE_STAGES: PipelineStage[] = [
  {
    name: 'Orchestrator Agent',
    source: 'llm',
    status: 'pending',
    summary: 'Waiting for the first sensor payload before routing starts.',
  },
  {
    name: 'Monitoring Agent',
    source: 'llm',
    status: 'pending',
    summary: 'Waiting for pH, EC, temperature, and stability readings.',
  },
  {
    name: 'Diagnostic Reasoning Agent',
    source: 'llm',
    status: 'pending',
    summary: 'Not started until monitoring confirms a condition to diagnose.',
  },
  {
    name: 'Decision Agent',
    source: 'llm',
    status: 'pending',
    summary: 'Not started until diagnostic context is available.',
  },
  {
    name: 'Dose Planning Agent',
    source: 'llm',
    status: 'pending',
    summary: 'No dose can be planned before a real reading is stored.',
  },
  {
    name: 'Consistency Review',
    source: 'deterministic',
    status: 'pending',
    summary: 'Waiting to cross-check a completed agent decision.',
  },
  {
    name: 'Safety Gate',
    source: 'deterministic',
    status: 'pending',
    summary: 'Waiting for a candidate pump command to validate.',
  },
  {
    name: 'Human Review Gate',
    source: 'operator',
    status: 'pending',
    summary: 'Waiting to know whether operator approval is required.',
  },
  {
    name: 'ESP32 Execution',
    source: 'embedded',
    status: 'pending',
    summary: 'ESP32 has not sent the first backend reading yet.',
  },
]

function liveStatusFor(stageIndex: number, currentStage: string | null): StageStatus | null {
  if (!currentStage || currentStage === 'completed') return null
  const activeIndex = STAGE_ORDER[currentStage] ?? -1
  if (activeIndex < 0) return null
  if (stageIndex < activeIndex) return 'completed'
  if (stageIndex === activeIndex) return 'active'
  return 'pending'
}

type Meta = Record<string, unknown>

function getField<T>(meta: Meta, key: string): T | undefined {
  return meta[key] as T | undefined
}

function s(v: unknown, fallback = '—'): string {
  return typeof v === 'string' && v.length > 0 ? v : fallback
}

function pctStr(v: unknown): string {
  return typeof v === 'number' ? `${Math.round(v * 100)}%` : '—'
}

function numeric(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

function diagnosticSummary(diagnostic: Meta | undefined): string {
  const classification = formatDisplayText(s(diagnostic?.classification, 'No deviation'))
  const primaryMetric = s(diagnostic?.primary_metric, 'none')
  if (primaryMetric === 'none') {
    return `${classification}. No primary correction metric was needed.`
  }
  return `${classification}. Primary correction: ${formatDisplayText(primaryMetric)}.`
}

function decisionSummary(log: LatestLog, confidence: unknown): string {
  if (log.pump_activated === 'none' || log.dose_ml <= 0) {
    return `No dosing command needed. Confidence ${pctStr(confidence)}.`
  }
  return `${formatDisplayText(log.pump_activated)} selected. Confidence ${pctStr(confidence)}.`
}

function consistencySummary(consistency: Meta | undefined): string {
  const reviewStatus = s(consistency?.review_status, 'pass')
  const issueCount = Array.isArray(consistency?.issues) ? (consistency.issues as unknown[]).length : 0
  if (reviewStatus === 'pass') {
    return issueCount > 0
      ? `Safety review passed with ${issueCount} issue${issueCount === 1 ? '' : 's'} flagged.`
      : 'Safety review passed. No issues flagged.'
  }
  if (reviewStatus === 'block') {
    return issueCount > 0
      ? `Safety review blocked the command. ${issueCount} issue${issueCount === 1 ? '' : 's'} flagged.`
      : 'Safety review blocked the command.'
  }
  return `${formatDisplayText(reviewStatus)}. ${issueCount} issue${issueCount === 1 ? '' : 's'} flagged.`
}

function skippedSet(meta: Meta): Set<string> {
  const orchestration = getField<Meta>(meta, 'orchestrator_agent')
  const skipped = Array.isArray(orchestration?.skipped_agents) ? orchestration.skipped_agents : []
  return new Set(skipped.filter((agent): agent is string => typeof agent === 'string'))
}

function missingSavedNote(stage: string): string {
  return `Last batch result did not store a ${stage} note`
}

function buildStages(log: LatestLog, cycle: ControlCycle): PipelineStage[] {
  const meta = (log.decision_metadata ?? {}) as Meta

  const monitoring = getField<Meta>(meta, 'monitoring_agent')
  const diagnostic  = getField<Meta>(meta, 'diagnostic_reasoning_agent')
  const dosePlan    = getField<Meta>(meta, 'dose_planning_agent')
  const consistency = getField<Meta>(meta, 'consistency_review')
  const doseCandidatePump = s(dosePlan?.pump_activated, 'none')
  const hasDoseCandidate = doseCandidatePump !== 'none'
  const isPerComponentDose = numeric(meta.dose_ml_per_component) !== null
  const skipped     = skippedSet(meta)

  const hasAny         = Object.keys(meta).length > 0
  const hasMonitoring  = !!monitoring
  const hasDiagnostic  = !!diagnostic && !skipped.has('diagnostic_reasoning_agent')
  const hasDecision    = meta.confidence != null && !skipped.has('decision_agent')
  const hasDosePlan    = !!dosePlan && !skipped.has('dose_planning_agent')
  const hasConsistency = !!consistency && !skipped.has('consistency_review')

  // Pipeline stopped early when no deviation was detected
  const stoppedEarly = skipped.has('diagnostic_reasoning_agent') || (hasMonitoring && !hasDiagnostic)
  const stoppedAtDecision = skipped.has('dose_planning_agent')

  const hitlBlocked = isHITLPending(cycle.status, log.decision)
  const esp32Active = cycle.status === 'dosing' || cycle.status === 'human_command_dispatched'
  const noHardwareCommand =
    cycle.pump_activated === 'none' || cycle.dose_ml <= 0 || cycle.duration_ms <= 0
  const esp32Done   =
    ['completed', 'mixing'].includes(cycle.status) ||
    (hasConsistency && cycle.pump_activated === 'none')

  function savedStatus(done: boolean): StageStatus {
    if (done) return 'completed'
    return 'pending'
  }

  function esp32Status(): StageStatus {
    if (hitlBlocked) return 'pending'
    if (esp32Active) return 'active'
    if (esp32Done)   return 'completed'
    return 'pending'
  }

  function hitlStatus(): StageStatus {
    if (hitlBlocked) return 'active'
    if (hasAny) return 'completed'
    return 'pending'
  }

  const notReached = stoppedEarly ? 'Skipped by Orchestrator after Monitoring' : '—'

  return [
    {
      name: 'Orchestrator Agent',
      source: 'llm',
      status: hasAny ? 'completed' : 'pending',
      summary: hasMonitoring
        ? stoppedEarly
          ? 'No deviation detected. Result routed to the Safety Gate.'
          : 'Deviation confirmed. Case routed to the specialist agents.'
        : hasAny ? missingSavedNote('orchestration') : 'Waiting for routing details',
    },
    {
      name: 'Monitoring Agent',
      source: 'llm',
      status: savedStatus(hasMonitoring),
      summary: hasMonitoring
        ? formatEmbeddedDisplayText(s(monitoring?.summary))
        : hasAny ? missingSavedNote('monitoring') : 'Waiting for monitoring details',
    },
    {
      name: 'Diagnostic Reasoning Agent',
      source: 'llm',
      status: savedStatus(hasDiagnostic),
      summary: hasDiagnostic
        ? diagnosticSummary(diagnostic)
        : notReached,
    },
    {
      name: 'Decision Agent',
      source: 'llm',
      status: savedStatus(hasDecision),
      summary: hasDecision
        ? decisionSummary(log, meta.confidence)
        : notReached,
    },
    {
      name: 'Dose Planning Agent',
      source: 'llm',
      status: savedStatus(hasDosePlan),
      summary: hasDosePlan
        ? formatEmbeddedDisplayText(s(dosePlan?.reason))
        : stoppedAtDecision ? 'Skipped by Orchestrator because Decision did not allow dosing.' : notReached,
    },
    {
      name: 'Consistency Review',
      source: 'deterministic',
      status: savedStatus(hasConsistency),
      summary: hasConsistency
        ? consistencySummary(consistency)
        : stoppedAtDecision ? 'Skipped because no candidate dose plan needed review.' : notReached,
    },
    {
      name: 'Safety Gate',
      source: 'deterministic',
      status: savedStatus(hasConsistency || hasAny),
      summary: hasConsistency || hasAny
        ? noHardwareCommand
          ? hasDoseCandidate
            ? `${formatDisplayText(doseCandidatePump)} candidate prepared for deterministic validation. The pump remains off.`
            : 'No pump command approved. Hardware stays idle.'
          : `${formatDoseMl(cycle.dose_ml)}${isPerComponentDose ? ' per component' : ''} verified within cap`
        : notReached,
    },
    {
      name: 'Human Review Gate',
      source: 'operator',
      status: hitlStatus(),
      summary: hitlBlocked
        ? 'Holding command for operator approval before ESP32 execution.'
        : noHardwareCommand
          ? 'No physical command requires operator approval.'
          : 'No HITL hold is active for this command.',
    },
    {
      name: 'ESP32 Execution',
      source: 'embedded',
      status: esp32Status(),
      summary:
        noHardwareCommand && (esp32Done || stoppedEarly || hasConsistency)
          ? 'No pump command was sent. Embedded controller stayed idle.'
        : esp32Done    ? `${formatPumpDuration(cycle.duration_ms)}, ${formatDoseMl(cycle.dose_ml)} dosed`
        : esp32Active ? 'Actuating pump'
        : hitlBlocked ? 'Awaiting operator approval'
        : stoppedEarly ? 'No action required'
        : 'Pending execution',
    },
  ]
}

export function AgentPipeline({
  latestLog,
  cycle,
  batchStatus = null,
  defaultExpanded = true,
  currentStage = null,
  onClose,
}: {
  latestLog: LatestLog
  cycle: ControlCycle
  batchStatus?: BatchStatus | null
  defaultExpanded?: boolean
  currentStage?: string | null
  onClose?: () => void
}) {
  const isEmpty = latestLog.status === 'empty' || cycle.status === 'empty'
  const liveStage = isEmpty ? null : currentStage
  const isLive = liveStage !== null && liveStage !== 'completed'
  const latestBatch = batchStatus?.batch_latest_analysis ?? null
  const useBatchResult = !isLive && !isEmpty && latestBatch?.decision_metadata
  const latestCycleSettled = ['completed', 'within_range', 'no_action', 'empty'].includes(cycle.status)
  const activeStageLabel = pipelineStageLabel(liveStage)
  const nextRunLabel =
    batchStatus?.batch_scheduler_running && batchStatus.batch_scheduler_next_run_seconds != null
      ? formatDuration(batchStatus.batch_scheduler_next_run_seconds, { compact: true }).replaceAll(' ', '\u00a0')
      : null
  const nextRunSummary =
    nextRunLabel ? `Next batch in ${nextRunLabel}.`
    : batchStatus?.batch_analysis_enabled ? 'Waiting for the next scheduled AI batch.'
    : 'Scheduled AI batch is disabled.'
  const lastRunLabel =
    batchStatus?.batch_latest_analysis?.timestamp
      ? `Cycle #${batchStatus.batch_latest_analysis.control_cycle_id}`
      : cycle.id ? `Cycle #${cycle.id}` : 'Latest saved cycle'

  if (latestLog.control_strategy !== 'agentic_ai') return null

  const pipelineLog: LatestLog = useBatchResult
    ? {
        ...latestLog,
        control_cycle_id: latestBatch.control_cycle_id,
        decision: latestBatch.decision,
        pump_activated: latestBatch.pump_activated,
        dose_ml: latestBatch.dose_ml,
        duration_ms: latestBatch.duration_ms,
        status: latestBatch.status,
        decision_metadata: latestBatch.decision_metadata,
      }
    : latestLog
  const pipelineCycle: ControlCycle = useBatchResult
    ? {
        ...cycle,
        id: latestBatch.control_cycle_id,
        status: latestBatch.status,
        pump_activated: latestBatch.pump_activated,
        dose_ml: latestBatch.dose_ml,
        duration_ms: latestBatch.duration_ms,
    }
    : cycle

  const pipelineMeta = (pipelineLog.decision_metadata ?? {}) as Meta
  const dosePlan = getField<Meta>(pipelineMeta, 'dose_planning_agent')
  const primaryShadow = getField<Meta>(pipelineMeta, 'agentic_primary_shadow')
  const toolResults = getField<Meta>(pipelineMeta, 'agentic_tool_results')
  const boundedDose = getField<Meta>(toolResults ?? {}, 'calculate_bounded_dose')
  const executionPump = s(
    dosePlan?.pump_activated ?? primaryShadow?.pump_activated ?? boundedDose?.pump_activated,
    pipelineCycle.pump_activated,
  )
  const executionDose = numeric(boundedDose?.bounded_dose_ml)
    ?? numeric(primaryShadow?.dose_ml)
    ?? (pipelineCycle.dose_ml > 0 ? pipelineCycle.dose_ml : null)
  const executionDuration = numeric(boundedDose?.duration_ms)
    ?? numeric(primaryShadow?.duration_ms)
    ?? (pipelineCycle.duration_ms > 0 ? pipelineCycle.duration_ms : null)
  const executionIsPerComponent = numeric(pipelineMeta.dose_ml_per_component) !== null
  const executionDurationLabel = executionDuration === null
    ? null
    : formatPumpDuration(executionDuration).replace(' ', '\u00a0')
  const liveExecutionSummary = liveStage === 'execution_agent'
    && executionPump !== 'none'
    && executionDose !== null
    && executionDuration !== null
    ? `Command sent to ESP32: ${formatDisplayText(executionPump)} · ${formatDoseMl(executionDose)}${executionIsPerComponent ? ' per component' : ''} · ${executionDurationLabel} delivery window.`
    : null

  const metaStages = isEmpty ? EMPTY_PIPELINE_STAGES : buildStages(pipelineLog, pipelineCycle)

  // When live pipeline is running, override statuses with real-time data
  const stages: PipelineStage[] = metaStages.map((stage, i) => {
    const live = liveStatusFor(i, liveStage)
    if (live === null) return stage
    const summary = live === 'active'
      ? liveStage === 'execution_agent' && pipelineCycle.status === 'dosing'
        ? 'Actuating pump and waiting for the embedded controller to report completion.'
        : pipelineLiveSummary(liveStage)
      : stage.summary
    return { ...stage, status: live, summary: live === 'active' ? summary : stage.summary }
  })

  let animCount = 0

  return (
    <div className="agent-pipeline">
      <div className="agent-pipeline-header">
        <div className="agent-pipeline-title">
          <span className="eyebrow">Agent pipeline</span>
          {isLive && <span className="pipeline-live-badge">● Live</span>}
        </div>
        {onClose && (
          <button
            type="button"
            className="agent-pipeline-close"
            onClick={onClose}
            aria-label="Hide AI Pipeline panel"
            title="Hide AI Pipeline"
          >
            <X size={15} strokeWidth={2.4} />
            <span>Close</span>
          </button>
        )}
      </div>

      <div className={`agent-pipeline-status ${isLive ? 'live' : 'idle'}`}>
        <span>{isEmpty ? 'Waiting for data' : isLive ? 'Live stage' : 'Last AI batch result'}</span>
        <strong>
          {isEmpty ? 'No sensor reading yet' : isLive ? activeStageLabel : lastRunLabel}
        </strong>
        <p>
          {isEmpty
            ? 'The Agentic AI pipeline will start after the ESP32 posts the first sensor payload.'
            : isLive
              ? liveExecutionSummary ?? (latestCycleSettled
                ? `${pipelineLiveSummary(liveStage)} Latest saved result remains on the dashboard until this live run completes.`
                : pipelineLiveSummary(liveStage))
              : nextRunSummary}
        </p>
      </div>

      <div className="agent-pipeline-body">
        {stages.map((stage, index) => {
          const isLast = index === stages.length - 1
          const nextStatus = isLast ? undefined : stages[index + 1].status
          const lineStatus =
            nextStatus === 'completed' ? 'completed'
            : nextStatus === 'active' ? 'active'
            : 'pending'

          const delay = stage.status !== 'pending'
            ? `${(animCount++) * 0.08}s`
            : undefined

          return (
            <div
              key={stage.name}
              className={`ap-row ap-row-${stage.status}`}
              style={delay ? ({ '--row-delay': delay } as CSSProperties) : undefined}
            >
              {/* Vertical timeline */}
              <div className="ap-timeline">
                <div className={`ap-dot ap-dot-${stage.status}`}>
                  {stage.status === 'completed' && <CheckCircle2 size={18} strokeWidth={2.5} />}
                  {stage.status === 'active' && <Loader2 size={16} strokeWidth={2.5} className="ap-spinner" />}
                  {stage.status === 'pending' && <Circle size={16} strokeWidth={1.5} />}
                </div>
                {!isLast && <div className={`ap-line ap-line-${lineStatus}`} />}
              </div>

              {/* Content */}
              <div className="ap-content">
                <div className="ap-name-row">
                  <strong className={`ap-name ap-name-${stage.status}`}>
                    {stage.name}
                  </strong>
                  <Pill
                    label={stage.source === 'embedded' ? 'Device' : formatDisplayText(stage.source)}
                    tone={stage.source === 'llm' ? 'info' : 'neutral'}
                  />
                </div>
                {defaultExpanded && (
                  <p className={`ap-desc ap-desc-${stage.status}`}>
                    {stage.summary}
                  </p>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
