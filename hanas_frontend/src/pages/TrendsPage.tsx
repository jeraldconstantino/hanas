import { useEffect, useRef, useState, type FocusEvent, type MouseEvent, type PointerEvent } from 'react'
import { Activity, LoaderCircle, RefreshCw, ShieldAlert, Wrench } from 'lucide-react'
import type { LatestLog, SensorHistoryEntry, ConnectionState, SystemSettings, Tone } from '../types'
import { clamp, formatDateTime, formatDoseMl, formatDuration, formatTime } from '../utils'
import { Panel } from '../components/ui/Panel'
import { maintenanceWindows, mergeTrendHistory, partitionTrendHistory, trendRangeCacheKey, type MaintenanceWindow } from '../trendMaintenance'

const TABS = ['Last 2h', 'Last 6h', 'Last 24h', 'Last 7 days', 'Crop cycle'] as const
type Tab = typeof TABS[number]

const TAB_HOURS: Record<Exclude<Tab, 'Crop cycle'>, number> = {
  'Last 2h': 2,
  'Last 6h': 6,
  'Last 24h': 24,
  'Last 7 days': 168,
}

const MAX_CHART_POINTS = 64
const MAX_RANGE_READINGS = 100_000
const MAX_CROP_CYCLE_HOURS = 24 * 366
const RANGE_REVALIDATE_INTERVAL_MS = 5 * 60_000
const MAX_RANGE_CACHE_ENTRIES = 8

type RangeCacheEntry = {
  history: SensorHistoryEntry[]
}

function cropCycleHours(transplantDate?: string | null): number | null {
  if (!transplantDate) return null
  const start = new Date(`${transplantDate}T00:00:00`)
  if (Number.isNaN(start.getTime())) return null
  return Math.min(
    MAX_CROP_CYCLE_HOURS,
    Math.max(1, Math.ceil((Date.now() - start.getTime()) / 3_600_000) + 1),
  )
}

function hoursForTab(tab: Tab, transplantDate?: string | null): number | null {
  return tab === 'Crop cycle' ? cropCycleHours(transplantDate) : TAB_HOURS[tab]
}

function limitForHours(hours: number, samplingIntervalSeconds = 60): number {
  const expectedReadings = Math.ceil((hours * 3_600) / Math.max(1, samplingIntervalSeconds))
  return Math.min(MAX_RANGE_READINGS, expectedReadings + 120)
}

function hasDose(entry: SensorHistoryEntry): boolean {
  const pump = entry.pump_activated ?? 'none'
  return Boolean(
    pump !== 'none' &&
    (entry.dose_ml ?? 0) > 0,
  )
}

function hasDoseForMetric(entry: SensorHistoryEntry, metric: 'ph' | 'ec'): boolean {
  if (!hasDose(entry)) return false
  return (entry.pump_activated ?? '').startsWith(`${metric}_`)
}

function maintenanceRangeLabel(window: MaintenanceWindow | undefined): string {
  if (!window) return ''

  const firstDate = new Date(window.startedAt)
  const lastDate = new Date(window.endedAt)
  if (Number.isNaN(firstDate.getTime()) || Number.isNaN(lastDate.getTime())) return ''

  const sameDay = firstDate.toDateString() === lastDate.toDateString()
  return sameDay
    ? `${shortDate(window.startedAt)} · ${shortTime(window.startedAt)}–${shortTime(window.endedAt)}`
    : `${shortDate(window.startedAt)} ${shortTime(window.startedAt)}–${shortDate(window.endedAt)} ${shortTime(window.endedAt)}`
}

function dosingEventSummary(entry: SensorHistoryEntry): string | undefined {
  if (!hasDose(entry)) return undefined
  const pumpKey = entry.pump_activated ?? 'none'
  const pump = formatPumpName(pumpKey)
  const dose = formatDoseMl(Number(entry.dose_ml ?? 0))
  const doseLabel = pumpKey === 'ec_up' ? `${dose} each component` : dose
  return `${pump} · ${doseLabel}`
}

function formatPumpName(pumpKey?: string | null): string {
  if (!pumpKey || pumpKey === 'none') return 'None'
  const names: Record<string, string> = {
    ph_up: 'pH Up',
    ph_down: 'pH Down',
    ec_up: 'EC Up',
    ec_down: 'EC Down',
  }
  return names[pumpKey] ?? pumpKey.replaceAll('_', ' ').replace(/\b\w/g, (char) => char.toUpperCase())
}

function formatDoseValue(value?: number | null, pumpKey?: string | null): string {
  const dose = Number(value ?? 0)
  if (dose <= 0) return '0.00 mL'
  const volume = dose >= 1000 ? `${(dose / 1000).toFixed(2)} L` : `${dose.toFixed(2)} mL`
  return pumpKey === 'ec_up' ? `${volume} each` : volume
}

function formatOptionalValue(value: number | null | undefined, unit: string, decimals = 2): string {
  return value == null ? 'Not recorded' : `${value.toFixed(decimals)} ${unit}`
}

type DoseChartEvent = {
  pump: string
  pumpKey: string
  doseLabel: string
  color: string
  ph: number
  ec: number
  temperature?: number | null
}

function pumpDoseColor(pumpKey?: string | null): string {
  const colors: Record<string, string> = {
    ph_up: 'var(--dose-ph-up)',
    ph_down: 'var(--dose-ph-down)',
    ec_up: 'var(--dose-ec-up)',
    ec_down: 'var(--dose-ec-down)',
  }
  return colors[pumpKey ?? ''] ?? 'var(--primary)'
}

function doseChartEvent(entry: SensorHistoryEntry): DoseChartEvent | null {
  if (!hasDose(entry)) return null
  const pumpKey = entry.pump_activated ?? 'none'
  return {
    pump: formatPumpName(pumpKey),
    pumpKey,
    doseLabel: formatDoseValue(entry.dose_ml, pumpKey),
    color: pumpDoseColor(pumpKey),
    ph: entry.ph,
    ec: entry.ec,
    temperature: entry.temperature,
  }
}

function niceAxisMax(values: number[], fallback = 10): number {
  const highest = values.reduce((maximum, value) => Math.max(maximum, value), 0)
  if (highest <= 0) return fallback

  const padded = highest * 1.15
  const magnitude = 10 ** Math.floor(Math.log10(padded))
  const normalized = padded / magnitude
  const nice = normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10
  return nice * magnitude
}

function sampleHistoryForChart(history: SensorHistoryEntry[], maxPoints: number): SensorHistoryEntry[] {
  if (history.length <= maxPoints) return history

  const doseEntries = history.filter(hasDose)
  const representativeCount = Math.max(2, maxPoints - doseEntries.length)
  const sampled = new Set<SensorHistoryEntry>(doseEntries)

  for (let bucketIndex = 0; bucketIndex < representativeCount; bucketIndex += 1) {
    const start = Math.floor((bucketIndex * history.length) / representativeCount)
    const end = Math.floor(((bucketIndex + 1) * history.length) / representativeCount)
    const bucket = history.slice(start, Math.max(start + 1, end))
    sampled.add(bucket[bucket.length - 1])
  }

  return [...sampled].sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime())
}

function filterByTab(
  history: SensorHistoryEntry[],
  tab: Tab,
  transplantDate?: string | null,
  referenceTimestamp?: string,
): SensorHistoryEntry[] {
  const hours = hoursForTab(tab, transplantDate)
  if (hours == null) return []
  const parsedReference = referenceTimestamp ? Date.parse(referenceTimestamp) : Number.NaN
  const referenceTime = Number.isFinite(parsedReference) ? parsedReference : Date.now()
  const cutoff = referenceTime - hours * 3_600_000
  return history.filter((e) => new Date(e.timestamp).getTime() >= cutoff)
}

function shortTime(ts: string): string {
  const d = new Date(ts)
  if (isNaN(d.getTime())) return ''
  return new Intl.DateTimeFormat('en-US', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: true,
  }).format(d)
}

function shortDate(ts: string): string {
  const d = new Date(ts)
  if (isNaN(d.getTime())) return ''
  return new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: 'numeric',
  }).format(d)
}

function axisTimestamp(ts: string, useDates: boolean): string {
  return useDates ? shortDate(ts) : shortTime(ts)
}

