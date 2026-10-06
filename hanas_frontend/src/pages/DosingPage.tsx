import { useEffect, useState, type CSSProperties } from 'react'
import {
  Activity, CheckCircle2, Clock, Droplets, Gauge, LockKeyhole, Power, Timer,
} from 'lucide-react'
import type { LatestLog, ControlCycle, SensorHistoryEntry, Tone, HumanReviewPayload } from '../types'
import { formatClockDuration, formatDoseMl, formatPumpDuration, formatTime, isHITLPending, isHITLState, PUMP_DISPATCHED_STATES, PUMP_QUEUED_STATES } from '../utils'
import { controlCycleDisplayState } from '../controlCycleDisplay'
import { Panel } from '../components/ui/Panel'
import { DataTable } from '../components/ui/DataTable'
import { HITLReviewCard } from '../components/ui/HITLReviewCard'
import { formatDisplayText } from '../text'

const PUMP_DEFS: Array<{ id: string; name: string; functionLabel: string }> = [
  { id: 'ph_up', name: 'pH Up', functionLabel: 'Raises pH' },
  { id: 'ph_down', name: 'pH Down', functionLabel: 'Lowers pH' },
  { id: 'ec_up', name: 'EC Up', functionLabel: 'Increases nutrient concentration' },
  { id: 'ec_down', name: 'EC Down', functionLabel: 'Lowers EC by dilution' },
]

function PumpCard({
  id,
  name,
  status,
  tone,
  functionLabel,
  stats,
  isRunning,
  isDosed,
}: {
  id: string
  name: string
  status: string
  tone: Tone
  functionLabel: string
  stats: string[]
  isRunning: boolean
  isDosed: boolean
}) {
  return (
    <article className={`pump-card pump-card-v2 ${id} ${tone} ${isRunning ? 'running' : ''} ${isDosed ? 'dosed' : ''}`}>
      <div className="pump-card-head">
        <div className="pump-channel">
          <span className="pump-status-dot" />
          <strong>{status}</strong>
        </div>
        <span className="pump-state-mark" aria-hidden="true">
          {isDosed ? <CheckCircle2 size={16} strokeWidth={2.3} /> : <Activity size={16} strokeWidth={2.2} />}
        </span>
      </div>

      <div className="pump-card-main">
        <div className="pump-title-block">
          <h3>{name}</h3>
          <p>{functionLabel}</p>
        </div>

        <div className="pump-flow-meter" aria-label={isRunning ? `${name} dosing flow is active` : `${name} dosing flow is idle`}>
          <span className="flow-track" />
          <span className="flow-pulse pulse-a" />
          <span className="flow-pulse pulse-b" />
          <span className="flow-pulse pulse-c" />
        </div>
      </div>

      <div className="pump-stats">
        {stats.map((stat) => <span key={stat}>{stat}</span>)}
      </div>
    </article>
  )
}

function MixingPanel({
  total,
  remaining,
  elapsedPct,
  pump,
  dose,
  interDose = false,
}: {
  total: number
  remaining: number
  elapsedPct: number
  pump: string
  dose: string
  interDose?: boolean
}) {
  const totalStr = formatClockDuration(total)
  const elapsedStr = formatClockDuration(Math.max(0, total - remaining))
  const isReady = remaining <= 0
  return (
    <Panel
      title={interDose ? isReady ? 'EC Up B ready' : 'Mixing between EC Up A and B' : isReady ? 'Mixing wait complete' : 'Mixing in progress'}
      eyebrow={interDose ? 'Two-part nutrient protection' : 'Post-dose protection'}
      className={`span-5 mixing-panel${isReady ? ' ready' : ''}`}
    >
      <div className="mixing-progress-layout">
        <div
          className="progress-ring"
          style={{ '--elapsed-pct': `${elapsedPct.toFixed(1)}%` } as CSSProperties}
        >
          <strong>{Math.round(Math.min(100, elapsedPct))}%</strong>
          <span>{isReady ? 'complete' : 'mixed'}</span>
        </div>
        <div className="mixing-progress-copy">
          <strong>{interDose ? isReady ? 'The A→B safety interval is complete' : 'Both nutrient pumps remain off during this interval' : isReady ? 'Ready for a fresh reading' : 'Pump is off while the reservoir circulates'}</strong>
          <p>
            {pump} delivered {dose}. {interDose
              ? isReady ? 'HANAS can now start EC Up B.' : 'HANAS waits for full circulation before starting EC Up B.'
              : 'HANAS blocks another correction until this protected mixing window ends.'}
          </p>
          <div className="mixing-progress-meta">
            <span>{elapsedStr} elapsed</span>
            <span>{totalStr} total</span>
          </div>
          <div
            className="mixing-progress-track"
            role="progressbar"
            aria-label="Mixing progress"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(Math.min(100, elapsedPct))}
          >
            <span style={{ width: `${Math.min(100, elapsedPct).toFixed(1)}%` }} />
          </div>
        </div>
      </div>
      <div className="mixing-timeline">
        <span className="done"><CheckCircle2 size={15} /> {interDose ? 'EC Up A delivered' : 'Dose delivered'}</span>
        <span className={isReady ? 'done' : 'active'}><Droplets size={15} /> Mixing</span>
        <span className={isReady ? 'active' : undefined}><Activity size={15} /> {interDose ? 'Start EC Up B' : 'Re-measure'}</span>
      </div>
    </Panel>
  )
}

