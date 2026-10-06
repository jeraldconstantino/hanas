import { useState } from 'react'
import { AlertTriangle, Radio, X } from 'lucide-react'
import type { Tone } from '../../types'

export function AlertBanner({
  title,
  body,
  tone,
  compact = false,
  actions,
  time,
  onDismiss,
}: {
  title: string
  body: string
  tone: Tone
  compact?: boolean
  actions?: boolean
  time?: string
  onDismiss?: () => void
}) {
  const [dismissed, setDismissed] = useState(false)

  if (dismissed) return null

  function handleDismiss() {
    setDismissed(true)
    onDismiss?.()
  }

  return (
    <section className={`alert-banner ${tone} ${compact ? 'compact' : ''} ${actions ? 'dismissible' : ''}`}>
      <div className="alert-icon">
        <AlertTriangle size={18} strokeWidth={2.2} />
      </div>
      <div className="alert-text">
        <strong>{title}</strong>
        <p>{body}</p>
      </div>
      {actions && (
        <div className="alert-actions">
          <button type="button" className="alert-btn" onClick={handleDismiss} aria-label="Dismiss alert" title="Dismiss alert">
            <X size={12} strokeWidth={2.6} aria-hidden="true" />
          </button>
        </div>
      )}
      {(compact || time) && (
        <span className={`alert-time ${compact ? 'ongoing' : ''}`}>
          {compact && <Radio size={13} strokeWidth={2.2} />}
          {compact ? 'Ongoing' : time}
        </span>
      )}
    </section>
  )
}
