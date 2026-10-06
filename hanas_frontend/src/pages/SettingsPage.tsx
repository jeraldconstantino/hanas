import { useEffect, useLayoutEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Bot, CalendarDays, Camera, ChevronDown, ChevronLeft, ChevronRight, Clock, Moon, ShieldCheck, ShieldAlert, Sprout, Sun } from 'lucide-react'
import type { LatestLog, ConnectionState, SystemSettings, BatchStatus, RuntimeSettingsUpdate } from '../types'
import { toneForConnection, formatCountdown, formatDoseMl, formatDuration, formatPumpDuration, formatTime, cropLifecycleFromSettings } from '../utils'
import { CAMERA_STREAM_URL } from '../constants'
import { cameraHostLabel, cleanCameraUrl, getSavedCameraUrl, isLikelyTailscaleUrl, isValidCameraUrl, saveCameraUrl } from '../camera'
import { Pill } from '../components/ui/Pill'
import { Panel } from '../components/ui/Panel'
import { DataTable } from '../components/ui/DataTable'
import { formatDisplayText, formatStrategyLabel, shouldShowBatchRunStatus } from '../text'
import { formatVersionLabel } from '../format'
import { MobileSectionPicker } from '../components/ui/MobileSectionPicker'

function SettingsDialogPortal({ children }: { children: ReactNode }) {
  return createPortal(children, document.body)
}

function InfoTile({ label, value, note }: { label: string; value: string; note: string }) {
  return (
    <div className="info-tile">
      <span>{label}</span>
      <strong>{value}</strong>
      <p>{note}</p>
    </div>
  )
}

