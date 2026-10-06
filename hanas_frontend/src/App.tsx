import { emptyLog, emptyCycle } from './emptyState'
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Bot, Loader2, Settings2 } from 'lucide-react'
import './App.css'
import './typography.css'
import type {
  Page, LatestLog, ControlCycle, ConnectionState,
  SensorHistoryEntry, NotificationLogEntry, SystemSettings, HumanReviewPayload, BatchStatus,
  OverviewSummaryStatus, RuntimeSettingsUpdate,
} from './types'
import { API_BASE_URL, PH_TARGET, EC_TARGET } from './constants'
import { normalizeLog, normalizeCycle, headerForPage } from './utils'
import { Sidebar } from './components/layout/Sidebar'
import { HeaderQuickActions, Topbar } from './components/layout/Topbar'
import { MobileNav } from './components/layout/MobileNav'
import { EmptyState } from './components/ui/EmptyState'
import { AgentPipeline } from './components/ui/AgentPipeline'
import { ActiveOperationBar } from './components/ui/ActiveOperationBar'
import { activeOperationCycle } from './controlCycleDisplay'
import { safeLocalStorage, safeSessionStorage } from './storage'

const OverviewPage = lazy(() => import('./pages/OverviewPage').then((module) => ({ default: module.OverviewPage })))
const ReservoirPage = lazy(() => import('./pages/ReservoirPage').then((module) => ({ default: module.ReservoirPage })))
const DosingPage = lazy(() => import('./pages/DosingPage').then((module) => ({ default: module.DosingPage })))
const TrendsPage = lazy(() => import('./pages/TrendsPage').then((module) => ({ default: module.TrendsPage })))
const CameraPage = lazy(() => import('./pages/CameraPage').then((module) => ({ default: module.CameraPage })))
const AIReasoningPage = lazy(() => import('./pages/AIReasoningPage').then((module) => ({ default: module.AIReasoningPage })))
const LogsAlertsPage = lazy(() => import('./pages/LogsAlertsPage').then((module) => ({ default: module.LogsAlertsPage })))
const HelpPage = lazy(() => import('./pages/HelpPage').then((module) => ({ default: module.HelpPage })))
const SettingsPage = lazy(() => import('./pages/SettingsPage').then((module) => ({ default: module.SettingsPage })))

const DASHBOARD_HISTORY_LIMIT = 500
const DASHBOARD_HISTORY_HOURS = 168
const DASHBOARD_HISTORY_REFRESH_MS = 60_000
const DASHBOARD_REQUEST_TIMEOUT_MS = 20_000
const dashboardHistoryCache: { loadedAt: number; baseUrl: string; data: SensorHistoryEntry[] } = {
  loadedAt: 0,
  baseUrl: '',
  data: [],
}

async function fetchDashboard(
  url: string,
  signal?: AbortSignal,
  headers?: HeadersInit,
): Promise<Response> {
  const controller = new AbortController()
  let timedOut = false
  const timeout = window.setTimeout(() => {
    timedOut = true
    controller.abort()
  }, DASHBOARD_REQUEST_TIMEOUT_MS)
  const forwardAbort = () => controller.abort()
  if (signal?.aborted) {
    controller.abort()
  } else {
    signal?.addEventListener('abort', forwardAbort, { once: true })
  }

  try {
    return await fetch(url, { signal: controller.signal, headers })
  } catch (error) {
    if (timedOut) throw new Error('Backend request timed out. Retrying.', { cause: error })
    throw error
  } finally {
    window.clearTimeout(timeout)
    signal?.removeEventListener('abort', forwardAbort)
  }
}

const PAGE_SLUGS: Record<string, Page> = {
  overview: 'Overview',
  reservoir: 'Reservoir',
  dosing: 'Dosing',
  trends: 'Trends',
  camera: 'Camera',
  'ai-reasoning': 'AI Reasoning',
  reasoning: 'AI Reasoning',
  logs: 'Logs & Alerts',
  'logs-alerts': 'Logs & Alerts',
  help: 'Help',
  settings: 'Settings',
}

function AppFooter() {
  return (
    <footer className="app-footer">
      <strong>Operator reminder</strong>
      <span>
        Agentic AI can make mistakes. Validate readings, selected pump, dose, and safety gate before acting.
      </span>
    </footer>
  )
}

function PageLoading() {
  return (
    <section className="page-loading" role="status">
      <Loader2 size={22} strokeWidth={2.2} aria-hidden="true" />
      <span>Loading workspace</span>
    </section>
  )
}

