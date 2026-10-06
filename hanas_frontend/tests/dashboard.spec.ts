import { normalizeLog, normalizeCycle } from '../src/utils'
import { expect, test, type Page } from '@playwright/test'
import { maintenanceWindows, mergeTrendHistory, partitionTrendHistory, trendRangeCacheKey } from '../src/trendMaintenance'
import { formatEmbeddedDisplayText, shouldShowBatchRunStatus } from '../src/text'
import { heroContent } from '../src/overviewHero'
import { overviewPipelineStagePresentation } from '../src/overviewPipeline'
import { activeOperationCycle, controlCycleDisplayState } from '../src/controlCycleDisplay'
import { humanNotificationMessage } from '../src/notifications'
import { createDashboardFixture, createReviewFixture } from './fixtures/dashboard'
import { formatDoseMl, formatVersionLabel } from '../src/format'
import { nextReadingCountdown } from '../src/readingCountdown'
import type { BatchStatus, ControlCycle, LatestLog, NotificationLogEntry, SensorHistoryEntry } from '../src/types'

const routes = ['overview', 'reservoir', 'dosing', 'trends', 'camera', 'ai-reasoning', 'logs', 'help', 'settings']
const viewports = [
  { name: 'phone-320', width: 320, height: 760 },
  { name: 'phone-360', width: 360, height: 800 },
  { name: 'phone-393', width: 393, height: 852 },
  { name: 'phone-landscape', width: 844, height: 390 },
  { name: 'tablet-768', width: 768, height: 1024 },
  { name: 'tablet-1024', width: 1024, height: 1366 },
  { name: 'tablet-landscape', width: 1180, height: 820 },
  { name: 'desktop-1366', width: 1366, height: 900 },
  { name: 'desktop-1920', width: 1920, height: 1080 },
  { name: 'desktop-2560', width: 2560, height: 1440 },
]

test('the next-reading clock repeats naturally every minute', () => {
  const latest = Date.parse('2026-09-16T10:46:20+08:00')
  expect(nextReadingCountdown(latest, latest)).toMatchObject({ remainingSeconds: 60, progress: 0 })
  expect(nextReadingCountdown(latest, latest + 1_000).remainingSeconds).toBe(59)
  expect(nextReadingCountdown(latest, latest + 59_000).remainingSeconds).toBe(1)
  expect(nextReadingCountdown(latest, latest + 60_000)).toMatchObject({ remainingSeconds: 60, progress: 0 })
  expect(nextReadingCountdown(latest, latest + 61_000).remainingSeconds).toBe(59)
  expect(nextReadingCountdown(latest, latest + 181_000).remainingSeconds).toBe(59)
})

test('experiment preflight blocks the workspace until the operator chooses a run', async ({ page }) => {
  const fixture = createDashboardFixture()
  const activeRun = {
    id: 41,
    run_name: 'HANAS Phase 3 Live Run',
    control_strategy: 'agentic_ai',
    start_time: '2026-09-12T08:00:00+08:00',
    last_activity_at: '2026-09-13T10:29:38+08:00',
    cycle_count: 18,
  }
  let preflightAction = ''
  let preflightRequired = true

  await page.route('**/api/**', async (route) => {
    const url = route.request().url()
    if (url.endsWith('/api/settings/experiment-run/preflight')) {
      const request = route.request().postDataJSON() as { action?: string; active_run_id?: number }
      preflightAction = `${request.action ?? ''}:${request.active_run_id ?? ''}`
      preflightRequired = false
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          ...fixture.systemSettings,
          full_agentic_mode_enabled: true,
          active_experiment_run: activeRun,
          experiment_preflight_required: false,
          experiment_reset_blocked: false,
          experiment_reset_blocked_reason: null,
        }),
      })
      return
    }
    if (url.endsWith('/api/settings')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          ...fixture.systemSettings,
          full_agentic_mode_enabled: true,
          active_experiment_run: activeRun,
          experiment_preflight_required: preflightRequired,
          experiment_reset_blocked: false,
          experiment_reset_blocked_reason: null,
        }),
      })
      return
    }
    await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  })

  await page.goto('http://127.0.0.1:4173/')
  const gate = page.getByRole('dialog', { name: 'Choose experiment context' })
  await expect(gate).toBeVisible()
  await expect(gate).toContainText('#41 · HANAS Phase 3 Live Run')
  await expect(gate).toContainText('18')
  await gate.getByRole('button', { name: 'Continue current run' }).click()
  await expect(gate).toBeHidden()
  expect(preflightAction).toBe('continue:41')
})

test('EC Up A transitions to its protected mixing countdown even before the next status callback', () => {
  const actionStartedMs = Date.parse('2026-09-13T10:00:00.000Z')
  const cycle: ControlCycle = {
    id: 51,
    status: 'dosing',
    pump_activated: 'ec_up',
    dose_ml: 15,
    duration_ms: 7_000,
    mixing_duration_seconds: 225,
    mixing_elapsed_seconds: 0,
    action_started_at: new Date(actionStartedMs).toISOString(),
    action_completed_at: null,
  }

  expect(controlCycleDisplayState(cycle, 70, actionStartedMs + 6_000)).toEqual({
    status: 'dosing',
    mixingElapsedSeconds: 0,
  })
  expect(controlCycleDisplayState(cycle, 70, actionStartedMs + 67_000)).toEqual({
    status: 'inter_dose_mixing',
    mixingElapsedSeconds: 60,
  })
})

test('pH and EC corrections keep the correct post-dose timer state', () => {
  const actionStartedMs = Date.parse('2026-09-13T10:00:00.000Z')
  const corrections = [
    { decision: 'ph_high', pump: 'ph_down', expectedStatus: 'mixing' },
    { decision: 'ph_low', pump: 'ph_up', expectedStatus: 'mixing' },
    { decision: 'ec_high', pump: 'ec_down', expectedStatus: 'mixing' },
    { decision: 'ec_low', pump: 'ec_up', expectedStatus: 'inter_dose_mixing' },
  ] as const

  const waitCycle: ControlCycle = {
    id: 69,
    status: 'completed',
    pump_activated: 'none',
    dose_ml: 0,
    duration_ms: 0,
    mixing_duration_seconds: 0,
    mixing_elapsed_seconds: 0,
    action_started_at: null,
    action_completed_at: null,
  }
  for (const [index, correction] of corrections.entries()) {
    const dosedCycle: ControlCycle = {
      id: 68 + index,
      status: 'dosing',
      pump_activated: correction.pump,
      dose_ml: 13,
      duration_ms: 7_000,
      mixing_duration_seconds: 225,
      mixing_elapsed_seconds: 0,
      action_started_at: new Date(actionStartedMs).toISOString(),
      action_completed_at: null,
    }
    expect(controlCycleDisplayState(dosedCycle, 70, actionStartedMs + 67_000)).toEqual({
      status: correction.expectedStatus,
      mixingElapsedSeconds: 60,
    })

    const batchStatus = {
      batch_physical_guard_window_seconds: 180,
      batch_recent_or_active_pump_command: {
        control_cycle_id: dosedCycle.id,
        system_log_id: 680 + index,
        timestamp: new Date(actionStartedMs).toISOString(),
        status: 'completed',
        action_started_at: new Date(actionStartedMs).toISOString(),
        action_completed_at: new Date(actionStartedMs + 7_000).toISOString(),
        decision: correction.decision,
        pump_activated: correction.pump,
        dose_ml: 13,
        duration_ms: 7_000,
        mixing_duration_seconds: 225,
        triggered_by: 'sensor_full_agentic',
        age_seconds: 60,
      },
    } as BatchStatus

    expect(activeOperationCycle(waitCycle, batchStatus)).toMatchObject({
      id: dosedCycle.id,
      status: 'mixing',
      pump_activated: correction.pump,
      mixing_duration_seconds: 225,
      mixing_elapsed_seconds: 60,
    })
  }
})

test('pending SMS copy distinguishes provider status from handset receipt', () => {
  const notification: NotificationLogEntry = {
    id: 1,
    timestamp: '2026-09-13T10:29:38+08:00',
    provider: 'semaphore',
    recipient_number: 'redacted',
    sender_id: 'HANAS',
    alert_type: 'dosing_started',
    message_body: 'HANAS: I scheduled 13.00 mL each of EC Up A/B for low EC at 1.03 mS/cm.',
    status: 'pending',
    provider_response: null,
    error_message: null,
    system_log_id: 1,
    control_cycle_id: 1,
    created_at: '2026-09-13T10:29:38+08:00',
  }

  const message = humanNotificationMessage(notification)
  expect(message).toContain('Semaphore has not yet updated its delivery status')
  expect(message).not.toContain('awaiting provider confirmation')
  expect(message).toContain('EC Up A/B command scheduled for 13.00 mL per pump.')
  expect(message).toContain('EC 1.03 mS/cm')
})

test('overview pipeline distinguishes skipped, waiting, and passed stages', () => {
  expect(overviewPipelineStagePresentation({
    stage: 'diagnostic_reasoning_agent',
    index: 2,
    isCollectionRow: false,
    activeIndex: -1,
    skipped: true,
    waitingForConfirmation: true,
  })).toEqual({ status: 'skipped', label: 'Skipped' })

  expect(overviewPipelineStagePresentation({
    stage: 'monitoring_agent',
    index: 1,
    isCollectionRow: false,
    activeIndex: -1,
    skipped: false,
    waitingForConfirmation: true,
  })).toEqual({ status: 'waiting', label: 'Waiting' })

  expect(overviewPipelineStagePresentation({
    stage: 'safety_gate',
    index: 6,
    isCollectionRow: false,
    activeIndex: -1,
    skipped: false,
    waitingForConfirmation: false,
  })).toEqual({ status: 'passed', label: 'Passed' })
})

test('maintenance readings stay out of operational trend data', () => {
  const base: SensorHistoryEntry = {
    control_cycle_id: 49139,
    timestamp: '2026-08-02T14:50:38.874330+08:00',
    action_started_at: null,
    action_completed_at: null,
    control_strategy: 'agentic_ai',
    ph: 6.97,
    ec: 0.021,
    temperature: 25.5,
    reservoir_volume_liters: 51.4,
    ph_stable_for_seconds: null,
    ec_stable_for_seconds: null,
    decision: 'maintenance_mode',
    pump_activated: 'none',
    dose_ml: 0,
    duration_ms: 0,
    ph_deviation: 0.47,
    ec_deviation: -1.179,
    status: 'no_action',
    decision_metadata: { triggered_by: 'maintenance_mode', maintenance_mode_enabled: true },
  }
  const normal = {
    ...base,
    control_cycle_id: 49259,
    decision: 'wait_near_boundary',
    status: 'waiting',
    ec: 1.425,
    decision_metadata: { triggered_by: 'batch_mode_collection' },
  }
  const statusMaintenance = {
    ...base,
    control_cycle_id: 49138,
    decision: 'within_range',
    status: 'maintenance_mode',
    decision_metadata: {},
  }
  const emergencyStop = {
    ...base,
    control_cycle_id: 49260,
    decision: 'emergency_stop',
    status: 'emergency_stopped',
    decision_metadata: { triggered_by: 'emergency_stop', emergency_stop_enabled: true },
  }

  const result = partitionTrendHistory([base, normal, statusMaintenance, emergencyStop])
  expect(result.operational).toEqual([normal])
  expect(result.maintenance).toEqual([base, statusMaintenance])
  expect(result.safety).toEqual([emergencyStop])
  expect(maintenanceWindows([base, normal, statusMaintenance])).toEqual([
    { startedAt: base.timestamp, endedAt: base.timestamp, readingCount: 1 },
    { startedAt: statusMaintenance.timestamp, endedAt: statusMaintenance.timestamp, readingCount: 1 },
  ])
})