function smoothSvgPath(points: Array<{ x: number; y: number }>): string {
  if (points.length === 0) return ''
  if (points.length === 1) return `M ${points[0].x} ${points[0].y}`

  return points.reduce((path, point, index) => {
    if (index === 0) return `M ${point.x} ${point.y}`
    const previous = points[index - 1]
    const controlX = (previous.x + point.x) / 2
    return `${path} C ${controlX} ${previous.y}, ${controlX} ${point.y}, ${point.x} ${point.y}`
  }, '')
}

// ── Tooltip ─────────────────────────────────────────────────────────

interface TooltipState {
  value: number
  formattedValue?: string
  index: number
  xPct: number          // 0–100, horizontal center of the bar
  unit: string
  valueLabel?: string
  ts?: string
  status?: string
  statusTone?: 'good' | 'warn' | 'danger' | 'info' | 'neutral'
  detail?: string
  rows?: Array<[string, string]>
}

type DrilldownState = {
  title: string
  metric: string
  value: string
  unit: string
  status: string
  statusTone: 'good' | 'warn' | 'danger' | 'neutral' | 'info'
  timestamp?: string
  target?: string
  previous?: string
  next?: string
  doseEvent?: string
  rows?: Array<[string, string]>
  detail: string
}

function ChartTooltip({ tip }: { tip: TooltipState }) {
  const clampedLeft = Math.min(Math.max(tip.xPct, 5), 95)
  const edgeClass = clampedLeft < 35 ? 'edge-left' : clampedLeft > 65 ? 'edge-right' : ''
  return (
    <div className={`chart-tooltip ${edgeClass}`} style={{ left: `${clampedLeft}%` }}>
      <strong>
        {tip.valueLabel && <span className="tooltip-value-label">{tip.valueLabel}</span>}
        {tip.formattedValue ?? tip.value.toFixed(2)}
        <span className="tooltip-unit"> {tip.unit}</span>
      </strong>
      {tip.ts && <time>{formatTime(tip.ts)}</time>}
      {tip.status && (
        <span className={`tooltip-status ${tip.statusTone}`}>{tip.status}</span>
      )}
      {tip.rows && tip.rows.length > 0 && (
        <div className="tooltip-metric-grid">
          {tip.rows.map(([label, value]) => (
            <span key={label}>
              <b>{label}</b>
              <em>{value}</em>
            </span>
          ))}
        </div>
      )}
      {tip.detail && <span className="tooltip-detail">{tip.detail}</span>}
    </div>
  )
}

function statusForTarget(value: number, target: { min: number; max: number }) {
  const inRange = value >= target.min && value <= target.max
  const near = Math.abs(value - target.min) <= 0.15 || Math.abs(value - target.max) <= 0.15
  const status = inRange ? 'In range' : near ? 'Near boundary' : 'Out of range'
  const tone: DrilldownState['statusTone'] = inRange ? 'good' : near ? 'warn' : 'danger'
  return { status, tone }
}

function DrilldownDrawer({
  detail,
  onClose,
}: {
  detail: DrilldownState | null
  onClose: () => void
}) {
  const panelRef = useRef<HTMLElement>(null)

  useEffect(() => {
    if (!detail) return
    panelRef.current?.focus()

    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose()
    }

    document.addEventListener('keydown', closeOnEscape)
    return () => document.removeEventListener('keydown', closeOnEscape)
  }, [detail, onClose])

  if (!detail) return null
  const baseRows: Array<[string, string]> = detail.rows ?? [
    ['Status', detail.status],
    ['Target', detail.target ?? 'Reference only'],
    ...(detail.previous ? [['Previous', detail.previous] as [string, string]] : []),
    ...(detail.next ? [['Next', detail.next] as [string, string]] : []),
    ...(detail.doseEvent ? [['Dose Event', detail.doseEvent] as [string, string]] : []),
  ]
  const hasRecordedRow = baseRows.some(([label]) => label.toLowerCase() === 'recorded')
  const rows = !hasRecordedRow && detail.timestamp
    ? [
        baseRows[0],
        ['Recorded', formatDateTime(detail.timestamp)] as [string, string],
        ...baseRows.slice(1),
      ]
    : baseRows

  function closeFromBackdrop(event: MouseEvent<HTMLDivElement>) {
    event.preventDefault()
    event.stopPropagation()
    onClose()
  }

  return (
    <div className="drilldown-overlay" role="presentation" onClick={closeFromBackdrop}>
      <aside
        ref={panelRef}
        className="drilldown-panel"
        role="dialog"
        aria-modal="true"
        aria-label={`${detail.metric} drilldown`}
        tabIndex={-1}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="drilldown-header">
          <div>
            <span>{detail.title}</span>
            <h2>{detail.metric}</h2>
          </div>
          <button type="button" onClick={onClose} aria-label="Close drilldown">×</button>
        </div>

        <div className={`drilldown-value ${detail.statusTone}`}>
          <strong>{detail.value}</strong>
          <span>{detail.unit}</span>
        </div>

        <div className="drilldown-grid">
          {rows.map(([label, value]) => (
            <div key={label}>
              <span>{label}</span>
              <strong>{value}</strong>
            </div>
          ))}
        </div>

        <p className="drilldown-detail">{detail.detail}</p>
      </aside>
    </div>
  )
}

// ── HistoryChart ─────────────────────────────────────────────────────

