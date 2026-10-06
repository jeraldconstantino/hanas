import { type CSSProperties, useCallback, useState, useEffect, useMemo, useRef } from 'react'
import { createPortal } from 'react-dom'
import { Bell, CheckCircle2, XCircle, Clock, X, CheckCheck } from 'lucide-react'
import type { NotificationLogEntry, Page } from '../../types'
import { formatDateTime } from '../../utils'
import {
  isRecentNotification,
  notificationPreview,
  notificationTitle,
  RECENT_ALERT_WINDOW_LABEL,
} from '../../notifications'
import { safeLocalStorage, safeSessionStorage } from '../../storage'

const STATUS_ICON = {
  submitted:   <Clock        size={14} className="notif-status-icon suppressed" />,
  pending:     <Clock        size={14} className="notif-status-icon suppressed" />,
  queued:      <Clock        size={14} className="notif-status-icon suppressed" />,
  sent:        <CheckCircle2 size={14} className="notif-status-icon sent" />,
  failed:      <XCircle      size={14} className="notif-status-icon failed" />,
  refunded:    <XCircle      size={14} className="notif-status-icon failed" />,
  suppressed:  <Clock        size={14} className="notif-status-icon suppressed" />,
  disabled:    <Clock        size={14} className="notif-status-icon disabled" />,
}

const READ_STORAGE_KEY = 'hanas_read_notification_ids'
const SELECTED_STORAGE_KEY = 'hanas_selected_notification_id'

function readStoredIds(): number[] {
  try {
    const parsed = JSON.parse(safeLocalStorage.getItem(READ_STORAGE_KEY) ?? '[]')
    return Array.isArray(parsed) ? parsed.filter((id) => Number.isFinite(id)) : []
  } catch {
    return []
  }
}

