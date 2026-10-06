import { useEffect, useMemo, useState } from 'react'
import { Camera, ExternalLink, Leaf, Power, RefreshCw, ShieldCheck, WifiOff } from 'lucide-react'
import { CAMERA_STREAM_URL } from '../constants'
import {
  cameraHostLabel,
  cleanCameraUrl,
  getSavedCameraLiveEnabled,
  getSavedCameraUrl,
  isLikelyTailscaleUrl,
  saveCameraLiveEnabled,
} from '../camera'
import type { Page } from '../types'

export function CameraPage({
  onNavigate,
}: {
  onNavigate?: (page: Page) => void
}) {
  const configuredUrl = cleanCameraUrl(CAMERA_STREAM_URL)
  const [savedUrl] = useState(() => getSavedCameraUrl())
  const [frameKey, setFrameKey] = useState(0)
  const [cameraReachability, setCameraReachability] = useState<{
    key: string
    status: 'online' | 'offline'
  } | null>(null)
  const streamUrl = cleanCameraUrl(savedUrl || configuredUrl)
  const hasStream = streamUrl.length > 0
  const [liveFeedEnabled, setLiveFeedEnabled] = useState(() => hasStream && getSavedCameraLiveEnabled(true))
  const effectiveLiveFeedEnabled = hasStream && liveFeedEnabled
  const displayedLiveFeedEnabled = effectiveLiveFeedEnabled
  const reachabilityKey = `${frameKey}:${streamUrl}`
  const cameraReachabilityStatus = !effectiveLiveFeedEnabled
    ? 'idle'
    : cameraReachability?.key === reachabilityKey
      ? cameraReachability.status
      : 'checking'
  const hostLabel = useMemo(() => cameraHostLabel(streamUrl), [streamUrl])
  const isTailscale = isLikelyTailscaleUrl(streamUrl)
  const isHttpsDashboard = typeof window !== 'undefined' && window.location.protocol === 'https:'
  const isHttpCameraStream = /^http:\/\//i.test(streamUrl)
  const mixedContentBlocked = effectiveLiveFeedEnabled && isHttpsDashboard && isHttpCameraStream
  const cameraStatusPill = hasStream && isTailscale
    ? mixedContentBlocked ? 'HTTPS Required' : 'Private View'
    : hasStream
      ? 'Camera Link Set'
      : 'Setup Needed'

  function setLiveFeedPreference(enabled: boolean) {
    const nextEnabled = hasStream && enabled
    setLiveFeedEnabled(nextEnabled)
    saveCameraLiveEnabled(nextEnabled)
  }

  useEffect(() => {
    if (!effectiveLiveFeedEnabled || !streamUrl || mixedContentBlocked) {
      return
    }

    let disposed = false
    const controller = new AbortController()
    const timer = window.setTimeout(() => controller.abort(), 6_000)

    fetch(streamUrl, {
      mode: 'no-cors',
      cache: 'no-store',
      signal: controller.signal,
    })
      .then(() => {
        if (!disposed) setCameraReachability({ key: reachabilityKey, status: 'online' })
      })
      .catch(() => {
        if (disposed) return
        if (!controller.signal.aborted) {
          setCameraReachability({ key: reachabilityKey, status: 'offline' })
          return
        }
        setCameraReachability({ key: reachabilityKey, status: 'offline' })
      })
      .finally(() => window.clearTimeout(timer))

    return () => {
      disposed = true
      controller.abort()
      window.clearTimeout(timer)
    }
  }, [effectiveLiveFeedEnabled, mixedContentBlocked, reachabilityKey, streamUrl])

  return (
    <section className={`page-content camera-page ${hasStream ? 'has-stream' : 'needs-setup'}`}>
      <section className="camera-hero panel">
        <div className="camera-hero-main">
          <div className="camera-icon-tile">
            <Camera size={24} strokeWidth={2.2} />
          </div>
          <div>
            <span>Crop camera</span>
            <h2>Live grow room view</h2>
            <p>Check the lettuce canopy, water channels, and equipment area without leaving the dashboard.</p>
          </div>
        </div>
        <div className="camera-hero-status">
          <span className={hasStream && isTailscale ? 'online' : 'offline'}>
            {cameraStatusPill}
          </span>
          {!hasStream && (
            <button type="button" className="camera-hero-settings-link" onClick={() => onNavigate?.('Settings')}>
              Camera Settings
            </button>
          )}
        </div>
      </section>

      <div className="camera-layout">
        <section className="camera-viewer-card panel">
          <div className="camera-viewer-header">
            <div>
              <span>Live View</span>
              <h2>Lettuce bed camera</h2>
            </div>
            <div className="camera-viewer-actions">
              <button
                type="button"
                className={`camera-live-toggle ${displayedLiveFeedEnabled ? 'on' : 'off'}`}
                onClick={() => setLiveFeedPreference(!displayedLiveFeedEnabled)}
                disabled={!hasStream}
                aria-pressed={displayedLiveFeedEnabled}
              >
                <Power size={15} strokeWidth={2.2} />
                {displayedLiveFeedEnabled ? 'Live on' : 'Live off'}
              </button>
              <button
                type="button"
                onClick={() => setFrameKey((key) => key + 1)}
                disabled={!displayedLiveFeedEnabled}
              >
                <RefreshCw size={15} strokeWidth={2.2} />
                Refresh
              </button>
              {hasStream && streamUrl && (
                <a href={streamUrl} target="_blank" rel="noreferrer">
                  <ExternalLink size={15} strokeWidth={2.2} />
                  Open
                </a>
              )}
            </div>
          </div>

          {mixedContentBlocked ? (
            <div className="camera-empty-state camera-network-state offline">
              <ShieldCheck size={34} strokeWidth={2.1} />
              <h3>Secure camera URL required</h3>
              <p>
                This dashboard uses HTTPS, so the browser will block an HTTP camera embed. Use an
                HTTPS camera gateway URL, or open the current camera address in a separate tab.
              </p>
              <div className="camera-empty-actions">
                <a href={streamUrl} target="_blank" rel="noreferrer">
                  <ExternalLink size={15} strokeWidth={2.2} />
                  Open Camera
                </a>
                {onNavigate && (
                  <button type="button" onClick={() => onNavigate('Settings')}>
                    Update URL
                  </button>
                )}
              </div>
            </div>
          ) : effectiveLiveFeedEnabled && cameraReachabilityStatus === 'online' ? (
            <>
              <div className="camera-frame-shell">
                <iframe
                  key={frameKey}
                  title="HANAS live camera"
                  src={streamUrl}
                  allow="autoplay; fullscreen; picture-in-picture"
                  allowFullScreen
                  referrerPolicy="no-referrer"
                />
              </div>
            </>
          ) : effectiveLiveFeedEnabled && cameraReachabilityStatus === 'checking' ? (
            <div className="camera-empty-state camera-network-state">
              <RefreshCw size={34} strokeWidth={2.1} />
              <h3>Checking private camera network</h3>
              <p>
                HANAS is checking whether this device can reach the Raspberry Pi camera gateway.
              </p>
            </div>
          ) : effectiveLiveFeedEnabled && cameraReachabilityStatus === 'offline' ? (
            <div className="camera-empty-state camera-network-state offline">
              <WifiOff size={34} strokeWidth={2.1} />
              <h3>Private camera network unavailable</h3>
              <p>
                The camera stream is configured, but this device cannot reach the private camera
                gateway. Turn on Tailscale on this device, then refresh the live view.
              </p>
              <div className="camera-empty-actions">
                <button type="button" onClick={() => setFrameKey((key) => key + 1)}>
                  <RefreshCw size={15} strokeWidth={2.2} />
                  Check Again
                </button>
                <a href={streamUrl} target="_blank" rel="noreferrer">
                  <ExternalLink size={15} strokeWidth={2.2} />
                  Open Camera
                </a>
              </div>
            </div>
          ) : hasStream ? (
            <div className="camera-empty-state camera-live-paused">
              <Power size={34} strokeWidth={2.1} />
              <h3>Live feed is off</h3>
              <p>
                Turn on the live feed when you need to inspect the grow room. Keeping it off
                avoids loading the camera stream in the browser.
              </p>
              <button type="button" onClick={() => setLiveFeedPreference(true)}>
                Turn On Live Feed
              </button>
            </div>
          ) : (
            <div className="camera-empty-state">
              <WifiOff size={34} strokeWidth={2.1} />
              <h3>Camera is not set up yet</h3>
              <p>
                Add the camera stream in Settings, then return here to view the grow room.
              </p>
              {onNavigate && (
                <button type="button" onClick={() => onNavigate('Settings')}>
                  Add Camera Source
                </button>
              )}
            </div>
          )}
        </section>

        <aside className="camera-side-stack">
          <section className="camera-checklist panel">
            <div className="camera-section-title">
              <span>Operator Notes</span>
              <h2>Lettuce checks</h2>
            </div>
            <ul>
              <li>
                <Leaf size={17} strokeWidth={2.2} />
                <span>Check lettuce heads for upright green leaves, wilting, or obvious edge browning.</span>
              </li>
              <li>
                <Camera size={17} strokeWidth={2.2} />
                <span>Compare head size across both beds for uneven or visibly stunted growth.</span>
              </li>
              <li>
                <ShieldCheck size={17} strokeWidth={2.2} />
                <span>Look for empty sites, collapsed outer leaves, or heads crowding their neighbors.</span>
              </li>
            </ul>
            <div className={`camera-network-note ${isTailscale ? 'good' : 'warn'}`}>
              {mixedContentBlocked
                ? (
                    <>
                      <strong>The configured camera URL uses HTTP.</strong>
                      <span>An HTTPS dashboard requires an HTTPS camera gateway for embedded video.</span>
                    </>
                  )
                : isTailscale && cameraReachabilityStatus === 'offline'
                ? (
                    <>
                      <strong>Private camera network is not reachable.</strong>
                      <span>Turn on Tailscale on this device, then refresh the live view.</span>
                    </>
                  )
                : isTailscale
                ? (
                    <>
                      <strong>Private camera connection is ready.</strong>
                      <span>Secure address: {hostLabel}</span>
                    </>
                  )
                : 'Camera source is not configured for private remote access. Update it in Settings.'}
            </div>
          </section>
        </aside>
      </div>
    </section>
  )
}