function HistoryChart({
  title,
  eyebrow,
  timeframe,
  data,
  min,
  max,
  target,
  eventIndexes,
  eventSummaries,
  supportingRows,
  kind,
  unit,
  timestamps,
  useDateAxis = false,
  onDrilldown,
}: {
  title: string
  eyebrow?: string
  timeframe?: string
  data: number[]
  min: number
  max: number
  target: { min: number; max: number }
  eventIndexes: number[]
  eventSummaries?: Array<string | undefined>
  supportingRows?: Array<Array<[string, string]>>
  kind: 'ph' | 'ec'
  unit: string
  timestamps?: string[]
  useDateAxis?: boolean
  onDrilldown: (detail: DrilldownState) => void
}) {
  const [tooltip, setTooltip] = useState<TooltipState | null>(null)

  const usesCompressedUpperScale = max > target.max * 3
  const scaleBreakValue = target.max + (target.max - target.min) * 0.75
  const lowerScaleShare = 0.72
  const valueToScalePct = (value: number) => {
    if (!usesCompressedUpperScale || scaleBreakValue >= max) {
      return clamp(((value - min) / (max - min)) * 100, 0, 100)
    }
    if (value <= scaleBreakValue) {
      return clamp(((value - min) / (scaleBreakValue - min)) * lowerScaleShare * 100, 0, lowerScaleShare * 100)
    }
    return clamp(
      (lowerScaleShare + ((value - scaleBreakValue) / (max - scaleBreakValue)) * (1 - lowerScaleShare)) * 100,
      lowerScaleShare * 100,
      100,
    )
  }
  const zoneBottomPct = valueToScalePct(target.min)
  const zoneHeightPct = valueToScalePct(target.max) - zoneBottomPct

  // Y-axis ticks: max, targetMax, targetMin, min
  const yTicks = [
    { val: max,        cls: '' },
    { val: target.max, cls: 'y-tick-target' },
    { val: target.min, cls: 'y-tick-target' },
    { val: min,        cls: '' },
  ]

  // X-axis labels: first, middle, last
  const xLabels: { label: string; pct: number }[] = []
  if (timestamps && timestamps.length > 0) {
    xLabels.push({ label: axisTimestamp(timestamps[0], useDateAxis), pct: 0 })
    if (timestamps.length > 3) {
      const mid = Math.floor(timestamps.length / 2)
      xLabels.push({ label: axisTimestamp(timestamps[mid], useDateAxis), pct: 50 })
    }
    xLabels.push({ label: axisTimestamp(timestamps[timestamps.length - 1], useDateAxis), pct: 100 })
  } else {
    xLabels.push({ label: 'Oldest', pct: 0 })
    xLabels.push({ label: 'Latest', pct: 100 })
  }

  function handleBarEnter(
    e: MouseEvent<HTMLButtonElement> | FocusEvent<HTMLButtonElement>,
    index: number,
  ) {
    const chart = e.currentTarget.parentElement!
    const rect = chart.getBoundingClientRect()
    const barRect = e.currentTarget.getBoundingClientRect()
    const xPct = ((barRect.left + barRect.width / 2 - rect.left) / rect.width) * 100

    showTooltipForIndex(index, xPct)
  }

  function showTooltipForIndex(index: number, xPct?: number) {
    const value = data[index]
    if (value == null) return
    const tooltipXPct = xPct ?? (data.length <= 1 ? 50 : ((index + 0.5) / data.length) * 100)
    const inRange = value >= target.min && value <= target.max
    const near = Math.abs(value - target.min) <= 0.15 || Math.abs(value - target.max) <= 0.15
    const status = inRange ? 'In range' : near ? 'Near boundary' : 'Out of range'
    const statusTone: TooltipState['statusTone'] = inRange ? 'good' : near ? 'warn' : 'danger'

    const doseSummary = eventIndexes.includes(index) ? eventSummaries?.[index] : undefined
    setTooltip({
      value,
      index,
      xPct: tooltipXPct,
      unit,
      ts: timestamps?.[index],
      status,
      statusTone,
      detail: doseSummary ? `Dose event: ${doseSummary}` : 'No dosing event at this point',
    })
  }

  function handleChartPointer(event: PointerEvent<HTMLDivElement>) {
    if (data.length === 0) return
    if (event.pointerType === 'mouse') return
    const rect = event.currentTarget.getBoundingClientRect()
    const ratio = clamp((event.clientX - rect.left) / rect.width, 0, 1)
    const index = Math.round(ratio * (data.length - 1))
    showTooltipForIndex(index)
  }

  function openBarDrilldown(value: number, index: number) {
    const { status, tone } = statusForTarget(value, target)
    const previous = data[index - 1]
    const next = data[index + 1]
    const doseEvent = eventIndexes.includes(index) ? eventSummaries?.[index] : undefined
    const direction =
      previous == null ? 'This is the first point in the selected range.'
      : value > previous ? `Up ${Math.abs(value - previous).toFixed(2)} ${unit} from previous reading.`
      : value < previous ? `Down ${Math.abs(value - previous).toFixed(2)} ${unit} from previous reading.`
      : 'Unchanged from previous reading.'

    onDrilldown({
      title: timeframe ? `${title} ${timeframe}` : title,
      metric: kind === 'ph' ? 'pH reading' : 'EC reading',
      value: value.toFixed(2),
      unit,
      status,
      statusTone: tone,
      timestamp: timestamps?.[index],
      target: `${target.min}-${target.max} ${unit}`,
      previous: previous != null ? `${previous.toFixed(2)} ${unit}` : undefined,
      next: next != null ? `${next.toFixed(2)} ${unit}` : undefined,
      doseEvent,
      rows: [
        ['Status', status],
        ['Target', `${target.min}-${target.max} ${unit}`],
        ...(supportingRows?.[index] ?? []),
        ['Previous', previous != null ? `${previous.toFixed(2)} ${unit}` : 'No previous point'],
        ['Next', next != null ? `${next.toFixed(2)} ${unit}` : 'No next point'],
        ...(doseEvent ? [['Dose Event', doseEvent] as [string, string]] : []),
      ],
      detail: `${direction} ${
        eventIndexes.includes(index)
          ? doseEvent
            ? `Dose recorded at this point: ${doseEvent}.`
            : 'A dose was recorded at this point, but the pump amount was not stored.'
          : 'No dosing event is marked at this point.'
      }`,
    })
  }

  if (data.length === 0) {
    return <Panel title={title} eyebrow={eyebrow} badge={timeframe} className="history-chart-panel"><div className="trend-chart-empty"><strong>No readings in this range</strong><p>Select a wider time range or the crop cycle to view older readings.</p></div></Panel>
  }

  return (
    <Panel title={title} eyebrow={eyebrow} badge={timeframe} className="history-chart-panel">
      <div className="chart-outer">
        {/* Y-axis */}
        <div className="chart-y-axis">
          <span className="chart-y-unit">{unit}</span>
          {yTicks.map(({ val, cls }) => {
            const bottomPct = valueToScalePct(val)
            return (
              <span
                key={val}
                className={`y-tick ${cls}`}
                style={{ bottom: `${bottomPct}%` }}
              >
                {val}
              </span>
            )
          })}
        </div>

        {/* Chart + X-axis */}
        <div className="chart-inner">
          <div
            className="history-chart"
            onMouseLeave={() => setTooltip(null)}
            onPointerDown={handleChartPointer}
            onPointerMove={handleChartPointer}
            onPointerCancel={() => setTooltip(null)}
          >
            <div
              className="chart-target-zone"
              style={{ bottom: `${zoneBottomPct}%`, height: `${zoneHeightPct}%` }}
              aria-hidden="true"
            />
            {usesCompressedUpperScale && (
              <div
                className="chart-scale-break"
                style={{ bottom: `${lowerScaleShare * 100}%` }}
                aria-hidden="true"
              />
            )}
            {data.map((value, index) => {
              const height = clamp(valueToScalePct(value), 5, 100)
              const inRange = value >= target.min && value <= target.max
              const near = Math.abs(value - target.min) <= 0.15 || Math.abs(value - target.max) <= 0.15
              return (
                <button
                  type="button"
                  className={inRange ? 'good' : near ? 'warn' : 'danger'}
                  key={`${title}-${index}`}
                  style={{ height: `${height}%` }}
                  onMouseEnter={(e) => handleBarEnter(e, index)}
                  onMouseMove={(e) => handleBarEnter(e, index)}
                  onFocus={(e) => handleBarEnter(e, index)}
                  onClick={() => openBarDrilldown(value, index)}
                  aria-label={`Open ${kind} reading ${value.toFixed(2)} ${unit} drilldown`}
                >
                  {eventIndexes.includes(index) && <i />}
                </button>
              )
            })}
          </div>

          {tooltip && (
            <div className="history-chart-tooltip-layer">
              <ChartTooltip tip={tooltip} />
            </div>
          )}

          {/* X-axis */}
          <div className="chart-x-axis">
            {xLabels.map(({ label, pct }) => (
              <span key={pct} style={{ left: `${pct}%` }}>{label}</span>
            ))}
          </div>
        </div>
      </div>

      <div className="chart-legend">
        <span>■ In range</span>
        <span>■ Out of range</span>
        <span>■ Near boundary</span>
        <span>▾ {kind === 'ph' ? 'pH' : 'EC'} dosing event</span>
        {usesCompressedUpperScale && (
          <span className="scale-break-legend">// Scale compressed above {scaleBreakValue.toFixed(1)} {unit}</span>
        )}
      </div>
    </Panel>
  )
}

// ── CompactChart ─────────────────────────────────────────────────────