function DosingLogPanel({ history, wide }: { history: SensorHistoryEntry[]; wide?: boolean }) {
  const dosedRows = history
    .filter((e) => e.pump_activated && e.pump_activated !== 'none')
    .slice(0, 8)
    .map((e) => {
      const dose = e.dose_ml == null ? '—' : formatDoseMl(e.dose_ml)
      const duration = e.duration_ms == null
        ? '—'
        : formatPumpDuration(e.duration_ms)

      return [
        formatTime(e.action_started_at ?? e.timestamp),
        e.pump_activated ?? '—',
        dose,
        duration,
        e.status === 'dosing' ? 'In progress' : e.status ?? '—',
      ]
    })

  const rows = dosedRows.length > 0 ? dosedRows : []
  const isEmpty = rows.length === 0

  return (
    <Panel
      title="Recent doses"
      eyebrow="Dosing log"
      badge="Last 12 hours"
      className={`dosing-log-panel${wide ? '' : ' span-7'}`}
    >
      {isEmpty ? (
        <div className="dosing-empty-state">
          <Clock size={24} strokeWidth={1.8} />
          <strong>No dosing events today</strong>
          <p>HANAS will record pump, dose, duration, and result here after the next correction cycle.</p>
        </div>
      ) : (
        <DataTable
          headers={['Started', 'Pump', 'Dose', 'Duration', 'Result']}
          rows={rows}
        />
      )}
    </Panel>
  )
}