test('cached trend ranges merge the latest reading without duplicates', () => {
  const base: SensorHistoryEntry = {
    control_cycle_id: 50001,
    timestamp: '2026-08-03T12:00:00+08:00',
    action_started_at: null,
    action_completed_at: null,
    control_strategy: 'agentic_ai',
    ph: 6.4,
    ec: 1.4,
    temperature: 25,
    reservoir_volume_liters: 50,
    ph_stable_for_seconds: 30,
    ec_stable_for_seconds: 30,
    decision: 'within_range',
    pump_activated: 'none',
    dose_ml: 0,
    duration_ms: 0,
    ph_deviation: 0,
    ec_deviation: 0,
    status: 'logged',
    decision_metadata: {},
  }
  const updated = { ...base, status: 'completed' }
  const latest = {
    ...base,
    control_cycle_id: 50002,
    timestamp: '2026-08-03T12:01:00+08:00',
    ph: 6.41,
  }

  const merged = mergeTrendHistory([base], [latest, updated])
  expect(merged).toEqual([latest, updated])
})

test('crop-cycle cache identity stays stable while elapsed hours increase', () => {
  const first = trendRangeCacheKey('https://api.example.com/', 'Crop cycle', 500, '2026-07-12')
  const later = trendRangeCacheKey('https://api.example.com', 'Crop cycle', 501, '2026-07-12')
  const nextCrop = trendRangeCacheKey('https://api.example.com', 'Crop cycle', 1, '2026-08-04')

  expect(first).toBe(later)
  expect(nextCrop).not.toBe(first)
  expect(trendRangeCacheKey('https://api.example.com', 'Last 24h', 24)).not.toBe(
    trendRangeCacheKey('https://api.example.com', 'Last 24h', 48),
  )
})

test('latest batch status hides only the redundant completed state', () => {
  expect(shouldShowBatchRunStatus('completed')).toBe(false)
  expect(shouldShowBatchRunStatus('failed')).toBe(true)
  expect(shouldShowBatchRunStatus('pending')).toBe(true)
  expect(shouldShowBatchRunStatus('mixing')).toBe(true)
})

test('backend-style identifiers are readable inside operator-facing prose', () => {
  expect(formatEmbeddedDisplayText(
    'Diagnostic classified ec_low, Dose Planning selected ec_up, and deferred ph_high.',
  )).toBe('Diagnostic classified EC Low, Dose Planning selected EC Up, and deferred pH High.')
  expect(formatEmbeddedDisplayText(
    'The combined_disturbance remains in monitoring_mode.',
  )).toBe('The Combined Disturbance remains in Monitoring Mode.')
})

test('milliliter volumes use grouped, fixed-precision operator formatting', () => {
  expect(formatDoseMl(20)).toBe('20.00 mL')
  expect(formatDoseMl(2_000)).toBe('2,000.00 mL')
  expect(formatDoseMl(2_000_000)).toBe('2,000,000.00 mL')
})

test('mixing banner uses the canonical success accent and compact action', async ({ page }) => {
  await mockDashboard(page)
  await page.goto('http://127.0.0.1:4173/?page=overview')
  await expect(page.locator('.overview-page')).toBeVisible()

  await page.evaluate(() => {
    const banner = document.createElement('section')
    banner.className = 'active-operation-bar info mixing-style-audit'
    banner.innerHTML = `
      <span class="active-operation-icon"><svg aria-hidden="true"></svg></span>
      <div><strong>Mixing after EC Up</strong><span>Pump is off.</span></div>
      <b>1:02 remaining</b>
      <button type="button" class="compact-card-action">View dosing <svg aria-hidden="true"></svg></button>
    `
    document.body.append(banner)

    const hero = document.createElement('section')
    hero.className = 'status-hero warn mixing-hero-alignment-audit'
    hero.innerHTML = `
      <div class="hero-left">
        <div class="hero-icon"><svg aria-hidden="true"></svg></div>
        <div class="hero-copy">
          <h2>Waiting for mixing window</h2>
          <p>No new dose was queued. HANAS is waiting for the previous pump action or safety window before re-evaluating.</p>
        </div>
      </div>
      <div class="hero-pills"><span class="pill warn">Mixing</span></div>
    `
    document.querySelector('.overview-page')!.append(hero)
  })

  const colors = await page.evaluate(() => {
    const probe = document.createElement('span')
    document.body.append(probe)
    const resolve = (token: string) => {
      probe.style.color = `var(${token})`
      return getComputedStyle(probe).color
    }
    const result = {
      text: resolve('--good-text'),
      border: resolve('--good-border'),
      background: resolve('--good-bg'),
    }
    probe.remove()
    return result
  })
  const banner = page.locator('.mixing-style-audit')
  const icon = banner.locator('.active-operation-icon')
  const action = banner.getByRole('button', { name: 'View dosing' })

  await expect(banner).toHaveCSS('border-left-color', colors.text)
  await expect(icon).toHaveCSS('border-color', colors.border)
  await expect(icon).toHaveCSS('background-color', colors.background)
  await expect(icon).toHaveCSS('color', colors.text)
  await expect(action).toHaveCSS('border-color', colors.text)
  await expect(action).toHaveCSS('background-color', colors.text)
  await expect(action).toHaveCSS('border-radius', '9px')
  await expect(action).toHaveCSS('height', '32px')
  const iconAlignment = await page.locator('.mixing-hero-alignment-audit').evaluate((hero) => {
    const icon = hero.querySelector<HTMLElement>('.hero-icon')!.getBoundingClientRect()
    const copy = hero.querySelector<HTMLElement>('.hero-copy')!.getBoundingClientRect()
    return Math.abs(icon.top - copy.top)
  })
  expect(iconAlignment).toBeLessThanOrEqual(2)
})

test('pH and EC comparison cards have equal height', async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 900 })
  await mockDashboard(page)
  await page.goto('http://127.0.0.1:4173/?page=reservoir')
  const cards = page.locator('.reservoir-left > .gauge-card')
  await expect(cards).toHaveCount(2)
  const heights = await cards.evaluateAll((elements) =>
    elements.map((element) => element.getBoundingClientRect().height),
  )
  expect(Math.abs(heights[0] - heights[1])).toBeLessThanOrEqual(1)
})

test('approved HITL overview status uses a compact neutral strip', async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 900 })
  await mockDashboard(page)
  await page.goto('http://127.0.0.1:4173/?page=overview')
  await expect(page.locator('.overview-page')).toBeVisible()
  await page.evaluate(() => {
    const strip = document.createElement('div')
    strip.className = 'hitl-overview-alert info hitl-overview-style-audit'
    strip.innerHTML = `
      <span class="pill info"><span class="pill-label">HITL</span></span>
      <div class="hitl-overview-alert-copy">
        <strong>Approved: ESP32 executing</strong>
        <span>Cycle #69917</span>
      </div>
    `
    document.querySelector('.overview-page')!.prepend(strip)
  })

  const audit = await page.locator('.hitl-overview-style-audit').evaluate((strip) => {
    const resolveColor = (token: string) => {
      const probe = document.createElement('span')
      probe.style.color = `var(${token})`
      document.body.append(probe)
      const color = getComputedStyle(probe).color
      probe.remove()
      return color
    }
    const style = getComputedStyle(strip)
    const pill = strip.querySelector<HTMLElement>('.pill')!
    const pillStyle = getComputedStyle(pill)
    return {
      surface: resolveColor('--surface'),
      line: resolveColor('--line'),
      success: resolveColor('--good-text'),
      inkStrong: resolveColor('--ink-strong'),
      inkMuted: resolveColor('--ink-muted'),
      background: style.backgroundColor,
      topBorder: style.borderTopColor,
      leftBorder: style.borderLeftColor,
      pillWidth: pill.getBoundingClientRect().width,
      pillColor: pillStyle.color,
      titleColor: getComputedStyle(strip.querySelector<HTMLElement>('strong')!).color,
      cycleColor: getComputedStyle(strip.querySelector<HTMLElement>('.hitl-overview-alert-copy span')!).color,
    }
  })

  expect(audit.background).toBe(audit.surface)
  expect(audit.topBorder).toBe(audit.line)
  expect(audit.leftBorder).toBe(audit.success)
  expect(audit.pillColor).toBe(audit.success)
  expect(audit.pillWidth).toBeLessThan(100)
  expect(audit.titleColor).toBe(audit.inkStrong)
  expect(audit.cycleColor).toBe(audit.inkMuted)
})

test('a cleared emergency stop is not presented as an active lockout', () => {
  const latestLog: LatestLog = {
    log_id: 100,
    control_cycle_id: 42,
    timestamp: '2026-09-12T19:56:57+08:00',
    ph: 5.16,
    ec: 13.22,
    temperature: 26.6,
    reservoir_volume_liters: 64.7,
    ph_stable_for_seconds: 30,
    ec_stable_for_seconds: 30,
    control_strategy: 'agentic_ai',
    decision: 'emergency_stop',
    pump_activated: 'none',
    dose_ml: 0,
    duration_ms: 0,
    mixing_time_ms: 0,
    ph_deviation: -0.34,
    ec_deviation: 11.22,
    status: 'emergency_stopped',
    decision_metadata: {},
  }
  const cycle: ControlCycle = {
    id: 42,
    status: 'emergency_stopped',
    pump_activated: 'none',
    dose_ml: 0,
    duration_ms: 0,
    mixing_duration_seconds: 0,
    mixing_elapsed_seconds: 0,
    action_started_at: null,
    action_completed_at: '2026-09-12T19:56:57+08:00',
  }

  const cleared = heroContent(latestLog, cycle, false)
  expect(cleared.title).toBe('Previous cycle emergency-stopped')
  expect(cleared.tone).toBe('warn')
  expect(cleared.body).toContain('Emergency stop is now cleared')

  const active = heroContent(latestLog, cycle, true)
  expect(active.title).toBe('Emergency stop active')
  expect(active.tone).toBe('danger')
})

test('Monitoring Only stays live and requires confirmation before control resumes', async ({ page }) => {
  await mockDashboard(page)
  await page.goto('http://127.0.0.1:4173/?page=settings')

  const monitoringToggle = page.getByRole('switch', { name: /Monitoring Only/i })
  await expect(monitoringToggle).toHaveAttribute('aria-checked', 'false')
  await monitoringToggle.click()
  await expect(monitoringToggle).toHaveAttribute('aria-checked', 'true')
  await expect(page.getByRole('complementary').getByText('Monitoring Only', { exact: true })).toBeVisible()

  await page.getByRole('button', { name: 'Overview' }).first().click()
  await expect(page.getByText('Monitoring Only Active')).toBeVisible()
  await expect(page.getByText('Live monitoring only')).toBeVisible()
  await expect(page.getByRole('complementary').getByText('Live Readings • AI and Pumps Paused')).toBeVisible()
  await expect(page.getByText('Phase 3 batch safety')).toHaveCount(0)

  await page.getByRole('button', { name: 'Settings' }).first().click()
  await monitoringToggle.click()
  const resumeDialog = page.getByRole('dialog', { name: 'Resume automatic control?' })
  await expect(resumeDialog).toBeVisible()
  await expect(resumeDialog).not.toContainText(';')
  await expect(resumeDialog).toHaveClass(/\bresume\b/)
  await expect(resumeDialog.getByRole('button', { name: 'Resume automatic control' })).toHaveClass(/\bprimary\b/)
  await resumeDialog.getByRole('button', { name: 'Keep monitoring' }).click()
  await expect(monitoringToggle).toHaveAttribute('aria-checked', 'true')

  await monitoringToggle.click()
  await page.getByRole('dialog', { name: 'Resume automatic control?' })
    .getByRole('button', { name: 'Resume automatic control' })
    .click()
  await expect(monitoringToggle).toHaveAttribute('aria-checked', 'false')
})