function ExperimentPreflightGate({
  settings,
  loading,
  error,
  operatorToken,
  onOperatorToken,
  onChoose,
}: {
  settings: SystemSettings
  loading: boolean
  error: string | null
  operatorToken: string
  onOperatorToken: (value: string) => void
  onChoose: (action: 'continue' | 'start_new') => Promise<void>
}) {
  const run = settings.active_experiment_run
  const started = run?.start_time
    ? new Date(run.start_time).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })
    : 'Not recorded'
  const lastActivity = run?.last_activity_at
    ? new Date(run.last_activity_at).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })
    : 'No readings yet'

  return (
    <div className="settings-confirm-overlay experiment-preflight-overlay" role="presentation">
      <section
        className="settings-confirm-dialog experiment-preflight-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="experiment-preflight-title"
      >
        <div className="settings-confirm-icon info" aria-hidden="true">
          <Bot size={22} strokeWidth={2.2} />
        </div>
        <div className="settings-confirm-copy">
          <span>Experiment preflight</span>
          <h3 id="experiment-preflight-title">Choose experiment context</h3>
          <p>
            Automatic dosing is paused until you confirm whether today should use the existing
            experiment history or begin with clean adaptive history.
          </p>
          <dl className="experiment-preflight-details">
            <div><dt>Active run</dt><dd>{run ? `#${run.id} · ${run.run_name}` : 'No active run'}</dd></div>
            <div><dt>Started</dt><dd>{started}</dd></div>
            <div><dt>Last activity</dt><dd>{lastActivity}</dd></div>
            <div><dt>Saved cycles</dt><dd>{run?.cycle_count ?? 0}</dd></div>
          </dl>
          <label className="experiment-preflight-auth">
            <span>Operator token</span>
            <input
              type="password"
              autoComplete="off"
              value={operatorToken}
              onChange={(event) => onOperatorToken(event.target.value)}
              placeholder="Required for this safety decision"
            />
          </label>
          {settings.experiment_reset_blocked && (
            <div className="settings-confirm-error" role="status">
              <strong>New run temporarily unavailable</strong>
              <span>{settings.experiment_reset_blocked_reason}</span>
            </div>
          )}
          {error && (
            <div className="settings-confirm-error" role="alert">
              <strong>Preflight blocked</strong>
              <span>{error}</span>
            </div>
          )}
        </div>
        <div className="settings-confirm-actions experiment-preflight-actions">
          <button
            type="button"
            className="secondary"
            disabled={loading || !run}
            onClick={() => void onChoose('continue')}
          >
            Continue current run
          </button>
          <button
            type="button"
            className="primary"
            disabled={loading || !run || settings.experiment_reset_blocked}
            onClick={() => void onChoose('start_new')}
          >
            {loading ? 'Checking...' : 'Start new experiment'}
          </button>
        </div>
      </section>
    </div>
  )
}

function SystemModeBanner({
  emergency = false,
  monitoring = false,
  onSettings,
}: {
  emergency?: boolean
  monitoring?: boolean
  onSettings: () => void
}) {
  return (
    <section
      className={`maintenance-banner${emergency ? ' emergency' : monitoring ? ' monitoring' : ''}`}
      role={emergency ? 'alert' : 'status'}
      aria-live={emergency ? 'assertive' : 'polite'}
    >
      <div>
        <strong>{emergency ? 'Emergency Stop Active' : monitoring ? 'Monitoring Only Active' : 'Maintenance Mode Active'}</strong>
        {emergency ? (
          <p>All pump commands are locked out. Verify pump hardware, then clear emergency stop in Settings.</p>
        ) : monitoring ? (
          <p>Live readings are updating. AI analysis and automatic pump commands are paused until control is resumed.</p>
        ) : (
          <p>
            Automatic dosing is paused. Readings may be missing while sensors, pumps, water flow, or aeration are checked.
          </p>
        )}
      </div>
      <button type="button" onClick={onSettings} aria-label="Review system mode settings">
        <Settings2 size={14} strokeWidth={2.2} aria-hidden="true" />
        <span>Review settings</span>
      </button>
    </section>
  )
}