export function DosingPage({
  cycle,
  latestLog,
  history,
  onHumanReview,
  previewPumpActivity = false,
  reservoirMaxLiters,
  phTarget,
  ecTarget,
}: {
  cycle: ControlCycle
  latestLog: LatestLog
  history: SensorHistoryEntry[]
  onHumanReview: (cycleId: number, payload: HumanReviewPayload) => Promise<void>
  previewPumpActivity?: boolean
  reservoirMaxLiters: number
  phTarget: { min: number; max: number }
  ecTarget: { min: number; max: number }
}) {
  const displayState = controlCycleDisplayState(cycle, reservoirMaxLiters)
  const isMixing = displayState.status === 'mixing'
  const isInterDoseMixing = displayState.status === 'inter_dose_mixing'
  const isDosingCycle = displayState.status === 'dosing' || displayState.status === 'ec_up_b_dosing'
  const isCompleted = displayState.status === 'completed'
  const isHITL = isHITLState(cycle.status, latestLog.decision)
  const isPendingHITL = isHITLPending(cycle.status, latestLog.decision)
  const isQueued = PUMP_QUEUED_STATES.has(cycle.status)
  const isDispatched = PUMP_DISPATCHED_STATES.has(cycle.status)
  const hasDose = cycle.pump_activated !== 'none' && cycle.dose_ml > 0 && cycle.duration_ms > 0

  // Local elapsed counter syncs from the API, then ticks every second while mixing.
  const interDoseTotal = reservoirMaxLiters <= 25 ? 120 : 180
  const phaseTotal = isInterDoseMixing ? interDoseTotal : cycle.mixing_duration_seconds
  const apiElapsed = displayState.mixingElapsedSeconds
  const [localElapsed, setLocalElapsed] = useState(apiElapsed)
  const apiElapsedKey = `${cycle.id}:${displayState.status}:${cycle.action_started_at}:${cycle.mixing_elapsed_seconds}`
  const [prevApiElapsedKey, setPrevApiElapsedKey] = useState(apiElapsedKey)

  if (prevApiElapsedKey !== apiElapsedKey) {
    setPrevApiElapsedKey(apiElapsedKey)
    setLocalElapsed(apiElapsed)
  }

  useEffect(() => {
    if (!isMixing && !isInterDoseMixing) return
    const interval = setInterval(() => {
      setLocalElapsed((prev) => Math.min(prev + 1, phaseTotal))
    }, 1000)
    return () => clearInterval(interval)
  }, [isMixing, isInterDoseMixing, phaseTotal])

  const remaining = Math.max(0, phaseTotal - localElapsed)
  const pumpDurationLabel = formatPumpDuration(cycle.duration_ms)
  const elapsedPct = phaseTotal > 0
    ? (localElapsed / phaseTotal) * 100
    : 0
  const activePumpLabel = formatDisplayText(cycle.pump_activated)
  const isMixingComplete = isMixing && remaining <= 0
  const isInterDoseComplete = isInterDoseMixing && remaining <= 0
  const isActiveOperation = hasDose && (isDosingCycle || isMixing || isInterDoseMixing || isQueued || isDispatched)
  const heroTone: Tone = hasDose
    ? (isDosingCycle || isDispatched ? 'warn' : isCompleted ? 'good' : 'info')
    : 'neutral'
  const heroTitle = hasDose
    ? isInterDoseMixing
      ? isInterDoseComplete ? 'EC Up B ready' : 'Mixing EC Up A before EC Up B'
      : isMixing
      ? isMixingComplete
        ? 'Mixing wait complete'
        : `Mixing after ${activePumpLabel} dose`
      : isQueued
        ? `${activePumpLabel} command queued`
        : isDispatched
          ? `${activePumpLabel} command dispatched`
          : isCompleted
            ? `${activePumpLabel} dose complete`
            : `${activePumpLabel} correction in progress`
    : 'No dosing active'
  const heroBody = hasDose
    ? isInterDoseMixing
      ? isInterDoseComplete
        ? `EC Up A delivered ${formatDoseMl(cycle.dose_ml)}. The safety interval is complete and EC Up B can start.`
        : `EC Up A delivered ${formatDoseMl(cycle.dose_ml)}. Its pump is off while the reservoir mixes before EC Up B starts.`
      : isMixing
      ? isMixingComplete
        ? `${formatDoseMl(cycle.dose_ml)} was delivered through ${activePumpLabel}. Waiting for the next confirmed reading.`
        : `${formatDoseMl(cycle.dose_ml)} was delivered through ${activePumpLabel}. The pump is off while the reservoir circulates.`
      : isQueued
        ? `Waiting for ESP32 pickup. Dose ${formatDoseMl(cycle.dose_ml)}. Duration ${pumpDurationLabel}.`
        : isDispatched
          ? `ESP32 received the command. Dose ${formatDoseMl(cycle.dose_ml)}. Duration ${pumpDurationLabel}.`
          : isCompleted
            ? `${formatDoseMl(cycle.dose_ml)} was delivered through ${activePumpLabel}. The pump is off and the cycle is complete.`
            : `${formatDoseMl(cycle.dose_ml)} · ${pumpDurationLabel} delivery window`
    : 'All pumps are idle. HANAS is monitoring sensor readings and will dose only when correction is needed.'
  return (
    <section className="page-content dosing-page">
      {isHITL && (
        <HITLReviewCard
          key={cycle.id}
          cycle={cycle}
          latestLog={latestLog}
          onReview={onHumanReview}
          phTarget={phTarget}
          ecTarget={ecTarget}
        />
      )}
      {isHITL && !isPendingHITL && <hr className="hitl-divider" />}
      {!isPendingHITL && (
        <section className={`dosing-hero ${heroTone}`}>
          <div className="dosing-hero-left">
            <div className={`solution-icon ${isActiveOperation ? 'active' : 'idle'}`}>
              {hasDose ? <Gauge size={24} strokeWidth={2.2} /> : <Power size={24} strokeWidth={2.2} />}
            </div>
            <div className="dosing-hero-copy">
              <h2>{heroTitle}</h2>
              <p>{heroBody}</p>
            </div>
          </div>
        </section>
      )}

      <section className="dosing-operational-strip" aria-label="Dosing operation summary">
        <div>
          <span className="dosing-operational-icon">
            <Activity size={18} strokeWidth={2.2} />
          </span>
          <span>Current cycle</span>
          <strong>#{cycle.id}</strong>
        </div>
        <div>
          <span className="dosing-operational-icon">
            <Timer size={18} strokeWidth={2.2} />
          </span>
          <span>Next action</span>
          <strong>
            {isPendingHITL
              ? 'Operator review required'
              : isInterDoseMixing
              ? 'Start EC Up B after mixing'
              : isMixingComplete
              ? 'Await fresh reading now'
              : isMixing
                ? 'Re-measure after mixing'
              : isQueued
                ? 'Waiting for ESP32 pickup'
                : isDispatched
                  ? 'Awaiting pump start confirmation'
                  : isDosingCycle
                    ? 'Await pump completion'
                    : 'Continue monitoring'}
          </strong>
        </div>
        <div>
          <span className="dosing-operational-icon">
            <LockKeyhole size={18} strokeWidth={2.2} />
          </span>
          <span>Dosing lock</span>
          <strong>{isPendingHITL ? 'Waiting for approval' : isMixing || isInterDoseMixing ? 'Active until mixing ends' : 'Safety gates enforced'}</strong>
        </div>
      </section>

      {!isPendingHITL && <div className="pump-grid">
        {PUMP_DEFS.map((pump) => {
          const isActivePump = pump.id === cycle.pump_activated
          const isDosed = isActivePump && (isMixing || isInterDoseMixing || cycle.status === 'completed')
          const isPumpDosing = isActivePump && isDosingCycle
          const isPreviewRunning = previewPumpActivity && isActivePump && hasDose
          const isPumpQueued = isActivePump && isQueued
          const isPumpDispatched = isActivePump && isDispatched

          const status = isInterDoseMixing && isActivePump
            ? 'Mixing A → B'
            : isPreviewRunning
            ? 'Dosing'
            : isPumpDosing
              ? 'Dosing'
              : isPumpDispatched
                ? 'Dispatched'
              : isPumpQueued
                  ? 'Queued'
                  : isDosed
                      ? 'Dosed'
                      : 'Ready'
          const tone: Tone =
            isPreviewRunning || isPumpDosing || isPumpDispatched
              ? 'warn'
              : isInterDoseMixing && isActivePump
                ? 'info'
              : isDosed
                ? 'good'
                : isPumpQueued
                  ? 'info'
                  : 'neutral'
          const stats = isActivePump
            ? [`Dose: ${formatDoseMl(cycle.dose_ml)}`, `Duration: ${pumpDurationLabel}`]
            : ['Awaiting command', 'Calibrated']

          return (
            <PumpCard
              key={pump.id}
              id={pump.id}
              name={pump.name}
              status={status}
              tone={tone}
              functionLabel={pump.functionLabel}
              stats={stats}
              isRunning={isPreviewRunning || isPumpDosing || isPumpDispatched}
              isDosed={!isPreviewRunning && isDosed}
            />
          )
        })}
      </div>}

      {isMixing || isInterDoseMixing ? (
        <div className="grid-12">
          <MixingPanel
            total={phaseTotal}
            remaining={remaining}
            elapsedPct={elapsedPct}
            pump={isInterDoseMixing ? 'EC Up A' : activePumpLabel}
            dose={formatDoseMl(cycle.dose_ml)}
            interDose={isInterDoseMixing}
          />
          <DosingLogPanel history={history} />
        </div>
      ) : (
        <DosingLogPanel history={history} wide />
      )}
    </section>
  )
}