test('settings confirmations use consistent semantic action colors', async ({ page }) => {
  await mockDashboard(page)
  await page.goto('http://127.0.0.1:4173/?page=settings')

  await page.getByRole('switch', { name: /emergency stop/i }).click()
  const emergencyDialog = page.getByRole('dialog', { name: 'Activate emergency stop?' })
  await expect(emergencyDialog).not.toContainText(';')
  await expect(emergencyDialog.getByRole('button', { name: 'Activate emergency stop' })).toHaveClass(/\bdanger\b/)
  await emergencyDialog.getByRole('button', { name: 'Cancel' }).click()

  await page.getByRole('switch', { name: /maintenance mode/i }).click()
  const maintenanceDialog = page.getByRole('dialog', { name: 'Enable maintenance mode?' })
  await expect(maintenanceDialog).not.toContainText(';')
  await expect(maintenanceDialog.getByRole('button', { name: 'Enable maintenance' })).toHaveClass(/\bwarning\b/)
  await maintenanceDialog.getByRole('button', { name: 'Cancel' }).click()

  await page.getByRole('switch', { name: /full agentic mode/i }).click()
  const agenticDialog = page.getByRole('dialog', { name: 'Enable Full Agentic Mode?' })
  await expect(agenticDialog).not.toContainText(';')
  await expect(agenticDialog).toContainText('This is about 10× more AI runs')
  await expect(agenticDialog.locator('.full-agentic-warning')).toHaveCount(0)
  await expect(agenticDialog.locator('svg')).toHaveCount(1)
  await expect(agenticDialog.getByRole('button', { name: 'Enable Full Agentic Mode' })).toHaveClass(/\bprimary\b/)
  await agenticDialog.getByRole('button', { name: 'Keep scheduled mode' }).click()
})

test('active emergency stop switch uses the standard danger red', async ({ page }) => {
  await mockDashboard(page)
  await page.goto('http://127.0.0.1:4173/?page=settings')

  const emergencyToggle = page.getByRole('switch', { name: /emergency stop/i })
  const emergencyRow = page.locator('.system-config-item.critical')
  const inactiveDangerColors = await Promise.all([
    emergencyRow.evaluate((element) => getComputedStyle(element).borderTopColor),
    emergencyRow.locator('.system-config-label > span').evaluate((element) => getComputedStyle(element).color),
    page.locator('.system-config-item.maintenance').evaluate((element) => getComputedStyle(element).borderTopColor),
    page.locator('.system-config-item.maintenance .system-config-label > span').evaluate((element) => getComputedStyle(element).color),
  ])
  expect(inactiveDangerColors[0]).not.toBe(inactiveDangerColors[2])
  expect(inactiveDangerColors[1]).not.toBe(inactiveDangerColors[3])
  await emergencyToggle.click()
  await page.getByRole('dialog', { name: 'Activate emergency stop?' })
    .getByRole('button', { name: 'Activate emergency stop' })
    .click()
  await expect(emergencyToggle).toHaveAttribute('aria-checked', 'true')

  const danger = await emergencyToggle.evaluate(() => {
    const probe = document.createElement('span')
    probe.style.color = 'var(--danger-text)'
    document.body.append(probe)
    const color = getComputedStyle(probe).color
    probe.remove()
    return color
  })

  await expect.poll(() => emergencyToggle.evaluate((element) =>
    getComputedStyle(element, '::before').backgroundColor,
  )).toBe(danger)
})

test('settings and operating modes use flat neutral surfaces with restrained accents', async ({ page }) => {
  await mockDashboard(page)
  await page.goto('http://127.0.0.1:4173/?page=settings')

  await page.getByRole('switch', { name: /full agentic mode/i }).click()
  await page.getByRole('dialog', { name: 'Enable Full Agentic Mode?' })
    .getByRole('button', { name: 'Enable Full Agentic Mode' })
    .click()

  await expect(page.locator('.system-config-item.agentic.active')).toBeVisible()
  const settingsAudit = await page.evaluate(() => {
    const resolveColor = (token: string) => {
      const probe = document.createElement('span')
      probe.style.color = `var(${token})`
      document.body.append(probe)
      const color = getComputedStyle(probe).color
      probe.remove()
      return color
    }
    const cards = [...document.querySelectorAll<HTMLElement>('.system-config-item')]
    const agentic = document.querySelector<HTMLElement>('.system-config-item.agentic.active')!
    const agenticStyle = getComputedStyle(agentic)
    const rangeCards = [...document.querySelectorAll<HTMLElement>('.range-insight-card')]
    const lifecycle = getComputedStyle(document.querySelector<HTMLElement>('.crop-lifecycle-settings')!)
    const lifecycleAction = getComputedStyle(
      document.querySelector<HTMLElement>('.crop-lifecycle-form-actions button[type="submit"]')!,
    )
    return {
      line: resolveColor('--line'),
      primary: resolveColor('--primary'),
      agenticTopBorder: agenticStyle.borderTopColor,
      agenticLeftBorder: agenticStyle.borderLeftColor,
      gradients: cards.map((card) => getComputedStyle(card).backgroundImage),
      rangeBorders: rangeCards.map((card) => getComputedStyle(card).borderTopColor),
      rangeBackgrounds: rangeCards.map((card) => getComputedStyle(card).backgroundImage),
      lifecycleBorder: lifecycle.borderTopColor,
      lifecycleBackground: lifecycle.backgroundImage,
      lifecycleActionBackground: lifecycleAction.backgroundColor,
    }
  })

  expect(settingsAudit.agenticTopBorder).toBe(settingsAudit.line)
  expect(settingsAudit.agenticLeftBorder).toBe(settingsAudit.primary)
  expect(settingsAudit.gradients.every((background) => background === 'none')).toBe(true)
  expect(settingsAudit.rangeBorders.every((border) => border === settingsAudit.line)).toBe(true)
  expect(settingsAudit.rangeBackgrounds.every((background) => background === 'none')).toBe(true)
  expect(settingsAudit.lifecycleBorder).toBe(settingsAudit.line)
  expect(settingsAudit.lifecycleBackground).toBe('none')
  expect(settingsAudit.lifecycleActionBackground).toBe(settingsAudit.primary)

  await page.getByRole('switch', { name: /Monitoring Only/i }).click()
  await page.getByRole('button', { name: 'Overview' }).first().click()
  await expect(page.locator('.maintenance-banner.monitoring')).toBeVisible()
  await expect(page.locator('.connection-card.monitoring')).toBeVisible()

  const modeAudit = await page.evaluate(() => {
    const banner = getComputedStyle(document.querySelector<HTMLElement>('.maintenance-banner.monitoring')!)
    const sidebar = getComputedStyle(document.querySelector<HTMLElement>('.connection-card.monitoring')!)
    const hero = getComputedStyle(document.querySelector<HTMLElement>('.status-hero.info')!)
    const resolveColor = (token: string) => {
      const probe = document.createElement('span')
      probe.style.color = `var(${token})`
      document.body.append(probe)
      const color = getComputedStyle(probe).color
      probe.remove()
      return color
    }
    return {
      line: resolveColor('--line'),
      primary: resolveColor('--primary'),
      success: resolveColor('--good-text'),
      successBorder: resolveColor('--good-border'),
      bannerTopBorder: banner.borderTopColor,
      bannerLeftBorder: banner.borderLeftColor,
      bannerBackground: banner.backgroundImage,
      sidebarBorder: sidebar.borderTopColor,
      sidebarBackground: sidebar.backgroundImage,
      heroBorder: hero.borderTopColor,
      heroLeftBorder: hero.borderLeftColor,
      heroBackground: hero.backgroundImage,
      heroIconColor: getComputedStyle(document.querySelector<HTMLElement>('.status-hero.info .hero-icon')!).color,
      heroIconBorder: getComputedStyle(document.querySelector<HTMLElement>('.status-hero.info .hero-icon')!).borderTopColor,
      heroTitleColor: getComputedStyle(document.querySelector<HTMLElement>('.status-hero.info h2')!).color,
      heroBodyColor: getComputedStyle(document.querySelector<HTMLElement>('.status-hero.info p')!).color,
      inkStrong: resolveColor('--ink-strong'),
      inkMuted: resolveColor('--ink-muted'),
    }
  })

  expect(modeAudit.bannerTopBorder).toBe(modeAudit.line)
  expect(modeAudit.bannerLeftBorder).toBe(modeAudit.primary)
  expect(modeAudit.bannerBackground).toBe('none')
  expect(modeAudit.sidebarBorder).toBe(modeAudit.line)
  expect(modeAudit.sidebarBackground).toBe('none')
  expect(modeAudit.heroBorder).toBe(modeAudit.line)
  expect(modeAudit.heroLeftBorder).toBe(modeAudit.success)
  expect(modeAudit.heroBackground).toBe('none')
  expect(modeAudit.heroIconColor).toBe(modeAudit.success)
  expect(modeAudit.heroIconBorder).toBe(modeAudit.successBorder)
  expect(modeAudit.heroTitleColor).toBe(modeAudit.inkStrong)
  expect(modeAudit.heroBodyColor).toBe(modeAudit.inkMuted)
})

test('maintenance log labels use plain text without chip highlighting', async ({ page }) => {
  await mockDashboard(page)
  await page.goto('http://127.0.0.1:4173/?page=logs')
  await expect(page.locator('.logs-alerts-page')).toBeVisible()
  await page.evaluate(() => {
    const probe = document.createElement('span')
    probe.id = 'maintenance-label-style-probe'
    probe.className = 'table-status-text action info'
    probe.textContent = 'Maintenance Mode'
    document.querySelector('.logs-alerts-page')?.append(probe)
  })
  const maintenanceLabel = page.locator('#maintenance-label-style-probe')
  await expect(maintenanceLabel).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')
  await expect(maintenanceLabel).toHaveCSS('background-image', 'none')
  await expect(maintenanceLabel).toHaveCSS('border-top-width', '0px')
})

test('confirming hero uses the standard primary green outline', async ({ page }) => {
  await mockDashboard(page)
  await page.goto('http://127.0.0.1:4173/?page=overview')
  await page.evaluate(() => {
    const probe = document.createElement('section')
    probe.id = 'confirming-hero-style-probe'
    probe.className = 'status-hero info'
    probe.innerHTML = `
      <span class="hero-icon">Loading</span>
      <span class="hero-pills"><span class="pill info">Confirming</span></span>
    `
    document.body.append(probe)
  })
  const expectedGreen = await page.evaluate(() => {
    const probe = document.createElement('span')
    probe.style.color = 'var(--primary)'
    document.body.append(probe)
    const color = getComputedStyle(probe).color
    probe.remove()
    return color
  })
  await expect(page.locator('#confirming-hero-style-probe .hero-icon')).toHaveCSS('border-color', expectedGreen)
  await expect(page.locator('#confirming-hero-style-probe .pill')).toHaveCSS('border-color', expectedGreen)
})

