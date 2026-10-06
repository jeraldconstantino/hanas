import { RadioTower, RefreshCw, Settings2, WifiOff } from 'lucide-react'

export function EmptyState({
  maintenanceMode = false,
  emergencyStop = false,
  connecting = false,
  offline = false,
  onRetry,
  onSettings,
}: {
  maintenanceMode?: boolean
  emergencyStop?: boolean
  connecting?: boolean
  offline?: boolean
  onRetry?: () => void
  onSettings?: () => void
}) {
  return (
    <section className="empty-state">
      <div className={`empty-state-icon${offline ? ' offline' : ''}`}>
        {offline ? <WifiOff size={42} strokeWidth={1.8} /> : <RadioTower size={42} strokeWidth={1.8} />}
      </div>
      <h2>
        {emergencyStop
          ? 'Emergency stop active'
          : offline
            ? 'HANAS is offline'
          : connecting
            ? 'Connecting to HANAS'
          : maintenanceMode
            ? 'Maintenance mode active'
            : 'Waiting for first reading'}
      </h2>
      <p>
        {emergencyStop
          ? 'Pump commands are locked out. Verify hardware and clear emergency stop in Settings when safe.'
          : offline
          ? 'The dashboard cannot reach the HANAS backend. Your last saved configuration is unchanged.'
          : connecting
          ? 'Waiting for the backend to respond. The dashboard will keep retrying.'
          : maintenanceMode
          ? 'Sensor readings may pause while calibration, pump checks, water changes, or air stone checks are in progress.'
          : 'The HANAS backend is connected. Data will appear here once the ESP32 sends its first sensor payload.'}
      </p>
      <div className="empty-state-hint">
        <span>
          {emergencyStop
            ? 'No AI or ESP32 pump command will be served while Emergency Stop is active'
            : offline
            ? 'Check the backend service, network connection, or API URL. Automatic retries will continue.'
            : connecting
            ? 'The backend may be starting. This request can finish even when it takes longer than the normal refresh interval.'
            : maintenanceMode
            ? 'Automatic dosing is paused until Maintenance Mode is turned off in Settings'
            : 'ESP32 -> POST /api/sensor-data -> dashboard populates'}
        </span>
      </div>
      {offline && (
        <div className="empty-state-actions">
          <button type="button" onClick={onRetry}>
            <RefreshCw size={16} strokeWidth={2.2} /> Retry now
          </button>
          <button type="button" className="secondary" onClick={onSettings}>
            <Settings2 size={16} strokeWidth={2.2} /> Connection settings
          </button>
        </div>
      )}
    </section>
  )
}
