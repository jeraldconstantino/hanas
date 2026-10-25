import { expect, test } from '@playwright/test'
import { activeOperationCycle, controlCycleDisplayState } from '../src/controlCycleDisplay'
import type { BatchStatus, ControlCycle } from '../src/types'

const cycle: ControlCycle = {
  id: 1, status: 'inter_dose_mixing', pump_activated: 'ec_up', dose_ml: 10,
  duration_ms: 10000, mixing_duration_seconds: 300, mixing_elapsed_seconds: 0,
  action_started_at: '2026-10-25T00:00:00Z',
  ec_up_a_started_at: '2026-10-25T00:00:00Z',
  inter_dose_mixing_started_at: '2026-10-25T00:00:10Z',
  ec_up_b_started_at: null, action_completed_at: null, completed_at: null,
}

test('inter-dose elapsed time starts at A shutdown, preserving sequence start', () => {
  const display = controlCycleDisplayState(cycle, 70, Date.parse('2026-10-25T00:00:15Z'))
  expect(display).toEqual({ status: 'inter_dose_mixing', mixingElapsedSeconds: 5 })
  expect(cycle.action_started_at).toBe('2026-10-25T00:00:00Z')
})

test('post-dose mixing starts at final pump shutdown', () => {
  const display = controlCycleDisplayState({ ...cycle, status: 'mixing',
    ec_up_b_started_at: '2026-10-25T00:03:10Z', action_completed_at: '2026-10-25T00:03:20Z',
  }, 70, Date.parse('2026-10-25T00:03:27Z'))
  expect(display).toEqual({ status: 'mixing', mixingElapsedSeconds: 7 })
})

test('B dosing and interrupted sequences never infer mixing from sequence age', () => {
  for (const status of ['ec_up_b_dosing', 'error', 'emergency_stopped', 'completed']) {
    expect(controlCycleDisplayState({ ...cycle, status }, 70, Date.parse('2026-10-25T01:00:00Z')).status).toBe(status)
  }
})

test('active command fallback carries stage clocks into the dashboard', () => {
  const command = { control_cycle_id: 1, status: cycle.status, pump_activated: 'ec_up',
    duration_ms: 10000, dose_ml: 10, age_seconds: 15, action_started_at: cycle.action_started_at,
    action_completed_at: null, inter_dose_mixing_started_at: cycle.inter_dose_mixing_started_at,
  }
  const active = activeOperationCycle({ ...cycle, pump_activated: 'none' }, {
    batch_recent_or_active_pump_command: command,
  } as BatchStatus)
  expect(controlCycleDisplayState(active, 70, Date.parse('2026-10-25T00:00:15Z')).mixingElapsedSeconds).toBe(5)
})

test('confirmed completion remains completed in the active command fallback', () => {
  const active = activeOperationCycle({ ...cycle, pump_activated: 'none' }, {
    batch_recent_or_active_pump_command: { control_cycle_id: 1, status: 'completed',
      pump_activated: 'ec_up', duration_ms: 10000, dose_ml: 10, age_seconds: 300,
      action_started_at: cycle.action_started_at, action_completed_at: '2026-10-25T00:03:20Z',
      completed_at: '2026-10-25T00:08:20Z',
    },
  } as BatchStatus)
  expect(active.status).toBe('completed')
})
