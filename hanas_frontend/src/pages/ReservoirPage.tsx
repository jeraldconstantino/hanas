import { isReadingStale, recordedDate } from '../readingFreshness'
import { useEffect, useMemo, useState, type CSSProperties } from 'react'
import { TrendingUp } from 'lucide-react'
import type { LatestLog, Page, Tone } from '../types'
import { clamp, deviationLabel, formatDuration, phTone, ecTone, phStatus, ecStatus } from '../utils'
import { Pill } from '../components/ui/Pill'
import { Panel } from '../components/ui/Panel'
import { InfoCell } from '../components/ui/InfoCell'
import { nextReadingCountdown } from '../readingCountdown'

function GaugeCard({
  title,
  value,
  unit,
  min,
  max,
  target,
  tone,
  deviation,
  statusLabel,
}: {
  title: string
  value: number
  unit: string
  min: number
  max: number
  target: { min: number; max: number }
  tone: Tone
  deviation: string
  statusLabel: string
}) {
  const percent = clamp(((value - min) / (max - min)) * 100, 0, 100)
  const targetLeft = clamp(((target.min - min) / (max - min)) * 100, 0, 100)
  const targetRight = clamp(((target.max - min) / (max - min)) * 100, 0, 100)
  const targetMid = (target.min + target.max) / 2
  const distanceFromCenter = Math.abs(value - targetMid)
  const targetHalfWidth = (target.max - target.min) / 2
  const positionLabel = value < target.min ? 'Below Target' : value > target.max ? 'Above Target' : 'Within Range'
  const isWithinTarget = value >= target.min && value <= target.max
  const closenessLabel =
    isWithinTarget
      ? distanceFromCenter <= targetHalfWidth * 0.35
        ? 'Centered'
        : 'Near Edge'
      : 'Correction Zone'
  const pillLabel = isWithinTarget ? 'Within Range' : statusLabel

  return (
    <article className={`gauge-card span-4 ${tone}`} aria-label={`${title} reading ${value.toFixed(2)} ${unit}`}>
      <div className="gauge-card-top">
        <span className="metric-label">{title}</span>
        <Pill label={pillLabel} tone={tone} />
      </div>
      <div className="gauge-reading">
        <strong>{value.toFixed(2)}</strong>
        <div>
          <span>{unit}</span>
          {!isWithinTarget && <b className="gauge-deviation-default">{deviation}</b>}
          {!isWithinTarget && (
            <b className="gauge-deviation-pipeline">
              {deviation.replace(' above max', ' above target maximum').replace(' below min', ' below target minimum')}
            </b>
          )}
        </div>
      </div>
      <div className="gauge-range">
        <div
          className="range-meter-track threshold-track"
          aria-label={`${title} scale from ${min} to ${max}. Target range ${target.min} to ${target.max}. Current ${value.toFixed(2)} ${unit}.`}
        >
          <span className="threshold-zone low-zone" style={{ left: 0, width: `${targetLeft}%` }} />
          <span className="threshold-zone target-zone" style={{ left: `${targetLeft}%`, width: `${targetRight - targetLeft}%` }} />
          <span className="threshold-zone high-zone" style={{ left: `${targetRight}%`, width: `${100 - targetRight}%` }} />
          <b
            className="current-marker"
            style={{ left: `${percent}%` }}
            tabIndex={0}
            aria-label={`Current ${title} reading is ${value.toFixed(2)} ${unit}`}
          >
            <em>{value.toFixed(2)} {unit}</em>
          </b>
        </div>
        <div className="range-meter-scale">
          <span>{min}</span>
          <span>{max}</span>
        </div>
      </div>
      <div className="gauge-detail-grid">
        <div>
          <span>Position</span>
          <strong>{positionLabel}</strong>
        </div>
        <div>
          <span>Target window</span>
          <strong>{target.min} - {target.max}</strong>
        </div>
        <div>
          <span>Center point</span>
          <strong>{targetMid.toFixed(title === 'pH' ? 1 : 2)}</strong>
        </div>
        <div>
          <span>Reading state</span>
          <strong>{closenessLabel}</strong>
        </div>
      </div>
    </article>
  )
}

