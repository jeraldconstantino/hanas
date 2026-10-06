import type { ReactNode } from 'react'
import { Pill } from './Pill'

export function Panel({
  title,
  eyebrow,
  children,
  className = '',
  badge,
}: {
  title: string
  eyebrow?: string
  children: ReactNode
  className?: string
  badge?: string
}) {
  return (
    <article className={`panel ${className}`}>
      {(title || eyebrow || badge) && (
        <div className="panel-header">
          <div>
            {eyebrow && <span>{eyebrow}</span>}
            {title && <h2>{title}</h2>}
          </div>
          {badge && <Pill label={badge} tone="neutral" />}
        </div>
      )}
      {children}
    </article>
  )
}
