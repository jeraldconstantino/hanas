import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  CheckCircle2,
  Info,
  Loader2,
  Pause,
  RotateCcw,
  ShieldCheck,
} from 'lucide-react'
import type { Tone } from '../../types'
import { Pill } from './Pill'

function HeroIcon({ icon, tone }: { icon: string; tone: Tone }) {
  const props = { size: 25, strokeWidth: 2.25 }
  if (icon === 'shield-check') return <ShieldCheck {...props} />
  if (icon === '✓') return <CheckCircle2 {...props} />
  if (icon === '⏸') return <Pause {...props} />
  if (icon === '⟳') return <RotateCcw {...props} />
  if (icon === '↑') return <ArrowUp {...props} />
  if (icon === '↓') return <ArrowDown {...props} />
  if (icon === '…') return <Loader2 {...props} className="hero-icon-spin" />
  if (icon === '⚠') return <AlertTriangle {...props} />
  if (icon === 'i' || tone === 'info') return <Info {...props} />
  return <span>{icon}</span>
}

export function StatusHero({
  title,
  body,
  icon,
  pills,
  tone,
}: {
  title: string
  body: string
  icon: string
  tone: Tone
  pills: Array<{ label: string; tone: Tone }>
}) {
  const displayTitle = title.replace(/\s+[—–]\s+/g, ': ')
  const displayBody = body.replace(/\s+[—–]\s+/g, ': ')
  const hasBody = Boolean(displayBody.trim())

  return (
    <section className={`status-hero ${tone}${hasBody ? '' : ' compact'}`}>
      <div className="hero-left">
        <div className="hero-icon">
          <HeroIcon icon={icon} tone={tone} />
        </div>
        <div className="hero-copy">
          <h2>{displayTitle}</h2>
          {hasBody && <p>{displayBody}</p>}
        </div>
      </div>
      <div className="hero-pills">
        {pills.map((pill) => (
          <Pill key={pill.label} label={pill.label} tone={pill.tone} />
        ))}
      </div>
    </section>
  )
}
