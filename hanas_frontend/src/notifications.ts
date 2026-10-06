import type { NotificationLogEntry } from './types'
import { formatDisplayText, normalizeUnits } from './text'
import { formatDoseMl } from './format'

export const RECENT_ALERT_WINDOW_HOURS = 24
export const RECENT_ALERT_WINDOW_LABEL = 'last 24 h'
const RECENT_ALERT_WINDOW_MS = RECENT_ALERT_WINDOW_HOURS * 60 * 60 * 1000

const ALERT_LABELS: Record<string, string> = {
  dosing_started: 'Dosing command scheduled',
  sensor_anomaly: 'Sensor anomaly',
  possible_delivery_issue: 'Delivery issue',
  human_review_required: 'Human review required',
  wait_safety_gate: 'Safety gate triggered',
}

type ParsedAlert = {
  ph?: string
  ec?: string
  temp?: string
  reservoir?: string
  pump?: string
  doseMl?: string
  dosePerPump?: boolean
  mixingSeconds?: string
}

function matchField(body: string, pattern: RegExp): string | undefined {
  const match = body.match(pattern)
  return match?.[1]?.trim()
}

function notificationTimestamp(log: NotificationLogEntry): number {
  const timestamp = Date.parse(log.timestamp || log.created_at)
  return Number.isFinite(timestamp) ? timestamp : 0
}

export function isRecentNotification(
  log: NotificationLogEntry,
  now = Date.now(),
  windowMs = RECENT_ALERT_WINDOW_MS,
): boolean {
  const timestamp = notificationTimestamp(log)
  return timestamp > 0 && timestamp <= now && now - timestamp <= windowMs
}

function parseAlertBody(body: string): ParsedAlert {
  return {
    ph: matchField(body, /\bpH\s+([0-9.]+)/i)
      ?? matchField(body, /\b(?:low|high)\s+pH\s+at\s+([0-9.]+)/i),
    ec: matchField(body, /\bEC\s+([0-9.]+)/i)
      ?? matchField(body, /\b(?:low|high)\s+EC\s+at\s+([0-9.]+)/i),
    temp: matchField(body, /\bwater temperature\s+([0-9.]+)\s*C\b/i),
    reservoir: matchField(body, /\breservoir\s+([0-9.]+)\s*L\b/i),
    pump: matchField(body, /\bscheduled\s+[0-9.]+\s*mL(?:\s+each)?\s+(?:of\s+)?(pH Up|pH Down|EC Up(?: A\/B)?|EC Down)\b/i),
    doseMl: matchField(body, /\bscheduled\s+([0-9.]+)\s*mL\b/i),
    dosePerPump: /\bscheduled\s+[0-9.]+\s*mL\s+each\b/i.test(body),
    mixingSeconds: matchField(body, /\bMix(?:ing)?(?:\s*:\s*|\s+)([0-9.]+)\s*s/i),
  }
}

function statusPrefix(status: NotificationLogEntry['status']): string {
  if (status === 'disabled') return 'SMS is disabled. The alert was recorded only.'
  if (status === 'suppressed') return 'SMS cooldown suppressed this alert.'
  if (status === 'failed') return 'SMS delivery failed.'
  if (status === 'refunded') return 'The SMS provider refunded this message.'
  if (status === 'submitted' || status === 'pending' || status === 'queued') {
    return 'SMS submitted. Semaphore has not yet updated its delivery status.'
  }
  return ''
}

function readingSentence(parsed: ParsedAlert): string {
  const pieces = [
    parsed.ph ? `pH ${parsed.ph}` : null,
    parsed.ec ? `EC ${parsed.ec} mS/cm` : null,
    parsed.temp ? `water temperature ${parsed.temp} °C` : null,
    parsed.reservoir ? `reservoir ${parsed.reservoir} L` : null,
  ].filter(Boolean)
  return pieces.length > 0 ? `Latest reading: ${pieces.join(', ')}.` : ''
}

export function notificationTitle(log: NotificationLogEntry): string {
  return ALERT_LABELS[log.alert_type] ?? formatDisplayText(log.alert_type)
}

export function humanNotificationMessage(log: NotificationLogEntry): string {
  const parsed = parseAlertBody(log.message_body)
  const prefix = statusPrefix(log.status)

  if (log.alert_type === 'dosing_started') {
    const pump = parsed.pump?.toUpperCase() === 'EC UP A/B'
      ? 'EC Up A/B'
      : parsed.pump ? formatDisplayText(parsed.pump) : null
    const dose = parsed.doseMl
      ? `${formatDoseMl(Number(parsed.doseMl))}${parsed.dosePerPump ? ' per pump' : ''}`
      : null
    const mixing = parsed.mixingSeconds ? ` Mixing wait: ${Number(parsed.mixingSeconds)} s.` : ''
    const command = pump && dose
      ? `${pump} command scheduled for ${dose}.`
      : pump
        ? `${pump} command scheduled. Volume is not available in this alert.`
        : dose
          ? `Dosing command scheduled for ${dose}. Pump is not available in this alert.`
          : 'Dosing command recorded. Pump and volume are not available in this alert.'
    return [prefix, command, readingSentence(parsed), mixing]
      .filter(Boolean)
      .join(' ')
      .replace(/\s+/g, ' ')
      .trim()
  }

  const fallback = normalizeUnits(log.message_body)
    .replace(/^HANAS alert:\s*/i, '')
    .replace(/_/g, ' ')
  return [prefix, fallback].filter(Boolean).join(' ').trim()
}

export function notificationPreview(log: NotificationLogEntry): string {
  const message = humanNotificationMessage(log)
  return message.length > 105 ? `${message.slice(0, 104).trim()}…` : message
}
