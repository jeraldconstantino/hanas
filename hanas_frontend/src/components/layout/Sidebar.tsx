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
import type { Page, ConnectionState } from '../../types'
import type { ReactNode } from 'react'
import { pages } from '../../constants'
import { toneForConnection } from '../../utils'

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

export function Sidebar({
  activePage,
  connectionState,
  connectionMessage,
  maintenanceMode,
  monitoringMode,
  emergencyStop,
  fullAgenticMode,
  onNavigate,
  mobileActions,
}: {
  activePage: Page
  connectionState: ConnectionState
  connectionMessage: string
  maintenanceMode: boolean
  monitoringMode: boolean
  emergencyStop: boolean
  fullAgenticMode: boolean
  onNavigate: (page: Page) => void
  mobileActions?: ReactNode
}) {
  const statusTone = emergencyStop ? 'danger' : maintenanceMode ? 'warn' : monitoringMode ? 'info' : toneForConnection(connectionState)
  const statusTitle = emergencyStop
    ? 'Emergency Stop'
    : maintenanceMode
    ? 'Maintenance Mode'
    : monitoringMode
    ? 'Monitoring Only'
    : connectionState === 'connected'
      ? 'System Online'
      : connectionState === 'connecting'
        ? 'Connecting...'
    : connectionState === 'offline'
      ? 'System Offline'
      : connectionState === 'empty'
          ? 'Waiting for First Reading'
          : 'Offline Preview'
  const statusDetail = emergencyStop
    ? 'Pump Commands Locked'
    : maintenanceMode
    ? 'Automatic Dosing Paused'
    : monitoringMode
    ? 'Live Readings • AI and Pumps Paused'
    : connectionState === 'connected'
      ? fullAgenticMode
        ? 'Full Agentic Mode • Backend connected'
        : 'Backend connected'
      : connectionState === 'offline'
        ? 'Backend Unavailable • Retrying'
      : connectionState === 'empty'
        ? 'Controller Connected • No Reading Yet'
        : connectionMessage

  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark" aria-hidden="true">
          <img className="brand-mark-light" src="/hanas-mark-light.svg" alt="" />
          <img className="brand-mark-dark" src="/hanas-mark-dark.svg" alt="" />
        </div>
        <div>
          <strong>HANAS</strong>
          <span>Nutrient Control System</span>
        </div>
      </div>

      {mobileActions && <div className="sidebar-mobile-actions">{mobileActions}</div>}

      <nav className="nav-list" aria-label="Main navigation">
        {pages.map((page) => {
          const Icon = PAGE_ICONS[page]
          return (
            <button
              key={page}
              type="button"
              className={page === activePage ? 'active' : ''}
              onClick={() => onNavigate(page)}
            >
              <span className="nav-icon">
                <Icon size={16} strokeWidth={2} />
              </span>
              <span>{page}</span>
            </button>
          )
        })}
      </nav>

      <div className={`connection-card ${emergencyStop ? 'emergency' : maintenanceMode ? 'maintenance' : monitoringMode ? 'monitoring' : fullAgenticMode ? 'agentic' : ''}`}>
        <span className={`status-dot ${statusTone}`} />
        <div>
          <strong>{statusTitle}</strong>
          <span>{statusDetail}</span>
        </div>
      </div>
    </aside>
  )
}