function TempLevelPanel({
  latestLog,
  reservoirMaxLiters,
}: {
  latestLog: LatestLog
  reservoirMaxLiters: number
}) {
  const tempPct = clamp((latestLog.temperature / 42) * 100, 0, 100)
  const resoPct = Math.round((latestLog.reservoir_volume_liters / reservoirMaxLiters) * 100)
  const visualLevelPct = clamp(resoPct, 0, 100)
  const reservoirOverLimit = latestLog.reservoir_volume_liters > reservoirMaxLiters

  return (
    <article className="temp-level-panel span-4">
      <div>
        <div className="inline-header">
          <span>Water Temperature</span>
        </div>
        <strong className="temp-value">{latestLog.temperature.toFixed(1)}°C</strong>
        <div className="thermo-bar">
          <span className="cold" />
          <span className="optimal" />
          <span className="warm" />
          <b
            className="current-marker temp-marker"
            style={{ left: `${tempPct}%` }}
            tabIndex={0}
            aria-label={`Current Water Temperature is ${latestLog.temperature.toFixed(1)} degrees Celsius`}
          >
            <em>{latestLog.temperature.toFixed(1)}°C</em>
          </b>
        </div>
        <div className="scale-labels">
          <span>0°C</span>
          <span>18 - 26°C Optimal</span>
          <span>42°C</span>
        </div>
        <p>
          {latestLog.temperature > 26
            ? 'Higher water temperature reduces dissolved oxygen. Consider aerating the reservoir.'
            : latestLog.temperature < 16
              ? 'Low water temperature may slow nutrient uptake.'
              : 'Water Temperature is within the optimal range.'}
        </p>
      </div>
      <hr />
      <div>
        <div className="inline-header">
          <span>Reservoir Volume</span>
        </div>
        <div
          className="tank-visual"
          role="img"
          aria-label={`Reservoir volume ${latestLog.reservoir_volume_liters.toFixed(1)} liters, ${resoPct}% of ${reservoirMaxLiters} liters`}
          style={{ '--level': `${visualLevelPct}%` } as CSSProperties}
        >
          <div className="tank-water">
            <span className="tank-wave wave-a" />
            <span className="tank-wave wave-b" />
            <span className="tank-shimmer" />
            <span className="tank-bubble bubble-a" />
            <span className="tank-bubble bubble-b" />
          </div>
          <strong>{resoPct}%</strong>
        </div>
        <strong>{latestLog.reservoir_volume_liters.toFixed(1)} L of {reservoirMaxLiters} L ({resoPct}%)</strong>
        <p>
          {reservoirOverLimit
            ? `Above configured ${reservoirMaxLiters} L operating volume. Check fill level and water-level sensor calibration.`
            : 'Level sensor · 4–20 mA input'}
        </p>
      </div>
    </article>
  )
}

function ReadingMeta({ latestLog }: { latestLog: LatestLog }) {
  const stableLabel = (seconds: number | null) => {
    if (seconds == null) return 'Not reported'
    return formatDuration(seconds)
  }
  const items: Array<[string, string]> = [
    ['Last reading', recordedDate(latestLog.timestamp)],
    ['pH stable for', stableLabel(latestLog.ph_stable_for_seconds)],
    ['EC stable for', stableLabel(latestLog.ec_stable_for_seconds)],
    ['Threshold', '±0.03'],
    ['Reservoir volume', `${latestLog.reservoir_volume_liters.toFixed(1)} L`],
  ]
  return (
    <Panel title="" eyebrow="" className="span-8 reading-meta">
      {items.map(([label, value]) => (
        <InfoCell key={label} label={label} value={value} />
      ))}
    </Panel>
  )
}

