import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { ListFilter } from 'lucide-react'
import type { LatestLog, ControlCycle, SensorHistoryEntry, NotificationLogEntry, ConnectionState, Tone } from '../types'
import { formatDateTime, formatDoseMl, formatDuration, formatTime } from '../utils'
import { Pill } from '../components/ui/Pill'
import { Panel } from '../components/ui/Panel'
import { DataTable } from '../components/ui/DataTable'
import { InfoList } from '../components/ui/InfoList'
import { AlertBanner } from '../components/ui/AlertBanner'
import { formatDisplayText, formatStrategyLabel } from '../text'
import { humanNotificationMessage, notificationTitle } from '../notifications'
import { safeLocalStorage, safeSessionStorage } from '../storage'

const SELECTED_CYCLE_KEY = 'hanas_selected_cycle_id'

type CycleLookupState = 'idle' | 'loading' | 'ready' | 'error' | 'invalid'

function parseCycleLookup(value: string): number | null {
  const normalized = value.trim().replace(/^#/, '')
  if (!/^\d+$/.test(normalized)) return null
  const cycleId = Number(normalized)
  return Number.isSafeInteger(cycleId) && cycleId > 0 ? cycleId : null
}

function CycleAudit({
  id,
  status,
  tone,
  rows,
  onFilter,
}: {
  id: number
  status: string
  tone: Tone
  rows: Array<[string, string]>
  onFilter?: (cycleId: number) => void
}) {
  const normalizedStatus = status.toLowerCase()
  const isInProgress = normalizedStatus === 'dosing' || normalizedStatus === 'mixing'
  return (
    <article className={`cycle-audit ${tone}${isInProgress ? ' in-progress' : ''}`}>
      <div>
        <strong>Cycle #{id}</strong>
      </div>
      <InfoList items={rows} />
      {onFilter && id > 0 && (
        <button type="button" className="cycle-audit-action" onClick={() => onFilter(id)}>
          <ListFilter size={14} strokeWidth={2.3} aria-hidden="true" />
          View cycle readings
        </button>
      )}
    </article>
  )
}

const SELECTED_NOTIFICATION_KEY = 'hanas_selected_notification_id'
const DISMISSED_DELIVERY_ALERT_KEY = 'hanas_dismissed_delivery_alert_id'
const SYSTEM_LOG_INITIAL_LIMIT = 10
const SYSTEM_LOG_PAGE_SIZE = 10
const SYSTEM_LOG_MAX_LIMIT = 200
const NOTIFICATION_INITIAL_LIMIT = 5
const NOTIFICATION_PAGE_SIZE = 5
const SYSTEM_LOG_HEADERS = ['Time', 'Cycle', 'Action State', 'pH', 'EC', 'Pump', 'Dose', 'Reason']

function hasPumpAction(entry: SensorHistoryEntry): boolean {
  return (entry.pump_activated ?? 'none') !== 'none' || (entry.dose_ml ?? 0) > 0 || (entry.duration_ms ?? 0) > 0
}

function actionStateForLog(entry: SensorHistoryEntry): string {
  if (entry.decision === 'emergency_stop' || entry.status === 'emergency_stopped') return 'emergency_stopped'
  if (entry.decision === 'maintenance_mode' || entry.status === 'maintenance_mode') return 'maintenance_mode'
  if (entry.decision === 'monitoring_mode' || entry.status === 'monitoring_mode') return 'monitoring_mode'
  if (!hasPumpAction(entry) && (entry.status === 'completed' || entry.status === 'within_range' || entry.status === 'no_action')) {
    return 'logged'
  }
  return entry.status ?? 'logged'
}

function NotificationLogsPanel({ logs }: { logs: NotificationLogEntry[] }) {
  const [visibleCount, setVisibleCount] = useState(NOTIFICATION_INITIAL_LIMIT)
  const [selectedId, setSelectedId] = useState<number | null>(() => {
    const stored = Number(safeSessionStorage.getItem(SELECTED_NOTIFICATION_KEY))
    if (!Number.isFinite(stored) || stored <= 0) return null
    safeSessionStorage.removeItem(SELECTED_NOTIFICATION_KEY)
    return stored
  })
  const rowRefs = useRef<Record<number, HTMLButtonElement | null>>({})

  useEffect(() => {
    if (selectedId == null) return
    const timer = window.setTimeout(() => {
      rowRefs.current[selectedId]?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }, 80)
    return () => window.clearTimeout(timer)
  }, [selectedId, logs.length])

  const selectedIndex = selectedId == null ? -1 : logs.findIndex((entry) => entry.id === selectedId)
  const effectiveVisibleCount = Math.max(visibleCount, selectedIndex + 1)
  const visibleLogs = logs.slice(0, effectiveVisibleCount)
  const hasMore = effectiveVisibleCount < logs.length

  if (logs.length === 0) {
    return (
      <Panel title="Notification history" eyebrow="SMS">
        <p className="theme-muted-note compact">
          No notification logs yet. SMS records appear here once the backend processes sensor readings.
        </p>
      </Panel>
    )
  }

  return (
    <Panel title="Notification history" eyebrow="SMS">
      <div className="notification-log-list">
        {visibleLogs.map((entry) => (
          <button
            type="button"
            key={entry.id}
            ref={(node) => { rowRefs.current[entry.id] = node }}
            className={`notification-log-row ${entry.id === selectedId ? 'selected' : ''}`}
            aria-expanded={entry.id === selectedId}
            onClick={() => setSelectedId((current) => current === entry.id ? null : entry.id)}
          >
            <span className="notification-log-meta">
              <time dateTime={entry.timestamp}>{formatDateTime(entry.timestamp)}</time>
              <Pill
                label={formatDisplayText(entry.status)}
                tone={
                  entry.status === 'sent' ? 'good'
                  : entry.status === 'failed' ? 'danger'
                  : entry.status === 'suppressed' ? 'warn'
                  : 'neutral'
                }
              />
            </span>
            <span className="notification-log-content">
              <strong>{notificationTitle(entry)}</strong>
              <span>{humanNotificationMessage(entry)}</span>
            </span>
          </button>
        ))}
      </div>
      {logs.length > NOTIFICATION_INITIAL_LIMIT && (
        <div className="notification-log-footer">
          <span>Showing {visibleLogs.length} of {logs.length} notifications</span>
          <div>
            {visibleCount > NOTIFICATION_INITIAL_LIMIT && (
              <button type="button" className="secondary" onClick={() => setVisibleCount(NOTIFICATION_INITIAL_LIMIT)}>
                Show fewer
              </button>
            )}
            {hasMore && (
              <button
                type="button"
                onClick={() => setVisibleCount(Math.min(effectiveVisibleCount + NOTIFICATION_PAGE_SIZE, logs.length))}
              >
                Show {Math.min(NOTIFICATION_PAGE_SIZE, logs.length - effectiveVisibleCount)} more
              </button>
            )}
          </div>
        </div>
      )}
    </Panel>
  )
}

export function LogsAlertsPage({
  latestLog,
  cycle,
  history,
  connectionState,
  notificationLogs,
  backendUrl,
  persistDeliveryAlertDismissal = true,
}: {
  latestLog: LatestLog
  cycle: ControlCycle
  history: SensorHistoryEntry[]
  connectionState: ConnectionState
  notificationLogs: NotificationLogEntry[]
  backendUrl: string
  persistDeliveryAlertDismissal?: boolean
}) {
  const isLive = connectionState === 'connected' && history.length > 0
  const [initialCycleQuery] = useState(() => {
    const stored = safeSessionStorage.getItem(SELECTED_CYCLE_KEY) ?? ''
    safeSessionStorage.removeItem(SELECTED_CYCLE_KEY)
    return stored
  })
  const [cycleQuery, setCycleQuery] = useState(initialCycleQuery)
  const [activeCycleId, setActiveCycleId] = useState<number | null>(() => parseCycleLookup(initialCycleQuery))
  const [cycleLookupState, setCycleLookupState] = useState<CycleLookupState>(
    parseCycleLookup(initialCycleQuery) ? 'loading' : 'idle',
  )
  const [cycleLookupRows, setCycleLookupRows] = useState<SensorHistoryEntry[]>([])
  const [cycleLookupVersion, setCycleLookupVersion] = useState(0)
  const [systemLogLimit, setSystemLogLimit] = useState(SYSTEM_LOG_INITIAL_LIMIT)
  const [dismissedDeliveryAlertId, setDismissedDeliveryAlertId] = useState(
    () => persistDeliveryAlertDismissal ? safeLocalStorage.getItem(DISMISSED_DELIVERY_ALERT_KEY) : null,
  )
  const logFeedRef = useRef<HTMLDivElement | null>(null)
  const pendingCycleScrollRef = useRef(Boolean(parseCycleLookup(initialCycleQuery)))

  useEffect(() => {
    if (!activeCycleId || !isLive) return

    const controller = new AbortController()
    const baseUrl = backendUrl.replace(/\/$/, '')

    fetch(`${baseUrl}/api/system-logs?limit=200&cycle_id=${activeCycleId}`, { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error(`Cycle lookup failed with status ${response.status}`)
        return response.json() as Promise<SensorHistoryEntry[]>
      })
      .then((rows) => {
        setCycleLookupRows(rows)
        setCycleLookupState('ready')
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return
        setCycleLookupRows([])
        setCycleLookupState('error')
      })

    return () => controller.abort()
  }, [activeCycleId, backendUrl, cycleLookupVersion, isLive])

  const startCycleLookup = (cycleId: number) => {
    pendingCycleScrollRef.current = true
    setCycleQuery(String(cycleId))
    setCycleLookupRows([])
    setCycleLookupState(isLive ? 'loading' : 'ready')
    setActiveCycleId(cycleId)
    setCycleLookupVersion((current) => current + 1)
    setSystemLogLimit(SYSTEM_LOG_INITIAL_LIMIT)
  }

  const cycleAuditFallbackRow = useMemo<SensorHistoryEntry | null>(
    () => activeCycleId && cycleLookupRows.length === 0 && (activeCycleId === cycle.id || activeCycleId === latestLog.control_cycle_id)
      ? {
        control_cycle_id: activeCycleId,
        timestamp: latestLog.control_cycle_id === activeCycleId ? latestLog.timestamp : cycle.action_started_at ?? latestLog.timestamp,
        action_started_at: cycle.action_started_at,
        action_completed_at: null,
        control_strategy: latestLog.control_strategy,
        ph: latestLog.ph,
        ec: latestLog.ec,
        temperature: latestLog.temperature,
        reservoir_volume_liters: latestLog.reservoir_volume_liters,
        ph_stable_for_seconds: latestLog.ph_stable_for_seconds,
        ec_stable_for_seconds: latestLog.ec_stable_for_seconds,
        decision: latestLog.control_cycle_id === activeCycleId ? latestLog.decision : null,
        pump_activated: cycle.pump_activated,
        dose_ml: cycle.dose_ml,
        duration_ms: cycle.duration_ms,
        ph_deviation: latestLog.control_cycle_id === activeCycleId ? latestLog.ph_deviation : null,
        ec_deviation: latestLog.control_cycle_id === activeCycleId ? latestLog.ec_deviation : null,
        status: cycle.status,
        decision_metadata: latestLog.control_cycle_id === activeCycleId ? latestLog.decision_metadata : {},
      }
      : null,
    [activeCycleId, cycle, cycleLookupRows.length, latestLog],
  )

  const displayedHistory = useMemo(
    () => activeCycleId ? (cycleAuditFallbackRow ? [cycleAuditFallbackRow] : cycleLookupRows) : history,
    [activeCycleId, cycleAuditFallbackRow, cycleLookupRows, history],
  )

  const handleCycleLookup = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const nextCycleId = parseCycleLookup(cycleQuery)
    if (!nextCycleId) {
      setActiveCycleId(null)
      setCycleLookupRows([])
      setCycleLookupState('invalid')
      return
    }
    startCycleLookup(nextCycleId)
  }

  const clearCycleLookup = () => {
    setCycleQuery('')
    setActiveCycleId(null)
    setCycleLookupRows([])
    setCycleLookupState('idle')
    setSystemLogLimit(SYSTEM_LOG_INITIAL_LIMIT)
  }

  const logRows: string[][] = useMemo(
    () => displayedHistory.slice(0, Math.min(systemLogLimit, SYSTEM_LOG_MAX_LIMIT)).map((e) => [
        formatTime(e.timestamp),
        e.control_cycle_id ? `#${e.control_cycle_id}` : '—',
        actionStateForLog(e),
        e.ph.toFixed(2),
        e.ec.toFixed(2),
        e.pump_activated ?? 'none',
        e.dose_ml != null ? formatDoseMl(e.dose_ml) : '—',
        e.decision ?? '—',
      ]),
    [displayedHistory, systemLogLimit],
  )

  const totalCount = history.length
  const shownCount = logRows.length
  const availableLogCount = Math.min(displayedHistory.length, SYSTEM_LOG_MAX_LIMIT)
  const hasMoreSystemLogs = shownCount < availableLogCount
  const isCycleLookupActive = activeCycleId !== null
  const effectiveCycleLookupState: CycleLookupState =
    activeCycleId && !isLive && cycleLookupState === 'loading' ? 'ready' : cycleLookupState

  useEffect(() => {
    if (!pendingCycleScrollRef.current || !activeCycleId || effectiveCycleLookupState === 'loading') return
    const timer = window.setTimeout(() => {
      logFeedRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
      pendingCycleScrollRef.current = false
    }, 120)
    return () => window.clearTimeout(timer)
  }, [activeCycleId, effectiveCycleLookupState, shownCount])

  const deliveryAlert = useMemo(
    () => notificationLogs
      .filter((entry) => entry.alert_type === 'possible_delivery_issue' && entry.status === 'sent')
      .sort((left, right) => (
        new Date(right.timestamp).getTime() - new Date(left.timestamp).getTime() || right.id - left.id
      ))[0],
    [notificationLogs],
  )
  const deliveryAlertResolved = useMemo(() => {
    if (!deliveryAlert) return false
    const alertTime = new Date(deliveryAlert.timestamp).getTime()
    return history.some((entry) => (
      new Date(entry.timestamp).getTime() > alertTime
      && !hasPumpAction(entry)
      && (entry.decision === 'within_range' || entry.status === 'within_range')
    ))
  }, [deliveryAlert, history])
  const hasDeliveryAlert = Boolean(
    deliveryAlert
    && !deliveryAlertResolved
    && String(deliveryAlert.id) !== dismissedDeliveryAlertId,
  )

  const dismissDeliveryAlert = () => {
    if (!deliveryAlert) return
    const alertId = String(deliveryAlert.id)
    if (persistDeliveryAlertDismissal) {
      safeLocalStorage.setItem(DISMISSED_DELIVERY_ALERT_KEY, alertId)
    }
    setDismissedDeliveryAlertId(alertId)
  }

  const cycleTone: Tone =
    cycle.status === 'dosing' || cycle.status === 'mixing' ? 'warn'
    : cycle.status === 'completed' ? 'good'
    : cycle.status.startsWith('human_') ? 'info'
    : 'neutral'

  return (
    <section className="page-content logs-alerts-page">
      {(hasDeliveryAlert || latestLog.temperature > 26) && (
        <div className="alerts-section">
          {hasDeliveryAlert && deliveryAlert && (
            <AlertBanner
              key={deliveryAlert.id}
              tone="warn"
              title="Pump delivery check"
              body={`${deliveryAlert.control_cycle_id ? `Cycle #${deliveryAlert.control_cycle_id}` : 'A previous dose'} showed less movement than expected. Check the solution level, tubing, and pump delivery before the next correction.`}
              actions
              time={formatTime(deliveryAlert.timestamp)}
              onDismiss={dismissDeliveryAlert}
            />
          )}
          {latestLog.temperature > 26 && (
            <AlertBanner
              tone="warn"
              title="Water temperature notice"
              body={`Water Temperature (${latestLog.temperature.toFixed(1)}°C) is above the optimal range (18 - 26°C). Higher water temperature reduces dissolved oxygen. Consider aerating the reservoir.`}
              compact
            />
          )}
        </div>
      )}

      <div className="grid-12">
        <Panel title="System log feed" eyebrow="Audit" className="span-8">
          <div ref={logFeedRef} className="log-feed-scroll-target" aria-hidden="true" />
          <form className="log-audit-toolbar" onSubmit={handleCycleLookup}>
            <div>
              <span>Cycle lookup</span>
              <p>Search an exact cycle number, including older rows outside the latest table window.</p>
            </div>
            <label>
              <span className="sr-only">Cycle number</span>
              <input
                type="search"
                inputMode="numeric"
                pattern="[0-9#]*"
                autoComplete="off"
                placeholder="Enter cycle number"
                value={cycleQuery}
                onChange={(event) => setCycleQuery(event.target.value)}
              />
            </label>
            <div className="log-audit-actions">
              <button type="submit">Search</button>
              {isCycleLookupActive && (
                <button type="button" className="secondary" onClick={clearCycleLookup}>
                  Clear
                </button>
              )}
            </div>
          </form>
          {effectiveCycleLookupState === 'invalid' && (
            <p className="theme-muted-note compact">
              Enter a valid positive cycle number.
            </p>
          )}
          {effectiveCycleLookupState === 'error' && activeCycleId && (
            <p className="theme-muted-note compact">
              Cycle #{activeCycleId} could not be loaded. Check the backend connection and try again.
            </p>
          )}
          {effectiveCycleLookupState === 'ready' && activeCycleId && shownCount === 0 && (
            <p className="theme-muted-note compact">
              No rows found for Cycle #{activeCycleId}. This cycle may belong to another environment, schema, or reset dataset.
            </p>
          )}
          {effectiveCycleLookupState === 'ready' && activeCycleId && cycleAuditFallbackRow && (
            <p className="theme-muted-note compact">
              Cycle #{activeCycleId} exists in the cycle audit. No matching stored sensor-log row was returned, so this table is showing the latest cycle audit details.
            </p>
          )}
          <div className="system-log-scroll">
            {effectiveCycleLookupState === 'loading' ? (
              <div className="table-loading-state">Looking up Cycle #{activeCycleId}...</div>
            ) : connectionState === 'connecting' && shownCount === 0 ? (
              <div className="table-loading-state">Waiting for live system logs...</div>
            ) : connectionState === 'empty' && shownCount === 0 ? (
              <div className="table-loading-state">No sensor logs have been recorded yet.</div>
            ) : (
              <DataTable
                headers={SYSTEM_LOG_HEADERS}
                rows={logRows}
              />
            )}
          </div>
          <div className="table-footer">
            <span>
              {isCycleLookupActive
                ? `Showing ${shownCount} reading${shownCount === 1 ? '' : 's'} for Cycle #${activeCycleId}`
                : totalCount > shownCount
                  ? `Showing newest ${shownCount} of ${totalCount} loaded readings`
                  : `Showing ${shownCount} reading${shownCount === 1 ? '' : 's'}`}
            </span>
            {isCycleLookupActive && (
              <button type="button" onClick={clearCycleLookup}>
                Clear filter
              </button>
            )}
            <div className="system-log-pagination">
              {systemLogLimit > SYSTEM_LOG_INITIAL_LIMIT && (
                <button type="button" onClick={() => setSystemLogLimit(SYSTEM_LOG_INITIAL_LIMIT)}>
                  Show fewer
                </button>
              )}
              {hasMoreSystemLogs && (
                <button
                  type="button"
                  onClick={() => setSystemLogLimit((current) => Math.min(current + SYSTEM_LOG_PAGE_SIZE, SYSTEM_LOG_MAX_LIMIT))}
                >
                  Show {Math.min(SYSTEM_LOG_PAGE_SIZE, availableLogCount - shownCount)} more
                </button>
              )}
            </div>
          </div>
        </Panel>

        <Panel title="Cycle audit" eyebrow="Control" className="span-4">
          <CycleAudit
            id={cycle.id}
            status={formatDisplayText(cycle.status)}
            tone={cycleTone}
            onFilter={startCycleLookup}
            rows={[
              ['Strategy', formatStrategyLabel(latestLog.control_strategy)],
              ['Started', cycle.action_started_at ? formatDateTime(cycle.action_started_at) : 'Not started'],
              ['Action', cycle.pump_activated === 'none' ? 'No pump' : formatDisplayText(cycle.pump_activated)],
              ['Dose', cycle.dose_ml > 0 ? formatDoseMl(cycle.dose_ml) : '—'],
              ['Status', formatDisplayText(cycle.status)],
              ['Mixing', cycle.status === 'mixing'
                ? `${formatDuration(Math.max(0, cycle.mixing_duration_seconds - cycle.mixing_elapsed_seconds))} remaining`
                : '—'],
            ]}
          />
        </Panel>
      </div>

      <NotificationLogsPanel logs={notificationLogs} />
    </section>
  )
}
