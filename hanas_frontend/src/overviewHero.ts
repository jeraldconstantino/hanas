import type { ControlCycle, LatestLog, Tone } from './types'
import { formatDisplayText } from './text'
import { formatDoseMl } from './format'

export function pumpLabel(pump: string): string {
  switch (pump) {
    case 'ph_up': return 'pH Up'
    case 'ph_down': return 'pH Down'
    case 'ec_up': return 'EC Up'
    case 'ec_down': return 'EC Down'
    default: return pump
  }
}

export function heroContent(
  latestLog: LatestLog,
  cycle: ControlCycle,
  emergencyStopActive = false,
  monitoringOnly = false,
  operatingContext?: {
    phTarget: { min: number; max: number }
    ecTarget: { min: number; max: number }
    reservoirCapacity: number
    minimumPumpable: number
  },
): { tone: Tone; title: string; body: string; icon: string } {
  const decision = latestLog.decision
  const status = cycle.status || latestLog.status

  if (emergencyStopActive) {
    return {
      tone: 'danger',
      title: 'Emergency stop active',
      body: 'Pump commands are locked out. Verify the hardware state, then clear emergency stop from Settings when safe.',
      icon: '⏸',
    }
  }

  if (monitoringOnly || decision === 'monitoring_mode' || status === 'monitoring_mode') {
    return {
      tone: 'info',
      title: 'Live monitoring only',
      body: `pH ${latestLog.ph.toFixed(2)} and EC ${latestLog.ec.toFixed(2)} mS/cm are updating live. AI analysis and pump commands are paused until automatic control is resumed.`,
      icon: 'i',
    }
  }

  if (decision === 'emergency_stop' || status === 'emergency_stopped') {
    return {
      tone: 'warn',
      title: 'Previous cycle emergency-stopped',
      body: `Emergency stop is now cleared. Cycle #${cycle.id} remains recorded as stopped. Verify the hardware before allowing another pump command.`,
      icon: '⏸',
    }
  }

  if (status === 'wait_human_review' || decision === 'wait_human_review') {
    return {
      tone: 'warn',
      title: 'Dosing paused for operator review',
      body: 'HITL is enabled. Go to the Dosing page to approve, reject, or override the proposed command.',
      icon: '⏸',
    }
  }

  if (status === 'mixing' || decision === 'wait_for_mixing') {
    const hasPhysicalDose = latestLog.pump_activated !== 'none' && latestLog.dose_ml > 0
    return {
      tone: 'warn',
      title: hasPhysicalDose
        ? `Mixing after ${pumpLabel(latestLog.pump_activated)} dose`
        : 'Waiting for mixing window',
      body: hasPhysicalDose
        ? `${formatDoseMl(latestLog.dose_ml)} dosed. Nutrients are distributing. Re-measurement pending.`
        : 'No new dose was queued. HANAS is waiting for the previous pump action or safety window before re-evaluating.',
      icon: '⟳',
    }
  }

  if (status === 'dosing') {
    const icon = latestLog.pump_activated === 'ph_down' || latestLog.pump_activated === 'ec_down' ? '↓' : '↑'
    return {
      tone: 'warn',
      title: `${pumpLabel(latestLog.pump_activated)} dosing in progress`,
      body: '',
      icon,
    }
  }

  if (decision === 'within_range' || decision === 'within_control_tolerance' || status === 'within_range') {
    const issues: string[] = []
    if (latestLog.temperature < 18 || latestLog.temperature > 26) {
      issues.push(`Recorded water temperature is ${latestLog.temperature < 18 ? 'low' : 'high'} at ${latestLog.temperature.toFixed(1)}°C.`)
    }
    if (operatingContext) {
      const { phTarget, ecTarget, reservoirCapacity, minimumPumpable } = operatingContext
      if (latestLog.ph < phTarget.min || latestLog.ph > phTarget.max
        || latestLog.ec < ecTarget.min || latestLog.ec > ecTarget.max) {
        issues.push('Recorded pH or EC is outside the configured target range. Validate the readings.')
      }
      const headroom = Math.max(0, latestLog.reservoir_volume_liters - minimumPumpable)
      const usableCapacity = reservoirCapacity - minimumPumpable
      if (latestLog.reservoir_volume_liters > reservoirCapacity) {
        issues.push('Reservoir reading exceeds capacity. Verify the water level before correcting volume.')
      } else if (latestLog.reservoir_volume_liters <= minimumPumpable) {
        issues.push('Reservoir is at or below its pumpable minimum. Refill promptly and validate the water level.')
      } else if (usableCapacity > 0 && headroom / usableCapacity <= 0.3) {
        issues.push(`Reservoir has ${headroom.toFixed(1)} L pumpable headroom. Plan a refill and validate the water level.`)
      }
    }
    if (issues.length) {
      return { tone: 'warn', title: 'Readings need attention', body: issues.join(' '), icon: 'shield-alert' }
    }
    return {
      tone: 'good',
      title: 'pH and EC on target',
      body: `Recorded pH ${latestLog.ph.toFixed(2)} and EC ${latestLog.ec.toFixed(2)} mS/cm are within target. Continue monitoring.`,
      icon: 'shield-check',
    }
  }

  if (status === 'completed' && latestLog.pump_activated !== 'none') {
    return {
      tone: 'good',
      title: 'Cycle completed',
      body: `Last action: ${pumpLabel(latestLog.pump_activated)}, ${formatDoseMl(latestLog.dose_ml)}. System is now re-reading sensors.`,
      icon: '✓',
    }
  }

  if (status === 'confirming' || decision === 'wait_initial_confirmation') {
    return {
      tone: 'info',
      title: 'Confirming initial reading',
      body: 'Waiting for a stable second reading before making a dosing decision.',
      icon: '…',
    }
  }

  if (status === 'unstable' || decision === 'wait_for_stability') {
    return {
      tone: 'info',
      title: 'Waiting for stable readings',
      body: 'Sensor readings are fluctuating. Waiting for pH and EC to stabilise before acting.',
      icon: '…',
    }
  }

  if (decision === 'sensor_anomaly' || status === 'sensor_anomaly') {
    return {
      tone: 'danger',
      title: 'Sensor anomaly detected',
      body: 'One or more readings appear abnormal. Check probe connections and calibration.',
      icon: '⚠',
    }
  }

  if (decision?.startsWith('wait_') || status === 'waiting') {
    return {
      tone: 'info',
      title: 'System waiting',
      body: `${decision ? formatDisplayText(decision) : 'Holding'}. pH ${latestLog.ph.toFixed(2)}, EC ${latestLog.ec.toFixed(2)} mS/cm.`,
      icon: '…',
    }
  }

  return {
    tone: 'info',
    title: 'Monitoring system',
    body: `pH ${latestLog.ph.toFixed(2)}, EC ${latestLog.ec.toFixed(2)} mS/cm. Awaiting next reading.`,
    icon: 'i',
  }
}