test('overview uses one canonical green and standard chip palettes', async ({ page }) => {
  await mockDashboard(page)
  await page.goto('http://127.0.0.1:4173/?page=overview')
  const overview = page.locator('.overview-page')
  await expect(overview).toBeVisible()
  await expect(overview.locator('.overview-ai-context')).toBeVisible()

  const audit = await overview.evaluate((root) => {
    const resolveColor = (token: string) => {
      const probe = document.createElement('span')
      probe.style.color = `var(${token})`
      root.append(probe)
      const color = getComputedStyle(probe).color
      probe.remove()
      return color
    }
    const chipStyles = (element: Element) => {
      const style = getComputedStyle(element)
      return {
        color: style.color,
        border: style.borderTopColor,
        background: style.backgroundColor,
      }
    }

    const chipProbe = document.createElement('div')
    chipProbe.innerHTML = `
      <span class="pill good"><span class="pill-label">Good</span></span>
      <span class="pill info"><span class="pill-label">Info</span></span>
      <article class="cycle-panel"><div class="panel-header"><span class="pill neutral">Cycle</span></div></article>
      <section class="overview-ai-context live">
        <div class="overview-ai-context-head"><span><svg></svg>AI decision context</span></div>
        <button class="overview-ai-reasoning-link">View full reasoning</button>
      </section>
    `
    root.append(chipProbe)

    const context = chipProbe.querySelector<HTMLElement>('.overview-ai-context')!
    const positiveColors = [
      getComputedStyle(context, '::before').backgroundColor,
      getComputedStyle(context.querySelector<HTMLElement>('.overview-ai-context-head > span')!).color,
      getComputedStyle(context.querySelector<SVGElement>('.overview-ai-context-head svg')!).color,
      getComputedStyle(context.querySelector<HTMLElement>('.overview-ai-reasoning-link')!).color,
    ]
    const result = {
      goodText: resolveColor('--good-text'),
      goodBorder: resolveColor('--good-border'),
      goodBackground: resolveColor('--good-bg'),
      line: resolveColor('--line'),
      neutralText: resolveColor('--neutral-text'),
      neutralBorder: resolveColor('--neutral-border'),
      neutralBackground: resolveColor('--neutral-bg'),
      positiveColors,
      goodChip: chipStyles(chipProbe.querySelector('.pill.good')!),
      infoChip: chipStyles(chipProbe.querySelector('.pill.info')!),
      cycleChip: chipStyles(chipProbe.querySelector('.cycle-panel .pill')!),
      lifecycleAction: getComputedStyle(root.querySelector<HTMLElement>('.crop-lifecycle-head-action button')!).backgroundColor,
      goodMetric: (() => {
        const style = getComputedStyle(root.querySelector<HTMLElement>('.metric-card.good')!)
        return { top: style.borderTopColor, left: style.borderLeftColor }
      })(),
      goodMetricIcon: getComputedStyle(root.querySelector<HTMLElement>('.metric-card.good .metric-label i')!).borderTopColor,
      aiContext: (() => {
        const style = getComputedStyle(context)
        return {
          top: style.borderTopColor,
          left: getComputedStyle(context, '::before').backgroundColor,
        }
      })(),
    }
    chipProbe.remove()
    return result
  })

  for (const color of audit.positiveColors) expect(color).toBe(audit.goodText)
  for (const chip of [audit.goodChip, audit.infoChip]) {
    expect(chip.color).toBe(audit.goodText)
    expect(chip.border).toBe(audit.goodBorder)
    expect(chip.background).toBe(audit.goodBackground)
  }
  expect(audit.lifecycleAction).toBe(audit.goodText)
  expect(audit.goodMetric.top).toBe(audit.line)
  expect(audit.goodMetric.left).toBe(audit.goodText)
  expect(audit.goodMetricIcon).toBe(audit.goodText)
  expect(audit.aiContext.top).toBe(audit.line)
  expect(audit.aiContext.left).toBe(audit.goodText)
  expect(audit.cycleChip.color).toBe(audit.neutralText)
  expect(audit.cycleChip.border).toBe(audit.neutralBorder)
  expect(audit.cycleChip.background).toBe(audit.neutralBackground)
})

test('success colors and outlines stay consistent across every tab', async ({ page }) => {
  let inspectedAccents = 0
  let inspectedOutlines = 0

  for (const route of routes) {
    await mockDashboard(page)
    await page.goto(`http://127.0.0.1:4173/?page=${route}`, { waitUntil: 'domcontentloaded' })
    await expect(page.locator('.page-content')).toBeVisible()

    if (route === 'settings') {
      const expectedColor = await page.evaluate(() => {
        const probe = document.createElement('span')
        probe.style.color = 'var(--good-text)'
        document.body.append(probe)
        const color = getComputedStyle(probe).color
        probe.remove()
        return color
      })
      await expect(page.locator('.system-config-strategy-item .pill')).toHaveCSS('color', expectedColor)
      await expect(page.locator('.batch-guard-card > .pill')).toHaveCSS('color', expectedColor)
    }
    const audit = await page.evaluate(() => {
      const resolveColor = (token: string) => {
        const probe = document.createElement('span')
        probe.style.color = `var(${token})`
        document.body.append(probe)
        const color = getComputedStyle(probe).color
        probe.remove()
        return color
      }
      const stylesFor = (selectors: string[]) => selectors.flatMap((selector) =>
        Array.from(document.querySelectorAll<HTMLElement>(selector)).map((element) => {
          const style = getComputedStyle(element)
          return {
            selector,
            classes: element.className,
            background: style.backgroundColor,
            borderTop: style.borderTopColor,
            borderTopWidth: style.borderTopWidth,
            text: style.color,
          }
        }))

      return {
        goodBackground: resolveColor('--good-bg'),
        goodBorder: resolveColor('--good-border'),
        goodText: resolveColor('--good-text'),
        line: resolveColor('--line'),
        accents: stylesFor([
          '.pill.good',
          '.tooltip-status.good',
          '.drilldown-value.good',
          '.camera-network-note.good',
          '.status-hero.good .hero-icon',
          '.metric-card.good .metric-label i',
          '.overview-pipeline-step.complete .overview-pipeline-step-icon',
          '.overview-pipeline-step.passed .overview-pipeline-step-icon',
          '.overview-pipeline-step.waiting .overview-pipeline-step-icon',
          '.overview-pipeline-step.active .overview-pipeline-step-icon',
          '.batch-guard-card.clear .batch-guard-icon',
        ]),
        outlines: stylesFor(['.metric-card.good', '.gauge-card.good']),
        pumpOutlines: stylesFor(['.pump-card.good']),
      }
    })

    inspectedAccents += audit.accents.length
    inspectedOutlines += audit.outlines.length
    for (const accent of audit.accents) {
      const context = `${route} ${accent.selector} (${accent.classes})`
      expect(accent.text, `${context} success accent text`).toBe(audit.goodText)
      if (accent.borderTopWidth !== '0px') {
        expect(accent.background, `${context} success accent background`).toBe(audit.goodBackground)
        const overviewUsesSolidIconOutline = route === 'overview'
          && accent.selector !== '.pill.good'
          && accent.selector !== '.status-hero.good .hero-icon'
        expect(accent.borderTop, `${context} success accent outline`).toBe(
          overviewUsesSolidIconOutline ? audit.goodText : audit.goodBorder,
        )
      }
    }
    for (const outline of audit.outlines) {
      expect(outline.borderTop, `${route} ${outline.selector} success card outline`).toBe(
        route === 'overview' ? audit.line : audit.goodBorder,
      )
    }
    for (const outline of audit.pumpOutlines) {
      expect(outline.borderTop, `${route} ${outline.selector} dosed pump outline`).toBe(audit.goodText)
    }
  }

  expect(inspectedAccents).toBeGreaterThan(0)
  expect(inspectedOutlines).toBeGreaterThan(0)
})

async function mockDeliveryAlert(page: Page) {
  const fixture = createDashboardFixture()
  const alert = {
    ...fixture.notificationLogs[0], id: 20, alert_type: 'possible_delivery_issue',
    timestamp: new Date(Date.parse(fixture.latestLog.timestamp) + 1000).toISOString(),
    control_cycle_id: 49799,
  }
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    const responses: Record<string, unknown> = {
      '/api/system-logs/latest': fixture.latestLog,
      '/api/control-cycles/latest': fixture.cycle,
      '/api/system-logs': fixture.history,
      '/api/notification-logs': [alert, ...fixture.notificationLogs],
      '/api/settings': { ...fixture.systemSettings, experiment_preflight_required: false },
      '/api/batch/status': fixture.batchStatus,
      '/api/overview-summary': fixture.overviewSummary,
      '/api/pipeline/progress': { stage: null },
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(responses[path] ?? {}) })
  })
}

test('pump delivery dismissal persists for the same alert only', async ({ page }) => {
  await mockDeliveryAlert(page)
  await page.goto('http://127.0.0.1:4173/?page=logs')
  const deliveryCheck = page.locator('.alert-banner').filter({ hasText: 'Pump delivery check' })
  await expect(deliveryCheck).toBeVisible()
  await expect(deliveryCheck).toContainText('Cycle #49799')
  await expect(deliveryCheck.locator('.alert-dot')).toHaveCount(0)

  const dismissAlert = deliveryCheck.getByRole('button', { name: 'Dismiss alert' })
  await expect(dismissAlert).toHaveText('')
  await expect(dismissAlert).not.toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')
  await expect(dismissAlert).toHaveCSS('border-radius', '999px')
  await expect(dismissAlert).toHaveCSS('width', '20px')
  await expect(dismissAlert).toHaveCSS('height', '20px')
  await dismissAlert.click()
  await expect(deliveryCheck).toHaveCount(0)
  await page.reload()
  await expect(deliveryCheck).toHaveCount(0)
  await expect.poll(() => page.evaluate(() => localStorage.getItem('hanas_dismissed_delivery_alert_id'))).toBe('20')

  await page.evaluate(() => localStorage.setItem('hanas_dismissed_delivery_alert_id', '19'))
  await page.reload()
  await expect(deliveryCheck).toBeVisible()
})

test('pump delivery alert stays unobstructed at every supported viewport', async ({ page }) => {
  await mockDeliveryAlert(page)
  for (const darkMode of [false, true]) {
    await page.goto('http://127.0.0.1:4173/?page=logs')
    await page.evaluate((dark) => localStorage.setItem('hanas-dark', dark ? '1' : '0'), darkMode)

    for (const viewport of viewports) {
      await page.setViewportSize(viewport)
      await page.reload({ waitUntil: 'domcontentloaded' })
      const deliveryCheck = page.locator('.alert-banner').filter({ hasText: 'Pump delivery check' })
      await expect(deliveryCheck).toBeVisible()

      const layout = await deliveryCheck.evaluate((banner) => {
        const bounds = (element: Element | null) => element?.getBoundingClientRect() ?? null
        const overlaps = (first: DOMRect | null, second: DOMRect | null) => Boolean(
          first && second
          && first.left < second.right
          && first.right > second.left
          && first.top < second.bottom
          && first.bottom > second.top
        )
        const bannerBounds = banner.getBoundingClientRect()
        const textBounds = bounds(banner.querySelector('.alert-text'))
        const timeBounds = bounds(banner.querySelector('.alert-time'))
        const buttonBounds = bounds(banner.querySelector('.alert-btn'))
        return {
          buttonInside: Boolean(buttonBounds)
            && buttonBounds!.left >= bannerBounds.left
            && buttonBounds!.top >= bannerBounds.top
            && buttonBounds!.right <= bannerBounds.right
            && buttonBounds!.bottom <= bannerBounds.bottom,
          buttonRightGap: buttonBounds ? bannerBounds.right - buttonBounds.right : Number.POSITIVE_INFINITY,
          buttonTopGap: buttonBounds ? buttonBounds.top - bannerBounds.top : Number.POSITIVE_INFINITY,
          buttonTextOverlap: overlaps(buttonBounds, textBounds),
          buttonTimeOverlap: overlaps(buttonBounds, timeBounds),
          overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        }
      })

      expect(layout.buttonInside, `${viewport.name} ${darkMode ? 'dark' : 'light'} button containment`).toBe(true)
      expect(layout.buttonRightGap, `${viewport.name} ${darkMode ? 'dark' : 'light'} right corner gap`).toBeLessThanOrEqual(8)
      expect(layout.buttonTopGap, `${viewport.name} ${darkMode ? 'dark' : 'light'} top corner gap`).toBeLessThanOrEqual(8)
      expect(layout.buttonTextOverlap, `${viewport.name} ${darkMode ? 'dark' : 'light'} text overlap`).toBe(false)
      expect(layout.buttonTimeOverlap, `${viewport.name} ${darkMode ? 'dark' : 'light'} timestamp overlap`).toBe(false)
      expect(layout.overflow, `${viewport.name} ${darkMode ? 'dark' : 'light'} page overflow`).toBeLessThanOrEqual(1)
    }
  }
})

