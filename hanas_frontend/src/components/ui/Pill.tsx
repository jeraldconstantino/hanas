import type { Tone } from '../../types'
import { formatDisplayText } from '../../text'

export function Pill({ label, tone }: { label: string; tone: Tone }) {
  return (
    <span className={`pill ${tone}`} data-label={label}>
      <span className="pill-label">{formatDisplayText(label)}</span>
    </span>
  )
}
