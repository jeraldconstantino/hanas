import { useEffect, useMemo, useState } from 'react'
import { Activity, ArrowRight, Droplets, Timer, UserCheck } from 'lucide-react'
import type { ControlCycle, LatestLog, Page } from '../../types'
import { formatClockDuration, formatDoseMl, isHITLPending } from '../../utils'
import { controlCycleDisplayState } from '../../controlCycleDisplay'
import { formatDisplayText } from '../../text'

export function ActiveOperationBar({
  cycle,
  latestLog,
  reservoirMaxLiters,
  activePage,
  onNavigate,
}: {
  cycle: ControlCycle
  latestLog: LatestLog
  reservoirMaxLiters: number
  activePage: Page
  onNavigate: (page: Page) => void
}) {
  const displayState = controlCycleDisplayState(cycle, reservoirMaxLiters)
  const interDoseMixing = displayState.status === 'inter_dose_mixing'
  const postDoseMixing = displayState.status === 'mixing'
  const ecUpBRunning = displayState.status === 'ec_up_b_dosing'
  const pendingReview = isHITLPending(cycle.status, latestLog.decision)
  const total = interDoseMixing
    ? (reservoirMaxLiters <= 25 ? 120 : 180)
    : cycle.mixing_duration_seconds
  const serverElapsed = displayState.mixingElapsedSeconds
  const syncKey = `${cycle.id}:${displayState.status}:${cycle.action_started_at}:${cycle.mixing_elapsed_seconds}`
  const [elapsed, setElapsed] = useState(serverElapsed)
  const [previousSyncKey, setPreviousSyncKey] = useState(syncKey)
  const intervalComplete = elapsed >= total

  if (previousSyncKey !== syncKey) {
    setPreviousSyncKey(syncKey)
    setElapsed(serverElapsed)
  }

  useEffect(() => {
    if (!interDoseMixing && !postDoseMixing) return
    const timer = window.setInterval(() => setElapsed((value) => Math.min(value + 1, total)), 1000)
    return () => window.clearInterval(timer)
  }, [interDoseMixing, postDoseMixing, total])

  const content = useMemo(() => {
    const pump = formatDisplayText(cycle.pump_activated)
    if (pendingReview) {
      const hitlMetadata = latestLog.decision_metadata?.human_in_the_loop
      const pendingDecision = (
        hitlMetadata && typeof hitlMetadata === 'object'
          ? (hitlMetadata as Record<string, unknown>).pending_decision
          : null
      )
      const proposal = pendingDecision && typeof pendingDecision === 'object'
        ? pendingDecision as Record<string, unknown>
        : null
      const proposalPump = typeof proposal?.pump_activated === 'string'
        && !['', 'none'].includes(proposal.pump_activated.toLowerCase())
        ? formatDisplayText(proposal.pump_activated)
        : cycle.pump_activated !== 'none' ? pump : null
      const proposalDose = typeof proposal?.dose_ml === 'number'
        && Number.isFinite(proposal.dose_ml)
        && proposal.dose_ml > 0
        ? proposal.dose_ml
        : cycle.dose_ml > 0 ? cycle.dose_ml : null
      const proposalSummary = proposalPump && proposalDose != null
        ? `${formatDoseMl(proposalDose)} ${proposalPump} proposal.`
        : 'A proposed dose is waiting.'
      return {
        label: 'Dose awaiting approval',
        detail: `${proposalSummary} The pump remains off until reviewed.`,
        remaining: '',
        tone: 'warn',
        icon: <UserCheck size={18} strokeWidth={2.2} />,
      }
    }
    if (interDoseMixing) {
      if (intervalComplete) {
        return {
          label: 'EC Up B ready',
          detail: 'The A→B safety interval is complete. Continue to the second EC Up component.',
          remaining: 'Ready',
          tone: 'good',
          icon: <Activity size={18} strokeWidth={2.2} />,
        }
      }
      return {
        label: 'EC Up A → B safety mixing',
        detail: 'EC Up A is off. The reservoir is circulating before EC Up B starts.',
        remaining: `${formatClockDuration(Math.max(0, total - elapsed))} remaining`,
        tone: 'info',
        icon: <Droplets size={18} strokeWidth={2.2} />,
      }
    }
    if (postDoseMixing) {
      if (intervalComplete) {
        return {
          label: 'Post-dose mixing complete',
          detail: 'The protected mixing window has ended. The reservoir is ready for re-measurement.',
          remaining: 'Ready',
          tone: 'good',
          icon: <Activity size={18} strokeWidth={2.2} />,
        }
      }
      return {
        label: `Mixing after ${pump}`,
        detail: 'Pump is off. A new correction is locked until re-measurement.',
        remaining: `${formatClockDuration(Math.max(0, total - elapsed))} remaining`,
        tone: 'info',
        icon: <Timer size={18} strokeWidth={2.2} />,
      }
    }
    return {
      label: 'EC Up B dosing',
      detail: 'The second EC Up component is being delivered after the protected A→B mixing delay.',
      remaining: 'Pump running',
      tone: 'warn',
      icon: <Activity size={18} strokeWidth={2.2} />,
    }
  }, [cycle.dose_ml, cycle.pump_activated, elapsed, interDoseMixing, intervalComplete, latestLog.decision_metadata, pendingReview, postDoseMixing, total])

  const onDosingPage = activePage === 'Dosing'
  if ((!interDoseMixing && !postDoseMixing && !ecUpBRunning && !pendingReview) || (pendingReview && onDosingPage)) return null

  return (
    <section className={`active-operation-bar ${content.tone}${pendingReview ? ' pending-review' : ''}${onDosingPage ? ' current-page' : ''}`} role="status" aria-live="polite">
      <span className="active-operation-icon" aria-hidden="true">{content.icon}</span>
      <div>
        <strong>{content.label}</strong>
        <span>{content.detail}</span>
      </div>
      {content.remaining && <b>{content.remaining}</b>}
      {!onDosingPage && (
        <button type="button" className="compact-card-action" onClick={() => onNavigate('Dosing')}>
          {pendingReview ? 'Review dose' : interDoseMixing && intervalComplete ? 'Continue dosing' : 'View dosing'} <ArrowRight size={13} strokeWidth={2.3} aria-hidden="true" />
        </button>
      )}
    </section>
  )
}
