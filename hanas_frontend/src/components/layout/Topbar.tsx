import { Bot, Clock3, PanelRightClose, PanelRightOpen } from 'lucide-react'
import type { Tone, ConnectionState, NotificationLogEntry, Page } from '../../types'
import { Pill } from '../ui/Pill'
import { NotificationBell } from '../ui/NotificationBell'
import { formatTime } from '../../utils'

export type PipelineAction = {
  isOpen: boolean
  isLive: boolean
  shouldNudge?: boolean
  onToggle: () => void
}

export function HeaderQuickActions({
  lastUpdated,
  notificationLogs,
  onNavigate,
  pipelineAction,
  notificationResetKey,
  persistNotificationReads = true,
  initiallyReadNotificationIds,
  className = 'topbar-quick-actions',
}: {
  lastUpdated: string
  notificationLogs: NotificationLogEntry[]
  onNavigate: (page: Page) => void
  pipelineAction?: PipelineAction
  notificationResetKey?: string
  persistNotificationReads?: boolean
  initiallyReadNotificationIds?: number[]
  className?: string
}) {
  const now = Date.parse(lastUpdated) || 0

  return (
    <div className={className}>
      <NotificationBell
        key={notificationResetKey}
        logs={notificationLogs}
        onNavigate={onNavigate}
        referenceNow={now}
        persistReadState={persistNotificationReads}
        initiallyReadIds={initiallyReadNotificationIds}
      />
      {pipelineAction && (
        <button
          type="button"
          className={`topbar-pipeline-btn ${pipelineAction.isOpen ? 'open' : ''} ${pipelineAction.isLive ? 'live' : ''} ${pipelineAction.shouldNudge ? 'nudge' : ''}`}
          onClick={pipelineAction.onToggle}
          aria-pressed={pipelineAction.isOpen}
          aria-label={pipelineAction.isOpen ? 'Hide AI Pipeline panel' : 'Show AI Pipeline panel'}
          title={pipelineAction.isOpen ? 'Hide AI Pipeline' : 'Show AI Pipeline'}
        >
          <Bot size={15} strokeWidth={2.2} />
          <span>AI Pipeline</span>
          {pipelineAction.isLive ? <i>Live</i> : null}
          {pipelineAction.isOpen
            ? <PanelRightClose size={14} strokeWidth={2.2} />
            : <PanelRightOpen size={14} strokeWidth={2.2} />}
        </button>
      )}
    </div>
  )
}

export function Topbar({
  eyebrow,
  title,
  pills,
  lastUpdated,
  connectionState,
  notificationLogs,
  onNavigate,
  pipelineAction,
  notificationResetKey,
  persistNotificationReads = true,
  initiallyReadNotificationIds,
}: {
  eyebrow: string
  title: string
  pills: Array<{ label: string; tone: Tone }>
  lastUpdated: string
  connectionState: ConnectionState
  notificationLogs: NotificationLogEntry[]
  onNavigate: (page: Page) => void
  pipelineAction?: PipelineAction
  notificationResetKey?: string
  persistNotificationReads?: boolean
  initiallyReadNotificationIds?: number[]
}) {
  return (
    <header className="topbar">
      {connectionState === 'connecting' && (
        <div className="topbar-progress" aria-hidden="true" />
      )}
      <div className="topbar-title-block">
        <p className="eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
        <time className="timestamp" dateTime={lastUpdated} title={`Dashboard refreshed ${formatTime(lastUpdated)}`}>
          <Clock3 size={13} strokeWidth={2.2} />
          <span>Refreshed {formatTime(lastUpdated)}</span>
        </time>
      </div>
      <div className="topbar-actions">
        {pills.map((pill) => (
          <Pill key={pill.label} label={pill.label} tone={pill.tone} />
        ))}
        <HeaderQuickActions
          lastUpdated={lastUpdated}
          notificationLogs={notificationLogs}
          onNavigate={onNavigate}
          pipelineAction={pipelineAction}
          notificationResetKey={notificationResetKey}
          persistNotificationReads={persistNotificationReads}
          initiallyReadNotificationIds={initiallyReadNotificationIds}
        />
      </div>
    </header>
  )
}