test('semantic color palette stays consistent in light and dark modes', async ({ page }) => {
  await mockDashboard(page)
  await page.goto('http://127.0.0.1:4173/?page=ai-reasoning')

  for (const darkMode of [false, true]) {
    await page.evaluate((dark) => localStorage.setItem('hanas-dark', dark ? '1' : '0'), darkMode)
    await page.reload()

    const palette = await page.evaluate(() => {
      const value = (token: string) => {
        const swatch = document.createElement('span')
        swatch.style.color = `var(${token})`
        document.body.append(swatch)
        const resolved = getComputedStyle(swatch).color
        swatch.remove()
        return resolved
      }
      const probe = document.createElement('div')
      probe.className = 'ai-reasoning-page'
      probe.innerHTML = `
        <section class="safety-checklist pending"><div class="context-card-title"><div><strong>Pending</strong></div></div></section>
        <section class="history-context idle"><span class="context-icon history"></span><div class="history-factor-grid"><div>Idle</div></div></section>
        <span class="pill info">Information</span>
        <span class="pill good">Success</span>
      `
      document.body.append(probe)
      const pending = getComputedStyle(probe.querySelector<HTMLElement>('.safety-checklist.pending')!)
      const history = getComputedStyle(probe.querySelector<HTMLElement>('.history-context.idle')!)
      const historyIcon = getComputedStyle(probe.querySelector<HTMLElement>('.context-icon.history')!)
      const infoPill = getComputedStyle(probe.querySelector<HTMLElement>('.pill.info')!)
      const goodPill = getComputedStyle(probe.querySelector<HTMLElement>('.pill.good')!)
      const result = {
        primary: value('--primary'),
        info: value('--info-text'),
        success: value('--good-text'),
        dataBlue: value('--data-blue'),
        neutral: value('--neutral-text'),
        pendingBorder: pending.borderLeftColor,
        historyBorder: history.borderLeftColor,
        historyIcon: historyIcon.color,
        infoPill: infoPill.color,
        goodPill: goodPill.color,
      }
      probe.remove()
      return result
    })

    const color = (value: string) => value.replaceAll(' ', '').toLowerCase()
    expect(color(palette.info)).toBe(color(palette.primary))
    expect(color(palette.success)).not.toBe(color(palette.primary))
    expect(color(palette.dataBlue)).not.toBe(color(palette.primary))
    expect(color(palette.pendingBorder)).toBe(color(palette.primary))
    expect(color(palette.historyBorder)).toBe(color(palette.neutral))
    expect(color(palette.historyIcon)).toBe(color(palette.neutral))
    expect(color(palette.infoPill)).toBe(color(palette.primary))
    expect(color(palette.goodPill)).toBe(color(palette.success))
  }
})

test('every route renders cleanly in both color schemes', async ({ page }) => {
  await mockDashboard(page)
  await page.goto('http://127.0.0.1:4173/?page=overview')

  for (const darkMode of [false, true]) {
    await page.evaluate((dark) => localStorage.setItem('hanas-dark', dark ? '1' : '0'), darkMode)

    for (const viewport of [
      { name: 'phone', width: 393, height: 852 },
      { name: 'desktop', width: 1366, height: 900 },
    ]) {
      await page.setViewportSize(viewport)

      for (const route of routes) {
        const runtimeErrors: string[] = []
        const onPageError = (error: Error) => runtimeErrors.push(error.message)
        page.on('pageerror', onPageError)
        await mockDashboard(page)
      await page.goto(`http://127.0.0.1:4173/?page=${route}`, { waitUntil: 'domcontentloaded' })
        await expect(page.locator('html')).toHaveClass(darkMode ? /\bdark\b/ : /^(?!.*\bdark\b)/)

        const audit = await page.evaluate(() => {
          const root = getComputedStyle(document.documentElement)
          const token = (name: string) => root.getPropertyValue(name).trim()
          const semanticTokens = [
            '--primary', '--primary-bg', '--primary-border',
            '--good-text', '--good-bg', '--good-border',
            '--warn-text', '--warn-bg', '--warn-border',
            '--danger-text', '--danger-bg', '--danger-border',
            '--neutral-text', '--neutral-bg', '--neutral-border',
          ]
          const brokenImages = [...document.images]
            .filter((image) => image.getClientRects().length > 0 && image.complete && image.naturalWidth === 0)
            .map((image) => image.currentSrc || image.src)
          return {
            overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
            brokenImages,
            missingTokens: semanticTokens.filter((name) => !token(name)),
            unresolvedText: /\b(?:NaN|undefined)\b/.test(document.body.innerText),
            primary: token('--primary'),
            success: token('--good-text'),
            warning: token('--warn-text'),
            danger: token('--danger-text'),
            dataBlue: token('--data-blue'),
          }
        })

        expect(audit.overflow, `${route} overflows in ${darkMode ? 'dark' : 'light'} ${viewport.name}`).toBeLessThanOrEqual(1)
        expect(audit.brokenImages, `${route} has broken images in ${darkMode ? 'dark' : 'light'} mode`).toEqual([])
        expect(audit.missingTokens).toEqual([])
        expect(audit.unresolvedText).toBe(false)
        expect(new Set([audit.primary, audit.success, audit.warning, audit.danger, audit.dataBlue]).size).toBe(5)
        expect(runtimeErrors, `${route} has runtime errors in ${darkMode ? 'dark' : 'light'} mode`).toEqual([])
        page.off('pageerror', onPageError)
      }
    }
  }
})