function DashboardApp({ initialPage = 'Overview' }: { initialPage?: Page }) {
  const [operatorToken, setOperatorToken] = useState(() => {
    const token = safeSessionStorage.getItem('hanas_operator_token')
      ?? safeLocalStorage.getItem('hanas_operator_token')
      ?? ''
    if (token) safeSessionStorage.setItem('hanas_operator_token', token)
    safeLocalStorage.removeItem('hanas_operator_token')
    return token
  })
  const [activePage, setActivePage] = useState<Page>(initialPage)
  const [settingsSectionTarget, setSettingsSectionTarget] = useState<'system' | null>(null)
  const [latestLog, setLatestLog] = useState<LatestLog>(() => emptyLog())
  const [cycle, setCycle] = useState<ControlCycle>(() => emptyCycle())
  const [history, setHistory] = useState<SensorHistoryEntry[]>(() => [])
  const [notificationLogs, setNotificationLogs] = useState<NotificationLogEntry[]>(() => [])
  const [systemSettings, setSystemSettings] = useState<SystemSettings | null>(() => null)
  const [batchStatus, setBatchStatus] = useState<BatchStatus | null>(() => null)
  const [overviewSummary, setOverviewSummary] = useState<OverviewSummaryStatus | null>(() => null)
  const [overviewSummaryGenerating, setOverviewSummaryGenerating] = useState(false)
  const [overviewSummaryError, setOverviewSummaryError] = useState<string | null>(null)
  const [experimentPreflightLoading, setExperimentPreflightLoading] = useState(false)
  const [experimentPreflightError, setExperimentPreflightError] = useState<string | null>(null)
  const [backendUrl, setBackendUrl] = useState(() => API_BASE_URL)
  const [refreshSeconds, setRefreshSeconds] = useState(5)
  const [connectionState, setConnectionState] = useState<ConnectionState>(() => 'connecting')
  const [connectionMessage, setConnectionMessage] = useState(() => 'Checking backend')
  const [lastUpdated, setLastUpdated] = useState(() => '')
  const [manualRefreshKey, setManualRefreshKey] = useState(0)
  const [pipelineStage, setPipelineStage] = useState<string | null>(() => null)
  const hasBackendResponseRef = useRef(false)
  const activeBackendUrlRef = useRef(backendUrl)
  const failedConnectionAttemptsRef = useRef(0)
  const settingsMutationRef = useRef(0)
  const settingsMutationInFlightRef = useRef(false)

  const handleBackendUrlChange = useCallback((nextBackendUrl: string) => {
    if (nextBackendUrl === backendUrl) return
    activeBackendUrlRef.current = nextBackendUrl
    hasBackendResponseRef.current = false
    failedConnectionAttemptsRef.current = 0
    setLatestLog(emptyLog())
    setCycle(emptyCycle())
    setHistory([])
    setNotificationLogs([])
    setSystemSettings(null)
    setBatchStatus(null)
    setOverviewSummary(null)
    setPipelineStage(null)
    setConnectionState('connecting')
    setConnectionMessage('Checking backend')
    setLastUpdated('')
    setBackendUrl(nextBackendUrl)
  }, [backendUrl])

  const handleRefreshSeconds = useCallback((value: number) => {
    setRefreshSeconds([3, 5, 10, 30].includes(value) ? value : 5)
  }, [])

  const loadLatest = useCallback(async (signal?: AbortSignal) => {
    const baseUrl = backendUrl.trim().replace(/\/$/, '')
    const settingsRevision = settingsMutationRef.current
    const isCurrentBackend = () =>
      activeBackendUrlRef.current.trim().replace(/\/$/, '') === baseUrl
    if (!baseUrl) {
      setConnectionState('connecting')
      setConnectionMessage('No backend URL configured')
      return
    }

    try {
      // Fetch dashboard sidecars independently so Settings still shows backend
      // version/config after a production data reset with no sensor rows yet.
      const shouldRefreshHistory =
        dashboardHistoryCache.baseUrl !== baseUrl ||
        Date.now() - dashboardHistoryCache.loadedAt > DASHBOARD_HISTORY_REFRESH_MS

      const [logResponse, cycleResponse] = await Promise.all([
        fetchDashboard(`${baseUrl}/api/system-logs/latest`, signal),
        fetchDashboard(`${baseUrl}/api/control-cycles/latest`, signal),
      ])
      if (!isCurrentBackend()) return

      const hasNoControllerData = logResponse.status === 404 && cycleResponse.status === 404
      if (hasNoControllerData) {
        setLatestLog(emptyLog())
        setCycle(emptyCycle())
        setPipelineStage(null)
        setConnectionState('empty')
        setConnectionMessage('Controller connected. Waiting for the first sensor reading.')
        setLastUpdated(new Date().toISOString())
        hasBackendResponseRef.current = true
        failedConnectionAttemptsRef.current = 0
      } else if (!logResponse.ok || !cycleResponse.ok) {
        throw new Error('Backend read endpoint unavailable')
      } else {
        const nextLog = normalizeLog((await logResponse.json()) as Partial<LatestLog>)
        const nextCycle = normalizeCycle((await cycleResponse.json()) as Partial<ControlCycle>)

        setLatestLog(nextLog)
        setCycle(nextCycle)
        setConnectionState('connected')
        setConnectionMessage('Live sensor data')
        setLastUpdated(new Date().toISOString())
        hasBackendResponseRef.current = true
        failedConnectionAttemptsRef.current = 0
      }

      const sidecarRequests = Promise.allSettled([
        shouldRefreshHistory
          ? fetchDashboard(`${baseUrl}/api/system-logs?limit=${DASHBOARD_HISTORY_LIMIT}&hours=${DASHBOARD_HISTORY_HOURS}`, signal)
          : Promise.resolve(null),
        fetchDashboard(
          `${baseUrl}/api/notification-logs?limit=200`,
          signal,
          operatorToken.trim() ? { 'X-Operator-Token': operatorToken.trim() } : undefined,
        ),
        fetchDashboard(`${baseUrl}/api/settings`, signal),
        fetchDashboard(`${baseUrl}/api/batch/status`, signal),
        fetchDashboard(`${baseUrl}/api/overview-summary`, signal),
      ])
      const [historyRes, notiRes, settingsRes, batchRes, overviewSummaryRes] = await sidecarRequests
      if (!isCurrentBackend()) return
      if (historyRes.status === 'fulfilled' && historyRes.value?.ok) {
        const nextHistory = (await historyRes.value.json()) as SensorHistoryEntry[]
        dashboardHistoryCache.loadedAt = Date.now()
        dashboardHistoryCache.baseUrl = baseUrl
        dashboardHistoryCache.data = nextHistory
        setHistory(nextHistory)
      } else if (dashboardHistoryCache.baseUrl === baseUrl && dashboardHistoryCache.data.length > 0) {
        setHistory(dashboardHistoryCache.data)
      }
      if (notiRes.status === 'fulfilled' && notiRes.value.ok) {
        setNotificationLogs((await notiRes.value.json()) as NotificationLogEntry[])
      }
      if (
        settingsRes.status === 'fulfilled'
        && settingsRes.value.ok
        && settingsRevision === settingsMutationRef.current
        && !settingsMutationInFlightRef.current
      ) {
        const nextSettings = (await settingsRes.value.json()) as SystemSettings
        setSystemSettings(nextSettings)
        setConnectionMessage(
          nextSettings.emergency_stop_enabled
            ? 'Emergency stop active'
            : nextSettings.maintenance_mode_enabled
              ? 'Maintenance mode active'
              : nextSettings.monitoring_mode_enabled
                ? 'Monitoring only — live readings, no AI or pumps'
              : hasNoControllerData
                ? 'Controller connected. Waiting for the first sensor reading.'
                : 'Live sensor data'
        )
      }
      if (batchRes.status === 'fulfilled' && batchRes.value.ok) {
        setBatchStatus((await batchRes.value.json()) as BatchStatus)
      }
      if (overviewSummaryRes.status === 'fulfilled' && overviewSummaryRes.value.ok) {
        setOverviewSummary((await overviewSummaryRes.value.json()) as OverviewSummaryStatus)
        setOverviewSummaryError(null)
      }
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return
      if (!isCurrentBackend()) return
      failedConnectionAttemptsRef.current += 1
      if (failedConnectionAttemptsRef.current >= 2) {
        setConnectionState('offline')
      } else if (!hasBackendResponseRef.current) {
        setConnectionState('connecting')
      }
      const detail = error instanceof Error ? error.message : 'Backend unavailable'
      setConnectionMessage(
        hasBackendResponseRef.current
          ? `Using the last live data while reconnecting: ${detail}`
          : `Waiting for backend: ${detail}`
      )
    }
  }, [backendUrl, operatorToken])

  useEffect(() => {
    let controller: AbortController | null = null
    let timer: number | null = null
    let stopped = false
    const wrappedLoad = async () => {
      if (stopped) return
      controller = new AbortController()
      await loadLatest(controller.signal)
      if (!stopped) {
        timer = window.setTimeout(wrappedLoad, refreshSeconds * 1000)
      }
    }
    wrappedLoad()
    return () => {
      stopped = true
      controller?.abort()
      if (timer != null) window.clearTimeout(timer)
    }
  }, [loadLatest, refreshSeconds, manualRefreshKey])

  const handleHumanReview = useCallback(
    async (cycleId: number, payload: HumanReviewPayload): Promise<void> => {
      const baseUrl = backendUrl.trim().replace(/\/$/, '')
      const headers: Record<string, string> = { 'Content-Type': 'application/json' }
      if (operatorToken.trim()) headers['X-Operator-Token'] = operatorToken.trim()
      const response = await fetch(`${baseUrl}/api/control-cycles/${cycleId}/human-review`, {
        method: 'POST',
        headers,
        body: JSON.stringify(payload),
      })
      if (!response.ok) {
        const body = await response.json().catch(() => null) as { detail?: unknown } | null
        throw new Error(typeof body?.detail === 'string' ? body.detail : 'Review submission failed')
      }
      setManualRefreshKey((k) => k + 1)
    },
    [backendUrl, operatorToken],
  )

  const handleUpdateSettings = useCallback(
    async (updates: RuntimeSettingsUpdate): Promise<void> => {
      const baseUrl = backendUrl.trim().replace(/\/$/, '')
      settingsMutationInFlightRef.current = true
      settingsMutationRef.current += 1
      const headers: Record<string, string> = { 'Content-Type': 'application/json' }
      if (operatorToken.trim()) headers['X-Operator-Token'] = operatorToken.trim()
      try {
        const response = await fetch(`${baseUrl}/api/settings`, {
          method: 'PATCH',
          headers,
          body: JSON.stringify(updates),
        })
        if (!response.ok) {
          if (response.status === 401) {
            throw new Error('Operator token required or incorrect. Add the backend OPERATOR_API_TOKEN in Connection settings.')
          }
          const text = await response.text().catch(() => '')
          let detail = ''
          if (text) {
            try {
              const parsed = JSON.parse(text) as { detail?: unknown }
              detail = typeof parsed.detail === 'string' ? parsed.detail : ''
            } catch {
              detail = ''
            }
          }
          throw new Error(detail || text || 'Settings update failed')
        }
        const nextSettings = (await response.json()) as SystemSettings
        setSystemSettings(nextSettings)
        setManualRefreshKey((k) => k + 1)
      } finally {
        settingsMutationInFlightRef.current = false
        settingsMutationRef.current += 1
      }
    },
    [backendUrl, operatorToken],
  )

  const handleExperimentPreflight = useCallback(
    async (action: 'continue' | 'start_new'): Promise<void> => {
      const baseUrl = backendUrl.trim().replace(/\/$/, '')
      const headers: Record<string, string> = { 'Content-Type': 'application/json' }
      if (operatorToken.trim()) headers['X-Operator-Token'] = operatorToken.trim()
      setExperimentPreflightLoading(true)
      setExperimentPreflightError(null)
      settingsMutationInFlightRef.current = true
      settingsMutationRef.current += 1
      try {
        const response = await fetch(`${baseUrl}/api/settings/experiment-run/preflight`, {
          method: 'POST',
          headers,
          body: JSON.stringify({
            action,
            active_run_id: systemSettings?.active_experiment_run?.id,
          }),
        })
        if (!response.ok) {
          const body = await response.json().catch(() => ({})) as { detail?: string }
          if (response.status === 401) {
            throw new Error('Operator token required or incorrect. Add it in Connection settings.')
          }
          throw new Error(body.detail || 'Experiment preflight failed.')
        }
        setSystemSettings((await response.json()) as SystemSettings)
        setManualRefreshKey((key) => key + 1)
      } catch (preflightError) {
        setExperimentPreflightError(
          preflightError instanceof Error ? preflightError.message : 'Experiment preflight failed.',
        )
      } finally {
        settingsMutationInFlightRef.current = false
        settingsMutationRef.current += 1
        setExperimentPreflightLoading(false)
      }
    },
    [backendUrl, operatorToken, systemSettings],
  )

  const handleGenerateOverviewSummary = useCallback(async (): Promise<void> => {
    const baseUrl = backendUrl.trim().replace(/\/$/, '')
    if (!baseUrl) {
      setOverviewSummaryError('No backend URL configured.')
      return
    }

    setOverviewSummaryGenerating(true)
    setOverviewSummaryError(null)
    try {
      const headers: Record<string, string> = {}
      if (operatorToken.trim()) headers['X-Operator-Token'] = operatorToken.trim()
      const response = await fetch(`${baseUrl}/api/overview-summary/trigger`, {
        method: 'POST',
        headers,
      })
      if (!response.ok) {
        if (response.status === 401) {
          throw new Error('Operator token required. Add it in Settings, then try again.')
        }
        const text = await response.text().catch(() => '')
        let detail = ''
        if (text) {
          try {
            const parsed = JSON.parse(text) as { detail?: unknown }
            detail = typeof parsed.detail === 'string' ? parsed.detail : ''
          } catch {
            detail = ''
          }
        }
        throw new Error(detail || text || 'Summary generation failed')
      }
      const nextSummary = (await response.json()) as OverviewSummaryStatus
      setOverviewSummary(nextSummary)
      setManualRefreshKey((k) => k + 1)
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Summary generation failed'
      setOverviewSummaryError(message)
      throw error
    } finally {
      setOverviewSummaryGenerating(false)
    }
  }, [backendUrl, operatorToken])

  const handleOperatorToken = useCallback((value: string) => {
    setOperatorToken(value)
    const trimmed = value.trim()
    if (trimmed) {
      safeSessionStorage.setItem('hanas_operator_token', trimmed)
      safeLocalStorage.removeItem('hanas_operator_token')
    } else {
      safeSessionStorage.removeItem('hanas_operator_token')
    }
  }, [])

  const header = useMemo(
    () => headerForPage(activePage),
    [activePage],
  )
  const maintenanceMode = Boolean(systemSettings?.maintenance_mode_enabled)
  const monitoringMode = Boolean(systemSettings?.monitoring_mode_enabled)
  const emergencyStop = Boolean(systemSettings?.emergency_stop_enabled)
  const displayHeader = useMemo(
    () => emergencyStop && activePage === 'Overview'
      ? { ...header, eyebrow: 'Emergency stop' }
      : maintenanceMode && activePage === 'Overview'
        ? { ...header, eyebrow: 'Maintenance active' }
      : monitoringMode && activePage === 'Overview'
        ? { ...header, eyebrow: 'Live monitoring' }
      : header,
    [activePage, emergencyStop, header, maintenanceMode, monitoringMode],
  )
  const headerPills = useMemo(
    () => {
      if (emergencyStop) {
        const suppressedDuringEmergency = new Set(['monitoring', 'ready', 'dosing'])
        return displayHeader.pills.filter((pill) => !suppressedDuringEmergency.has(pill.label.toLowerCase()))
      }
      if (!maintenanceMode && !monitoringMode) return displayHeader.pills
      const suppressedDuringMaintenance = new Set(['monitoring', 'ready'])
      return displayHeader.pills.filter((pill) => !suppressedDuringMaintenance.has(pill.label.toLowerCase()))
    },
    [displayHeader.pills, emergencyStop, maintenanceMode, monitoringMode],
  )
  const isEmptyAndNotSettings = connectionState === 'empty' && activePage !== 'Settings'
  const isConnectingAndNotSettings = connectionState === 'connecting' && activePage !== 'Settings'
  const reservoirMaxLiters = systemSettings
    ? systemSettings.force_fixed_reservoir_volume
      ? systemSettings.fixed_reservoir_volume_liters
      : systemSettings.default_reservoir_max_volume_liters
    : 70
  const phTarget = {
    min: systemSettings?.ph_target_min ?? PH_TARGET.min,
    max: systemSettings?.ph_target_max ?? PH_TARGET.max,
  }
  const ecTarget = {
    min: systemSettings?.ec_target_min ?? EC_TARGET.min,
    max: systemSettings?.ec_target_max ?? EC_TARGET.max,
  }

  // Persist the selected theme across sessions.
  const [darkMode, setDarkMode] = useState(() => safeLocalStorage.getItem('hanas-dark') === '1')

  useEffect(() => {
    document.documentElement.classList.toggle('dark', darkMode)
    safeLocalStorage.setItem('hanas-dark', darkMode ? '1' : '0')
  }, [darkMode])

  const [pipelineOpen, setPipelineOpen] = useState(false)
  const [pipelineCompact, setPipelineCompact] = useState(() =>
    typeof window !== 'undefined' ? window.matchMedia('(max-width: 1320px)').matches : false,
  )
  const pipelineDrawerRef = useRef<HTMLElement>(null)
  const showPipeline = latestLog.control_strategy === 'agentic_ai'

  useEffect(() => {
    if (typeof window === 'undefined') return
    const media = window.matchMedia('(max-width: 1320px)')

    function syncPipelineMode(event?: MediaQueryListEvent) {
      const compact = event ? event.matches : media.matches
      setPipelineCompact(compact)
      if (!compact) setPipelineOpen(activePage === 'AI Reasoning')
    }

    syncPipelineMode()

    media.addEventListener('change', syncPipelineMode)
    return () => media.removeEventListener('change', syncPipelineMode)
  }, [activePage])

  // Refresh pipeline progress while its status is visible.
  useEffect(() => {
    if (!showPipeline || connectionState !== 'connected') {
      return
    }
    const baseUrl = backendUrl.trim().replace(/\/$/, '')
    if (!baseUrl) return
    const poll = async () => {
      try {
        const res = await fetch(`${baseUrl}/api/pipeline/progress`)
        if (res.ok) {
          const data = (await res.json()) as { stage: string | null; updated_at: string | null; stale?: boolean }
          // Ignore active stages that have not been refreshed within the backend timeout.
          if (data.stage && data.updated_at) {
            const ageMs = Date.now() - new Date(data.updated_at).getTime()
            setPipelineStage(!data.stale && ageMs < 60_000 ? data.stage : null)
          } else {
            setPipelineStage(null)
          }
        }
      } catch {
        setPipelineStage(null)
      }
    }
    poll()
    const timer = window.setInterval(poll, 1000)
    return () => window.clearInterval(timer)
  }, [showPipeline, connectionState, backendUrl])
  const visiblePipelineStage = showPipeline && connectionState === 'connected' ? pipelineStage : null
  const pipelineActivityLive = visiblePipelineStage !== null
    || latestLog.status === 'processing'
  const pipelineNeedsAttention = !pipelineOpen
    && (activePage === 'AI Reasoning' || pipelineActivityLive)

  useEffect(() => {
    if (!pipelineCompact || activePage !== 'AI Reasoning' || visiblePipelineStage !== 'human_review_gate') return
    const frame = window.requestAnimationFrame(() => setPipelineOpen(false))
    return () => window.cancelAnimationFrame(frame)
  }, [activePage, pipelineCompact, visiblePipelineStage])

  const operationCycle = useMemo(
    () => activeOperationCycle(cycle, batchStatus),
    [batchStatus, cycle],
  )

  useEffect(() => {
    if (!pipelineOpen || !pipelineCompact) return

    function closeOnOutsidePress(event: PointerEvent) {
      const target = event.target
      if (!(target instanceof Element)) return
      if (pipelineDrawerRef.current?.contains(target)) return
      if (target.closest('.topbar-pipeline-btn')) return
      setPipelineOpen(false)
    }

    document.addEventListener('pointerdown', closeOnOutsidePress)
    return () => document.removeEventListener('pointerdown', closeOnOutsidePress)
  }, [pipelineCompact, pipelineOpen])

  useEffect(() => {
    if (!pipelineOpen) return

    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === 'Escape') setPipelineOpen(false)
    }

    document.addEventListener('keydown', closeOnEscape)
    if (pipelineCompact) pipelineDrawerRef.current?.focus()
    return () => document.removeEventListener('keydown', closeOnEscape)
  }, [pipelineCompact, pipelineOpen])

  useEffect(() => {
    if (typeof window === 'undefined') return
    const media = window.matchMedia('(max-width: 1320px)')

    function syncScrollLock() {
      const shouldLock = showPipeline && pipelineOpen && media.matches
      document.documentElement.classList.toggle('pipeline-scroll-lock', shouldLock)
      document.body.classList.toggle('pipeline-scroll-lock', shouldLock)
    }

    syncScrollLock()
    media.addEventListener('change', syncScrollLock)
    return () => {
      media.removeEventListener('change', syncScrollLock)
      document.documentElement.classList.remove('pipeline-scroll-lock')
      document.body.classList.remove('pipeline-scroll-lock')
    }
  }, [showPipeline, pipelineOpen])

  const handleNavigate = useCallback((page: Page, settingsTarget: 'system' | null = null) => {
    setSettingsSectionTarget(page === 'Settings' ? settingsTarget : null)
    setActivePage(page)
    const canUseWindow = typeof window !== 'undefined'
    if (canUseWindow) {
      if (page === 'Settings' && settingsTarget === 'system') {
        window.requestAnimationFrame(() => {
          window.requestAnimationFrame(() => {
            document.getElementById('settings-system')?.scrollIntoView({ behavior: 'auto', block: 'start' })
          })
        })
      } else {
        window.scrollTo({ top: 0, behavior: 'auto' })
      }
    }
    if (page === 'AI Reasoning' && latestLog.control_strategy === 'agentic_ai') {
      setPipelineOpen(true)
    }
    if (page === 'Overview') {
      setPipelineOpen(false)
    }
  }, [latestLog.control_strategy])

  const handleMenuNavigate = useCallback((page: Page) => {
    handleNavigate(page)
  }, [handleNavigate])

  const handlePipelineToggle = useCallback(() => {
    setPipelineOpen((open) => !open)
  }, [])

  const handleReviewSystemSettings = useCallback(() => {
    handleNavigate('Settings', 'system')
  }, [handleNavigate])

  const pageContent = (
    <>
      {emergencyStop && activePage !== 'Settings' && (
        <SystemModeBanner emergency onSettings={handleReviewSystemSettings} />
      )}

      {maintenanceMode && !emergencyStop && activePage !== 'Settings' && (
        <SystemModeBanner onSettings={handleReviewSystemSettings} />
      )}

      {monitoringMode && !maintenanceMode && !emergencyStop && activePage !== 'Settings' && (
        <SystemModeBanner monitoring onSettings={handleReviewSystemSettings} />
      )}

      {connectionState !== 'offline' && (
        <ActiveOperationBar
          cycle={operationCycle}
          latestLog={latestLog}
          reservoirMaxLiters={reservoirMaxLiters}
          activePage={activePage}
          onNavigate={handleMenuNavigate}
        />
      )}

      {isConnectingAndNotSettings && <EmptyState connecting />}
      {connectionState === 'offline' && activePage !== 'Settings' && (
        <EmptyState
          offline
          onRetry={() => {
            failedConnectionAttemptsRef.current = 0
            setConnectionState('connecting')
            setManualRefreshKey((key) => key + 1)
          }}
          onSettings={() => handleNavigate('Settings')}
        />
      )}
      {isEmptyAndNotSettings && <EmptyState maintenanceMode={maintenanceMode} emergencyStop={emergencyStop} />}

      {!isEmptyAndNotSettings && !isConnectingAndNotSettings && connectionState !== 'offline' && (
        <>
          {activePage === 'Overview' && (
            <OverviewPage
              latestLog={latestLog}
              cycle={cycle}
              history={history}
              reservoirMaxLiters={reservoirMaxLiters}
              phTarget={phTarget}
              ecTarget={ecTarget}
              onNavigate={handleNavigate}
              currentStage={visiblePipelineStage}
              batchStatus={batchStatus}
              overviewSummary={overviewSummary}
              onGenerateOverviewSummary={handleGenerateOverviewSummary}
              overviewSummaryGenerating={overviewSummaryGenerating}
              overviewSummaryError={overviewSummaryError}
              systemSettings={systemSettings}
            />
          )}
          {activePage === 'Reservoir' && (
            <ReservoirPage
              latestLog={latestLog}
              reservoirMaxLiters={reservoirMaxLiters}
              phTarget={phTarget}
              ecTarget={ecTarget}
              onNavigate={handleNavigate}
            />
          )}
          {activePage === 'Dosing' && (
            <DosingPage
              cycle={operationCycle}
              latestLog={latestLog}
              history={history}
              onHumanReview={handleHumanReview}
              reservoirMaxLiters={reservoirMaxLiters}
              phTarget={phTarget}
              ecTarget={ecTarget}
            />
          )}
          {activePage === 'Trends' && (
            <TrendsPage
              latestLog={latestLog}
              history={history}
              connectionState={connectionState}
              backendUrl={backendUrl}
              systemSettings={systemSettings}
              phTarget={phTarget}
              ecTarget={ecTarget}
            />
          )}
          {activePage === 'Camera' && (
            <CameraPage
              onNavigate={handleNavigate}
            />
          )}
          {activePage === 'AI Reasoning' && (
            <AIReasoningPage
              latestLog={latestLog}
              cycle={cycle}
              batchStatus={batchStatus}
              currentStage={visiblePipelineStage}
              phTarget={phTarget}
              ecTarget={ecTarget}
            />
          )}
          {activePage === 'Logs & Alerts' && (
            <LogsAlertsPage
              latestLog={latestLog}
              cycle={cycle}
              history={history}
              connectionState={connectionState}
              notificationLogs={notificationLogs}
              backendUrl={backendUrl}
            />
          )}
          {activePage === 'Help' && <HelpPage />}
          {activePage === 'Settings' && (
            <SettingsPage
              backendUrl={backendUrl}
              refreshSeconds={refreshSeconds}
              latestLog={latestLog}
              connectionState={connectionState}
              connectionMessage={connectionMessage}
              systemSettings={systemSettings}
              batchStatus={batchStatus}
              reservoirMaxLiters={reservoirMaxLiters}
              darkMode={darkMode}
              onBackendUrl={handleBackendUrlChange}
              operatorToken={operatorToken}
              onOperatorToken={handleOperatorToken}
              onRefreshSeconds={handleRefreshSeconds}
              onUpdateSettings={handleUpdateSettings}
              onToggleDarkMode={() => setDarkMode((v) => !v)}
              phTarget={phTarget}
              ecTarget={ecTarget}
              sectionTarget={settingsSectionTarget}
            />
          )}
        </>
      )}
    </>
  )

  return (
    <div className="app-shell">
      <Sidebar
        activePage={activePage}
        connectionState={connectionState}
        connectionMessage={connectionMessage}
        maintenanceMode={maintenanceMode}
        monitoringMode={monitoringMode}
        emergencyStop={emergencyStop}
        fullAgenticMode={Boolean(systemSettings?.full_agentic_mode_enabled)}
        onNavigate={handleMenuNavigate}
        mobileActions={(
          <HeaderQuickActions
            lastUpdated={lastUpdated}
            notificationLogs={notificationLogs}
            onNavigate={handleNavigate}
            pipelineAction={showPipeline ? {
              isOpen: pipelineOpen,
              isLive: pipelineActivityLive,
              shouldNudge: pipelineNeedsAttention,
              onToggle: handlePipelineToggle,
            } : undefined}
            className="mobile-header-quick-actions"
          />
        )}
      />

      {showPipeline ? (
        <div className={`main-wrapper ${pipelineOpen ? 'pipeline-open' : 'pipeline-closed'}`}>
          <main className="main-content">
            <Topbar
              eyebrow={displayHeader.eyebrow}
              title={displayHeader.title}
              pills={headerPills}
              lastUpdated={lastUpdated}
              connectionState={connectionState}
              notificationLogs={notificationLogs}
              onNavigate={handleNavigate}
              pipelineAction={{
                isOpen: pipelineOpen,
                isLive: pipelineActivityLive,
                shouldNudge: pipelineNeedsAttention,
                onToggle: handlePipelineToggle,
              }}
            />
            <Suspense fallback={<PageLoading />}>{pageContent}</Suspense>
            <AppFooter />
          </main>

          <button
            type="button"
            className="pipeline-backdrop"
            aria-label="Close AI Pipeline"
            onClick={() => setPipelineOpen(false)}
          />
          <aside
            className="pipeline-drawer"
            ref={pipelineDrawerRef}
            role={pipelineCompact ? 'dialog' : 'complementary'}
            aria-modal={pipelineCompact ? true : undefined}
            aria-label="AI Pipeline"
            aria-hidden={!pipelineOpen}
            inert={!pipelineOpen}
            tabIndex={-1}
          >
            <div className="pipeline-drawer-content">
              <AgentPipeline
          latestLog={latestLog}
                cycle={cycle}
                batchStatus={batchStatus}
                defaultExpanded
                currentStage={visiblePipelineStage}
                onClose={() => setPipelineOpen(false)}
              />
            </div>
          </aside>
        </div>
      ) : (
        <main className="main-content">
          <Topbar
            eyebrow={displayHeader.eyebrow}
            title={displayHeader.title}
            pills={headerPills}
            lastUpdated={lastUpdated}
            connectionState={connectionState}
            notificationLogs={notificationLogs}
            onNavigate={handleNavigate}
          />
          <Suspense fallback={<PageLoading />}>{pageContent}</Suspense>
          <AppFooter />
        </main>
      )}

      <MobileNav activePage={activePage} onNavigate={handleMenuNavigate} />
      {systemSettings?.experiment_preflight_required
        && systemSettings.full_agentic_mode_enabled
        && !systemSettings.monitoring_mode_enabled
        && !systemSettings.maintenance_mode_enabled
        && !systemSettings.emergency_stop_enabled
        && (
        <ExperimentPreflightGate
          settings={systemSettings}
          loading={experimentPreflightLoading}
          error={experimentPreflightError}
          operatorToken={operatorToken}
          onOperatorToken={handleOperatorToken}
          onChoose={handleExperimentPreflight}
        />
        )}
    </div>
  )
}

function App() {
  const params = typeof window !== 'undefined' ? new URLSearchParams(window.location.search) : null
  const requestedPage = params?.get('page')?.trim().toLowerCase() ?? ''
  return <DashboardApp initialPage={PAGE_SLUGS[requestedPage] ?? 'Overview'} />
}

export default App