function CompactChart({
  title,
  data,
  min,
  max,
  color,
  footer,
  unit,
  timestamps,
  useDateAxis = false,
  onDrilldown,
  mode = 'bar',
  targetBand,
  stats,
  emptyState,
  doseEvents,
}: {
  title: string
  data: number[]
  min: number
  max: number
  color: string
  footer: string
  unit: string
  timestamps?: string[]
  useDateAxis?: boolean
  onDrilldown: (detail: DrilldownState) => void
  mode?: 'bar' | 'line'
  targetBand?: { min: number; max: number; label: string }
  stats?: CompactChartStat[]
  emptyState?: { title: string; detail: string }
  doseEvents?: Array<DoseChartEvent | null>
}) {
  const [tooltip, setTooltip] = useState<TooltipState | null>(null)
  const chartRange = Math.max(0.0001, max - min)
  const latestValue = data[data.length - 1]
  const statusLabel = latestValue == null
    ? 'No readings'
    : targetBand && latestValue > targetBand.max
      ? 'Above optimal'
      : targetBand && latestValue < targetBand.min
        ? 'Below optimal'
        : targetBand
          ? 'Optimal'
          : 'Recorded'
  const statusTone = latestValue == null
    ? 'neutral'
    : targetBand && latestValue > targetBand.max
      ? 'warn'
      : targetBand && latestValue < targetBand.min
        ? 'info'
        : 'good'

  function handleBarEnter(
    e: MouseEvent<HTMLButtonElement> | FocusEvent<HTMLButtonElement>,
    index: number,
  ) {
    const chart = e.currentTarget.parentElement!
    const rect = chart.getBoundingClientRect()
    const barRect = e.currentTarget.getBoundingClientRect()
    const xPct = ((barRect.left + barRect.width / 2 - rect.left) / rect.width) * 100
    showBarTooltipForIndex(index, xPct)
  }

  function showBarTooltipForIndex(index: number, xPct?: number) {
    const value = data[index]
    if (value == null) return
    const tooltipXPct = xPct ?? (data.length <= 1 ? 50 : ((index + 0.5) / data.length) * 100)
    const doseEvent = doseEvents?.[index]
    setTooltip({
      value,
      formattedValue: doseEvents ? formatDoseMl(value).replace(/ mL$/, '') : undefined,
      index,
      xPct: tooltipXPct,
      unit,
      valueLabel: doseEvents ? 'Dose' : undefined,
      ts: timestamps?.[index],
      status: doseEvent ? undefined : 'No dose recorded',
      statusTone: doseEvent ? 'good' : 'neutral',
      rows: doseEvent ? [
        ['pH', doseEvent.ph.toFixed(2)],
        ['EC', `${doseEvent.ec.toFixed(2)} mS/cm`],
        ['Temp', doseEvent.temperature != null ? `${doseEvent.temperature.toFixed(1)}°C` : 'Not recorded'],
      ] : undefined,
      detail: doseEvent ? undefined : 'Pump did not run for this cycle',
    })
  }

  function handleCompactBarPointer(event: PointerEvent<HTMLDivElement>) {
    if (data.length === 0) return
    if (event.pointerType === 'mouse') return
    const rect = event.currentTarget.getBoundingClientRect()
    const ratio = clamp((event.clientX - rect.left) / rect.width, 0, 1)
    const index = Math.round(ratio * (data.length - 1))
    showBarTooltipForIndex(index)
  }

  function handleLinePointEnter(value: number, index: number, xPct: number) {
    const status =
      targetBand && value > targetBand.max ? 'Above optimal'
      : targetBand && value < targetBand.min ? 'Below optimal'
      : targetBand ? 'Optimal range'
      : 'Recorded'
    const statusTone: TooltipState['statusTone'] =
      targetBand && value > targetBand.max ? 'warn'
      : targetBand && value < targetBand.min ? 'info'
      : targetBand ? 'good'
      : 'neutral'
    setTooltip({
      value,
      index,
      xPct,
      unit,
      ts: timestamps?.[index],
      status,
      statusTone,
      detail: targetBand ? `Target ${targetBand.min} - ${targetBand.max}${unit}` : undefined,
    })
  }

  function handleLinePointer(event: PointerEvent<HTMLDivElement>) {
    if (linePoints.length === 0) return
    if (event.pointerType === 'mouse') return
    const rect = event.currentTarget.getBoundingClientRect()
    const ratio = clamp((event.clientX - rect.left) / rect.width, 0, 1)
    const index = Math.round(ratio * (linePoints.length - 1))
    const point = linePoints[index]
    if (point) handleLinePointEnter(point.value, point.index, point.x)
  }

  function openBarDrilldown(value: number, index: number) {
    const previous = data[index - 1]
    const next = data[index + 1]
    const doseEvent = doseEvents?.[index]
    const isDoseChart = Boolean(doseEvents)
    const doseRows: Array<[string, string]> | undefined = isDoseChart
      ? [
        ['Pump', doseEvent?.pump ?? 'No pump activated'],
        ['Dose volume', doseEvent?.doseLabel ?? '0.00 mL'],
        ['Previous cycle', previous != null ? `${previous.toFixed(2)} ${unit}` : 'No previous dose'],
        ['Next cycle', next != null ? `${next.toFixed(2)} ${unit}` : 'No next dose'],
      ]
      : undefined
    onDrilldown({
      title,
      metric: isDoseChart ? (doseEvent ? `${doseEvent.pump} correction` : 'No dosing event') : title,
      value: isDoseChart ? formatDoseMl(value).replace(/ mL$/, '') : value.toFixed(2),
      unit,
      status: value === 0 ? 'No dose recorded' : 'Dosing recorded',
      statusTone: value === 0 ? 'neutral' : 'info',
      timestamp: timestamps?.[index],
      previous: previous != null ? `${previous.toFixed(2)} ${unit}` : undefined,
      next: next != null ? `${next.toFixed(2)} ${unit}` : undefined,
      rows: doseRows,
      detail: value === 0
        ? 'No dose was recorded for this cycle.'
        : isDoseChart && doseEvent
          ? `HANAS recorded ${doseEvent.doseLabel} through ${doseEvent.pump} at this point. Use the nearby pH and EC trend charts to confirm whether the reservoir responded after mixing.`
          : 'This point shows the recorded value for the selected cycle. Compare it with nearby readings to judge response.',
    })
  }

  const xLabels: { label: string; pct: number }[] = []
  if (timestamps && timestamps.length > 0) {
    xLabels.push({ label: axisTimestamp(timestamps[0], useDateAxis), pct: 0 })
    if (timestamps.length > 3) {
      const mid = Math.floor(timestamps.length / 2)
      xLabels.push({ label: axisTimestamp(timestamps[mid], useDateAxis), pct: 50 })
    }
    xLabels.push({ label: axisTimestamp(timestamps[timestamps.length - 1], useDateAxis), pct: 100 })
  } else {
    xLabels.push({ label: 'Oldest', pct: 0 })
    xLabels.push({ label: 'Latest', pct: 100 })
  }

  const linePoints = data.map((value, index) => {
    const x = data.length <= 1 ? 50 : (index / (data.length - 1)) * 100
    const y = 68 - clamp(((value - min) / chartRange) * 56, 0, 56)
    return { value, index, x, y }
  })
  const smoothLinePath = smoothSvgPath(linePoints)
  const targetTop = targetBand ? 68 - clamp(((targetBand.max - min) / chartRange) * 56, 0, 56) : 0
  const targetBottom = targetBand ? 68 - clamp(((targetBand.min - min) / chartRange) * 56, 0, 56) : 0
  const targetY = Math.min(targetTop, targetBottom)
  const targetHeight = Math.max(1.5, Math.abs(targetBottom - targetTop))

  if (data.length === 0) {
    return <Panel title={title} className="span-6 compact-chart"><div className="trend-chart-empty"><strong>{doseEvents ? 'No dosing events in this range' : 'No temperature readings in this range'}</strong><p>{doseEvents ? 'Recorded doses will appear here when available.' : 'Select a wider time range or the crop cycle to view older readings.'}</p></div></Panel>
  }

  return (
    <Panel title={title} className={`span-6 compact-chart ${mode}${stats && stats.length > 0 ? ' has-stats' : ''}${doseEvents ? ' dose-chart' : ''}`}>
      {mode === 'line' && (
        <div className="compact-chart-summary">
          <b className={statusTone}>{statusLabel}</b>
        </div>
      )}
      <div className="chart-inner compact-chart-inner">
        {mode === 'line' ? (
          <div className="compact-line-chart" onMouseLeave={() => setTooltip(null)}>
            <div className="compact-y-axis" aria-hidden="true">
              <span>{max.toFixed(0)}</span>
              <span>{((min + max) / 2).toFixed(0)}</span>
              <span>{min.toFixed(0)}</span>
            </div>
            <div
              className="compact-line-plot"
              onPointerDown={handleLinePointer}
              onPointerMove={handleLinePointer}
              onPointerCancel={() => setTooltip(null)}
            >
              <svg viewBox="0 0 100 72" preserveAspectRatio="none" role="img" aria-label={`${title} line chart`}>
                <line className="axis-line y-axis-line" x1="0" x2="0" y1="8" y2="68" />
                <line className="axis-line x-axis-line" x1="0" x2="100" y1="68" y2="68" />
                <line className="grid-line" x1="0" x2="100" y1="12" y2="12" />
                <line className="grid-line" x1="0" x2="100" y1="40" y2="40" />
                {targetBand && (
                  <g className="temperature-target-band">
                    <rect x="0" y={targetY} width="100" height={targetHeight} rx="1.6" />
                  </g>
                )}
                <path className="temperature-line-shadow" d={smoothLinePath} />
                <path className="temperature-line" d={smoothLinePath} stroke={color} />
                {linePoints.map((point, index) => {
                  const previousX = linePoints[index - 1]?.x ?? 0
                  const nextX = linePoints[index + 1]?.x ?? 100
                  const left = index === 0 ? 0 : (previousX + point.x) / 2
                  const right = index === linePoints.length - 1 ? 100 : (point.x + nextX) / 2
                  return (
                    <rect
                      key={`${title}-${point.index}`}
                      tabIndex={0}
                      role="button"
                      className="temperature-hit-zone"
                      x={left}
                      y="0"
                      width={Math.max(1, right - left)}
                      height="72"
                      onMouseEnter={() => handleLinePointEnter(point.value, point.index, point.x)}
                      onMouseMove={() => handleLinePointEnter(point.value, point.index, point.x)}
                      onFocus={() => handleLinePointEnter(point.value, point.index, point.x)}
                      onClick={() => openBarDrilldown(point.value, point.index)}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter' || event.key === ' ') {
                          event.preventDefault()
                          openBarDrilldown(point.value, point.index)
                        }
                      }}
                      aria-label={`Open ${title} ${point.value.toFixed(2)} ${unit} drilldown`}
                    />
                  )
                })}
              </svg>
              {tooltip && <ChartTooltip tip={tooltip} />}
            </div>
          </div>
        ) : (
          <div className="compact-bar-chart">
            <div className="compact-y-axis compact-bar-y-axis" aria-hidden="true">
              <span>{max.toFixed(0)}</span>
              <span>{(max / 2).toFixed(0)}</span>
              <span>0</span>
            </div>
            <div className="compact-bar-plot">
              <div className="compact-bar-grid" aria-hidden="true">
                <i />
                <i />
              </div>
              <div
                className="small-bars"
                onMouseLeave={() => setTooltip(null)}
                onPointerDown={handleCompactBarPointer}
                onPointerMove={handleCompactBarPointer}
                onPointerCancel={() => setTooltip(null)}
              >
                {data.map((value, index) => {
                  const doseEvent = doseEvents?.[index]
                  const barColor = value === 0 ? 'var(--surface-muted)' : doseEvent?.color ?? color
                  return (
                    <button
                      type="button"
                      key={`${title}-${index}`}
                      style={{
                        height: `${clamp(((value - min) / chartRange) * 100, 4, 100)}%`,
                        background: barColor,
                      }}
                      onMouseEnter={(e) => handleBarEnter(e, index)}
                      onMouseMove={(e) => handleBarEnter(e, index)}
                      onFocus={(e) => handleBarEnter(e, index)}
                      onClick={() => openBarDrilldown(value, index)}
                      aria-label={`Open ${title} ${doseEvents ? formatDoseMl(value) : `${value.toFixed(2)} ${unit}`} drilldown`}
                    />
                  )
                })}
              </div>
            </div>
            {tooltip && (
              <div className="compact-bar-tooltip-layer" aria-hidden="true">
                <ChartTooltip tip={tooltip} />
              </div>
            )}
          </div>
        )}
        <div className="chart-x-axis">
          {xLabels.map(({ label, pct }) => (
            <span key={pct} style={{ left: `${pct}%` }}>{label}</span>
          ))}
        </div>
      </div>
      {mode === 'line' && targetBand ? (
        <div className="compact-line-footer">
          <span><i className="line-dot" /> Current reading</span>
          <span><i className="band-dot" /> {targetBand.label} {targetBand.min} - {targetBand.max}{unit}</span>
        </div>
      ) : null}
      {mode === 'bar' && doseEvents ? (
        <div className="dose-chart-legend" aria-label="Dose pump legend">
          <span><i className="ph-up" /> pH Up</span>
          <span><i className="ph-down" /> pH Down</span>
          <span><i className="ec-up" /> EC Up</span>
          <span><i className="ec-down" /> EC Down</span>
        </div>
      ) : null}
      <p>{footer}</p>
      {stats && stats.length > 0 ? (
        <div className="compact-chart-stats">
          {stats.map((stat) => (
            <div key={stat.label}>
              <span>{stat.label}</span>
              <strong>{stat.value}</strong>
              {stat.helper && <small>{stat.helper}</small>}
            </div>
          ))}
        </div>
      ) : emptyState ? (
        <div className="compact-chart-empty">
          <strong>{emptyState.title}</strong>
          <span>{emptyState.detail}</span>
        </div>
      ) : null}
    </Panel>
  )
}

