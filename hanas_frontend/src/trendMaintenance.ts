import type { SensorHistoryEntry } from './types'

export function isMaintenanceReading(entry: SensorHistoryEntry): boolean {
  return (
    entry.decision === 'maintenance_mode' ||
    entry.status === 'maintenance_mode' ||
    entry.decision_metadata?.triggered_by === 'maintenance_mode' ||
    entry.decision_metadata?.maintenance_mode_enabled === true
  )
}

export function isSafetyStopReading(entry: SensorHistoryEntry): boolean {
  return (
    entry.decision === 'emergency_stop' ||
    entry.status === 'emergency_stopped' ||
    entry.decision_metadata?.triggered_by === 'emergency_stop' ||
    entry.decision_metadata?.emergency_stop_enabled === true
  )
}

export function partitionTrendHistory(history: SensorHistoryEntry[]): {
  maintenance: SensorHistoryEntry[]
  safety: SensorHistoryEntry[]
  operational: SensorHistoryEntry[]
} {
  return history.reduce<{ maintenance: SensorHistoryEntry[]; safety: SensorHistoryEntry[]; operational: SensorHistoryEntry[] }>(
    (partition, entry) => {
      const group = isMaintenanceReading(entry)
        ? 'maintenance'
        : isSafetyStopReading(entry)
          ? 'safety'
          : 'operational'
      partition[group].push(entry)
      return partition
    },
    { maintenance: [], safety: [], operational: [] },
  )
}

export function mergeTrendHistory(
  cachedHistory: SensorHistoryEntry[],
  recentHistory: SensorHistoryEntry[],
): SensorHistoryEntry[] {
  const byReading = new Map<string, SensorHistoryEntry>()

  cachedHistory.forEach((entry) => {
    byReading.set(`${entry.control_cycle_id ?? 'none'}|${entry.timestamp}`, entry)
  })
  recentHistory.forEach((entry) => {
    byReading.set(`${entry.control_cycle_id ?? 'none'}|${entry.timestamp}`, entry)
  })

  return [...byReading.values()].sort(
    (left, right) => new Date(right.timestamp).getTime() - new Date(left.timestamp).getTime(),
  )
}

export function trendRangeCacheKey(
  backendUrl: string,
  range: string,
  hours: number | null,
  cropTransplantDate?: string | null,
): string {
  const baseUrl = backendUrl.trim().replace(/\/$/, '')
  const rangeIdentity = range === 'Crop cycle'
    ? cropTransplantDate ?? 'disabled'
    : hours ?? 'disabled'
  return `${baseUrl}|${range}|${rangeIdentity}`
}

export type MaintenanceWindow = {
  startedAt: string
  endedAt: string
  readingCount: number
}

export function maintenanceWindows(history: SensorHistoryEntry[]): MaintenanceWindow[] {
  const windows: MaintenanceWindow[] = []
  let current: MaintenanceWindow | null = null

  history.forEach((entry) => {
    if (isMaintenanceReading(entry)) {
      if (current) {
        current.endedAt = entry.timestamp
        current.readingCount += 1
      } else {
        current = {
          startedAt: entry.timestamp,
          endedAt: entry.timestamp,
          readingCount: 1,
        }
      }
      return
    }

    if (current) {
      windows.push(current)
      current = null
    }
  })

  if (current) windows.push(current)
  return windows
}
