import {
  LayoutDashboard,
  Droplets,
  Pipette,
  TrendingUp,
  Brain,
  Bell,
  Camera,
  CircleHelp,
  Settings,
  type LucideProps,
} from 'lucide-react'
import { useEffect, useRef } from 'react'
import type { Page } from '../../types'
import { pages } from '../../constants'

type IconComponent = React.ComponentType<LucideProps>

const PAGE_ICONS: Record<Page, IconComponent> = {
  'Overview':      LayoutDashboard,
  'Reservoir':     Droplets,
  'Dosing':        Pipette,
  'Trends':        TrendingUp,
  'Camera':        Camera,
  'AI Reasoning':  Brain,
  'Logs & Alerts': Bell,
  'Help':          CircleHelp,
  'Settings':      Settings,
}

const MOBILE_LABELS: Record<Page, string> = {
  'Overview':      'Overview',
  'Reservoir':     'Reservoir',
  'Dosing':        'Dosing',
  'Trends':        'Trends',
  'Camera':        'Camera',
  'AI Reasoning':  'AI',
  'Logs & Alerts': 'Alerts',
  'Help':          'Help',
  'Settings':      'Settings',
}

export function MobileNav({
  activePage,
  onNavigate,
}: {
  activePage: Page
  onNavigate: (page: Page) => void
}) {
  const buttonRefs = useRef(new Map<Page, HTMLButtonElement>())

  useEffect(() => {
    buttonRefs.current.get(activePage)?.scrollIntoView({
      block: 'nearest',
      inline: 'nearest',
      behavior: 'smooth',
    })
  }, [activePage])

  return (
    <nav className="mobile-nav" aria-label="Mobile navigation">
      <div className="mobile-nav-scroll">
        {pages.map((page) => {
          const Icon = PAGE_ICONS[page]
          const isActive = page === activePage
          return (
            <button
              key={page}
              type="button"
              className={isActive ? 'active' : ''}
              aria-current={isActive ? 'page' : undefined}
              aria-label={page}
              title={page}
              ref={(node) => {
                if (node) {
                  buttonRefs.current.set(page, node)
                } else {
                  buttonRefs.current.delete(page)
                }
              }}
              onClick={() => onNavigate(page)}
            >
              <Icon size={18} strokeWidth={2.2} />
              <span>{MOBILE_LABELS[page]}</span>
            </button>
          )
        })}
      </div>
    </nav>
  )
}