// ── Stats ────────────────────────────────────────────────────────────

function StatCard({ value, label, detail, delta, tone, onClick }: {
  value: string; label: string; detail: string; delta: string; tone: Tone
  onClick: () => void
}) {
  return (
    <button type="button" className={`stat-card span-3 ${tone}`} onClick={onClick}>
      <strong>{value}</strong>
      <div>
        <span>{label}</span>
        <p>{detail}</p>
        <b>{delta}</b>
      </div>
    </button>
  )
}

function computeStats(
  phData: number[],
  ecData: number[],
  doseData: number[],
  phTarget: { min: number; max: number },
  ecTarget: { min: number; max: number },
) {
  if (phData.length === 0) return { phInRange: '—', ecInRange: '—', dosingCount: '—' }
  const phIn = phData.filter((v) => v >= phTarget.min && v <= phTarget.max).length
  const ecIn = ecData.filter((v) => v >= ecTarget.min && v <= ecTarget.max).length
  return {
    phInRange: `${Math.round((phIn / phData.length) * 100)}%`,
    ecInRange: `${Math.round((ecIn / ecData.length) * 100)}%`,
    dosingCount: String(doseData.filter((v) => v > 0).length),
  }
}

function averageCorrectionResponse(
  history: SensorHistoryEntry[],
  safetyHistory: SensorHistoryEntry[],
  phTarget: { min: number; max: number },
  ecTarget: { min: number; max: number },
): { seconds: number; pairCount: number } | null {
  const interruptedCycles = new Set(
    safetyHistory
      .map((entry) => entry.control_cycle_id)
      .filter((cycleId): cycleId is number => cycleId != null),
  )
  const responseSeconds: number[] = []

  history.forEach((entry, index) => {
    if (!hasDose(entry) || (entry.control_cycle_id != null && interruptedCycles.has(entry.control_cycle_id))) return

    const metric = (entry.pump_activated ?? '').startsWith('ph_') ? 'ph' : 'ec'
    const target = metric === 'ph' ? phTarget : ecTarget
    const corrected = history.slice(index + 1).find((candidate) => {
      if (hasDose(candidate)) return false
      const value = metric === 'ph' ? candidate.ph : candidate.ec
      return value >= target.min && value <= target.max
    })
    if (!corrected) return

    const seconds = (Date.parse(corrected.timestamp) - Date.parse(entry.timestamp)) / 1_000
    if (Number.isFinite(seconds) && seconds > 0) responseSeconds.push(seconds)
  })

  if (responseSeconds.length === 0) return null
  return {
    seconds: responseSeconds.reduce((sum, seconds) => sum + seconds, 0) / responseSeconds.length,
    pairCount: responseSeconds.length,
  }
}

type CompactChartStat = {
  label: string
  value: string
  helper?: string
}