for (const viewport of viewports) {
  test(`all routes render cleanly at ${viewport.name}`, async ({ page }) => {
    await page.setViewportSize(viewport)
    const runtimeErrors: string[] = []
    page.on('pageerror', (error) => runtimeErrors.push(error.message))
    page.on('console', (message) => {
      if (message.type() === 'error') runtimeErrors.push(message.text())
    })

    for (const route of routes) {
      await mockDashboard(page)
      await page.goto(`http://127.0.0.1:4173/?page=${route}`, { waitUntil: 'domcontentloaded' })
      await expect(page.locator('#root')).not.toBeEmpty()
      await expect(page.locator('.page-content')).toBeVisible()

      const audit = await page.evaluate(() => {
        const html = document.documentElement
        const bodyText = document.body.innerText
        const brokenImages = [...document.images]
          .filter((img) => img.getClientRects().length > 0 && img.complete && img.naturalWidth === 0)
          .map((img) => img.currentSrc || img.src)
        return {
          overflow: html.scrollWidth - html.clientWidth,
          brokenImages,
          hasInvalidText: /\b(?:NaN|undefined)\b/.test(bodyText),
          pageWidth: document.querySelector<HTMLElement>('.page-content')?.getBoundingClientRect().width ?? 0,
          sidebarWidth: document.querySelector<HTMLElement>('.sidebar')?.getBoundingClientRect().width ?? 0,
          textBase: Number.parseFloat(getComputedStyle(html).getPropertyValue('--text-base')),
        }
      })

      expect(audit.overflow, `${route} horizontally overflows at ${viewport.name}`).toBeLessThanOrEqual(1)
      expect(audit.brokenImages, `${route} has broken images at ${viewport.name}`).toEqual([])
      expect(audit.hasInvalidText, `${route} renders invalid values at ${viewport.name}`).toBe(false)

      if (viewport.width >= 2200) {
        expect(audit.pageWidth, `${route} content is too wide at ${viewport.name}`).toBeLessThanOrEqual(1881)
        expect(audit.pageWidth, `${route} content is unexpectedly narrow at ${viewport.name}`).toBeGreaterThan(1500)
        expect(audit.sidebarWidth, `${route} sidebar is undersized at ${viewport.name}`).toBe(300)
        expect(audit.textBase, `${route} base type is undersized at ${viewport.name}`).toBe(16)
      }

      if (route === 'ai-reasoning') {
        await expect(page.locator('.ai-confidence')).toHaveCount(0)
        await expect(
          page.locator('.agent-panel').filter({ hasText: 'Decision & dose plan' }).getByText('Confidence', { exact: true }),
        ).toHaveCount(0)
        await expect(page.locator('.research-result-card').filter({ hasText: 'Safety trace' })).toContainText(
          'Passed',
        )
        await expect(page.locator('.history-context')).toContainText('Dose history')
        const timingChipLayout = await page.locator('.phase3-model-card').evaluateAll((cards) => cards.map((card) => {
          const cardBounds = card.getBoundingClientRect()
          const contentBounds = card.querySelector(':scope > div')?.getBoundingClientRect()
          const pillBounds = card.querySelector(':scope > .pill')?.getBoundingClientRect()
          return {
            insideCard: Boolean(pillBounds)
              && pillBounds!.left >= cardBounds.left
              && pillBounds!.right <= cardBounds.right
              && pillBounds!.top >= cardBounds.top
              && pillBounds!.bottom <= cardBounds.bottom,
            alignedWithContent: Boolean(pillBounds && contentBounds)
              && Math.abs(pillBounds!.left - contentBounds!.left) <= 1,
          }
        }))
        expect(timingChipLayout).toHaveLength(3)
        expect(timingChipLayout.every((layout) => layout.insideCard)).toBe(true)
        expect(timingChipLayout.every((layout) => layout.alignedWithContent)).toBe(true)

        if (viewport.width >= 1366) {
          const canvas = page.locator('.live-agent-graph-canvas')
          const legend = page.locator('.live-agent-graph-legend')
          await expect(legend).toContainText('Skipped')
          await expect(legend).toContainText('Agent routing')
          await expect(legend).not.toContainText('Conditional route')
          await expect(legend).not.toContainText('Feedback')
          const edgeLegendAudit = await page.evaluate(() => {
            const main = document.querySelector<SVGPathElement>('.live-agent-edge.main')
            const conditional = document.querySelector<SVGPathElement>('.live-agent-edge.conditional')
            const feedback = document.querySelector<SVGPathElement>('.live-agent-edge.feedback')
            const mainKey = document.querySelector<HTMLElement>('.edge-key.main')
            const routingKey = document.querySelector<HTMLElement>('.edge-key.routing')
            return {
              mainDash: main ? getComputedStyle(main).strokeDasharray : '',
              conditionalDash: conditional ? getComputedStyle(conditional).strokeDasharray : '',
              feedbackDash: feedback ? getComputedStyle(feedback).strokeDasharray : '',
              mainLegend: mainKey ? getComputedStyle(mainKey, '::before').borderTopStyle : '',
              routingLegend: routingKey ? getComputedStyle(routingKey, '::before').borderTopStyle : '',
            }
          })
          expect(edgeLegendAudit.mainDash).toBe('none')
          expect(edgeLegendAudit.conditionalDash).not.toBe('none')
          expect(edgeLegendAudit.feedbackDash).not.toBe('none')
          expect(edgeLegendAudit.conditionalDash).toBe(edgeLegendAudit.feedbackDash)
          expect(edgeLegendAudit.mainLegend).toBe('solid')
          expect(edgeLegendAudit.routingLegend).toBe('dashed')
          const defaultOverflow = await canvas.evaluate((element) => ({
            x: element.scrollWidth - element.clientWidth,
            y: element.scrollHeight - element.clientHeight,
            overflowY: getComputedStyle(element).overflowY,
          }))
          expect(defaultOverflow.x).toBeLessThanOrEqual(1)
          expect(defaultOverflow.y <= 1 || defaultOverflow.overflowY === 'hidden').toBe(true)

          await page.locator('.live-agent-svg-node').nth(6).click()
          const stageDetails = page.locator('.live-agent-graph-selection')
          await expect(stageDetails).toBeVisible()
          await expect(canvas.locator('.live-agent-graph-selection')).toHaveCount(1)
          const popoverAudit = await Promise.all([
            canvas.boundingBox(),
            stageDetails.boundingBox(),
          ])
          expect(popoverAudit[0]).not.toBeNull()
          expect(popoverAudit[1]).not.toBeNull()
          expect(popoverAudit[1]!.x).toBeGreaterThanOrEqual(popoverAudit[0]!.x)
          expect(popoverAudit[1]!.y).toBeGreaterThanOrEqual(popoverAudit[0]!.y)
          expect(popoverAudit[1]!.x + popoverAudit[1]!.width).toBeLessThanOrEqual(
            popoverAudit[0]!.x + popoverAudit[0]!.width + 1,
          )
          expect(popoverAudit[1]!.y + popoverAudit[1]!.height).toBeLessThanOrEqual(
            popoverAudit[0]!.y + popoverAudit[0]!.height + 1,
          )
          await stageDetails.getByRole('button', { name: 'Close stage details' }).click()
          await expect(stageDetails).toHaveCount(0)
        }
      }

      if (route === 'dosing') {
        await expect(page.locator('.dosing-hero-copy')).toContainText('pH Down correction in progress')
        await expect(page.locator('.dosing-hero-copy')).not.toContainText('All pumps are idle')
        const operationCards = await page.locator('.dosing-operational-strip > div').evaluateAll((cards) =>
          cards.map((card) => {
            const bounds = card.getBoundingClientRect()
            const icon = card.querySelector('.dosing-operational-icon')?.getBoundingClientRect()
            const label = card.querySelector(':scope > span:not(.dosing-operational-icon)')?.getBoundingClientRect()
            const value = card.querySelector(':scope > strong')?.getBoundingClientRect()
            return {
              inside: Boolean(icon && label && value)
                && icon!.left >= bounds.left
                && icon!.right <= bounds.right
                && value!.right <= bounds.right
                && value!.bottom <= bounds.bottom,
              copyAligned: Boolean(label && value) && Math.abs(label!.left - value!.left) <= 1,
            }
          }),
        )
        expect(operationCards).toHaveLength(3)
        expect(operationCards.every((card) => card.inside)).toBe(true)
        expect(operationCards.every((card) => card.copyAligned)).toBe(true)
      }

      if (route === 'reservoir') {
        const reservoirVolume = page.locator('.temp-level-panel > div').filter({ hasText: 'Reservoir Volume' })
        await expect(page.locator('.temp-level-panel .mini-fill')).toHaveCount(0)
        await expect(reservoirVolume.getByRole('img')).toHaveAttribute('aria-label', /Reservoir volume .* liters/)
        await expect(reservoirVolume).toContainText('Level sensor · 4–20 mA input')
      }

      if (route === 'logs') {
        const systemLog = page.locator('.system-log-scroll')
        await expect(systemLog.locator('.pill')).toHaveCount(0)
        const rowCount = await systemLog.locator('tbody tr').count()
        await expect(systemLog.locator('.table-status-text.action')).toHaveCount(rowCount)
        await expect(systemLog.locator('.table-status-text.reason')).toHaveCount(rowCount)
        const cycleAudit = page.locator('.cycle-audit')
        await expect(cycleAudit.locator('.pill')).toHaveCount(0)
        await expect(cycleAudit.getByText('Status', { exact: true })).toBeVisible()
        const statusValue = cycleAudit.locator('.info-list > div').filter({ hasText: /^Status/ }).locator('strong')
        await expect(statusValue).not.toHaveText('')
      }

      if (route === 'settings') {
        await expect(page.locator('.range-operator-note')).toHaveCount(0)
        const latestBatch = page.locator('.batch-run-card')
        await expect(latestBatch.locator('.batch-run-meta > .pill')).toHaveCount(0)
        const latestBatchStatus = latestBatch.locator('.batch-run-meta.has-run .batch-run-status')
        await expect(latestBatchStatus).toHaveCount(1)
        await expect(latestBatchStatus).not.toHaveText('Completed')
      }

      if (route === 'trends' && viewport.width <= 420) {
        const summaryLayout = await page.locator('.trends-stats-grid .stat-card').evaluateAll((cards) =>
          cards.map((card) => {
            const value = card.querySelector(':scope > strong')?.getBoundingClientRect()
            const copy = card.querySelector(':scope > div')?.getBoundingClientRect()
            return value && copy ? value.right + 4 <= copy.left : false
          }),
        )
        expect(
          summaryLayout.every(Boolean),
          `Trend summary values overlap their labels at ${viewport.name}`,
        ).toBe(true)
      }
    }

    expect(runtimeErrors).toEqual([])
  })
}

test('critical mobile interactions remain keyboard and touch operable', async ({ page }) => {
  await page.setViewportSize({ width: 393, height: 852 })

  await mockDashboard(page)
  await page.goto('http://127.0.0.1:4173/?page=settings')
  await expect(page.locator('.topbar-alert-link')).toHaveCount(0)
  const notificationBell = page.locator('.notif-bell-btn:visible')
  await expect(notificationBell).toBeVisible()
  const bounds = await notificationBell.boundingBox()
  expect(bounds).not.toBeNull()
  expect(bounds!.x).toBeGreaterThanOrEqual(0)
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(393)
  await notificationBell.click()
  await expect(page.getByRole('dialog', { name: 'Notifications' })).toBeVisible()
  await page.keyboard.press('Escape')

  await mockDashboard(page)
  await page.goto('http://127.0.0.1:4173/?page=overview')
  await expect(page.getByText('Operator recap', { exact: true })).toHaveCount(0)
  const todayMarkerAnimation = await page.locator('.crop-lifecycle-marker.current i').evaluate((marker) =>
    getComputedStyle(marker).animationName,
  )
  expect(todayMarkerAnimation).toContain('crop-today-heartbeat')
  await page.emulateMedia({ reducedMotion: 'reduce' })
  const reducedMotionDuration = await page.locator('.crop-lifecycle-marker.current i').evaluate((marker) =>
    Number.parseFloat(getComputedStyle(marker).animationDuration),
  )
  expect(reducedMotionDuration).toBeLessThanOrEqual(0.001)
  await page.emulateMedia({ reducedMotion: 'no-preference' })
  const editCrop = await page.getByRole('button', { name: 'Edit crop' }).boundingBox()
  const cropStatus = await page.locator('.crop-lifecycle-status').boundingBox()
  expect(editCrop).not.toBeNull()
  expect(cropStatus).not.toBeNull()
  expect(Math.abs(editCrop!.height - cropStatus!.height)).toBeLessThanOrEqual(1)

  const reservoirCard = page.locator('.metric-card').filter({ hasText: 'Reservoir' }).first()
  const reservoirBar = reservoirCard.locator('.mini-fill')
  const ecCard = page.locator('.metric-card').filter({ hasText: 'Target 1.2 - 2 mS/cm' }).first()
  await expect(ecCard.locator('.metric-label')).toHaveText('EC')
  await expect(reservoirBar).toHaveAttribute('aria-label', /20\.0 L pumpable minimum/)
  await expect(reservoirCard.locator('.mini-fill-threshold, .mini-fill-current')).toHaveCount(0)
  const reservoirGradient = await reservoirBar.evaluate((bar) => getComputedStyle(bar).backgroundImage)
  expect(reservoirGradient).toContain('linear-gradient')
  expect(reservoirGradient).toContain('rgb(3, 105, 161)')
  expect(reservoirGradient).not.toContain('rgb(214, 95, 107)')
  expect(reservoirGradient).not.toContain('rgb(22, 143, 104)')

  await mockDashboard(page)
  await page.goto('http://127.0.0.1:4173/?page=reservoir')
  await expect(page.locator('.reservoir-page')).toBeVisible()
  const gradients = await page.evaluate(() => ({
    phLow: getComputedStyle(document.querySelector('.threshold-zone.low-zone')!).backgroundImage,
    phTarget: getComputedStyle(document.querySelector('.threshold-zone.target-zone')!).backgroundImage,
    temperatureWarm: getComputedStyle(document.querySelector('.thermo-bar .warm')!).backgroundImage,
  }))
  expect(gradients.phLow).toContain('linear-gradient')
  expect(gradients.phTarget).toContain('linear-gradient')
  expect(gradients.temperatureWarm).toContain('linear-gradient')
  const signalInsets = await page.locator('.signal-strip-compact > .signal-bars').evaluateAll((rows) =>
    rows.map((row) => {
      const rowBounds = row.getBoundingClientRect()
      const barsBounds = row.querySelector('.signal-strength')?.getBoundingClientRect()
      return barsBounds ? rowBounds.right - barsBounds.right : 0
    }),
  )
  expect(signalInsets.length).toBe(4)
  expect(signalInsets.every((inset) => inset >= 13)).toBe(true)

  await mockDashboard(page)
  await page.goto('http://127.0.0.1:4173/?page=logs')
  const systemLogRows = page.locator('.logs-alerts-page .system-log-scroll tbody tr')
  await expect(systemLogRows).toHaveCount(10)
  await page.locator('.system-log-pagination').getByRole('button', { name: 'Show 10 more' }).click()
  await expect(systemLogRows).toHaveCount(20)
  await page.locator('.system-log-pagination').getByRole('button', { name: 'Show fewer' }).click()
  await expect(systemLogRows).toHaveCount(10)
  await expect(page.locator('.notification-log-row')).toHaveCount(createDashboardFixture().notificationLogs.length)
  await expect(page.locator('.notification-log-row').first().locator('.notification-log-meta')).toBeVisible()
  await expect(page.locator('.notification-log-row').first().locator('.notification-log-content')).toBeVisible()

  await mockDashboard(page)
  await page.goto('http://127.0.0.1:4173/?page=settings')
  const settingsPicker = page.getByRole('button', { name: 'Choose settings section' })
  await settingsPicker.click()
  await expect(page.getByRole('listbox', { name: 'Choose settings section' })).toBeVisible()
  await page.getByRole('option', { name: 'Safety' }).click()
  await expect(settingsPicker).toContainText('Safety')
  await page.getByRole('button', { name: /Switch to (dark|light) mode/ }).click()

  await mockDashboard(page)
  await page.goto('http://127.0.0.1:4173/?page=help')
  const helpPicker = page.getByRole('button', { name: 'Choose help section' })
  await helpPicker.click()
  await page.getByRole('option', { name: 'Operating model' }).click()
  await expect(helpPicker).toContainText('Operating model')

  await mockDashboard(page)
  await page.goto('http://127.0.0.1:4173/?page=trends')
  await expect(page.getByText('pH level', { exact: true })).toHaveCount(0)
  await expect(page.getByText('EC level', { exact: true })).toHaveCount(0)
  const temperatureChart = page.locator('.compact-chart.line')
  await expect(temperatureChart.locator('.temperature-area')).toHaveCount(0)
  await expect(temperatureChart.locator('.temperature-target-band line')).toHaveCount(0)
  const temperatureLayers = await temperatureChart.evaluate((chart) => ({
    band: getComputedStyle(chart.querySelector('.temperature-target-band rect')!).fill,
    reading: getComputedStyle(chart.querySelector('.temperature-line')!).stroke,
  }))
  expect(temperatureLayers.band).not.toBe(temperatureLayers.reading)
  const lastSixHours = page.getByRole('button', { name: 'Last 6h' })
  await lastSixHours.click()
  await expect(lastSixHours).toHaveAttribute('aria-pressed', 'true')
  await page.getByRole('button', { name: /Open ph reading .* drilldown/ }).first().click()
  const readingDrilldown = page.getByRole('dialog', { name: 'pH reading drilldown' })
  const recordedValue = readingDrilldown.locator('.drilldown-grid > div').filter({ hasText: /^Recorded/ }).locator('strong')
  await expect(recordedValue).toHaveText(/[A-Z][a-z]{2} \d{2}, \d{4}, \d{2}:\d{2}:\d{2} (AM|PM)/)
  await readingDrilldown.getByRole('button', { name: 'Close drilldown' }).click()

  await mockDashboard(page)
  await page.goto('http://127.0.0.1:4173/?page=ai-reasoning')
  const graphToggle = page.getByRole('button', { name: 'Hide graph' })
  await graphToggle.click()
  await expect(page.getByRole('button', { name: 'Show graph' })).toBeVisible()
  await page.getByRole('button', { name: 'Show graph' }).click()
  await expect(page.getByRole('button', { name: 'Hide graph' })).toBeVisible()

  const graphCanvas = page.locator('.live-agent-graph-canvas')
  await page.locator('.live-agent-svg-node').nth(4).click()
  const mobileStageDetails = page.locator('.live-agent-graph-selection.mobile-viewport')
  await expect(mobileStageDetails).toBeVisible()
  const mobilePopoverAudit = await Promise.all([
    graphCanvas.boundingBox(),
    mobileStageDetails.boundingBox(),
    mobileStageDetails.evaluate((details) => ({
      overflow: details.scrollWidth - details.clientWidth,
      position: getComputedStyle(details).position,
    })),
  ])
  expect(mobilePopoverAudit[0]).not.toBeNull()
  expect(mobilePopoverAudit[1]).not.toBeNull()
  expect(mobilePopoverAudit[1]!.x).toBeGreaterThanOrEqual(mobilePopoverAudit[0]!.x)
  expect(mobilePopoverAudit[1]!.x + mobilePopoverAudit[1]!.width).toBeLessThanOrEqual(
    mobilePopoverAudit[0]!.x + mobilePopoverAudit[0]!.width + 1,
  )
  expect(mobilePopoverAudit[2].overflow).toBeLessThanOrEqual(1)
  expect(mobilePopoverAudit[2].position).toBe('absolute')
  await mobileStageDetails.getByRole('button', { name: 'Close stage details' }).click()
  await expect(mobileStageDetails).toHaveCount(0)

  const pipeline = page.getByRole('button', { name: 'Show AI Pipeline panel' })
  await pipeline.click()
  await expect(page.locator('.topbar-pipeline-btn:visible').first()).toHaveAttribute('aria-pressed', 'true')
  await page.keyboard.press('Escape')
  await expect(page.getByRole('button', { name: 'Show AI Pipeline panel' })).toBeVisible()
})