function SettingsDropdown({
  label,
  value,
  options,
  onChange,
}: {
  label: string
  value: number
  options: Array<{ value: number; label: string }>
  onChange: (value: number) => void
}) {
  const [open, setOpen] = useState(false)
  const selected = options.find((option) => option.value === value) ?? options[0]

  return (
    <div className="settings-dropdown" onBlur={(event) => {
      if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false)
    }}>
      <button
        type="button"
        className="settings-dropdown-trigger"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={label}
        onClick={() => setOpen((current) => !current)}
      >
        <span>{selected.label}</span>
        <ChevronDown size={16} strokeWidth={2.3} aria-hidden="true" />
      </button>
      {open && (
        <div className="settings-dropdown-menu" role="listbox" aria-label={label}>
          {options.map((option) => (
            <button
              key={option.value}
              type="button"
              role="option"
              aria-selected={option.value === value}
              className={option.value === value ? 'selected' : ''}
              onClick={() => {
                onChange(option.value)
                setOpen(false)
              }}
            >
              {option.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

const SETTINGS_SECTIONS = [
  { id: 'connection', label: 'Connection' },
  { id: 'camera', label: 'Camera' },
  { id: 'crop', label: 'Crop' },
  { id: 'pumps', label: 'Pumps' },
  { id: 'display', label: 'Display' },
  { id: 'system', label: 'System' },
  { id: 'safety', label: 'Safety' },
] as const

const SETTINGS_SECTION_OPTIONS = SETTINGS_SECTIONS.map((section) => ({
  ...section,
  id: `settings-${section.id}`,
}))

function SettingsSectionNav() {
  return (
    <>
      <MobileSectionPicker label="Choose settings section" sections={SETTINGS_SECTION_OPTIONS} />
      <nav className="settings-section-nav" aria-label="Settings sections">
        {SETTINGS_SECTIONS.map((section) => (
          <a key={section.id} href={`#settings-${section.id}`}>
            {section.label}
          </a>
        ))}
      </nav>
    </>
  )
}

function buildIdentityParts(settings: SystemSettings): string[] {
  const normalizedEnvironment = settings.app_env.trim().toLowerCase()
  const environmentLabel = normalizedEnvironment === 'prod' || normalizedEnvironment === 'production'
    ? 'Production'
    : normalizedEnvironment === 'dev' || normalizedEnvironment === 'development'
      ? 'Development'
      : formatDisplayText(settings.app_env)
  return [environmentLabel]
}

function rangeInsight(
  label: 'pH' | 'EC',
  current: number,
  target: { min: number; max: number },
  unit: string
) {
  const center = (target.min + target.max) / 2
  const delta = current - center
  const isOnTarget = current >= target.min && current <= target.max
  const position = current < target.min ? 'Below target' : current > target.max ? 'Above target' : 'Within range'
  const distance = Math.abs(delta)
  const displayUnit = label === 'pH' ? 'pH' : unit
  const formattedDistance = `${distance.toFixed(2)}${displayUnit ? ` ${displayUnit}` : ''}`
  return {
    label,
    position,
    current: `${current.toFixed(2)}${displayUnit ? ` ${displayUnit}` : ''}`,
    window: `${target.min}-${target.max}${displayUnit ? ` ${displayUnit}` : ''}`,
    delta: isOnTarget ? `${formattedDistance} from center` : `${formattedDistance} ${delta < 0 ? 'below' : 'above'} center`,
    tone: isOnTarget ? 'good' : 'warn',
  } as const
}

function RangeInsightCard({
  insight,
}: {
  insight: ReturnType<typeof rangeInsight>
}) {
  return (
    <div className={`range-insight-card ${insight.tone}`}>
      <div className="range-insight-card-header">
        <span>{insight.label}</span>
        <Pill label={insight.position} tone={insight.tone} />
      </div>
      <dl>
        <div>
          <dt>Current</dt>
          <dd>{insight.current}</dd>
        </div>
        <div>
          <dt>Target</dt>
          <dd>{insight.window}</dd>
        </div>
        <div>
          <dt>Offset</dt>
          <dd>{insight.delta}</dd>
        </div>
      </dl>
    </div>
  )
}

function dateValueFromLocalDate(date: Date): string {
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

function parseDateValue(value: string): Date | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
  if (!match) return null
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]))
  if (
    date.getFullYear() !== Number(match[1]) ||
    date.getMonth() !== Number(match[2]) - 1 ||
    date.getDate() !== Number(match[3])
  ) {
    return null
  }
  return date
}

function formatCropDate(value: string): string {
  const parsed = parseDateValue(value)
  if (!parsed) return 'Select date'
  return parsed.toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  })
}

function CropDatePicker({
  value,
  onChange,
  invalid,
}: {
  value: string
  onChange: (value: string) => void
  invalid: boolean
}) {
  const selectedDate = parseDateValue(value)
  const pickerRef = useRef<HTMLDivElement | null>(null)
  const [open, setOpen] = useState(false)
  const [visibleMonth, setVisibleMonth] = useState(() => {
    const base = selectedDate ?? new Date()
    return new Date(base.getFullYear(), base.getMonth(), 1)
  })
  const todayValue = dateValueFromLocalDate(new Date())
  const monthLabel = visibleMonth.toLocaleDateString(undefined, { month: 'long', year: 'numeric' })
  const firstDay = visibleMonth.getDay()
  const daysInMonth = new Date(visibleMonth.getFullYear(), visibleMonth.getMonth() + 1, 0).getDate()
  const previousMonthDays = new Date(visibleMonth.getFullYear(), visibleMonth.getMonth(), 0).getDate()
  const cells = Array.from({ length: 42 }, (_, index) => {
    const monthOffset = index < firstDay ? -1 : index >= firstDay + daysInMonth ? 1 : 0
    const day = monthOffset === -1
      ? previousMonthDays - firstDay + index + 1
      : monthOffset === 1
        ? index - firstDay - daysInMonth + 1
        : index - firstDay + 1
    const cellDate = new Date(visibleMonth.getFullYear(), visibleMonth.getMonth() + monthOffset, day)
    return {
      date: cellDate,
      value: dateValueFromLocalDate(cellDate),
      day,
      muted: monthOffset !== 0,
      future: dateValueFromLocalDate(cellDate) > todayValue,
    }
  })

  useEffect(() => {
    if (!open) return

    function handlePointerDown(event: PointerEvent) {
      const target = event.target
      if (target instanceof Node && pickerRef.current?.contains(target)) return
      setOpen(false)
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') setOpen(false)
    }

    document.addEventListener('pointerdown', handlePointerDown)
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('pointerdown', handlePointerDown)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [open])

  function moveMonth(delta: number) {
    setVisibleMonth((current) => new Date(current.getFullYear(), current.getMonth() + delta, 1))
  }

  function chooseDate(nextValue: string) {
    onChange(nextValue)
    setOpen(false)
  }

  function chooseToday() {
    const today = new Date()
    setVisibleMonth(new Date(today.getFullYear(), today.getMonth(), 1))
    chooseDate(todayValue)
  }

  return (
    <div className="crop-date-picker" ref={pickerRef}>
      <button
        type="button"
        className={`crop-date-trigger ${invalid ? 'invalid' : ''}`}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => {
          if (!open && selectedDate) {
            setVisibleMonth(new Date(selectedDate.getFullYear(), selectedDate.getMonth(), 1))
          }
          setOpen((current) => !current)
        }}
      >
        <span>{formatCropDate(value)}</span>
        <CalendarDays size={16} strokeWidth={2.2} aria-hidden="true" />
      </button>
      {open && (
        <div className="crop-date-popover" role="dialog" aria-label="Choose transplant date">
          <div className="crop-date-popover-head">
            <button type="button" aria-label="Previous month" onClick={() => moveMonth(-1)}>
              <ChevronLeft size={16} strokeWidth={2.3} />
            </button>
            <strong>{monthLabel}</strong>
            <button type="button" aria-label="Next month" onClick={() => moveMonth(1)}>
              <ChevronRight size={16} strokeWidth={2.3} />
            </button>
          </div>
          <div className="crop-date-weekdays" aria-hidden="true">
            {['Su', 'Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa'].map((day) => (
              <span key={day}>{day}</span>
            ))}
          </div>
          <div className="crop-date-grid">
            {cells.map((cell) => (
              <button
                type="button"
                key={cell.value}
                className={`${cell.muted ? 'muted' : ''} ${cell.future ? 'future' : ''} ${cell.value === value ? 'selected' : ''} ${cell.value === todayValue ? 'today' : ''}`}
                disabled={cell.future}
                aria-disabled={cell.future}
                onClick={() => chooseDate(cell.value)}
              >
                {cell.day}
              </button>
            ))}
          </div>
          <div className="crop-date-popover-actions">
            <button
              type="button"
              onClick={() => {
                onChange('')
                setOpen(false)
              }}
            >
              Clear
            </button>
            <button type="button" onClick={chooseToday}>
              Today
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

function CropLifecycleSettings({
  settings,
  onUpdateSettings,
}: {
  settings: SystemSettings | null
  onUpdateSettings: (updates: RuntimeSettingsUpdate) => Promise<void>
}) {
  const lifecycle = cropLifecycleFromSettings(settings)
  const [variety, setVariety] = useState(settings?.crop_variety ?? '')
  const [transplantDate, setTransplantDate] = useState(settings?.crop_transplant_date ?? '')
  const [harvestStartDay, setHarvestStartDay] = useState(settings?.crop_harvest_start_day ?? 30)
  const [harvestEndDay, setHarvestEndDay] = useState(settings?.crop_harvest_end_day ?? 35)
  const [saving, setSaving] = useState(false)
  const [resetting, setResetting] = useState(false)
  const [confirmResetOpen, setConfirmResetOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const dateRequiredError = 'Select a transplant date before saving the crop lifecycle.'

  async function submitLifecycle(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError(null)
    setSaved(false)
    if (!transplantDate) {
      setError(dateRequiredError)
      return
    }
    setSaving(true)
    try {
      await onUpdateSettings({
        crop_variety: variety.trim(),
        crop_transplant_date: transplantDate,
        crop_harvest_start_day: harvestStartDay,
        crop_harvest_end_day: harvestEndDay,
      })
      setSaved(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Crop lifecycle update failed.')
    } finally {
      setSaving(false)
    }
  }

  async function resetLifecycle() {
    setResetting(true)
    setError(null)
    setSaved(false)
    try {
      await onUpdateSettings({
        crop_variety: '',
        crop_transplant_date: null,
        crop_harvest_start_day: settings?.crop_harvest_start_day ?? harvestStartDay,
        crop_harvest_end_day: settings?.crop_harvest_end_day ?? harvestEndDay,
      })
      setVariety('')
      setTransplantDate('')
      setConfirmResetOpen(false)
      setSaved(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Crop lifecycle reset failed.')
    } finally {
      setResetting(false)
    }
  }

  return (
    <div className="crop-lifecycle-settings">
      <div className="crop-lifecycle-settings-summary">
        <div className="crop-lifecycle-settings-icon">
          <Sprout size={20} strokeWidth={2.2} />
        </div>
        <div>
          <span>Crop lifecycle</span>
          <strong>{lifecycle.configured ? `Day ${lifecycle.ageDays}: ${lifecycle.stageLabel}` : 'Transplant date not set'}</strong>
          <p>
            {lifecycle.configured
              ? `${lifecycle.cropVariety || lifecycle.cropType} is tracked against a day ${lifecycle.harvestStartDay}-${lifecycle.harvestEndDay} harvest window.`
              : 'Set the transplant date so the dashboard and Agentic AI context know crop age.'}
          </p>
        </div>
      </div>
      <form className="crop-lifecycle-form" onSubmit={submitLifecycle} noValidate>
        <label>
          <span>Variety / batch label</span>
          <input
            value={variety}
            onChange={(event) => setVariety(event.target.value)}
            placeholder={settings?.crop_type ?? 'Lettuce'}
          />
        </label>
        <label>
          <span>Transplant date</span>
          <CropDatePicker
            value={transplantDate}
            invalid={!transplantDate && error === dateRequiredError}
            onChange={(nextValue) => {
              setTransplantDate(nextValue)
              setSaved(false)
              if (nextValue && error === dateRequiredError) setError(null)
            }}
          />
        </label>
        <label>
          <span>Harvest starts</span>
          <input
            type="number"
            min={1}
            max={120}
            value={harvestStartDay}
            onChange={(event) => setHarvestStartDay(Number(event.target.value))}
          />
        </label>
        <label>
          <span>Harvest ends</span>
          <input
            type="number"
            min={harvestStartDay}
            max={160}
            value={harvestEndDay}
            onChange={(event) => setHarvestEndDay(Number(event.target.value))}
          />
        </label>
        <div className="crop-lifecycle-form-actions">
          <button type="submit" disabled={saving || resetting || harvestEndDay < harvestStartDay}>
            <CalendarDays size={15} strokeWidth={2.2} />
            {saving ? 'Saving' : 'Save lifecycle'}
          </button>
          {lifecycle.configured && (
            <button
              type="button"
              className="crop-lifecycle-reset-button"
              disabled={saving || resetting}
              onClick={() => {
                setError(null)
                setConfirmResetOpen(true)
              }}
            >
              End lifecycle
            </button>
          )}
          {saved && <span>Saved</span>}
          {error && <p role="alert">{error}</p>}
          <small className="crop-lifecycle-form-note">
            Default harvest window: day 30-35 after transplant.
          </small>
        </div>
      </form>
      {confirmResetOpen && (
        <SettingsDialogPortal>
          <div
            className="settings-confirm-overlay"
            role="presentation"
            onClick={() => {
              if (!resetting) setConfirmResetOpen(false)
            }}
          >
            <div
              className="settings-confirm-dialog destructive"
              role="dialog"
              aria-modal="true"
              aria-labelledby="crop-lifecycle-reset-title"
              onClick={(event) => event.stopPropagation()}
            >
              <div className="settings-confirm-icon danger" aria-hidden="true">
                <ShieldAlert size={22} strokeWidth={2.3} />
              </div>
              <div className="settings-confirm-copy">
                <span>Operator confirmation</span>
                <h3 id="crop-lifecycle-reset-title">End this crop lifecycle?</h3>
                <p>
                  Clears the transplant date and batch label. Crop-age context stays off until you save a new lifecycle.
                </p>
                {error && (
                  <div className="settings-confirm-error" role="alert">
                    <strong>Reset failed</strong>
                    <span>{error}</span>
                  </div>
                )}
              </div>
              <div className="settings-confirm-actions">
                <button
                  type="button"
                  className="secondary"
                  disabled={resetting}
                  onClick={() => setConfirmResetOpen(false)}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  className="danger"
                  disabled={resetting}
                  onClick={resetLifecycle}
                >
                  {resetting ? 'Ending...' : 'End lifecycle'}
                </button>
              </div>
            </div>
          </div>
        </SettingsDialogPortal>
      )}
    </div>
  )
}

function formatBatchSource(source: string | null | undefined, mode?: string | null) {
  if (source === 'sensor_full_agentic' || mode === 'per_reading') return 'Per-reading full-agentic run'
  if (source === 'manual_batch_trigger') return 'Manual trigger'
  if (source === 'batch_scheduler') return mode === 'manual' ? 'Manual trigger' : 'Scheduled run'
  return 'No batch run yet'
}

function batchRunStatusTone(status: string) {
  const normalized = status.trim().toLowerCase()
  if (['failed', 'error', 'expired', 'blocked', 'rejected'].includes(normalized)) return 'danger'
  if (['pending', 'queued', 'dosing', 'mixing', 'dispatched'].includes(normalized)) return 'warn'
  if (['within_range', 'no_action'].includes(normalized)) return 'good'
  return 'neutral'
}

function BatchGuardPanel({ batchStatus }: { batchStatus: BatchStatus | null }) {
  const unresolved = batchStatus?.batch_unresolved_command ?? null
  const physicalBlocker = batchStatus?.batch_recent_or_active_pump_command ?? null
  const latest = batchStatus?.batch_latest_analysis ?? null
  const schedulerRunning = Boolean(batchStatus?.batch_scheduler_running)
  const blocked = Boolean(
    batchStatus?.batch_has_unresolved_command ||
    batchStatus?.batch_has_recent_or_active_pump_command ||
    (batchStatus?.batch_analysis_enabled && !schedulerRunning)
  )
  const interval = batchStatus ? Math.round(batchStatus.batch_analysis_interval_seconds / 60) : null
  const expiry = batchStatus ? Math.round(batchStatus.batch_pending_command_expiry_seconds / 60) : null
  const physicalWindow = batchStatus ? Math.round(batchStatus.batch_physical_guard_window_seconds / 60) : null
  const fullAgenticMode = Boolean(batchStatus?.full_agentic_mode_enabled)

  return (
    <Panel title="Phase 3 batch guard" eyebrow="Production safety">
      <div className={`batch-guard-card ${blocked ? 'blocked' : 'clear'}`}>
        <div className="batch-guard-icon">
          {blocked ? <ShieldAlert size={24} strokeWidth={2.2} /> : <ShieldCheck size={24} strokeWidth={2.2} />}
        </div>
        <div className="batch-guard-main">
          <strong>
            {batchStatus?.batch_analysis_enabled && !schedulerRunning
              ? 'Batch scheduler is not running'
              : blocked ? 'Batch dosing is blocked' : 'Batch dosing is clear'}
          </strong>
          <p>
            {batchStatus?.batch_analysis_enabled && !schedulerRunning
              ? `Phase 3 batch mode is enabled in config, but this backend process has not started the ${batchStatus ? Math.round(batchStatus.batch_analysis_interval_seconds / 60) : 10}-minute scheduler loop.`
              : blocked
              ? physicalBlocker
                ? 'The scheduler remains active, but HANAS will skip dosing while the physical pump window is still settling.'
                : 'The scheduler remains active, but HANAS will not queue another batch dose until the unresolved command is completed, rejected, or expired.'
              : 'No unresolved or recently active pump command is blocking the next scheduled agentic evaluation.'}
          </p>
        </div>
        <Pill label={batchStatus?.batch_analysis_enabled ? 'Enabled' : 'Disabled'} tone={batchStatus?.batch_analysis_enabled ? 'good' : 'neutral'} />
      </div>

      <div className="batch-guard-grid">
        <InfoTile
          label="Batch interval"
          value={interval != null ? `${interval} min` : '—'}
          note="Agentic pipeline schedule"
        />
        <InfoTile
          label="Next safety check"
          value={schedulerRunning ? formatCountdown(batchStatus?.batch_scheduler_next_run_seconds) : 'Not running'}
          note={batchStatus?.batch_scheduler_next_run_at ? formatTime(batchStatus.batch_scheduler_next_run_at) : 'Scheduler timer'}
        />
        <InfoTile
          label="History window"
          value={batchStatus ? `${batchStatus.batch_analysis_window_readings} readings` : '—'}
          note="Physical readings used per batch"
        />
        <InfoTile
          label="LLM key"
          value={batchStatus?.openai_api_key_configured ? 'Configured' : 'Missing'}
          note={batchStatus?.agentic_ai_model ?? 'Agent model'}
        />
        <InfoTile
          label="Command expiry"
          value={expiry != null ? `${expiry} min` : '—'}
          note="Undispatched batch doses expire before late execution"
        />
        <InfoTile
          label="Physical guard"
          value={physicalWindow != null ? `${physicalWindow} min` : '—'}
          note="Batch skips while recent pump action may still be mixing"
        />
        <InfoTile
          label="Last scheduler run"
          value={batchStatus ? formatDisplayText(batchStatus.batch_scheduler_last_status) : '—'}
          note={batchStatus?.batch_scheduler_last_run_finished_at ? formatTime(batchStatus.batch_scheduler_last_run_finished_at) : 'No scheduled run yet'}
        />
      </div>

      {physicalBlocker && (
        <div className="batch-unresolved">
          <div className="batch-unresolved-head">
            <Clock size={16} strokeWidth={2.2} />
            <strong>Current physical blocker</strong>
            <Pill label={formatDisplayText(physicalBlocker.status)} tone="warn" />
          </div>
          <div className="batch-unresolved-grid">
            <span>Cycle #{physicalBlocker.control_cycle_id}</span>
            <span>{formatDisplayText(physicalBlocker.pump_activated)}</span>
            <span>{formatDoseMl(physicalBlocker.dose_ml)}</span>
            <span>{formatPumpDuration(physicalBlocker.duration_ms)}</span>
            <span>{formatDuration(physicalBlocker.age_seconds)} old</span>
            <span>{physicalBlocker.triggered_by ? formatDisplayText(physicalBlocker.triggered_by) : 'Unknown source'}</span>
          </div>
        </div>
      )}

      <div className="batch-run-card">
        <div>
          <span>{fullAgenticMode ? 'Latest full-agentic analysis' : 'Latest batch analysis'}</span>
          <strong>{latest ? formatBatchSource(latest.triggered_by, latest.batch_trigger_mode) : 'No run saved'}</strong>
          <p>
            {latest
              ? fullAgenticMode
                ? 'The current sensor reading was evaluated with recent saved history.'
                : `${latest.batch_window_readings} source readings analyzed from ${latest.batch_candidate_rows_read} candidate rows.`
              : 'Appears after the first scheduled or manual batch run.'}
          </p>
        </div>
        <div className={`batch-run-meta ${latest ? 'has-run' : 'empty'}`}>
          {latest ? (
            <>
              {shouldShowBatchRunStatus(latest.status) && (
                <span className={`batch-run-status ${batchRunStatusTone(latest.status)}`}>
                  {formatDisplayText(latest.status)}
                </span>
              )}
              <div className="batch-run-facts">
                <span>
                  <b>Cycle</b>
                  <strong>#{latest.control_cycle_id}</strong>
                </span>
                <span>
                  <b>Decision</b>
                  <strong>{formatDisplayText(latest.decision)}</strong>
                </span>
                <span>
                  <b>Dose</b>
                  <strong>{formatDoseMl(latest.dose_ml)}</strong>
                </span>
                <span>
                  <b>{latest.status.trim().toLowerCase() === 'completed' ? 'Finished' : 'Recorded'}</b>
                  <strong>{latest.timestamp ? formatTime(latest.timestamp) : 'Not recorded'}</strong>
                  <small>{formatDuration(latest.age_seconds)} ago</small>
                </span>
              </div>
            </>
          ) : (
            <>
              <span className="batch-run-status neutral">Waiting</span>
              <span>First batch run pending</span>
            </>
          )}
        </div>
      </div>

      {unresolved && (
        <div className="batch-unresolved">
          <div className="batch-unresolved-head">
            <Clock size={16} strokeWidth={2.2} />
            <strong>Unresolved command details</strong>
            <Pill label={formatDisplayText(unresolved.status)} tone="warn" />
          </div>
          <div className="batch-unresolved-grid">
            <span>Cycle #{unresolved.control_cycle_id}</span>
            <span>{formatDisplayText(unresolved.pump_activated)}</span>
            <span>{formatDoseMl(unresolved.dose_ml)}</span>
            <span>{formatPumpDuration(unresolved.duration_ms)}</span>
            <span>{formatDuration(unresolved.age_seconds)} old</span>
            <span>{unresolved.timestamp ? formatTime(unresolved.timestamp) : 'No timestamp'}</span>
          </div>
        </div>
      )}
    </Panel>
  )
}

function ToggleSwitch({
  enabled,
  onToggle,
  loading,
  id,
  label,
  tone = 'default',
}: {
  enabled: boolean
  onToggle: () => void
  loading: boolean
  id: string
  label: string
  tone?: 'default' | 'danger' | 'warning'
}) {
  return (
    <button
      id={id}
      type="button"
      role="switch"
      aria-checked={enabled}
      className={`toggle-switch ${enabled ? 'on' : 'off'} ${tone}`}
      onClick={onToggle}
      disabled={loading}
      aria-label={`${enabled ? 'Disable' : 'Enable'} ${label}`}
    >
      <span className="toggle-thumb" />
    </button>
  )
}

function SystemConfigPanel({
  settings,
  onUpdateSettings,
}: {
  settings: SystemSettings
  onUpdateSettings: (updates: RuntimeSettingsUpdate) => Promise<void>
}) {
  const [hitlLoading, setHitlLoading] = useState(false)
  const [smsLoading, setSmsLoading] = useState(false)
  const [maintenanceLoading, setMaintenanceLoading] = useState(false)
  const [monitoringLoading, setMonitoringLoading] = useState(false)
  const [emergencyLoading, setEmergencyLoading] = useState(false)
  const [fullAgenticLoading, setFullAgenticLoading] = useState(false)
  const [confirmMaintenanceOpen, setConfirmMaintenanceOpen] = useState(false)
  const [confirmResumeControlOpen, setConfirmResumeControlOpen] = useState(false)
  const [confirmEmergencyOpen, setConfirmEmergencyOpen] = useState(false)
  const [confirmFullAgenticOpen, setConfirmFullAgenticOpen] = useState(false)
  const [hitlError, setHitlError] = useState<string | null>(null)
  const [smsError, setSmsError] = useState<string | null>(null)
  const [maintenanceError, setMaintenanceError] = useState<string | null>(null)
  const [monitoringError, setMonitoringError] = useState<string | null>(null)
  const [emergencyError, setEmergencyError] = useState<string | null>(null)
  const [fullAgenticError, setFullAgenticError] = useState<string | null>(null)
  const emergencyDialogRef = useRef<HTMLElement>(null)
  const maintenanceDialogRef = useRef<HTMLElement>(null)
  const resumeControlDialogRef = useRef<HTMLElement>(null)
  const fullAgenticDialogRef = useRef<HTMLElement>(null)
  const dialogScrollPositionRef = useRef({ x: 0, y: 0 })
  const agenticFrequency = Math.max(
    1,
    Math.round(settings.batch_analysis_interval_seconds / settings.sensor_sampling_interval_seconds),
  )

  useLayoutEffect(() => {
    const dialog = confirmEmergencyOpen
      ? emergencyDialogRef.current
      : confirmMaintenanceOpen
        ? maintenanceDialogRef.current
        : confirmResumeControlOpen
          ? resumeControlDialogRef.current
        : confirmFullAgenticOpen
          ? fullAgenticDialogRef.current
          : null
    if (!dialog) return
    const activeDialog = dialog

    const previouslyFocused = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null
    document.body.classList.add('settings-dialog-open')
    activeDialog.focus({ preventScroll: true })
    window.scrollTo({
      left: dialogScrollPositionRef.current.x,
      top: dialogScrollPositionRef.current.y,
      behavior: 'auto',
    })

    function preventBackgroundScroll(event: Event) {
      event.preventDefault()
    }

    function handleDialogKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        if (confirmEmergencyOpen && !emergencyLoading) setConfirmEmergencyOpen(false)
        if (confirmMaintenanceOpen && !maintenanceLoading) setConfirmMaintenanceOpen(false)
        if (confirmResumeControlOpen && !monitoringLoading) setConfirmResumeControlOpen(false)
        if (confirmFullAgenticOpen && !fullAgenticLoading) setConfirmFullAgenticOpen(false)
        return
      }
      if (event.key !== 'Tab') return

      const focusable = Array.from(activeDialog.querySelectorAll<HTMLElement>(
        'button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])',
      ))
      if (focusable.length === 0) {
        event.preventDefault()
        activeDialog.focus()
        return
      }
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', handleDialogKeyDown)
    document.addEventListener('wheel', preventBackgroundScroll, { passive: false })
    document.addEventListener('touchmove', preventBackgroundScroll, { passive: false })
    return () => {
      document.removeEventListener('keydown', handleDialogKeyDown)
      document.removeEventListener('wheel', preventBackgroundScroll)
      document.removeEventListener('touchmove', preventBackgroundScroll)
      document.body.classList.remove('settings-dialog-open')
      previouslyFocused?.focus({ preventScroll: true })
    }
  }, [
    confirmEmergencyOpen,
    confirmFullAgenticOpen,
    confirmMaintenanceOpen,
    confirmResumeControlOpen,
    emergencyLoading,
    fullAgenticLoading,
    maintenanceLoading,
    monitoringLoading,
  ])

  function settingsErrorMessage(error: unknown) {
    return error instanceof Error && error.message ? error.message : 'Failed to update. Check backend connection.'
  }

  async function toggleHitl() {
    setHitlLoading(true)
    setHitlError(null)
    try {
      await onUpdateSettings({ hitl_enabled: !settings.hitl_enabled })
    } catch (error) {
      setHitlError(settingsErrorMessage(error))
    } finally {
      setHitlLoading(false)
    }
  }

  async function toggleSms() {
    setSmsLoading(true)
    setSmsError(null)
    try {
      await onUpdateSettings({ sms_enabled: !settings.sms_enabled })
    } catch (error) {
      setSmsError(settingsErrorMessage(error))
    } finally {
      setSmsLoading(false)
    }
  }

  async function updateMaintenanceMode(enabled: boolean) {
    setMaintenanceLoading(true)
    setMaintenanceError(null)
    try {
      await onUpdateSettings({ maintenance_mode_enabled: enabled })
      setConfirmMaintenanceOpen(false)
    } catch (error) {
      setMaintenanceError(settingsErrorMessage(error))
    } finally {
      setMaintenanceLoading(false)
    }
  }

  async function updateMonitoringMode(enabled: boolean) {
    setMonitoringLoading(true)
    setMonitoringError(null)
    try {
      await onUpdateSettings({ monitoring_mode_enabled: enabled })
      setConfirmResumeControlOpen(false)
    } catch (error) {
      setMonitoringError(settingsErrorMessage(error))
    } finally {
      setMonitoringLoading(false)
    }
  }

  async function updateEmergencyStop(enabled: boolean) {
    setEmergencyLoading(true)
    setEmergencyError(null)
    try {
      await onUpdateSettings({ emergency_stop_enabled: enabled })
      setConfirmEmergencyOpen(false)
    } catch (error) {
      setEmergencyError(settingsErrorMessage(error))
    } finally {
      setEmergencyLoading(false)
    }
  }

  async function updateFullAgenticMode(enabled: boolean) {
    setFullAgenticLoading(true)
    setFullAgenticError(null)
    try {
      await onUpdateSettings({ full_agentic_mode_enabled: enabled })
      setConfirmFullAgenticOpen(false)
    } catch (error) {
      setFullAgenticError(settingsErrorMessage(error))
    } finally {
      setFullAgenticLoading(false)
    }
  }

  function toggleMaintenance() {
    if (settings.maintenance_mode_enabled) {
      void updateMaintenanceMode(false)
      return
    }
    setMaintenanceError(null)
    dialogScrollPositionRef.current = { x: window.scrollX, y: window.scrollY }
    setConfirmMaintenanceOpen(true)
  }

  function toggleMonitoringMode() {
    if (!settings.monitoring_mode_enabled) {
      void updateMonitoringMode(true)
      return
    }
    setMonitoringError(null)
    dialogScrollPositionRef.current = { x: window.scrollX, y: window.scrollY }
    setConfirmResumeControlOpen(true)
  }

  function toggleEmergencyStop() {
    if (settings.emergency_stop_enabled) {
      void updateEmergencyStop(false)
      return
    }
    setEmergencyError(null)
    dialogScrollPositionRef.current = { x: window.scrollX, y: window.scrollY }
    setConfirmEmergencyOpen(true)
  }

  function toggleFullAgenticMode() {
    if (settings.full_agentic_mode_enabled) {
      void updateFullAgenticMode(false)
      return
    }
    setFullAgenticError(null)
    dialogScrollPositionRef.current = { x: window.scrollX, y: window.scrollY }
    setConfirmFullAgenticOpen(true)
  }

  return (
    <Panel title="System configuration" eyebrow="Backend config">
      <div className="system-config-grid">
        <div className={`system-config-item critical ${settings.emergency_stop_enabled ? 'active' : ''}`}>
          <label htmlFor="emergency-stop-toggle" className="system-config-label">
            <span>Emergency Stop</span>
            <p>
              {settings.emergency_stop_enabled
                ? 'Safety lockout is active. Pump commands are blocked and unresolved cycles are marked stopped.'
                : 'Use only for overdose risk, unsafe pump behavior, or immediate command shutdown.'}
            </p>
          </label>
          <div className="system-config-control system-config-toggle-control">
            <ToggleSwitch
              id="emergency-stop-toggle"
              enabled={settings.emergency_stop_enabled}
              onToggle={toggleEmergencyStop}
              loading={emergencyLoading}
              label="emergency stop"
              tone="danger"
            />
            <span className={`toggle-label ${settings.emergency_stop_enabled ? 'danger' : 'off'}`}>
              {emergencyLoading ? '…' : settings.emergency_stop_enabled ? 'Active' : 'Off'}
            </span>
          </div>
          {emergencyError && !confirmEmergencyOpen && <p className="system-config-error">{emergencyError}</p>}
        </div>

        <div className={`system-config-item maintenance ${settings.maintenance_mode_enabled ? 'active' : ''}`}>
          <label htmlFor="maintenance-toggle" className="system-config-label">
            <span>Maintenance Mode</span>
            <p>
              {settings.maintenance_mode_enabled
                ? 'Automatic dosing is paused. Readings may be missing while sensors, pumps, or aeration are checked.'
                : 'Use during sensor calibration, pump inspection, water changes, or air stone checks.'}
            </p>
          </label>
          <div className="system-config-control system-config-toggle-control">
            <ToggleSwitch
              id="maintenance-toggle"
              enabled={settings.maintenance_mode_enabled}
              onToggle={toggleMaintenance}
              loading={maintenanceLoading}
              label="maintenance mode"
              tone="warning"
            />
            <span className={`toggle-label ${settings.maintenance_mode_enabled ? 'warning' : 'off'}`}>
              {maintenanceLoading ? '…' : settings.maintenance_mode_enabled ? 'Enabled' : 'Disabled'}
            </span>
          </div>
          {maintenanceError && !confirmMaintenanceOpen && <p className="system-config-error">{maintenanceError}</p>}
        </div>

        <div className={`system-config-item monitoring ${settings.monitoring_mode_enabled ? 'active' : ''}`}>
          <label htmlFor="monitoring-mode-toggle" className="system-config-label">
            <span>Monitoring Only</span>
            <p>
              {settings.monitoring_mode_enabled
                ? 'Live readings continue to be stored and shown. New automatic commands are paused. Use Emergency Stop to interrupt a running pump.'
                : 'Pause control-agent analysis and new dosing commands while keeping live ESP32 readings visible on the dashboard.'}
            </p>
          </label>
          <div className="system-config-control system-config-toggle-control">
            <ToggleSwitch
              id="monitoring-mode-toggle"
              enabled={settings.monitoring_mode_enabled}
              onToggle={toggleMonitoringMode}
              loading={monitoringLoading}
              label="Monitoring Only"
            />
            <span className={`toggle-label ${settings.monitoring_mode_enabled ? 'on' : 'off'}`}>
              {monitoringLoading ? '…' : settings.monitoring_mode_enabled ? 'Monitoring' : 'Off'}
            </span>
          </div>
          {monitoringError && !confirmResumeControlOpen && <p className="system-config-error">{monitoringError}</p>}
        </div>

        <div className={`system-config-item agentic ${settings.full_agentic_mode_enabled ? 'active' : ''}`}>
          <label htmlFor="full-agentic-toggle" className="system-config-label">
            <span>Full Agentic Mode</span>
            <p>
              {settings.full_agentic_mode_enabled
                ? `The full LLM pipeline runs for every ${settings.sensor_sampling_interval_seconds}-second sensor reading. Scheduled batch analysis is paused.`
                : `Use the full LLM pipeline for every ${settings.sensor_sampling_interval_seconds}-second sensor reading instead of per-reading deterministic control.`}
            </p>
          </label>
          <div className="system-config-control system-config-toggle-control">
            <ToggleSwitch
              id="full-agentic-toggle"
              enabled={settings.full_agentic_mode_enabled}
              onToggle={toggleFullAgenticMode}
              loading={fullAgenticLoading}
              label="Full Agentic Mode"
            />
            <span className={`toggle-label ${settings.full_agentic_mode_enabled ? 'on' : 'off'}`}>
              {fullAgenticLoading ? '…' : settings.full_agentic_mode_enabled ? 'Enabled' : 'Disabled'}
            </span>
          </div>
          {fullAgenticError && !confirmFullAgenticOpen && <p className="system-config-error">{fullAgenticError}</p>}
        </div>

        <div className={`system-config-item hitl ${settings.hitl_enabled ? 'active' : ''}`}>
          <label htmlFor="hitl-toggle" className="system-config-label">
            <span>Human-in-the-Loop</span>
            <p>
              {settings.hitl_enabled
                ? 'Agentic AI dosing commands are held for operator review before the pump fires.'
                : 'Agentic AI decisions execute immediately without operator approval.'}
            </p>
          </label>
          <div className="system-config-control system-config-toggle-control">
            <ToggleSwitch
              id="hitl-toggle"
              enabled={settings.hitl_enabled}
              onToggle={toggleHitl}
              loading={hitlLoading}
              label="Human-in-the-Loop"
            />
            <span className={`toggle-label ${settings.hitl_enabled ? 'on' : 'off'}`}>
              {hitlLoading ? '…' : settings.hitl_enabled ? 'Enabled' : 'Disabled'}
            </span>
          </div>
          {hitlError && <p className="system-config-error">{hitlError}</p>}
        </div>

        <div className="system-config-item">
          <label htmlFor="sms-toggle" className="system-config-label">
            <span>SMS Notifications</span>
            <p>
              {settings.sms_enabled
                ? `Enabled for critical alerts. Cooldown: ${settings.sms_cooldown_seconds} s between same alert type.`
                : 'Disabled from the dashboard. Notification attempts will be recorded as disabled.'}
            </p>
          </label>
          <div className="system-config-control system-config-toggle-control">
            <ToggleSwitch
              id="sms-toggle"
              enabled={settings.sms_enabled}
              onToggle={toggleSms}
              loading={smsLoading}
              label="SMS notifications"
            />
            <span className={`toggle-label ${settings.sms_enabled ? 'on' : 'off'}`}>
              {smsLoading ? '…' : settings.sms_enabled ? 'Enabled' : 'Disabled'}
            </span>
          </div>
          {smsError && <p className="system-config-error">{smsError}</p>}
        </div>

        <div className="system-config-item system-config-strategy-item">
          <div className="system-config-label">
            <span>Control Strategy</span>
          </div>
          <div className="system-config-control">
            <Pill
              label={formatStrategyLabel(settings.control_strategy)}
              tone={settings.control_strategy === 'agentic_ai' ? 'good' : 'neutral'}
            />
          </div>
        </div>
      </div>

      {confirmEmergencyOpen && (
        <SettingsDialogPortal>
          <div
            className="settings-confirm-overlay"
            role="presentation"
            onPointerDown={() => {
              if (!emergencyLoading) setConfirmEmergencyOpen(false)
            }}
          >
            <section
              ref={emergencyDialogRef}
              className="settings-confirm-dialog emergency"
              role="dialog"
              aria-modal="true"
              aria-labelledby="emergency-confirm-title"
              tabIndex={-1}
              onPointerDown={(event) => event.stopPropagation()}
            >
            <div className="settings-confirm-icon danger" aria-hidden="true">
              <ShieldAlert size={22} strokeWidth={2.2} />
            </div>
            <div className="settings-confirm-copy">
              <span>Safety lockout</span>
              <h3 id="emergency-confirm-title">Activate emergency stop?</h3>
              <p>
                Blocks new pump commands and stops an active pump through the connected ESP32. AI control stays
                paused until you clear the lockout. If the controller is offline, use local STOP or disconnect pump power.
              </p>
              {emergencyError && (
                <div className="settings-confirm-error" role="alert">
                  <strong>Update blocked</strong>
                  <span>{emergencyError}</span>
                </div>
              )}
            </div>
            <div className="settings-confirm-actions">
              <button
                type="button"
                className="secondary"
                onClick={() => setConfirmEmergencyOpen(false)}
                disabled={emergencyLoading}
              >
                Cancel
              </button>
              <button
                type="button"
                className="danger"
                onClick={() => void updateEmergencyStop(true)}
                disabled={emergencyLoading}
              >
                {emergencyLoading ? 'Stopping...' : 'Activate emergency stop'}
              </button>
            </div>
            </section>
          </div>
        </SettingsDialogPortal>
      )}

      {confirmFullAgenticOpen && (
        <SettingsDialogPortal>
          <div
            className="settings-confirm-overlay"
            role="presentation"
            onPointerDown={() => {
              if (!fullAgenticLoading) setConfirmFullAgenticOpen(false)
            }}
          >
            <section
              ref={fullAgenticDialogRef}
              className="settings-confirm-dialog agentic"
              role="dialog"
              aria-modal="true"
              aria-labelledby="full-agentic-confirm-title"
              tabIndex={-1}
              onPointerDown={(event) => event.stopPropagation()}
            >
            <div className="settings-confirm-icon info" aria-hidden="true">
              <Bot size={22} strokeWidth={2.2} />
            </div>
            <div className="settings-confirm-copy">
              <span>Higher token usage</span>
              <h3 id="full-agentic-confirm-title">Enable Full Agentic Mode?</h3>
              <p>
                Runs the full AI pipeline every {settings.sensor_sampling_interval_seconds} seconds instead of every{' '}
                {Math.round(settings.batch_analysis_interval_seconds / 60)} minutes. This is about {agenticFrequency}×
                more AI runs and may increase token use, cost, and response time.
              </p>
              <ul className="settings-confirm-list">
                <li>Pump limits, mixing locks, and Emergency Stop remain active.</li>
                <li>Concurrent runs fail closed without issuing a pump command.</li>
                <li>Model: {settings.agentic_ai_model}</li>
              </ul>
              {!settings.openai_api_key_configured && (
                <div className="settings-confirm-error" role="alert">
                  <strong>OpenAI key required</strong>
                  <span>Configure OPENAI_API_KEY on the backend before enabling this mode.</span>
                </div>
              )}
              {fullAgenticError && (
                <div className="settings-confirm-error" role="alert">
                  <strong>Update blocked</strong>
                  <span>{fullAgenticError}</span>
                </div>
              )}
            </div>
            <div className="settings-confirm-actions">
              <button
                type="button"
                className="secondary"
                onClick={() => setConfirmFullAgenticOpen(false)}
                disabled={fullAgenticLoading}
              >
                Keep scheduled mode
              </button>
              <button
                type="button"
                className="primary"
                onClick={() => void updateFullAgenticMode(true)}
                disabled={fullAgenticLoading || !settings.openai_api_key_configured}
              >
                {fullAgenticLoading ? 'Enabling...' : 'Enable Full Agentic Mode'}
              </button>
            </div>
            </section>
          </div>
        </SettingsDialogPortal>
      )}

      {confirmResumeControlOpen && (
        <SettingsDialogPortal>
          <div
            className="settings-confirm-overlay"
            role="presentation"
            onPointerDown={() => {
              if (!monitoringLoading) setConfirmResumeControlOpen(false)
            }}
          >
            <section
              ref={resumeControlDialogRef}
              className="settings-confirm-dialog resume"
              role="dialog"
              aria-modal="true"
              aria-labelledby="resume-control-confirm-title"
              tabIndex={-1}
              onPointerDown={(event) => event.stopPropagation()}
            >
            <div className="settings-confirm-icon info" aria-hidden="true">
              <ShieldCheck size={22} strokeWidth={2.2} />
            </div>
            <div className="settings-confirm-copy">
              <span>Automatic control</span>
              <h3 id="resume-control-confirm-title">Resume automatic control?</h3>
              <p>
                Turns off Monitoring Only. The next stable reading may trigger a safety-checked pump command.
              </p>
              <ul className="settings-confirm-list">
                <li>Verify pH, EC, water level, stock tanks, and tubing.</li>
                <li>Confirm Maintenance Mode and Emergency Stop are off.</li>
                <li>HITL is {settings.hitl_enabled ? 'enabled' : 'disabled'}.</li>
              </ul>
              {monitoringError && (
                <div className="settings-confirm-error" role="alert">
                  <strong>Update blocked</strong>
                  <span>{monitoringError}</span>
                </div>
              )}
            </div>
            <div className="settings-confirm-actions">
              <button
                type="button"
                className="secondary"
                onClick={() => setConfirmResumeControlOpen(false)}
                disabled={monitoringLoading}
              >
                Keep monitoring
              </button>
              <button
                type="button"
                className="primary"
                onClick={() => void updateMonitoringMode(false)}
                disabled={monitoringLoading}
              >
                {monitoringLoading ? 'Resuming...' : 'Resume automatic control'}
              </button>
            </div>
            </section>
          </div>
        </SettingsDialogPortal>
      )}

      {confirmMaintenanceOpen && (
        <SettingsDialogPortal>
          <div
            className="settings-confirm-overlay"
            role="presentation"
            onPointerDown={() => {
              if (!maintenanceLoading) setConfirmMaintenanceOpen(false)
            }}
          >
            <section
              ref={maintenanceDialogRef}
              className="settings-confirm-dialog"
              role="dialog"
              aria-modal="true"
              aria-labelledby="maintenance-confirm-title"
              tabIndex={-1}
              onPointerDown={(event) => event.stopPropagation()}
            >
            <div className="settings-confirm-icon warn" aria-hidden="true">
              <ShieldAlert size={22} strokeWidth={2.2} />
            </div>
            <div className="settings-confirm-copy">
              <span>Operator confirmation</span>
              <h3 id="maintenance-confirm-title">Enable maintenance mode?</h3>
              <p>
                Pauses automatic dosing while you service sensors, pumps, the reservoir, or aeration.
              </p>
              {maintenanceError && (
                <div className="settings-confirm-error" role="alert">
                  <strong>Update blocked</strong>
                  <span>{maintenanceError}</span>
                </div>
              )}
            </div>
            <div className="settings-confirm-actions">
              <button
                type="button"
                className="secondary"
                onClick={() => setConfirmMaintenanceOpen(false)}
                disabled={maintenanceLoading}
              >
                Cancel
              </button>
              <button
                type="button"
                className="warning"
                onClick={() => void updateMaintenanceMode(true)}
                disabled={maintenanceLoading}
              >
                {maintenanceLoading ? 'Enabling...' : 'Enable maintenance'}
              </button>
            </div>
            </section>
          </div>
        </SettingsDialogPortal>
      )}
    </Panel>
  )
}

function CameraSettingsPanel() {
  const configuredUrl = cleanCameraUrl(CAMERA_STREAM_URL)
  const [savedUrl, setSavedUrl] = useState(() => getSavedCameraUrl())
  const [draftUrl, setDraftUrl] = useState(savedUrl || configuredUrl)
  const [urlError, setUrlError] = useState('')
  const activeUrl = cleanCameraUrl(savedUrl || configuredUrl)
  const hostLabel = cameraHostLabel(activeUrl)
  const privateAccess = isLikelyTailscaleUrl(activeUrl)

  function save() {
    if (!isValidCameraUrl(draftUrl)) {
      setUrlError('Enter a complete HTTP or HTTPS camera URL.')
      return
    }
    const nextUrl = cleanCameraUrl(draftUrl)
    saveCameraUrl(nextUrl)
    setSavedUrl(nextUrl)
    setUrlError('')
  }

  function clear() {
    saveCameraUrl('')
    setSavedUrl('')
    setDraftUrl(configuredUrl)
    setUrlError('')
  }

  return (
    <Panel title="Camera connection" eyebrow="Grow view" className="settings-anchor-section" >
      <div className="camera-settings-summary">
        <div className="camera-icon-tile">
          <Camera size={22} strokeWidth={2.2} />
        </div>
        <div>
          <strong>{activeUrl ? 'Camera source is configured' : 'Camera source is not configured'}</strong>
          <p>
            {activeUrl
              ? `Current source: ${hostLabel}`
              : 'Add the Raspberry Pi camera stream so the Camera page can show the grow room.'}
          </p>
        </div>
        <Pill label={privateAccess ? 'Private network' : activeUrl ? 'Check access' : 'Waiting'} tone={privateAccess ? 'good' : activeUrl ? 'warn' : 'neutral'} />
      </div>

      <label className="camera-url-field">
        <span>Camera stream URL</span>
        <input
          type="url"
          inputMode="url"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          value={draftUrl}
          aria-invalid={urlError ? true : undefined}
          aria-describedby={urlError ? 'camera-url-error' : undefined}
          onChange={(event) => {
            setDraftUrl(event.target.value)
            if (urlError) setUrlError('')
          }}
          placeholder="http://100.x.x.x:1984/stream.html?src=lettuce_cam"
        />
        {urlError && <small id="camera-url-error" className="camera-url-error" role="alert">{urlError}</small>}
        <small>
          Use the Raspberry Pi private stream URL. Phones or laptops must be connected to the same private camera network before opening the live view.
        </small>
      </label>

      <div className="camera-config-actions">
        <button type="button" onClick={save}>Save camera source</button>
        <button type="button" onClick={clear} className="secondary">Use deployment default</button>
      </div>
    </Panel>
  )
}

export function SettingsPage({
  backendUrl,
  refreshSeconds,
  latestLog,
  connectionState,
  connectionMessage,
  systemSettings,
  batchStatus,
  reservoirMaxLiters,
  darkMode,
  onBackendUrl,
  operatorToken,
  onOperatorToken,
  onRefreshSeconds,
  onUpdateSettings,
  onToggleDarkMode,
  phTarget,
  ecTarget,
  sectionTarget = null,
}: {
  backendUrl: string
  refreshSeconds: number
  latestLog: LatestLog
  connectionState: ConnectionState
  connectionMessage: string
  systemSettings: SystemSettings | null
  batchStatus: BatchStatus | null
  reservoirMaxLiters: number
  darkMode: boolean
  onBackendUrl: (value: string) => void
  operatorToken: string
  onOperatorToken: (value: string) => void
  onRefreshSeconds: (value: number) => void
  onUpdateSettings: (updates: RuntimeSettingsUpdate) => Promise<void>
  onToggleDarkMode: () => void
  phTarget: { min: number; max: number }
  ecTarget: { min: number; max: number }
  sectionTarget?: 'system' | null
}) {
  const sectionTargetHandledRef = useRef(false)
  const systemSectionRef = useRef<HTMLElement>(null)

  useLayoutEffect(() => {
    if (sectionTarget !== 'system') {
      sectionTargetHandledRef.current = false
      return
    }
    if (!systemSettings || sectionTargetHandledRef.current) return
    const section = systemSectionRef.current
    if (!section) return
    sectionTargetHandledRef.current = true
    const scrollToSystem = () => {
      section.scrollIntoView({ behavior: 'auto', block: 'start' })
    }
    scrollToSystem()
    const frame = window.requestAnimationFrame(scrollToSystem)
    const settledFrame = window.setTimeout(scrollToSystem, 250)
    return () => {
      window.cancelAnimationFrame(frame)
      window.clearTimeout(settledFrame)
    }
  }, [sectionTarget, systemSettings])

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    onBackendUrl(String(form.get('backend-url') ?? backendUrl).trim())
  }

  return (
    <section className="page-content settings-page">
      <SettingsSectionNav />

      <div className="settings-top-grid">
        <section id="settings-connection" className="settings-anchor-section">
          <Panel title="Connection settings" eyebrow="Backend" className="settings-panel" >
            <form className="settings-form" onSubmit={submit}>
              <label>
                <span>System API URL</span>
                <input
                  name="backend-url"
                  type="url"
                  inputMode="url"
                  autoCapitalize="none"
                  autoCorrect="off"
                  spellCheck={false}
                  defaultValue={backendUrl}
                />
                <small>Azure App Service endpoint or local HANAS backend URL</small>
              </label>
              <label>
                <span>Operator token</span>
                <input
                  name="operator-token"
                  type="password"
                  autoComplete="off"
                  autoCapitalize="none"
                  autoCorrect="off"
                  spellCheck={false}
                  value={operatorToken}
                  onChange={(event) => onOperatorToken(event.target.value)}
                  placeholder="Required only when OPERATOR_API_TOKEN is set"
                />
                <small>Kept only for this browser tab session. Used for HITL review and settings updates.</small>
              </label>
              <label>
                <span>Refresh interval</span>
                <SettingsDropdown
                  label="Refresh interval"
                  value={refreshSeconds}
                  onChange={onRefreshSeconds}
                  options={[
                    { value: 3, label: '3 seconds' },
                    { value: 5, label: '5 seconds' },
                    { value: 10, label: '10 seconds' },
                    { value: 30, label: '30 seconds' },
                  ]}
                />
              </label>
              <button type="submit">Apply</button>
            </form>
            <div className="settings-status">
              <span
                className={`status-dot ${
                  systemSettings?.emergency_stop_enabled
                    ? 'danger'
                    : systemSettings?.maintenance_mode_enabled
                      ? 'warn'
                      : systemSettings?.monitoring_mode_enabled
                        ? 'info'
                      : toneForConnection(connectionState)
                }`}
              />
              <strong>
                {systemSettings?.emergency_stop_enabled
                  ? 'Emergency Stop'
                  : systemSettings?.maintenance_mode_enabled
                  ? 'Maintenance Mode'
                  : systemSettings?.monitoring_mode_enabled
                  ? 'Monitoring Only'
                  : connectionState === 'connected'
                    ? 'System Online'
                    : formatDisplayText(connectionState)}
              </strong>
              <span>{connectionMessage}</span>
            </div>
            {systemSettings && (
              <div className="settings-version-card" aria-label="Backend deployment version">
                <div>
                  <span>Backend version</span>
                  <strong>{formatVersionLabel(systemSettings.app_version)}</strong>
                </div>
                <p>
                  {buildIdentityParts(systemSettings).map((part) => (
                    <span key={part}>{part}</span>
                  ))}
                </p>
              </div>
            )}
          </Panel>
        </section>

        <section id="settings-crop" className="settings-anchor-section">
          <Panel title="Active reference range" eyebrow="Crop profile" className="reference-range-panel">
            {(() => {
              const phInsight = rangeInsight('pH', latestLog.ph, phTarget, '')
              const ecInsight = rangeInsight('EC', latestLog.ec, ecTarget, 'mS/cm')
              return (
                <>
            <div className="crop-tiles">
              <InfoTile
                label="Crop type"
                value={systemSettings?.crop_type || 'Lettuce'}
                note={systemSettings?.crop_variety || 'Configured crop'}
              />
              <InfoTile label="System" value="DFT" note="Deep Flow Technique" />
              <InfoTile
                label="Reservoir"
                value={`${reservoirMaxLiters} L`}
                note={systemSettings?.force_fixed_reservoir_volume ? 'Fixed dev volume' : 'Configured operating volume'}
              />
            </div>
            <div className="range-insight-grid" aria-label="Reference range interpretation">
              <RangeInsightCard insight={phInsight} />
              <RangeInsightCard insight={ecInsight} />
            </div>
            <CropLifecycleSettings
              key={[
                systemSettings?.crop_variety ?? '',
                systemSettings?.crop_transplant_date ?? '',
                systemSettings?.crop_harvest_start_day ?? 30,
                systemSettings?.crop_harvest_end_day ?? 35,
              ].join('|')}
              settings={systemSettings}
              onUpdateSettings={onUpdateSettings}
            />
                </>
              )
            })()}
          </Panel>
        </section>
      </div>

      <section id="settings-camera" className="settings-anchor-section">
        <CameraSettingsPanel />
      </section>

      <section id="settings-pumps" className="settings-anchor-section">
      <Panel title="Pump calibration reference" eyebrow="Pumps">
        <DataTable
          headers={['Pump', 'Max dose / cycle', 'Max duration', 'Flow rate', 'Dose factor']}
          rows={[
            ['pH Up',   '10 mL',               '10,000 ms',    '121 mL/min', '0.80 mL/L/unit'],
            ['pH Down', '10 mL',               '10,000 ms',    '121 mL/min', '0.71 mL/L/unit'],
            ['EC Up',   '100 mL per component', '60,000 ms',   '125 mL/min', '4.45 mL/L/unit'],
            ['EC Down', '6,000 mL',             '2,880,000 ms','125 mL/min', '592.12 mL/L/unit'],
          ]}
        />
      </Panel>
      </section>

      {/* Appearance */}
      <section id="settings-display" className="settings-anchor-section">
      <Panel title="Appearance" eyebrow="Display">
        <div className="system-config-grid">
          <div className="system-config-item appearance-config-item">
            <div className="system-config-label">
              <span>Dark mode</span>
              <p>Switch between light and dark interface theme. Preference is saved in your browser.</p>
            </div>
            <div className="system-config-control appearance-config-control">
              <button
                type="button"
                className={`dark-mode-toggle ${darkMode ? 'dark' : 'light'}`}
                onClick={onToggleDarkMode}
                aria-label={darkMode ? 'Switch to light mode' : 'Switch to dark mode'}
              >
                <span className="dark-mode-track">
                  <span className="dark-mode-thumb">
                    {darkMode ? <Moon size={12} strokeWidth={2} /> : <Sun size={12} strokeWidth={2} />}
                  </span>
                </span>
                <span className="dark-mode-label">{darkMode ? 'Dark' : 'Light'}</span>
              </button>
            </div>
          </div>
        </div>
      </Panel>
      </section>

      {systemSettings && (
        <section ref={systemSectionRef} id="settings-system" className="settings-anchor-section">
          <SystemConfigPanel settings={systemSettings} onUpdateSettings={onUpdateSettings} />
        </section>
      )}

      <section id="settings-safety" className="settings-anchor-section">
        <BatchGuardPanel batchStatus={batchStatus} />
      </section>
    </section>
  )
}
