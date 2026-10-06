import type { LatestLog, ControlCycle, ConnectionState, Page, SystemSettings, Tone } from './types'
import { emptyLog, emptyCycle } from './emptyState'
export { formatDoseMl } from './format'

export function clamp(value: number, min: number, max: number) {
  return Math.min(Math.max(value, min), max)
}

export function formatTime(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return new Intl.DateTimeFormat('en-US', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: true,
  }).format(date).replace(/\b(am|pm)\b/i, (marker) => marker.toUpperCase())
}

export function formatDateTime(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return new Intl.DateTimeFormat('en-US', {
    year: 'numeric',
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: true,
  }).format(date).replace(/\b(am|pm)\b/i, (marker) => marker.toUpperCase())
}

export function formatTimeMinute(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return new Intl.DateTimeFormat('en-US', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: true,
  }).format(date).replace(/\b(am|pm)\b/i, (marker) => marker.toUpperCase())
}

export function formatDuration(seconds: number, options: { compact?: boolean; showSecondsWithHours?: boolean } = {}) {
  if (!Number.isFinite(seconds) || seconds < 0) return '—'
  const safe = Math.max(0, Math.round(seconds))
  const hours = Math.floor(safe / 3600)
  const minutes = Math.floor((safe % 3600) / 60)
  const secs = safe % 60
  const durationPart = (value: number | string, compactUnit: string, fullUnit: string) => (
    options.compact ? `${value}${compactUnit}` : `${value} ${fullUnit}`
  )

  if (hours > 0) {
    const parts = [durationPart(hours, 'h', 'hr')]
    if (minutes > 0) parts.push(durationPart(minutes, 'm', 'min'))
    if (options.showSecondsWithHours && secs > 0) parts.push(durationPart(secs, 's', 's'))
    return parts.join(' ')
  }
  if (minutes > 0) {
    const minuteLabel = durationPart(minutes, 'm', 'min')
    if (secs === 0) return minuteLabel
    const secondLabel = durationPart(secs.toString().padStart(2, '0'), 's', 's')
    return `${minuteLabel} ${secondLabel}`
  }
  return durationPart(secs, 's', 's')
}

export function formatPumpDuration(durationMs: number, options: { compact?: boolean } = {}) {
  if (!Number.isFinite(durationMs) || durationMs < 0) return '—'
  const seconds = durationMs / 1000
  if (seconds < 60 && !Number.isInteger(seconds)) {
    const value = Number(seconds.toFixed(3))
    return `${value}${options.compact ? 's' : ' s'}`
  }
  return formatDuration(seconds, options)
}

export function formatCountdown(seconds: number | null | undefined) {
  if (seconds == null || !Number.isFinite(seconds)) return 'Waiting'
  return formatDuration(seconds)
}

export function formatClockDuration(seconds: number) {
  if (!Number.isFinite(seconds) || seconds < 0) return '0:00'
  const safe = Math.max(0, Math.round(seconds))
  const hours = Math.floor(safe / 3600)
  const minutes = Math.floor((safe % 3600) / 60)
  const secs = safe % 60
  if (hours > 0) return `${hours}:${minutes.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`
  return `${minutes}:${secs.toString().padStart(2, '0')}`
}

export type CropLifecycleStageKey =
  | 'not_configured'
  | 'transplant'
  | 'establishment'
  | 'vegetative'
  | 'sizing'
  | 'harvest_window'
  | 'overdue'

export type CropLifecycle = {
  configured: boolean
  cropType: string
  cropVariety: string
  transplantDate: string | null
  ageDays: number | null
  stageKey: CropLifecycleStageKey
  stageLabel: string
  stageNote: string
  harvestStartDay: number
  harvestEndDay: number
  daysUntilHarvestWindow: number | null
  daysPastHarvestWindow: number | null
  progressPct: number
}

export function cropLifecycleFromSettings(
  settings: SystemSettings | null | undefined,
  now = new Date(),
): CropLifecycle {
  const harvestStartDay = Math.max(1, settings?.crop_harvest_start_day ?? 30)
  const harvestEndDay = Math.max(harvestStartDay, settings?.crop_harvest_end_day ?? 35)
  const transplantDate = settings?.crop_transplant_date ?? null
  const parsedTransplant = transplantDate ? parseLocalDate(transplantDate) : null
  const today = dateOnly(now)
  const ageDays = parsedTransplant && parsedTransplant <= today
    ? daysBetweenLocalDates(parsedTransplant, today)
    : null
  const stage = cropLifecycleStage(ageDays, harvestStartDay, harvestEndDay)
  const progressPct = ageDays == null
    ? 0
    : clamp((ageDays / harvestEndDay) * 100, 0, ageDays > harvestEndDay ? 100 : 96)

  return {
    configured: ageDays != null,
    cropType: settings?.crop_type || 'Lettuce',
    cropVariety: settings?.crop_variety || '',
    transplantDate,
    ageDays,
    stageKey: stage.key,
    stageLabel: stage.label,
    stageNote: stage.note,
    harvestStartDay,
    harvestEndDay,
    daysUntilHarvestWindow: ageDays == null ? null : Math.max(0, harvestStartDay - ageDays),
    daysPastHarvestWindow: ageDays == null ? null : Math.max(0, ageDays - harvestEndDay),
    progressPct,
  }
}

