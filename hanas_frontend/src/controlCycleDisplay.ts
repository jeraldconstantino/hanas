import type { BatchStatus, ControlCycle } from './types'

export function controlCycleDisplayState(
  cycle: ControlCycle,
  reservoirMaxLiters: number,
  nowMs = Date.now(),
): { status: string; mixingElapsedSeconds: number } {
  const fallback = {
    status: cycle.status,
    mixingElapsedSeconds: cycle.mixing_elapsed_seconds,
  }
  if (cycle.status !== 'dosing' || cycle.pump_activated === 'none' || !cycle.action_started_at) {
    return fallback
  }

  const actionStartedMs = new Date(cycle.action_started_at).getTime()
  const doseDurationMs = Math.max(0, cycle.duration_ms)
  if (!Number.isFinite(actionStartedMs) || doseDurationMs <= 0) return fallback

  const elapsedSinceStartMs = Math.max(0, nowMs - actionStartedMs)
  const postDoseElapsedMs = elapsedSinceStartMs - doseDurationMs
  if (postDoseElapsedMs < 0) return fallback

  if (cycle.pump_activated !== 'ec_up') {
    const mixingDurationMs = Math.max(0, cycle.mixing_duration_seconds) * 1000
    if (mixingDurationMs <= 0 || postDoseElapsedMs >= mixingDurationMs) return fallback
    return {
      status: 'mixing',
      mixingElapsedSeconds: Math.floor(postDoseElapsedMs / 1000),
    }
  }

  const interDoseDurationMs = (reservoirMaxLiters <= 25 ? 120 : 180) * 1000
  const interDoseElapsedMs = postDoseElapsedMs
  if (interDoseElapsedMs < 0 || interDoseElapsedMs >= interDoseDurationMs) return fallback

  return {
    status: 'inter_dose_mixing',
    mixingElapsedSeconds: Math.floor(interDoseElapsedMs / 1000),
  }
}

export function activeOperationCycle(
  cycle: ControlCycle,
  batchStatus: BatchStatus | null,
): ControlCycle {
  const command = batchStatus?.batch_recent_or_active_pump_command
  const cycleOwnsPhysicalOperation = cycle.pump_activated !== 'none' && cycle.duration_ms > 0
  if (!command || cycleOwnsPhysicalOperation) return cycle

  const mixingDuration = command.mixing_duration_seconds
    || batchStatus?.batch_physical_guard_window_seconds
    || 0
  const pumpSeconds = Math.max(0, Math.ceil(command.duration_ms / 1000))
  const pumpHasFinishedLocally = command.status === 'dosing' && command.age_seconds >= pumpSeconds
  const isMixing = Boolean(command.action_completed_at)
    || command.status === 'completed'
    || command.status === 'mixing'
    || (pumpHasFinishedLocally && command.pump_activated !== 'ec_up')
  const mixingElapsed = isMixing
    ? command.action_completed_at
      ? command.age_seconds
      : Math.max(0, command.age_seconds - pumpSeconds)
    : 0

  return {
    id: command.control_cycle_id,
    status: isMixing ? 'mixing' : command.status,
    pump_activated: command.pump_activated,
    dose_ml: command.dose_ml,
    duration_ms: command.duration_ms,
    mixing_duration_seconds: mixingDuration,
    mixing_elapsed_seconds: Math.min(mixingElapsed, mixingDuration),
    action_started_at: command.action_started_at ?? command.timestamp,
    action_completed_at: command.action_completed_at,
  }
}