export function TrendsPage({
  latestLog,
  history,
  connectionState,
  backendUrl,
  systemSettings,
  phTarget,
  ecTarget,
}: {
  latestLog: LatestLog
  history: SensorHistoryEntry[]
  connectionState: ConnectionState
  backendUrl: string
  systemSettings: SystemSettings | null
  phTarget: { min: number; max: number }
  ecTarget: { min: number; max: number }
}) {
  const [activeTab, setActiveTab] = useState<Tab>('Last 24h')
  const [drilldown, setDrilldown] = useState<DrilldownState | null>(null)
  const [rangeCache, setRangeCache] = useState<Record<string, RangeCacheEntry>>({})
  const [rangePendingKey, setRangePendingKey] = useState<string | null>(null)
  const [rangeErrorKey, setRangeErrorKey] = useState<string | null>(null)
  const [rangeRetryVersion, setRangeRetryVersion] = useState(0)
  const rangeRequestIdRef = useRef(0)
  const activeHours = hoursForTab(activeTab, systemSettings?.crop_transplant_date)
  const rangeRequestKey = trendRangeCacheKey(
    backendUrl,
    activeTab,
    activeHours,
    systemSettings?.crop_transplant_date,
  )

  useEffect(() => {
    if (connectionState !== 'connected') return

    const controller = new AbortController()
    const baseUrl = backendUrl.trim().replace(/\/$/, '')
    const hours = activeHours
    if (hours == null) return
    const limit = limitForHours(hours, systemSettings?.sensor_sampling_interval_seconds)
    const requestId = ++rangeRequestIdRef.current
    let requestInFlight = false

    async function loadRange() {
      if (requestInFlight) return
      requestInFlight = true
      setRangePendingKey(rangeRequestKey)
      setRangeErrorKey((current) => current === rangeRequestKey ? null : current)

      try {
        const response = await fetch(`${baseUrl}/api/system-logs?hours=${hours}&limit=${limit}`, {
          signal: controller.signal,
        })
        if (!response.ok) throw new Error('Unable to load trend history')
        const nextHistory = (await response.json()) as SensorHistoryEntry[]
        if (controller.signal.aborted || rangeRequestIdRef.current !== requestId) return
        setRangeCache((current) => {
          const next = { ...current }
          delete next[rangeRequestKey]
          next[rangeRequestKey] = { history: nextHistory }
          const keys = Object.keys(next)
          keys
            .slice(0, Math.max(0, keys.length - MAX_RANGE_CACHE_ENTRIES))
            .forEach((key) => delete next[key])
          return next
        })
        setRangeErrorKey(null)
      } catch {
        if (!controller.signal.aborted && rangeRequestIdRef.current === requestId) {
          setRangeErrorKey(rangeRequestKey)
        }
      } finally {
        requestInFlight = false
        if (rangeRequestIdRef.current === requestId) {
          setRangePendingKey((current) => current === rangeRequestKey ? null : current)
        }
      }
    }

    loadRange()
    const refreshTimer = window.setInterval(loadRange, RANGE_REVALIDATE_INTERVAL_MS)
    return () => {
      window.clearInterval(refreshTimer)
      controller.abort()
      if (rangeRequestIdRef.current === requestId) rangeRequestIdRef.current += 1
    }
  }, [
    activeTab,
    activeHours,
    backendUrl,
    connectionState,
    rangeRequestKey,
    rangeRetryVersion,
    systemSettings?.crop_transplant_date,
    systemSettings?.sensor_sampling_interval_seconds,
  ])

  const cachedRange = rangeCache[rangeRequestKey]
  const hasRequestedRange = Boolean(cachedRange)
  const rangeLoadFailed = rangeErrorKey === rangeRequestKey
  const rangeMayBeTruncated = Boolean(
    hasRequestedRange &&
    activeHours != null &&
    limitForHours(activeHours, systemSettings?.sensor_sampling_interval_seconds) === MAX_RANGE_READINGS &&
    cachedRange.history.length >= MAX_RANGE_READINGS,
  )
  const rangeLoading = (
    connectionState === 'connected' &&
    !hasRequestedRange &&
    !rangeLoadFailed
  )
  const rangeRefreshing = (
    connectionState === 'connected' &&
    hasRequestedRange &&
    rangePendingKey === rangeRequestKey
  )
  const sourceHistory = cachedRange ? mergeTrendHistory(cachedRange.history, history) : history
  const hasLiveConnection = connectionState === 'connected'
  const chronologicalHistory = hasLiveConnection ? [...sourceHistory].reverse() : []
  const filteredHistory = hasLiveConnection
    ? filterByTab(chronologicalHistory, activeTab, systemSettings?.crop_transplant_date)
    : null

  const partitionedHistory = filteredHistory ? partitionTrendHistory(filteredHistory) : null
  const maintenanceHistory = partitionedHistory?.maintenance ?? []
  const safetyHistory = partitionedHistory?.safety ?? []
  const operationalHistory = partitionedHistory?.operational ?? null
  const monitoringHistory = operationalHistory?.filter((entry) => (
    entry.decision === 'monitoring_mode' ||
    entry.status === 'monitoring_mode' ||
    entry.decision_metadata?.monitoring_mode_enabled === true
  )) ?? []
  const recordedMaintenanceWindows = filteredHistory ? maintenanceWindows(filteredHistory) : []
  const phData        = operationalHistory ? operationalHistory.map((e) => e.ph)                    : []
  const ecData        = operationalHistory ? operationalHistory.map((e) => e.ec)                    : []
  const tempData      = operationalHistory ? operationalHistory.filter((e) => e.temperature != null).map((e) => e.temperature as number) : []
  const doseData      = operationalHistory ? operationalHistory.map((e) => e.dose_ml ?? 0)          : []
  const timestamps    = operationalHistory ? operationalHistory.map((e) => e.timestamp)             : undefined
  const tempTimestamps= operationalHistory ? operationalHistory.filter((e) => e.temperature != null).map(e => e.timestamp) : undefined

  const chartHistory = operationalHistory ? sampleHistoryForChart(operationalHistory, MAX_CHART_POINTS) : null
  const chartPhData = chartHistory ? chartHistory.map((e) => e.ph) : phData.slice(-MAX_CHART_POINTS)
  const chartEcData = chartHistory ? chartHistory.map((e) => e.ec) : ecData.slice(-MAX_CHART_POINTS)
  const chartTemperatureAtIndex = chartHistory ? chartHistory.map((e) => e.temperature ?? null) : tempData.slice(-MAX_CHART_POINTS)
  const doseEntries = operationalHistory?.filter(hasDose) ?? []
  const chartDoseData = operationalHistory
    ? doseEntries.map((entry) => entry.dose_ml ?? 0)
    : doseData.filter((value) => value > 0)
  const chartDoseEvents = operationalHistory
    ? doseEntries.map(doseChartEvent)
    : chartDoseData.map((value, index) => value > 0 ? {
        pump: 'Dose',
        pumpKey: 'none',
        doseLabel: formatDoseMl(value),
        color: 'var(--primary)',
        ph: chartPhData[index] ?? latestLog.ph,
        ec: chartEcData[index] ?? latestLog.ec,
        temperature: chartTemperatureAtIndex[index] ?? null,
      } : null)
  const chartDoseTimestamps = operationalHistory
    ? doseEntries.map((entry) => entry.timestamp)
    : timestamps?.filter((_, index) => (doseData[index] ?? 0) > 0)
  const chartTimestamps = chartHistory ? chartHistory.map((e) => e.timestamp) : timestamps?.slice(-MAX_CHART_POINTS)
  const chartPhDoseIndexes = chartHistory
    ? chartHistory.reduce<number[]>((acc, entry, index) => {
        if (hasDoseForMetric(entry, 'ph')) acc.push(index)
        return acc
      }, [])
    : []
  const chartEcDoseIndexes = chartHistory
    ? chartHistory.reduce<number[]>((acc, entry, index) => {
        if (hasDoseForMetric(entry, 'ec')) acc.push(index)
        return acc
      }, [])
    : []
  const chartPhDoseSummaries = chartHistory
    ? chartHistory.map((entry) => hasDoseForMetric(entry, 'ph') ? dosingEventSummary(entry) : undefined)
    : []
  const chartEcDoseSummaries = chartHistory
    ? chartHistory.map((entry) => hasDoseForMetric(entry, 'ec') ? dosingEventSummary(entry) : undefined)
    : []
  const chartTempHistory = chartHistory?.filter((e) => e.temperature != null)
  const chartTempData = chartTempHistory ? chartTempHistory.map((e) => e.temperature ?? 0) : tempData.slice(-MAX_CHART_POINTS)
  const chartTempTimestamps = chartTempHistory ? chartTempHistory.map((e) => e.timestamp) : tempTimestamps?.slice(-MAX_CHART_POINTS)
  const chartPhSupportingRows = chartPhData.map((_, index): Array<[string, string]> => [
    ['EC at same time', formatOptionalValue(chartEcData[index], 'mS/cm')],
    ['Temperature', formatOptionalValue(chartTemperatureAtIndex[index], '°C', 1)],
  ])
  const chartEcSupportingRows = chartEcData.map((_, index): Array<[string, string]> => [
    ['pH at same time', formatOptionalValue(chartPhData[index], 'pH')],
    ['Temperature', formatOptionalValue(chartTemperatureAtIndex[index], '°C', 1)],
  ])

  const stats = hasLiveConnection
      ? computeStats(phData, ecData, doseData, phTarget, ecTarget)
      : { phInRange: '—', ecInRange: '—', dosingCount: '—' }
  const averageResponse = operationalHistory
      ? averageCorrectionResponse(operationalHistory, safetyHistory, phTarget, ecTarget)
      : null
  const averageResponseLabel = averageResponse
    ? formatDuration(Math.round(averageResponse.seconds))
    : '—'
  const averageResponseCompactLabel = averageResponse
    ? `${Math.floor(Math.round(averageResponse.seconds) / 60)}m ${Math.round(averageResponse.seconds) % 60}s`
    : '—'
  const complianceDetail = hasLiveConnection ? `${phData.length} recorded readings` : 'Last 24h'
  const dosingDetail = hasLiveConnection ? `${doseData.length} recorded readings` : 'Today'
  const chartTimeframe = hasLiveConnection ? activeTab : 'Last 24h'
  const noData = hasLiveConnection && phData.length === 0 && maintenanceHistory.length === 0 && !rangeLoading
  const hasOnlyMaintenanceData = hasLiveConnection && phData.length === 0 && maintenanceHistory.length > 0 && !rangeLoading
  const latestMaintenanceWindow = recordedMaintenanceWindows.at(-1)
  const maintenanceLabel = maintenanceRangeLabel(latestMaintenanceWindow)
  const earlierMaintenanceWindowCount = Math.max(0, recordedMaintenanceWindows.length - 1)
  const earlierMaintenanceWindows = recordedMaintenanceWindows.slice(0, -1).reverse()
  const hasDisplayedDose = chartDoseData.some((value) => value > 0)
  const chartDoseMax = niceAxisMax(chartDoseData)
  const highestEc = chartEcData.reduce((maximum, value) => Math.max(maximum, value), 0)
  const ecAxisMax = highestEc > 2.6
    ? Math.ceil(highestEc * 1.1 * 10) / 10
    : 2.6
  const useDateAxis = activeTab === 'Last 7 days' || activeTab === 'Crop cycle'
  const temperatureSummary = tempData.reduce(
    (summary, value) => ({
      sum: summary.sum + value,
      min: Math.min(summary.min, value),
      max: Math.max(summary.max, value),
      inRange: summary.inRange + (value >= 18 && value <= 26 ? 1 : 0),
    }),
    { sum: 0, min: Number.POSITIVE_INFINITY, max: Number.NEGATIVE_INFINITY, inRange: 0 },
  )
  const temperatureAverage = tempData.length > 0 ? temperatureSummary.sum / tempData.length : null
  const temperatureInRange = tempData.length > 0
    ? Math.round((temperatureSummary.inRange / tempData.length) * 100)
    : null
  const temperatureStats: CompactChartStat[] = tempData.length > 0
    ? [
        { label: 'Latest', value: `${tempData.at(-1)!.toFixed(1)}°`, helper: 'Most recent reading' },
        { label: 'Average', value: `${(temperatureAverage!).toFixed(1)}°`, helper: activeTab },
        {
          label: 'Observed range',
          value: `${(temperatureSummary.min).toFixed(1)}–${(temperatureSummary.max).toFixed(1)}°`,
          helper: 'Selected range',
        },
        { label: 'Readings optimal', value: `${temperatureInRange}%`, helper: '18–26°C' },
      ]
    : []
  const temperatureAxisMin = tempData.length > 0 ? Math.floor(Math.min(18, temperatureSummary.min) - 1) : 18
  const temperatureAxisMax = tempData.length > 0 ? Math.ceil(Math.max(26, temperatureSummary.max) + 1) : 34
  const doseChartStats: CompactChartStat[] = doseEntries.length > 0
    ? (() => {
        const totalDose = doseEntries.reduce((sum, entry) => sum + Number(entry.dose_ml ?? 0), 0)
        const largest = doseEntries.reduce((maxEntry, entry) => (
          Number(entry.dose_ml ?? 0) > Number(maxEntry.dose_ml ?? 0) ? entry : maxEntry
        ), doseEntries[0])
        const latestDose = doseEntries[doseEntries.length - 1]
        return [
          { label: 'Events', value: String(doseEntries.length), helper: activeTab },
          { label: 'Total logged', value: formatDoseValue(totalDose), helper: 'Selected range' },
          {
            label: 'Largest cycle',
            value: formatDoseValue(largest.dose_ml, largest.pump_activated),
            helper: formatPumpName(largest.pump_activated),
          },
          {
            label: 'Latest dose',
            value: formatDoseValue(latestDose.dose_ml, latestDose.pump_activated),
            helper: latestDose.timestamp
              ? axisTimestamp(latestDose.timestamp, useDateAxis)
              : formatPumpName(latestDose.pump_activated),
          },
        ]
      })()
    : hasDisplayedDose
      ? (() => {
          const dosePoints = chartDoseData
            .map((value, index) => ({ value, index, event: chartDoseEvents[index] }))
            .filter((point) => point.value > 0)
          const totalDose = dosePoints.reduce((sum, point) => sum + point.value, 0)
          const largest = dosePoints.reduce((maxPoint, point) => (
            point.value > maxPoint.value ? point : maxPoint
          ), dosePoints[0])
          const latestDose = dosePoints[dosePoints.length - 1]
          return [
            { label: 'Events', value: String(dosePoints.length), helper: hasLiveConnection ? activeTab : 'Preview data' },
            { label: 'Total shown', value: formatDoseValue(totalDose), helper: 'Displayed bars' },
            {
              label: 'Largest cycle',
              value: largest.event?.doseLabel ?? formatDoseMl(largest.value),
              helper: largest.event?.pump ?? 'Recorded dose',
            },
            {
              label: 'Latest shown',
              value: latestDose.event?.doseLabel ?? formatDoseMl(latestDose.value),
              helper: chartDoseTimestamps?.[latestDose.index]
                ? axisTimestamp(chartDoseTimestamps[latestDose.index], useDateAxis)
                : 'Most recent bar',
            },
          ]
        })()
      : []

  return (
    <section className="page-content trends-page">
      <div className="tabs" role="group" aria-label="Trend time range">
        {TABS.map((tab) => (
          <button
            key={tab}
            type="button"
            className={tab === activeTab ? 'active' : ''}
            aria-pressed={tab === activeTab}
            disabled={tab === 'Crop cycle' && !systemSettings?.crop_transplant_date}
            title={tab === 'Crop cycle' && !systemSettings?.crop_transplant_date
              ? 'Set the transplant date in Settings to enable the crop-cycle view.'
              : undefined}
            onClick={() => setActiveTab(tab)}
          >
            {tab}
          </button>
        ))}
      </div>

      {noData && (
        <p className="theme-muted-note">
          No readings in the selected time range. Try a wider window.
        </p>
      )}
      {hasOnlyMaintenanceData && (
        <p className="theme-muted-note">
          No chart-eligible readings in this range. Maintenance readings are summarized below.
        </p>
      )}
      {rangeLoading && (
        <div className="trend-range-loading" role="status" aria-live="polite">
          <LoaderCircle className="trend-range-loading-spinner" size={20} aria-hidden="true" />
          <div>
            <strong>Loading complete {activeTab.toLowerCase()} history</strong>
            <span>Recent cached readings remain visible while the full range is fetched.</span>
          </div>
        </div>
      )}
      {rangeRefreshing && (
        <div className="trend-range-loading refreshing" role="status" aria-live="polite">
          <LoaderCircle className="trend-range-loading-spinner" size={18} aria-hidden="true" />
          <div>
            <strong>Updating latest {activeTab.toLowerCase()} history</strong>
            <span>Cached readings remain visible until the refresh is complete.</span>
          </div>
        </div>
      )}
      {rangeLoadFailed && (
        <div className="trend-range-error" role="alert">
          <span>
            {hasRequestedRange
              ? `The latest ${activeTab.toLowerCase()} refresh failed. Showing the previously loaded history.`
              : `The complete ${activeTab.toLowerCase()} history could not be loaded. Showing cached recent readings only.`}
          </span>
          <button
            type="button"
            className="trend-range-retry"
            onClick={() => setRangeRetryVersion((version) => version + 1)}
          >
            <RefreshCw size={16} aria-hidden="true" />
            Retry
          </button>
        </div>
      )}
      {rangeMayBeTruncated && (
        <p className="theme-muted-note warn" role="status">
          This range reached the 100,000-reading retrieval limit. The earliest readings may not be included.
        </p>
      )}
      {(maintenanceHistory.length > 0 || safetyHistory.length > 0 || monitoringHistory.length > 0) && (
        <section className="trend-data-notes" aria-label="Chart data handling">
          {maintenanceHistory.length > 0 && (
            <aside className="trend-maintenance-note" role="note" aria-label="Maintenance activity recorded">
              <span className="trend-maintenance-icon" aria-hidden="true"><Wrench size={16} /></span>
              <div>
                <strong>Maintenance excluded</strong>
                <p>{maintenanceHistory.length} {maintenanceHistory.length === 1 ? 'sample' : 'samples'} retained in System Log</p>
                {maintenanceLabel && (
                  <div className="trend-maintenance-time">
                    <time>{maintenanceLabel}</time>
                    {earlierMaintenanceWindowCount > 0 && (
                      <span className="trend-maintenance-history-trigger">
                        <span aria-hidden="true"> · </span>
                        <button
                          type="button"
                          aria-describedby="earlier-maintenance-windows"
                        >
                          +{earlierMaintenanceWindowCount} earlier
                        </button>
                        <span
                          id="earlier-maintenance-windows"
                          className="trend-maintenance-tooltip"
                          role="tooltip"
                        >
                          <strong>Earlier maintenance windows</strong>
                          <span className="trend-maintenance-tooltip-list">
                            {earlierMaintenanceWindows.map((window) => (
                              <span key={`${window.startedAt}-${window.endedAt}`}>
                                <b>{maintenanceRangeLabel(window)}</b>
                                <em>{window.readingCount} {window.readingCount === 1 ? 'sample' : 'samples'}</em>
                              </span>
                            ))}
                          </span>
                        </span>
                      </span>
                    )}
                  </div>
                )}
              </div>
            </aside>
          )}
          {safetyHistory.length > 0 && (
            <aside className="trend-maintenance-note safety" role="note" aria-label="Emergency Stop activity excluded">
              <span className="trend-maintenance-icon" aria-hidden="true"><ShieldAlert size={16} /></span>
              <div>
                <strong>Emergency Stop excluded</strong>
                <p>
                  {safetyHistory.length} safety {safetyHistory.length === 1 ? 'sample' : 'samples'} retained in System Log.
                  {' '}The interrupted cycle is not counted twice.
                </p>
              </div>
            </aside>
          )}
          {monitoringHistory.length > 0 && (
            <aside className="trend-maintenance-note monitoring" role="note" aria-label="Monitoring Only readings included">
              <span className="trend-maintenance-icon" aria-hidden="true"><Activity size={16} /></span>
              <div>
                <strong>Monitoring included</strong>
                <p>{monitoringHistory.length} live readings count toward charts. Automatic dosing was paused.</p>
              </div>
            </aside>
          )}
        </section>
      )}

      <div className="grid-12 trends-stats-grid">
        <StatCard value={stats.phInRange} label={'pH in range'} detail={complianceDetail} delta={hasLiveConnection ? '' : '+5% vs yesterday'} tone="good" onClick={() => setDrilldown({
          title: activeTab,
          metric: 'pH in target range',
          value: stats.phInRange,
          unit: 'of readings',
          status: 'Target compliance',
          statusTone: 'good',
          target: `${phTarget.min} - ${phTarget.max} pH`,
          detail: 'This summarizes how often pH stayed inside the configured control band for the selected window.',
        })} />
        <StatCard value={stats.ecInRange} label={'EC in range'} detail={complianceDetail} delta={hasLiveConnection ? '' : '−3% vs yesterday'} tone={'warn'} onClick={() => setDrilldown({
          title: activeTab,
          metric: 'EC in target range',
          value: stats.ecInRange,
          unit: 'of readings',
          status: 'Target compliance',
          statusTone: stats.ecInRange === '100%' ? 'good' : 'warn',
          target: `${ecTarget.min} - ${ecTarget.max} mS/cm`,
          detail: 'This summarizes how often EC stayed inside the nutrient concentration band for the selected window.',
        })} />
        <StatCard value={stats.dosingCount} label="Dosing events" detail={dosingDetail} delta="" tone="info" onClick={() => setDrilldown({
          title: activeTab,
          metric: 'Dosing events',
          value: stats.dosingCount,
          unit: 'events',
          status: 'Recorded activity',
          statusTone: 'info',
          detail: 'This counts cycles with a recorded dose. Click individual dose bars below to inspect each event.',
        })} />
        <StatCard value={averageResponseCompactLabel} label={'Avg response'} detail={averageResponse ? `${averageResponse.pairCount} completed ${averageResponse.pairCount === 1 ? 'correction' : 'corrections'}` : 'Needs paired correction data'} delta="" tone="neutral" onClick={() => setDrilldown({
          title: activeTab,
          metric: 'Average response',
          value: averageResponse ? averageResponseLabel : 'Pending',
          unit: averageResponse ? 'average' : 'calculation',
          status: averageResponse ? `${averageResponse.pairCount} verified corrections` : 'Needs paired correction data',
          statusTone: 'neutral',
          detail: averageResponse
              ? 'This measures elapsed time from each completed dose record to the first later reading inside that metric\'s configured target. Interrupted Emergency Stop cycles are excluded.'
              : 'This can be calculated once enough paired dose and post-mix in-range readings are available.',
        })} />
      </div>

      <HistoryChart
        title="pH trend"
        timeframe={chartTimeframe}
        data={chartPhData} min={4.8} max={7.4}
        target={phTarget} eventIndexes={chartPhDoseIndexes}
        eventSummaries={chartPhDoseSummaries}
        supportingRows={chartPhSupportingRows}
        kind="ph" unit="pH" timestamps={chartTimestamps} useDateAxis={useDateAxis}
        onDrilldown={setDrilldown}
      />
      <HistoryChart
        title="EC trend"
        timeframe={chartTimeframe}
        data={chartEcData} min={0.6} max={ecAxisMax}
        target={ecTarget}
        eventIndexes={chartEcDoseIndexes}
        eventSummaries={chartEcDoseSummaries}
        supportingRows={chartEcSupportingRows}
        kind="ec" unit="mS/cm" timestamps={chartTimestamps} useDateAxis={useDateAxis}
        onDrilldown={setDrilldown}
      />

      <div className="grid-12">
        <CompactChart
          title="Water temperature (°C)" unit="°C"
          data={chartTempData} min={temperatureAxisMin} max={temperatureAxisMax} color="var(--data-blue)"
          footer={
            chartTempData.at(-1) != null && chartTempData.at(-1)! > 26
              ? 'Above optimal temperature. Consider aeration or cooling before dosing changes.'
              : chartTempData.at(-1) != null && chartTempData.at(-1)! < 18
                ? 'Below optimal temperature. Check the heater and water circulation before dosing changes.'
                : 'Temperature is within the preferred operating window.'
          }
          timestamps={chartTempTimestamps}
          useDateAxis={useDateAxis}
          onDrilldown={setDrilldown}
          mode="line"
          targetBand={{ min: 18, max: 26, label: 'Optimal range' }}
          stats={temperatureStats}
        />
        <CompactChart
          title="Dose volume by event (mL)" unit="mL"
          data={chartDoseData} min={0} max={chartDoseMax} color="var(--primary)"
          footer={hasLiveConnection ? 'Every recorded dosing event in the selected range' : 'EC Up values show the per-component dose (A = B)'}
          timestamps={chartDoseTimestamps}
          useDateAxis={useDateAxis}
          onDrilldown={setDrilldown}
          doseEvents={chartDoseEvents}
          stats={doseChartStats}
          emptyState={hasDisplayedDose ? undefined : {
            title: 'No dosing in this range',
            detail: 'Pump activity will appear here when HANAS applies a correction.',
          }}
        />
      </div>
      <DrilldownDrawer detail={drilldown} onClose={() => setDrilldown(null)} />
    </section>
  )
}