function cropLifecycleStage(
  ageDays: number | null,
  harvestStartDay: number,
  harvestEndDay: number,
): { key: CropLifecycleStageKey; label: string; note: string } {
  if (ageDays == null) {
    return {
      key: 'not_configured',
      label: 'Set transplant date',
      note: 'Add the transplant date so AI context includes crop age.',
    }
  }
  if (ageDays <= 0) {
    return { key: 'transplant', label: 'Transplant day', note: 'Seedlings are settling into the system.' }
  }
  if (ageDays <= 7) {
    return { key: 'establishment', label: 'Establishment', note: 'Keep readings steady while roots recover.' }
  }
  if (ageDays <= 20) {
    return { key: 'vegetative', label: 'Vegetative growth', note: 'Maintain stable pH, EC, temperature, and water level.' }
  }
  if (ageDays < harvestStartDay) {
    return { key: 'sizing', label: 'Sizing / harvest prep', note: 'Avoid sharp swings as heads approach harvest size.' }
  }
  if (ageDays <= harvestEndDay) {
    return { key: 'harvest_window', label: 'Harvest window', note: 'Verify head size, roots, and quality before harvest.' }
  }
  return { key: 'overdue', label: 'Past harvest window', note: 'Check crop quality and plan harvest soon.' }
}

function parseLocalDate(value: string): Date | null {
  const parts = value.split('-').map(Number)
  if (parts.length !== 3 || parts.some((part) => !Number.isFinite(part))) return null
  const [year, month, day] = parts
  return new Date(year, month - 1, day)
}

function dateOnly(value: Date): Date {
  return new Date(value.getFullYear(), value.getMonth(), value.getDate())
}

function daysBetweenLocalDates(start: Date, end: Date): number {
  const msPerDay = 86_400_000
  return Math.floor((dateOnly(end).getTime() - dateOnly(start).getTime()) / msPerDay)
}

export function toneForConnection(state: ConnectionState): Tone {
  if (state === 'offline') return 'danger'
  if (state === 'connected') return 'good'
  if (state === 'connecting' || state === 'empty') return 'info'
  return 'warn'
}

export function phTone(ph: number, target: { min: number; max: number }): Tone {
  if (isDisplayBoundary(ph, target)) return 'info'
  return ph >= target.min && ph <= target.max ? 'good' : 'danger'
}

export function ecTone(ec: number, target: { min: number; max: number }): Tone {
  if (isDisplayBoundary(ec, target)) return 'info'
  return ec >= target.min && ec <= target.max ? 'good' : 'warn'
}

export function tempTone(temp: number): Tone {
  return temp > 26 || temp < 16 ? 'warn' : 'good'
}

export function phStatus(ph: number, target: { min: number; max: number }): string {
  if (isDisplayBelow(ph, target)) return 'At lower limit'
  if (isDisplayAbove(ph, target)) return 'At upper limit'
  if (ph > target.max) return 'High'
  if (ph < target.min) return 'Low'
  return 'In range'
}

export function ecStatus(ec: number, target: { min: number; max: number }): string {
  if (isDisplayBelow(ec, target)) return 'At lower limit'
  if (isDisplayAbove(ec, target)) return 'At upper limit'
  if (ec > target.max) return 'High'
  if (ec < target.min) return 'Low'
  return 'In range'
}

export function tempStatus(temp: number): string {
  if (temp > 26) return 'Warm'
  if (temp < 16) return 'Cold'
  return 'Optimal'
}

export const HITL_STATES = new Set([
  'wait_human_review',
  'human_approved_pending_execution',
  'human_override_pending_execution',
  'human_command_dispatched',
  'human_rejected',
])

export function isHITLState(status: string, decision?: string): boolean {
  return HITL_STATES.has(status) || decision === 'wait_human_review'
}

export function isHITLPending(status: string, decision?: string): boolean {
  return status === 'wait_human_review' || decision === 'wait_human_review'
}

export const PUMP_QUEUED_STATES = new Set([
  'batch_pending',
  'human_approved_pending_execution',
  'human_override_pending_execution',
])

export const PUMP_DISPATCHED_STATES = new Set([
  'batch_dispatched',
  'human_command_dispatched',
])

