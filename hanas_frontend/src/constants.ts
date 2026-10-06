import type { Page } from './types'

const configuredApiBaseUrl = import.meta.env.VITE_API_BASE_URL?.trim().replace(/\/+$/, '')

export const API_BASE_URL = configuredApiBaseUrl || 'http://localhost:8080'
export const CAMERA_STREAM_URL = import.meta.env.VITE_CAMERA_STREAM_URL ?? ''

export const PH_TARGET = { min: 5.5, max: 6.5 }
export const EC_TARGET = { min: 1.2, max: 2.0 }

export const pages: Page[] = [
  'Overview',
  'Reservoir',
  'Dosing',
  'Trends',
  'Camera',
  'AI Reasoning',
  'Logs & Alerts',
  'Help',
  'Settings',
]