test('mobile controls have accessible names and unique ids', async ({ page }) => {
  await page.setViewportSize({ width: 393, height: 852 })

  for (const route of routes) {
    await mockDashboard(page)
    await page.goto(`http://127.0.0.1:4173/?page=${route}`, { waitUntil: 'domcontentloaded' })
    const audit = await page.evaluate(() => {
      const visible = (element: HTMLElement) => element.getClientRects().length > 0
      const controlName = (element: HTMLElement) =>
        element.getAttribute('aria-label')?.trim()
        || ('labels' in element
          ? [...((element as HTMLInputElement).labels ?? [])].map((label) => label.textContent?.trim()).filter(Boolean).join(' ')
          : '')
        || element.getAttribute('title')?.trim()
        || element.textContent?.trim()
        || ''
      const controls = [...document.querySelectorAll<HTMLElement>('button, a[href], input, select, textarea')]
        .filter(visible)
      const unnamed = controls.filter((element) => !controlName(element)).map((element) => element.outerHTML.slice(0, 180))
      const ids = [...document.querySelectorAll<HTMLElement>('[id]')].map((element) => element.id)
      const duplicates = [...new Set(ids.filter((id, index) => id && ids.indexOf(id) !== index))]
      return { unnamed, duplicates }
    })

    expect(audit.unnamed, `${route} contains unnamed controls`).toEqual([])
    expect(audit.duplicates, `${route} contains duplicate ids`).toEqual([])
  }
})

test('desktop pipeline reflows every page and scrolls fully to its final row', async ({ page }) => {
  for (const width of [1366, 1568, 1920, 2560, 1190]) {
    await page.setViewportSize({ width, height: 910 })
    for (const route of routes) {
    await mockDashboard(page)
    await page.goto(`http://127.0.0.1:4173/?page=${route}`, { waitUntil: 'domcontentloaded' })
    await expect(page.locator('.page-content')).toBeVisible()
    await expect(page.getByRole('button', { name: /(?:Show|Hide) AI Pipeline panel/ }).first()).toBeVisible()
    const pipelineToggle = page.getByRole('button', { name: 'Show AI Pipeline panel' })
    if (await pipelineToggle.isVisible()) await pipelineToggle.click()

    const drawer = page.locator('.pipeline-drawer')
    const drawerContent = page.locator('.pipeline-drawer-content')
    await expect(drawer).toBeVisible()
    await expect(page.locator('.main-wrapper')).toHaveClass(/pipeline-open/)

    const contentAudit = await page.locator('.main-content').evaluate((main) => {
      const bounds = main.getBoundingClientRect()
      const offenders = [...main.querySelectorAll<HTMLElement>('.page-content *')]
        .filter((element) => element.getClientRects().length > 0)
        .filter((element) => {
          if (element.closest(
            '.table-wrap, .system-log-scroll, .live-agent-graph-canvas, .history-chart, .compact-chart',
          )) return false
          const rect = element.getBoundingClientRect()
          return rect.left < bounds.left - 1 || rect.right > bounds.right + 1
        })
        .map((element) => `${element.parentElement?.getAttribute('class') || element.parentElement?.tagName} > ${element.getAttribute('class') || element.tagName}`)
        .slice(0, 10)
      return {
        overflow: main.scrollWidth - main.clientWidth,
        offenders,
      }
    })
    expect(contentAudit.offenders, `${route} clips page content with the pipeline open at ${width}px`).toEqual([])
    expect(contentAudit.overflow, `${route} main content overflows with the pipeline open at ${width}px`).toBeLessThanOrEqual(1)

    if (route === 'help') {
      const helpGridAudit = await page.locator('.help-topic-grid').evaluate((grid) => {
        const bounds = grid.getBoundingClientRect()
        const cards = [...grid.querySelectorAll<HTMLElement>('.help-topic-card')]
        return {
          overflow: grid.scrollWidth - grid.clientWidth,
          clippedCards: cards.filter((card) => {
            const cardBounds = card.getBoundingClientRect()
            return cardBounds.left < bounds.left - 1 || cardBounds.right > bounds.right + 1
          }).length,
        }
      })
      expect(helpGridAudit.overflow, `help topic grid overflows with the pipeline open at ${width}px`).toBeLessThanOrEqual(1)
      expect(helpGridAudit.clippedCards, `help topic cards sit behind the pipeline at ${width}px`).toBe(0)
    }

    if (route === 'reservoir' && width > 1320) {
      await expect(page.locator('.reservoir-left .signal-strip-desktop')).toBeHidden()
      const fullWidthSignal = page.locator('.reservoir-layout > .signal-strip-compact')
      await expect(fullWidthSignal).toBeVisible()
      await expect.poll(() => page.evaluate(() => {
        const layout = document.querySelector('.reservoir-layout')?.getBoundingClientRect()
        const signal = document.querySelector('.reservoir-layout > .signal-strip-compact')?.getBoundingClientRect()
        const countdown = document.querySelector('.countdown-panel')?.getBoundingClientRect()
        return Boolean(layout && signal && countdown
          && Math.abs(signal.left - layout.left) <= 1
          && Math.abs(signal.right - layout.right) <= 1
          && signal.top >= countdown.bottom)
      })).toBe(true)
    }

    if (route === 'settings') {
      const batchCardAudit = await page.locator('.batch-run-card').evaluate((card) => {
        const bounds = card.getBoundingClientRect()
        const facts = [...card.querySelectorAll<HTMLElement>('.batch-run-facts > span')]
        return {
          overflow: card.scrollWidth - card.clientWidth,
          clippedFacts: facts.filter((fact) => {
            const factBounds = fact.getBoundingClientRect()
            return factBounds.left < bounds.left - 1 || factBounds.right > bounds.right + 1
          }).length,
          redundantCompletedStatusCount: [...card.querySelectorAll<HTMLElement>('.batch-run-meta.has-run .batch-run-status')]
            .filter((status) => status.textContent?.trim().toLowerCase() === 'completed').length,
        }
      })
      expect(batchCardAudit.overflow, `settings batch card overflows with the pipeline open at ${width}px`).toBeLessThanOrEqual(1)
      expect(batchCardAudit.clippedFacts, `settings batch facts sit behind the pipeline at ${width}px`).toBe(0)
      expect(batchCardAudit.redundantCompletedStatusCount).toBe(0)
    }

    const scrollAudit = await drawerContent.evaluate((content) => {
      content.scrollTop = content.scrollHeight
      const drawerBounds = content.closest('.pipeline-drawer')!.getBoundingClientRect()
      const lastRow = content.querySelector<HTMLElement>('.agent-pipeline-body > :last-child')
      const contentBounds = content.getBoundingClientRect()
      const lastBounds = lastRow?.getBoundingClientRect()
      const visibleTop = Math.max(drawerBounds.top, 0)
      const visibleBottom = Math.min(drawerBounds.bottom, window.innerHeight)
      return {
        drawerFitsViewport: drawerBounds.height <= window.innerHeight + 1
          && visibleBottom > visibleTop,
        atBottom: Math.abs(content.scrollHeight - content.clientHeight - content.scrollTop) <= 1,
        finalRowVisible: Boolean(lastBounds)
          && lastBounds!.bottom <= Math.min(contentBounds.bottom, visibleBottom) + 1
          && lastBounds!.top >= Math.max(contentBounds.top, visibleTop) - 1,
      }
    })
    expect(scrollAudit.drawerFitsViewport, `${route} pipeline exceeds the viewport at ${width}px`).toBe(true)
    expect(scrollAudit.atBottom, `${route} pipeline cannot reach its scroll bottom at ${width}px`).toBe(true)
    expect(scrollAudit.finalRowVisible, `${route} pipeline final row remains clipped at ${width}px`).toBe(true)
    }
  }
})

