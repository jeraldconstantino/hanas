import { safeLocalStorage } from './storage'

export const CAMERA_URL_STORAGE_KEY = 'hanas_camera_stream_url'
export const CAMERA_LIVE_ENABLED_STORAGE_KEY = 'hanas_camera_live_enabled'

export function cleanCameraUrl(value: string): string {
  const candidate = value.trim()
  if (!candidate) return ''

  try {
    const url = new URL(candidate)
    return url.protocol === 'http:' || url.protocol === 'https:' ? candidate : ''
  } catch {
    return ''
  }
}

export function isValidCameraUrl(value: string): boolean {
  return value.trim().length > 0 && cleanCameraUrl(value).length > 0
}

export function getSavedCameraUrl(): string {
  return safeLocalStorage.getItem(CAMERA_URL_STORAGE_KEY) ?? ''
}

export function saveCameraUrl(value: string): void {
  const nextUrl = cleanCameraUrl(value)
  if (nextUrl) {
    safeLocalStorage.setItem(CAMERA_URL_STORAGE_KEY, nextUrl)
  } else {
    safeLocalStorage.removeItem(CAMERA_URL_STORAGE_KEY)
  }
}

export function getSavedCameraLiveEnabled(defaultEnabled = true): boolean {
  const savedValue = safeLocalStorage.getItem(CAMERA_LIVE_ENABLED_STORAGE_KEY)
  if (savedValue == null) return defaultEnabled
  return savedValue === 'true'
}

export function saveCameraLiveEnabled(enabled: boolean): void {
  safeLocalStorage.setItem(CAMERA_LIVE_ENABLED_STORAGE_KEY, String(enabled))
}

export function isLikelyTailscaleUrl(value: string): boolean {
  const cameraUrl = cleanCameraUrl(value)
  try {
    const url = new URL(cameraUrl)
    const hostname = url.hostname.toLowerCase()
    return /^100\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(hostname) || hostname.endsWith('.ts.net')
  } catch {
    return /^https?:\/\/100\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?\//i.test(cameraUrl)
  }
}

export function cameraHostLabel(value: string): string {
  try {
    const url = new URL(value)
    return url.host
  } catch {
    return 'Not configured'
  }
}