function CountdownPanel({
  latestTimestamp,
  referenceTime,
}: {
  latestTimestamp: string
  referenceTime?: string
}) {
  const samplingSeconds = 60
  const [nowMs, setNowMs] = useState(() => Date.now())
  const latestMs = useMemo(() => new Date(latestTimestamp).getTime(), [latestTimestamp])
  const referenceMs = referenceTime ? new Date(referenceTime).getTime() : nowMs
  const { remainingSeconds, progress } = nextReadingCountdown(latestMs, referenceMs, samplingSeconds)
  const label = `${Math.floor(remainingSeconds / 60)}:${String(remainingSeconds % 60).padStart(2, '0')}`

  useEffect(() => {
    if (referenceTime) return
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [referenceTime])

  if (isReadingStale(latestTimestamp, referenceMs)) {
    return <article className="countdown-panel span-4"><span>Sensor updates overdue</span><strong>Awaiting reading</strong><p>Last recorded: {recordedDate(latestTimestamp)}. No next-reading time is confirmed.</p></article>
  }
  return (
    <article
      className="countdown-panel span-4"
      style={{ '--reading-progress': `${progress}%` } as CSSProperties}
    >
      <span>Next reading in</span>
      <strong>{label}</strong>
      <div
        className="small-ring"
        aria-hidden="true"
        title={`${Math.round(progress)}% of the reading interval elapsed`}
      />
      <p>Estimated from the recurring 60-second sampling interval</p>
    </article>
  )
}

function SignalStrip({
  onNavigate,
  className = '',
}: {
  onNavigate?: (page: Page) => void
  className?: string
}) {
  return (
    <section className={`signal-strip ${className}`.trim()}>
      <strong>Sensor Signal Quality</strong>
      {['pH raw signal', 'EC raw signal', 'Temperature', 'Water Level'].map((label, index) => (
        <div className="signal-bars" key={label}>
          <span className="metric-label">{label}</span>
          <span className="signal-strength" aria-hidden="true">
            {[8, 12, 10, 11, 9].map((height, barIndex) => (
              <i key={barIndex} style={{ height: `${height + index}px` }} />
            ))}
          </span>
        </div>
      ))}
      <Pill label="ADS1115 stable" tone="good" />
      <div className="signal-strip-actions">
        <span className="sample-note">20 samples averaged per reading</span>
        {onNavigate && (
          <button type="button" onClick={() => onNavigate('Trends')}>
            <TrendingUp size={15} strokeWidth={2.2} />
            <span>View trends</span>
          </button>
        )}
      </div>
    </section>
  )
}

export function ReservoirPage({
  latestLog,
  reservoirMaxLiters,
  phTarget,
  ecTarget,
  onNavigate,
  referenceTime,
}: {
  latestLog: LatestLog
  reservoirMaxLiters: number
  phTarget: { min: number; max: number }
  ecTarget: { min: number; max: number }
  onNavigate?: (page: Page) => void
  referenceTime?: string
}) {
  return (
    <section className="page-content reservoir-page">
      <div className="reservoir-layout">
        <div className="reservoir-left">
          <GaugeCard
            title="pH"
            value={latestLog.ph}
            unit="pH"
            min={0}
            max={14}
            target={phTarget}
            tone={phTone(latestLog.ph, phTarget)}
            deviation={deviationLabel(latestLog.ph, latestLog.ph_deviation, phTarget)}
            statusLabel={phStatus(latestLog.ph, phTarget)}
          />
          <GaugeCard
            title="EC"
            value={latestLog.ec}
            unit="mS/cm"
            min={0}
            max={4}
            target={ecTarget}
            tone={ecTone(latestLog.ec, ecTarget)}
            deviation={deviationLabel(latestLog.ec, latestLog.ec_deviation, ecTarget, ' mS/cm')}
            statusLabel={ecStatus(latestLog.ec, ecTarget)}
          />
          <ReadingMeta latestLog={latestLog} />
          {!isReadingStale(latestLog.timestamp) && <SignalStrip className="signal-strip-desktop" onNavigate={onNavigate} />}
        </div>

        <div className="reservoir-right">
          <TempLevelPanel latestLog={latestLog} reservoirMaxLiters={reservoirMaxLiters} />
          <CountdownPanel latestTimestamp={latestLog.timestamp} referenceTime={referenceTime} />
        </div>

        {!isReadingStale(latestLog.timestamp) && <SignalStrip className="signal-strip-compact" onNavigate={onNavigate} />}
      </div>
    </section>
  )
}
