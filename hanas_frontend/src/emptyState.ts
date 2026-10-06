import type { LatestLog, ControlCycle } from './types'

export function emptyLog(): LatestLog {
  return {
    timestamp: new Date().toISOString(),
    temperature: 0,
    ph: 0,
    ec: 0,
    reservoir_volume_liters: 0,
    log_id: 0,
    control_cycle_id: 0,
    decision: 'no_data',
    pump_activated: 'none',
    dose_ml: 0,
    duration_ms: 0,
    mixing_time_ms: 0,
    ph_stable_for_seconds: null,
    ec_stable_for_seconds: null,
    control_strategy: 'baseline',
    ph_deviation: 0,
    ec_deviation: 0,
    status: 'empty',
    decision_metadata: {},
  }
}

export function emptyCycle(): ControlCycle {
  return {
    id: 0,
    action_started_at: null,
    action_completed_at: null,
    status: 'empty',
    pump_activated: 'none',
    dose_ml: 0,
    duration_ms: 0,
    mixing_duration_seconds: 0,
    mixing_elapsed_seconds: 0,
  }
}