export function NotificationBell({
  logs,
  onNavigate,
  referenceNow,
  persistReadState = true,
  initiallyReadIds = [],
}: {
  logs: NotificationLogEntry[]
  onNavigate: (page: Page) => void
  referenceNow: number
  persistReadState?: boolean
  initiallyReadIds?: number[]
}) {
  const [open, setOpen] = useState(false)
  const [dropdownStyle, setDropdownStyle] = useState<CSSProperties | undefined>()
  const [readIds, setReadIds] = useState<number[]>(() => (
    persistReadState ? readStoredIds() : initiallyReadIds
  ))
  const ref = useRef<HTMLDivElement>(null)
  const dropdownRef = useRef<HTMLDivElement>(null)

  const recentLogs = useMemo(
    () => logs.filter((n) => isRecentNotification(n, referenceNow)),
    [logs, referenceNow],
  )
  const visibleLogs = useMemo(() => recentLogs.slice(0, 8), [recentLogs])
  const unreadLogs = useMemo(
    () => recentLogs.filter((n) => !readIds.includes(n.id)),
    [recentLogs, readIds],
  )
  const badgeCount = unreadLogs.length

  const persistReadIds = useCallback((nextIds: number[]) => {
    const bounded = Array.from(new Set(nextIds)).slice(-100)
    setReadIds(bounded)
    if (persistReadState) safeLocalStorage.setItem(READ_STORAGE_KEY, JSON.stringify(bounded))
  }, [persistReadState])

  const markAllAsRead = useCallback(() => {
    if (recentLogs.length === 0) return
    persistReadIds([...readIds, ...recentLogs.map((n) => n.id)])
  }, [persistReadIds, readIds, recentLogs])

  const closeDropdown = useCallback(() => {
    markAllAsRead()
    setOpen(false)
  }, [markAllAsRead])

  const updateDropdownPosition = useCallback(() => {
    if (!ref.current || !window.matchMedia('(max-width: 900px)').matches) {
      setDropdownStyle(undefined)
      return
    }

    const button = ref.current.querySelector<HTMLButtonElement>('.notif-bell-btn')
    if (!button) return

    const rect = button.getBoundingClientRect()
    const viewportWidth = window.innerWidth
    const viewportHeight = window.innerHeight
    const top = Math.min(rect.bottom + 10, Math.max(16, viewportHeight - 180))
    const maxHeight = Math.max(220, viewportHeight - top - 16)

    if (viewportWidth <= 760) {
      const inset = 12
      setDropdownStyle({
        position: 'fixed',
        zIndex: 1200,
        top,
        left: inset,
        right: inset,
        width: 'auto',
        maxHeight,
      })
      return
    }

    const width = Math.min(380, viewportWidth - 28)
    const left = Math.min(
      Math.max(14, rect.right - width),
      Math.max(14, viewportWidth - width - 14),
    )
    setDropdownStyle({
      position: 'fixed',
      zIndex: 1200,
      top,
      left,
      width,
      maxHeight,
    })
  }, [])

  function openNotification(log: NotificationLogEntry) {
    persistReadIds([...readIds, log.id])
    safeSessionStorage.setItem(SELECTED_STORAGE_KEY, String(log.id))
    setOpen(false)
    onNavigate('Logs & Alerts')
  }

  // Close on outside click or tap
  useEffect(() => {
    if (!open) return
    updateDropdownPosition()

    function handle(e: PointerEvent) {
      if (!(e.target instanceof Node)) return
      const clickedBell = ref.current?.contains(e.target)
      const clickedDropdown = dropdownRef.current?.contains(e.target)
      if (!clickedBell && !clickedDropdown) {
        closeDropdown()
      }
    }
    function handleViewportChange() {
      updateDropdownPosition()
    }
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') closeDropdown()
    }

    document.addEventListener('pointerdown', handle)
    document.addEventListener('keydown', handleKeyDown)
    window.addEventListener('resize', handleViewportChange)
    window.addEventListener('scroll', handleViewportChange, true)
    return () => {
      document.removeEventListener('pointerdown', handle)
      document.removeEventListener('keydown', handleKeyDown)
      window.removeEventListener('resize', handleViewportChange)
      window.removeEventListener('scroll', handleViewportChange, true)
    }
  }, [open, closeDropdown, updateDropdownPosition])

  const dropdown = open ? (
    <div
      className="notif-dropdown"
      role="dialog"
      aria-label="Notifications"
      style={dropdownStyle}
      ref={dropdownRef}
    >
      <div className="notif-dropdown-header">
        <div>
          <strong>Notifications</strong>
          <span>{badgeCount > 0 ? `${badgeCount} unread, ${RECENT_ALERT_WINDOW_LABEL}` : 'All caught up'}</span>
        </div>
        {recentLogs.length > 0 && (
          <button
            type="button"
            className="notif-mark-read-btn"
            onClick={markAllAsRead}
            aria-label="Mark all notifications as read"
            data-tooltip="Mark all as read"
          >
            <CheckCheck size={14} />
            <span>Mark all as read</span>
          </button>
        )}
        <button
          type="button"
          className="notif-close-btn"
          onClick={closeDropdown}
          aria-label="Close notifications"
        >
          <X size={14} />
        </button>
      </div>

      <div className="notif-dropdown-body">
        {visibleLogs.length === 0 ? (
          <div className="notif-empty">
            <Bell size={24} strokeWidth={1.5} />
            <p>No recent alerts</p>
            <span>Alerts from the {RECENT_ALERT_WINDOW_LABEL} will appear here</span>
          </div>
        ) : (
          visibleLogs.map((n) => (
            <button
              type="button"
              key={n.id}
              className={`notif-item notif-item-${n.status} ${readIds.includes(n.id) ? 'read' : 'unread'}`}
              onClick={() => openNotification(n)}
            >
              {STATUS_ICON[n.status as keyof typeof STATUS_ICON] ?? STATUS_ICON.disabled}
              <div className="notif-item-content">
                <strong>{notificationTitle(n)}</strong>
                <span className="notif-item-preview">
                  {notificationPreview(n)}
                </span>
                <time className="notif-item-time" dateTime={n.timestamp}>
                  {formatDateTime(n.timestamp)}
                </time>
              </div>
            </button>
          ))
        )}
      </div>

      {logs.length > 0 && (
        <button
          type="button"
          className="notif-view-all"
          onClick={() => { closeDropdown(); onNavigate('Logs & Alerts') }}
        >
          View all in Logs & Alerts
        </button>
      )}
    </div>
  ) : null

  return (
    <div className="notif-bell-wrap" ref={ref}>
      <button
        type="button"
        className={`notif-bell-btn ${open ? 'active' : ''}`}
        onClick={() => { if (open) closeDropdown(); else setOpen(true) }}
        aria-label={`Notifications${badgeCount > 0 ? `, ${badgeCount} unread alerts from the ${RECENT_ALERT_WINDOW_LABEL}` : ''}`}
        title="Notifications"
      >
        <Bell size={16} strokeWidth={2} />
        {badgeCount > 0 && (
          <span className="notif-badge">{badgeCount > 9 ? '9+' : badgeCount}</span>
        )}
      </button>

      {dropdownStyle && dropdown ? createPortal(dropdown, document.body) : dropdown}
    </div>
  )
}