export function deviationLabel(
  value: number,
  deviation: number,
  target: { min: number; max: number },
  unit = '',
): string {
  if (isDisplayBelow(value, target) || (value < target.min && isDisplayZeroDeviation(deviation))) {
    return 'At lower limit'
  }
  if (isDisplayAbove(value, target) || (value > target.max && isDisplayZeroDeviation(deviation))) {
    return 'At upper limit'
  }
  if (value > target.max) return `+${deviation.toFixed(2)}${unit} above max`
  if (value < target.min) return `-${deviation.toFixed(2)}${unit} below min`
  return 'Within range'
}

const DISPLAY_RANGE_EPSILON = 0.005

export function isDisplayBelow(value: number, target: { min: number; max: number }): boolean {
  const gap = target.min - value
  return gap > 0 && (gap < DISPLAY_RANGE_EPSILON || roundForDisplay(value) >= roundForDisplay(target.min))
}

export function isDisplayAbove(value: number, target: { min: number; max: number }): boolean {
  const gap = value - target.max
  return gap > 0 && (gap < DISPLAY_RANGE_EPSILON || roundForDisplay(value) <= roundForDisplay(target.max))
}

export function isDisplayBoundary(value: number, target: { min: number; max: number }): boolean {
  return isDisplayBelow(value, target) || isDisplayAbove(value, target)
}

export function isDisplayZeroDeviation(deviation: number): boolean {
  return Math.abs(deviation) < DISPLAY_RANGE_EPSILON
}

function roundForDisplay(value: number): number {
  return Math.round((value + Number.EPSILON) * 100) / 100
}

function numberOrFallback(value: unknown, fallback: number): number {
  if (value == null || value === '') return fallback
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : fallback
}

export function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

export function normalizeLog(input: Partial<LatestLog>): LatestLog {
  return {
    ...emptyLog(),
    ...input,
    timestamp: String(input.timestamp ?? emptyLog().timestamp),
    decision_metadata: isObject(input.decision_metadata) ? input.decision_metadata : emptyLog().decision_metadata,
    ph: numberOrFallback(input.ph, emptyLog().ph),
    ec: numberOrFallback(input.ec, emptyLog().ec),
    temperature: numberOrFallback(input.temperature, emptyLog().temperature),
    reservoir_volume_liters: numberOrFallback(input.reservoir_volume_liters, emptyLog().reservoir_volume_liters),
    ph_stable_for_seconds: input.ph_stable_for_seconds == null ? null : numberOrFallback(input.ph_stable_for_seconds, 0),
    ec_stable_for_seconds: input.ec_stable_for_seconds == null ? null : numberOrFallback(input.ec_stable_for_seconds, 0),
    dose_ml: numberOrFallback(input.dose_ml, emptyLog().dose_ml),
    duration_ms: numberOrFallback(input.duration_ms, emptyLog().duration_ms),
    mixing_time_ms: numberOrFallback(input.mixing_time_ms, emptyLog().mixing_time_ms),
  }
}

export function normalizeCycle(input: Partial<ControlCycle>): ControlCycle {
  return {
    ...emptyCycle(),
    ...input,
    id: numberOrFallback(input.id, emptyCycle().id),
    dose_ml: numberOrFallback(input.dose_ml, emptyCycle().dose_ml),
    duration_ms: numberOrFallback(input.duration_ms, emptyCycle().duration_ms),
    mixing_duration_seconds: numberOrFallback(input.mixing_duration_seconds, emptyCycle().mixing_duration_seconds),
    mixing_elapsed_seconds: numberOrFallback(input.mixing_elapsed_seconds, emptyCycle().mixing_elapsed_seconds),
    action_started_at: input.action_started_at == null ? null : String(input.action_started_at),
  }
}

export function headerForPage(
  page: Page,
): { eyebrow: string; title: string; pills: Array<{ label: string; tone: Tone }> } {
  const headers: Record<Page, { eyebrow: string; title: string; pills: Array<{ label: string; tone: Tone }> }> = {
    Overview: { eyebrow: 'Live monitoring', title: 'Overview', pills: [] },
    Reservoir: { eyebrow: 'Sensor readings', title: 'Reservoir Monitor', pills: [] },
    Dosing: { eyebrow: 'Pump control', title: 'Dosing Activity', pills: [] },
    Trends: { eyebrow: 'Historical data', title: 'Trends & Analytics', pills: [] },
    Camera: { eyebrow: 'Live camera', title: 'Grow View', pills: [] },
    'AI Reasoning': {
      eyebrow: 'AI decision trace',
      title: 'AI Reasoning',
      pills: [],
    },
    'Logs & Alerts': { eyebrow: 'Audit trail', title: 'Logs & Alerts', pills: [] },
    Help: { eyebrow: 'System guide', title: 'Help', pills: [] },
    Settings: { eyebrow: 'Configuration', title: 'System Settings', pills: [] },
  }
  return headers[page]
}