test('overview keeps temperature and reservoir concerns visible for an in-range control decision', () => {
  const fixture = createDashboardFixture()
  const log = { ...fixture.latestLog, decision: 'within_range', status: 'completed', ph: 6, ec: 1.5, temperature: 31.5, reservoir_volume_liters: 20 }
  const cycle = { ...fixture.cycle, status: 'completed' }
  const context = { phTarget: { min: 5.5, max: 6.5 }, ecTarget: { min: 1.2, max: 2 }, reservoirCapacity: 70, minimumPumpable: 20 }
  const hero = heroContent(log, cycle, false, false, context)
  expect(hero.title).toBe('Readings need attention')
  expect(hero.body).toContain('high at 31.5°C')
  expect(hero.body).toContain('Refill promptly')
  const overfilled = heroContent({ ...log, temperature: 24, reservoir_volume_liters: 72 }, cycle, false, false, context)
  expect(overfilled.body).toContain('exceeds capacity')
  expect(overfilled.body).not.toContain('refill')
  const healthy = heroContent({ ...log, temperature: 24, reservoir_volume_liters: 70 }, cycle, false, false, context)
  expect(healthy.title).toBe('pH and EC on target')
})

async function mockDashboard(page: Page, fixture = createDashboardFixture()) {
  fixture.systemSettings.app_env = 'prod'
  const offset = Date.now() - Date.parse(fixture.latestLog.timestamp)
  fixture.history = fixture.history.map(reading => ({
    ...reading,
    timestamp: new Date(Date.parse(reading.timestamp) + offset).toISOString(),
  }))
  fixture.latestLog.timestamp = new Date(Date.parse(fixture.latestLog.timestamp) + offset).toISOString()
  let settings = { ...fixture.systemSettings, experiment_preflight_required: false }
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/settings' && route.request().method() === 'PATCH') {
      settings = { ...settings, ...route.request().postDataJSON() }
    }
    const responses: Record<string, unknown> = {
      '/api/pipeline/progress': { stage: null },
      '/api/system-logs/latest': fixture.latestLog,
      '/api/control-cycles/latest': fixture.cycle,
      '/api/sensor-history': fixture.history,
      '/api/notification-logs': fixture.notificationLogs,
      '/api/settings': settings,
      '/api/batch/status': fixture.batchStatus,
      '/api/overview-summary': fixture.overviewSummary,
      '/api/system-logs': fixture.history,
    }
    if (!(path in responses)) { await route.fallback(); return }
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(responses[path]) })
  })
}

test('missing API fields never become sample readings or pump activity', () => {
  expect(normalizeLog({})).toMatchObject({ log_id: 0, ph: 0, ec: 0, temperature: 0, status: 'empty', pump_activated: 'none', decision_metadata: {} })
  expect(normalizeCycle({})).toMatchObject({ id: 0, status: 'empty', pump_activated: 'none', duration_ms: 0 })
})

test('old demo URLs cannot supply readings when the backend is unavailable', async ({ page }) => {
  await page.route('**/api/**', route => route.abort())
  await page.goto('http://127.0.0.1:4173/?demo=recording&scenario=ec-low')
  await expect(page.getByText('Loading workspace')).toHaveCount(0)
  await expect(page.locator('.empty-state')).toBeVisible()
  await expect(page.locator('.recording-controller, .demo-banner')).toHaveCount(0)
})

test('camera requires a configured stream and never loads a bundled recording', async ({ page }) => {
  await mockDashboard(page)
  await page.goto('http://127.0.0.1:4173/?demo=recording&scenario=emergency-stop&page=camera')
  await expect(page.getByText('Camera is not set up yet')).toBeVisible()
  await expect(page.locator('video')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Live off' })).toBeDisabled()
})

test('backend deployment labels only add v to numeric versions', async ({ page }) => {
  expect(formatVersionLabel('Phase 3')).toBe('Phase 3')
  expect(formatVersionLabel('1.4.2')).toBe('v1.4.2')
  expect(formatVersionLabel('v1.4.2')).toBe('v1.4.2')
  expect(formatVersionLabel(createDashboardFixture().systemSettings.app_version)).toBe('v0.3.8')

  await mockDashboard(page)
  await page.goto('http://127.0.0.1:4173/?page=settings')
  const versionCard = page.getByLabel('Backend deployment version')
  await expect(versionCard).toContainText('v0.3.8')
  await expect(versionCard.locator('p span')).toHaveText(['Production'])
  await expect(versionCard).not.toContainText('vPhase 3')
  await expect(versionCard).not.toContainText('Phase 3 dataset')
  await expect(versionCard).not.toContainText('HANAS production dashboard')
})

test('pending HITL banner routes the operator to the full dose review', async ({ page }) => {
  await page.setViewportSize({ width: 393, height: 852 })
  await mockDashboard(page, createReviewFixture())
  await page.goto('http://127.0.0.1:4173/?page=overview')

  const banner = page.locator('.active-operation-bar')
  await expect(banner).toContainText('Dose awaiting approval')
  await expect(banner).toContainText('14.96 mL pH Down proposal. The pump remains off until reviewed.')
  await expect(banner).not.toContainText('Operator action required')
  await expect(banner.locator('b')).toHaveCount(0)
  const reviewDoseButton = banner.getByRole('button', { name: 'Review dose' })
  const editCropButton = page.getByRole('button', { name: 'Edit crop' })
  const standardGreen = await page.evaluate(() => {
    const probe = document.createElement('span')
    probe.style.color = 'var(--good-text)'
    document.body.append(probe)
    const color = getComputedStyle(probe).color
    probe.remove()
    return color
  })
  const standardWarning = await page.evaluate(() => {
    const probe = document.createElement('span')
    probe.style.color = 'var(--warn-text)'
    document.body.append(probe)
    const color = getComputedStyle(probe).color
    probe.remove()
    return color
  })
  await expect(reviewDoseButton).toHaveClass(/compact-card-action/)
  await expect(editCropButton).toHaveClass(/compact-card-action/)
  await expect(reviewDoseButton).toHaveCSS('background-color', standardWarning)
  await expect(editCropButton).toHaveCSS('background-color', standardGreen)
  await expect(reviewDoseButton).toHaveCSS('min-height', '36px')
  await expect(editCropButton).toHaveCSS('min-height', '36px')
  await expect(reviewDoseButton).toHaveCSS('border-radius', '9px')
  await expect(editCropButton).toHaveCSS('border-radius', '9px')
  await expect(reviewDoseButton.locator('svg')).toHaveCSS('width', '13px')
  await expect(editCropButton.locator('svg')).toHaveCSS('width', '13px')
  await expect(page.locator('.hitl-review-card')).toHaveCount(0)
  await expect(page.locator('.hitl-overview-alert')).toHaveCount(0)
  await expect(page.locator('.overview-page .status-hero')).toHaveCount(0)
  await expect(page.locator('.overview-ai-context')).toContainText('HANAS proposed a 14.96 mL pH Down dose')
  await expect(page.locator('.overview-ai-context')).not.toContainText('HANAS started')

  await banner.getByRole('button', { name: 'Review dose' }).click()
  const review = page.locator('.hitl-review-card')
  await expect(page.locator('.dosing-page')).toBeVisible()
  await expect(banner).toHaveCount(0)
  await expect(review).toContainText('Review proposed dose')
  await expect(review.locator('.pill')).toHaveCount(0)
  await expect(review.locator('.hitl-cycle-id')).toHaveText('Cycle #49826')
  await expect(review.locator('.hitl-reading-state')).toHaveCount(3)
  await expect(review.getByRole('button', { name: 'Approve' })).toBeVisible()
  await expect(review.getByRole('button', { name: 'Reject' })).toBeVisible()
  const modifyDoseButton = review.getByRole('button', { name: 'Modify dose' })
  await expect(modifyDoseButton).toBeVisible()
  await modifyDoseButton.click()
  await expect(review.getByRole('button', { name: 'Approve' })).toHaveCount(0)
  await expect(review.getByRole('button', { name: 'Reject' })).toHaveCount(0)
  await expect(review.getByRole('button', { name: 'Cancel changes' })).toBeVisible()
  await expect(review.getByRole('button', { name: 'Apply modified dose' })).toHaveCSS('background-color', 'rgb(15, 118, 110)')
  await expect(review.getByText('Dose (mL)', { exact: true })).toBeVisible()
  await expect(review.locator('.hitl-reading-state').first()).toHaveCSS('text-align', 'left')
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 393)
})

test('Review settings opens the System configuration section directly', async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 900 })
  const fixture = createDashboardFixture()
  fixture.systemSettings.monitoring_mode_enabled = true
  await mockDashboard(page, fixture)
  await page.goto('http://127.0.0.1:4173/?page=overview')
  await page.getByRole('button', { name: 'Review system mode settings' }).click()
  await expect(page.locator('#settings-system')).toBeVisible()
  await expect.poll(async () => page.locator('#settings-system').evaluate(element =>
    Math.abs(element.getBoundingClientRect().top),
  )).toBeLessThan(100)
})

test('dashboard loads when browser storage access is blocked', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.addInitScript(() => {
    for (const name of ['localStorage', 'sessionStorage']) {
      Object.defineProperty(window, name, {
        configurable: true,
        get() { throw new DOMException('Storage blocked', 'SecurityError') },
      })
    }
  })
  const fixture = createDashboardFixture()
  fixture.systemSettings.monitoring_mode_enabled = true
  await mockDashboard(page, fixture)
  await page.goto('http://127.0.0.1:4173/?page=overview')
  await expect(page.getByRole('button', { name: 'Review system mode settings' })).toBeVisible()
  expect(errors).toEqual([])
})


test('offline connection settings remain accessible from the recovery button', async ({ page }) => {
  await page.route('**/api/**', route => route.abort())
  await page.goto('http://127.0.0.1:4173/?page=overview')
  await page.getByRole('button', { name: 'Connection settings', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Connection settings', exact: true })).toBeVisible()
  await expect(page.getByRole('textbox', { name: 'System API URL' })).toBeVisible()
  await expect(page.locator('#settings-connection').getByRole('button', { name: 'Apply', exact: true })).toBeVisible()
})

test('stale readings are historical and do not predict another sensor sample', async ({ page }) => {
  const fixture = createDashboardFixture()
  const oldLog = { ...fixture.latestLog, timestamp: '2026-01-01T00:00:00Z' }
  await mockDashboard(page, fixture)
  await page.route('**/api/system-logs/latest', route => route.fulfill({ json: oldLog }))
  await page.goto('http://127.0.0.1:4173/?page=overview')
  await expect(page.getByText('Readings are stale', { exact: true })).toBeVisible()
  await expect(page.getByText('Readings are stale', { exact: true })).toHaveCount(1)
  await expect(page.getByRole('heading', { name: 'Latest recorded reading', exact: true })).toHaveCount(0)
  await expect(page.getByText('Current sensor reading', { exact: true })).toHaveCount(0)
  await page.goto('http://127.0.0.1:4173/?page=reservoir')
  await expect(page.getByText('Sensor updates overdue', { exact: true })).toBeVisible()
  await expect(page.getByText('Next reading in', { exact: true })).toHaveCount(0)
  await expect(page.getByText('Sensor Signal Quality', { exact: true })).toHaveCount(0)
})
