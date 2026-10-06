import { useState } from 'react'
import { Check, CheckCircle2, CircleX, Send, SlidersHorizontal, X } from 'lucide-react'
import type { LatestLog, ControlCycle, HumanReviewPayload, Tone } from '../../types'
import { formatDoseMl, formatPumpDuration, phTone, ecTone, tempTone, phStatus, ecStatus, tempStatus } from '../../utils'
import { formatDisplayText } from '../../text'

type PendingDecision = {
  decision: string
  pump_activated: string
  dose_ml: number
  duration_ms: number
  reason: string
  metadata?: { confidence?: number }
}

type SensorSnap = {
  ph?: number
  ec?: number
  temperature?: number
  reservoir_volume_liters?: number
}

const HITL_STATUS_MAP: Record<string, { label: string; tone: Tone; desc: string }> = {
  human_approved_pending_execution: {
    label: 'Approved',
    tone: 'good',
    desc: 'Waiting for ESP32 to execute the approved command.',
  },
  human_override_pending_execution: {
    label: 'Override Applied',
    tone: 'warn',
    desc: 'Waiting for ESP32 to execute the modified command.',
  },
  human_command_dispatched: {
    label: 'Dispatched',
    tone: 'info',
    desc: 'Command has been dispatched to the ESP32 device.',
  },
  human_rejected: {
    label: 'Rejected',
    tone: 'neutral',
    desc: 'The decision was rejected. No dosing action will be taken.',
  },
}

function HITLStatusDisplay({ cycle }: { cycle: ControlCycle }) {
  const info = HITL_STATUS_MAP[cycle.status]
  if (!info) return null
  const StatusIcon = cycle.status === 'human_command_dispatched'
    ? Send
    : cycle.status === 'human_override_pending_execution'
      ? SlidersHorizontal
      : cycle.status === 'human_rejected'
        ? CircleX
        : CheckCircle2
  return (
    <section className={`hitl-status-card ${info.tone}`} aria-label={`Operator review: ${info.label}`}>
      <span className="hitl-status-icon" aria-hidden="true">
        <StatusIcon size={21} strokeWidth={2.1} />
      </span>
      <div className="hitl-status-copy">
        <span className="hitl-status-eyebrow">Operator review</span>
        <strong>{info.label}</strong>
        <p>{info.desc}</p>
      </div>
      <dl className="hitl-status-facts">
        <div>
          <dt>Cycle</dt>
          <dd>#{cycle.id}</dd>
        </div>
        {cycle.pump_activated !== 'none' && (
          <>
            <div>
              <dt>Pump</dt>
              <dd>{formatDisplayText(cycle.pump_activated)}</dd>
            </div>
            <div>
              <dt>Dose</dt>
              <dd>{formatDoseMl(cycle.dose_ml)}</dd>
            </div>
          </>
        )}
      </dl>
    </section>
  )
}

export function HITLReviewCard({
  cycle,
  latestLog,
  onReview,
  phTarget,
  ecTarget,
}: {
  cycle: ControlCycle
  latestLog: LatestLog
  onReview: (cycleId: number, payload: HumanReviewPayload) => Promise<void>
  phTarget: { min: number; max: number }
  ecTarget: { min: number; max: number }
}) {
  const meta = latestLog.decision_metadata as Record<string, unknown>
  const hitlMeta = meta?.human_in_the_loop as Record<string, unknown> | undefined
  const pending = hitlMeta?.pending_decision as PendingDecision | undefined
  const snap = hitlMeta?.sensor_snapshot as SensorSnap | undefined

  const [reviewer, setReviewer] = useState('')
  const [reason, setReason] = useState('')
  const [showOverride, setShowOverride] = useState(false)
  const [overridePump, setOverridePump] = useState<HumanReviewPayload['pump_activated']>(
    (pending?.pump_activated as HumanReviewPayload['pump_activated']) ?? 'ph_down',
  )
  const [overrideDose, setOverrideDose] = useState(String(pending?.dose_ml ?? ''))
  const [overrideDuration, setOverrideDuration] = useState(String(pending?.duration_ms ?? ''))
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const isPendingReview = cycle.status === 'wait_human_review' || latestLog.decision === 'wait_human_review'

  // Show post-review status for non-pending HITL states
  if (!isPendingReview) {
    return <HITLStatusDisplay cycle={cycle} />
  }

  const confidence = typeof pending?.metadata?.confidence === 'number'
    ? `${Math.round(pending.metadata.confidence * 100)}%`
    : '—'

  const ph = snap?.ph ?? latestLog.ph
  const ec = snap?.ec ?? latestLog.ec
  const temp = snap?.temperature ?? latestLog.temperature
  const volume = snap?.reservoir_volume_liters ?? latestLog.reservoir_volume_liters
  async function submit(action: 'approve' | 'reject' | 'override') {
    if ((action === 'reject' || action === 'override') && !reason.trim()) {
      setError('A reason is required to reject or override.')
      return
    }
    if (action === 'override') {
      const dose = Number(overrideDose)
      const dur = Number(overrideDuration)
      if (!overrideDose.trim() || !Number.isFinite(dose) || dose < 0) { setError('Override dose must be a valid number ≥ 0.'); return }
      if (!overrideDuration.trim() || !Number.isSafeInteger(dur) || dur < 0) { setError('Override duration must be a whole number ≥ 0.'); return }
      if (overridePump === 'none' ? dose !== 0 || dur !== 0 : dose <= 0 || dur <= 0) {
        setError('A pump needs a positive dose and runtime; None needs both values set to zero.'); return
      }
    }

    setSubmitting(true)
    setError(null)

    const payload: HumanReviewPayload = {
      action,
      reviewer: reviewer.trim() || undefined,
      reason: reason.trim(),
      ...(action === 'override' && {
        pump_activated: overridePump,
        dose_ml: Number(overrideDose),
        duration_ms: Number(overrideDuration),
      }),
    }

    try {
      await onReview(cycle.id, payload)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Submission failed. Please try again.')
      setSubmitting(false)
    }
  }

  return (
    <section className="hitl-review-card">
      <header className="hitl-review-header">
        <div>
          <span className="hitl-review-eyebrow">Operator approval</span>
          <strong>Review proposed dose</strong>
          <span>No pump command will be sent until you approve or modify this proposal.</span>
        </div>
        <span className="hitl-cycle-id">Cycle #{cycle.id}</span>
      </header>

      <div className="hitl-review-grid">
        <div className="hitl-proposal">
          <strong>Proposed dose</strong>
          <div className="hitl-proposal-rows">
            <div><span>Pump</span><strong>{pending?.pump_activated ? formatDisplayText(pending.pump_activated) : '—'}</strong></div>
            <div><span>Dose</span><strong>{pending?.dose_ml != null ? formatDoseMl(pending.dose_ml as number) : '—'}</strong></div>
            <div><span>Duration</span><strong>{pending?.duration_ms != null ? formatPumpDuration(pending.duration_ms as number) : '—'}</strong></div>
            <div><span>Confidence</span><strong>{confidence}</strong></div>
          </div>
          {pending?.reason && <p className="hitl-proposal-reason">{String(pending.reason)}</p>}
        </div>

        <div className="hitl-snapshot">
          <strong>Sensor readings</strong>
          <div className="hitl-snapshot-rows">
            <div>
              <span>pH</span>
              <strong>{ph.toFixed(2)}</strong>
              <span className={`hitl-reading-state ${phTone(ph, phTarget)}`}>{phStatus(ph, phTarget)}</span>
            </div>
            <div>
              <span>EC</span>
              <strong>{ec.toFixed(2)} mS/cm</strong>
              <span className={`hitl-reading-state ${ecTone(ec, ecTarget)}`}>{ecStatus(ec, ecTarget)}</span>
            </div>
            <div>
              <span>Temperature</span>
              <strong>{temp.toFixed(1)}°C</strong>
              <span className={`hitl-reading-state ${tempTone(temp)}`}>{tempStatus(temp)}</span>
            </div>
            <div>
              <span>Reservoir</span>
              <strong>{volume.toFixed(1)} L</strong>
            </div>
          </div>
        </div>
      </div>

      <div className="hitl-form">
        <label>
          <span>Reviewer name <em>(optional)</em></span>
          <input
            type="text"
            autoComplete="name"
            value={reviewer}
            onChange={(e) => setReviewer(e.target.value)}
            placeholder="Your name or operator ID"
            disabled={submitting}
          />
        </label>
        <label>
          <span>Reason <em>(required for reject / override)</em></span>
          <textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Explain your decision…"
            rows={3}
            disabled={submitting}
          />
        </label>
      </div>

      {showOverride && (
        <div className="hitl-override">
          <strong>Override Parameters</strong>
          <div className="hitl-override-fields">
            <label>
              <span>Pump</span>
              <select
                value={overridePump}
                onChange={(e) => setOverridePump(e.target.value as HumanReviewPayload['pump_activated'])}
                disabled={submitting}
              >
                <option value="ph_up">pH Up</option>
                <option value="ph_down">pH Down</option>
                <option value="ec_up">EC Up</option>
                <option value="ec_down">EC Down</option>
                <option value="none">None (cancel dose)</option>
              </select>
            </label>
            <label>
              <span>Dose (mL)</span>
              <input
                type="number"
                min={0}
                inputMode="decimal"
                step={0.01}
                value={overrideDose}
                onChange={(e) => setOverrideDose(e.target.value)}
                disabled={submitting}
              />
            </label>
            <label>
              <span>Duration (ms)</span>
              <input
                type="number"
                min={0}
                inputMode="numeric"
                step={1}
                value={overrideDuration}
                onChange={(e) => setOverrideDuration(e.target.value)}
                disabled={submitting}
              />
            </label>
          </div>
        </div>
      )}

      {error && <p className="hitl-error">{error}</p>}

      <div className="hitl-actions">
        {!showOverride ? <>
        <button
          type="button"
          className="hitl-btn approve"
          onClick={() => submit('approve')}
          disabled={submitting}
        >
          <Check size={16} strokeWidth={2.3} aria-hidden="true" />
          Approve
        </button>
        <button
          type="button"
          className="hitl-btn reject"
          onClick={() => submit('reject')}
          disabled={submitting}
        >
          <X size={16} strokeWidth={2.3} aria-hidden="true" />
          Reject
        </button>
        <button
          type="button"
          className={`hitl-btn override ${showOverride ? 'active' : ''}`}
          onClick={() => setShowOverride((v) => !v)}
          disabled={submitting}
        >
          <SlidersHorizontal size={16} strokeWidth={2.2} aria-hidden="true" />
          Modify dose
        </button>
        </> : <>
          <button
            type="button"
            className="hitl-btn override"
            onClick={() => setShowOverride(false)}
            disabled={submitting}
          >
            <X size={16} strokeWidth={2.3} aria-hidden="true" />
            Cancel changes
          </button>
          <button
            type="button"
            className="hitl-btn apply-override"
            onClick={() => submit('override')}
            disabled={submitting}
          >
            <Check size={16} strokeWidth={2.3} aria-hidden="true" />
            Apply modified dose
          </button>
        </>}
        {submitting && <span className="hitl-submitting">Submitting…</span>}
      </div>
    </section>
  )
}
