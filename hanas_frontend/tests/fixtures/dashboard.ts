import type {
  BatchStatus,
  ControlCycle,
  LatestLog,
  NotificationLogEntry,
  OverviewSummaryStatus,
  SystemSettings,
} from '../../src/types'
import { PHASE3_PRODUCTION_HISTORY } from './phase3ProductionHistory'

export type DashboardFixture = {
  latestLog: LatestLog
  cycle: ControlCycle
  history: typeof PHASE3_PRODUCTION_HISTORY
  notificationLogs: NotificationLogEntry[]
  systemSettings: SystemSettings
  batchStatus: BatchStatus
  overviewSummary: OverviewSummaryStatus
  capturedAt: string
}

/**
 * Browser-test fixture based on Phase 3 control cycle #49826.
 * Recorded values come from the Phase 3 production workbook.
 * The capture time falls within the pump interval to test dosing countdowns.
 */
export function createDashboardFixture(): DashboardFixture {
  const capturedAt = '2026-08-13T01:22:32.000000+08:00'
  const actionStartedAt = '2026-08-13T01:22:28.651075+08:00'

  const decisionMetadata = {
    confidence: 0.82,
    combined_disturbance: false,
    safety_gate_source: 'deterministic_python',
    reasoning_source: 'llm_agents',
    applied_dose_adjustment_factor: 0.75,
    dose_adjustment_factor: 0.75,
    orchestrator_agent: {
      action: 'dose',
      route: 'safety_gate',
      reason: 'Monitoring classified a stable pH-high deviation, dose planning selected pH Down, and the deterministic safety gate issued the bounded command.',
      confidence: 0.82,
    },
    monitoring_agent: {
      status: 'deviation',
      summary: 'pH is above target; EC is within range; readings are stable and not yet recovering.',
      is_stable: true,
      risk_flags: ['out_of_range'],
    },
    diagnostic_reasoning_agent: {
      classification: 'ph_high',
      primary_metric: 'ph',
      summary: 'Primary classification is pH high.',
    },
    decision_agent: {
      action: 'dose',
      reason: 'Stable deviation remains after monitoring and diagnostic checks; dose planning should select a bounded adaptive correction.',
      confidence: 0.82,
    },
    baseline_shadow: {
      decision: 'ph_high',
      pump_activated: 'ph_down',
      dose_ml: 19.95,
      duration_ms: 9893,
      reason: 'pH is above the configured target range.',
    },
    same_pump_response: {
      available: false,
      recommended_dose_factor: 1,
      reason: 'No recent same-pump dose exists inside the fresh history window.',
    },
    dose_planning_agent: {
      pump_activated: 'ph_down',
      dose_adjustment_factor: 1,
      mixing_adjustment_factor: 1.25,
      reason: 'Use a reduced 0.75 crop-stage factor for a cautious pH Down correction during the harvest window; remeasure after mixing.',
    },
    consistency_review: {
      review_status: 'pass',
      issues: [],
      reason: 'Agent outputs are consistent with the sensor payload, history, and safety context.',
    },
    crop_lifecycle: {
      stage: 'harvest_window',
      stage_label: 'Harvest window',
      age_days: 33,
      crop_type: 'Lettuce',
      crop_variety: 'Olmetie RZ',
      transplant_date: '2026-07-11',
      harvest_start_day: 30,
      harvest_end_day: 35,
    },
    llm_models_used: ['gpt-4.1-nano'],
  }

  const latestLog: LatestLog = {
    log_id: 49826,
    control_cycle_id: 49826,
    timestamp: '2026-08-13T01:22:26.454655+08:00',
    ph: 6.64,
    ec: 1.548,
    temperature: 25.75,
    reservoir_volume_liters: 43.91,
    ph_stable_for_seconds: 31,
    ec_stable_for_seconds: 31,
    control_strategy: 'agentic_ai',
    decision: 'ph_high',
    pump_activated: 'ph_down',
    dose_ml: 14.96,
    duration_ms: 7418,
    mixing_time_ms: 225000,
    ph_deviation: 0.14,
    ec_deviation: 0,
    status: 'dosing',
    decision_metadata: decisionMetadata,
  }

  const cycle: ControlCycle = {
    id: 49826,
    status: 'dosing',
    pump_activated: 'ph_down',
    dose_ml: 14.96,
    duration_ms: 7418,
    mixing_duration_seconds: 225,
    mixing_elapsed_seconds: 0,
    action_started_at: actionStartedAt,
    action_completed_at: null,
  }

  const history = PHASE3_PRODUCTION_HISTORY.map((entry) => entry.control_cycle_id === 49826
    ? { ...entry, status: 'dosing', action_completed_at: null, decision_metadata: decisionMetadata }
    : entry)

  const notificationLogs: NotificationLogEntry[] = [
    {
      id: 19,
      timestamp: '2026-08-13T01:22:26.490337+08:00',
      provider: 'semaphore',
      recipient_number: 'redacted',
      sender_id: 'HANAS',
      alert_type: 'dosing_started',
      message_body: 'HANAS: I scheduled 14.96 mL pH Down for high pH at 6.64. EC 1.55 mS/cm, water temperature 25.8 C, reservoir 43.9 L. Mix 225 s. Then I will recheck.',
      status: 'sent',
      provider_response: 'Message accepted by provider.',
      error_message: null,
      system_log_id: 49826,
      control_cycle_id: 49826,
      created_at: '2026-08-13T01:22:26.490337+08:00',
    },
    {
      id: 18,
      timestamp: '2026-08-12T23:16:00.330866+08:00',
      provider: 'semaphore',
      recipient_number: 'redacted',
      sender_id: 'HANAS',
      alert_type: 'dosing_started',
      message_body: 'HANAS: I scheduled 6000.00 mL EC Down for high EC at 2.20 mS/cm. pH 5.95, water temperature 25.2 C. Mixing: 300 s. Dose capped. monitor next reading.',
      status: 'sent',
      provider_response: 'Message accepted by provider.',
      error_message: null,
      system_log_id: 49799,
      control_cycle_id: 49799,
      created_at: '2026-08-12T23:16:00.330866+08:00',
    },
    {
      id: 17,
      timestamp: '2026-08-12T17:24:04.827341+08:00',
      provider: 'semaphore',
      recipient_number: 'redacted',
      sender_id: 'HANAS',
      alert_type: 'dosing_started',
      message_body: 'HANAS: I scheduled 15.00 mL each of EC Up A/B for low EC at 1.17 mS/cm. pH 6.34, water temperature 25.3 C. Mixing: 270 s. Dose capped. monitor next reading.',
      status: 'sent',
      provider_response: 'Message accepted by provider.',
      error_message: null,
      system_log_id: 49532,
      control_cycle_id: 49532,
      created_at: '2026-08-12T17:24:04.827341+08:00',
    },
  ]

  const systemSettings: SystemSettings = {
    app_version: '0.3.8',
    app_env: 'prod',
    db_schema: 'phase3_report',
    build_sha: 'report-snapshot',
    build_label: 'Final report production data',
    default_reservoir_max_volume_liters: 70,
    minimum_pumpable_reservoir_volume_liters: 20,
    fixed_reservoir_volume_liters: 70,
    force_fixed_reservoir_volume: false,
    hitl_enabled: false,
    sms_enabled: true,
    maintenance_mode_enabled: false,
    monitoring_mode_enabled: false,
    emergency_stop_enabled: false,
    full_agentic_mode_enabled: false,
    openai_api_key_configured: true,
    agentic_ai_model: 'gpt-4.1-nano',
    sensor_sampling_interval_seconds: 60,
    batch_analysis_interval_seconds: 600,
    sms_cooldown_seconds: 900,
    control_strategy: 'agentic_ai',
    ph_target_min: 5.5,
    ph_target_max: 6.5,
    ec_target_min: 1.2,
    ec_target_max: 2.0,
    crop_type: 'Lettuce',
    crop_variety: 'Olmetie RZ',
    crop_transplant_date: '2026-07-11',
    crop_harvest_start_day: 30,
    crop_harvest_end_day: 35,
  }

  const pendingCommand = {
    control_cycle_id: 49826,
    system_log_id: 49826,
    timestamp: latestLog.timestamp,
    status: 'dosing',
    action_started_at: actionStartedAt,
    action_completed_at: null,
    decision: 'ph_high',
    pump_activated: 'ph_down',
    dose_ml: 14.96,
    duration_ms: 7418,
    triggered_by: 'batch_scheduler',
    age_seconds: 6,
  }

  const batchStatus: BatchStatus = {
    batch_scheduler_configured: true,
    batch_scheduler_running: true,
    batch_scheduler_started_at: '2026-07-23T18:00:00.000000+08:00',
    batch_scheduler_next_run_at: '2026-08-13T01:32:26.454655+08:00',
    batch_scheduler_next_run_seconds: 594,
    batch_scheduler_last_run_started_at: latestLog.timestamp,
    batch_scheduler_last_run_finished_at: null,
    batch_scheduler_last_status: 'running',
    batch_scheduler_last_message: 'Cycle 49826 passed the deterministic safety gate and dispatched a bounded pH Down command.',
    batch_last_skip_message: '',
    batch_analysis_enabled: true,
    full_agentic_mode_enabled: false,
    batch_analysis_interval_seconds: 600,
    batch_analysis_window_readings: 30,
    batch_pending_command_expiry_seconds: 900,
    batch_physical_guard_window_seconds: 360,
    batch_has_recent_or_active_pump_command: true,
    batch_recent_or_active_pump_command: pendingCommand,
    batch_has_unresolved_command: true,
    batch_unresolved_command: pendingCommand,
    batch_latest_analysis: {
      control_cycle_id: 49826,
      system_log_id: 49826,
      timestamp: latestLog.timestamp,
      status: 'dosing',
      decision: 'ph_high',
      pump_activated: 'ph_down',
      dose_ml: 14.96,
      duration_ms: 7418,
      triggered_by: 'batch_scheduler',
      batch_trigger_mode: 'scheduled',
      batch_window_readings: 30,
      batch_candidate_rows_read: 30,
      decision_metadata: decisionMetadata,
      age_seconds: 6,
    },
    openai_api_key_configured: true,
    agentic_ai_use_llm: true,
    agentic_ai_model: 'gpt-4.1-nano',
    app_env: 'prod',
    db_schema: 'phase3_report',
  }

  const overviewSummary: OverviewSummaryStatus = {
    overview_summary_enabled: true,
    overview_summary_interval_seconds: 43200,
    overview_summary_scheduler_running: true,
    overview_summary_scheduler_next_run_at: '2026-08-13T12:00:00.000000+08:00',
    overview_summary_scheduler_next_run_seconds: 38248,
    overview_summary_scheduler_last_run_started_at: '2026-08-13T00:00:00.000000+08:00',
    overview_summary_scheduler_last_run_finished_at: '2026-08-13T00:00:03.000000+08:00',
    overview_summary_scheduler_last_status: 'completed',
    overview_summary_scheduler_last_message: 'Operational summary generated from 597 readings.',
    latest_summary: {
      generated_at: '2026-08-13T00:00:03.000000+08:00',
      period_start: '2026-08-12T12:00:00.000000+08:00',
      period_end: '2026-08-13T00:00:00.000000+08:00',
      title: 'Mostly stable conditions with two EC corrections',
      summary: 'Conditions were mostly stable during the completed 12-hour reporting window. pH stayed within its target range, while EC briefly fell below 1.20\u00a0mS/cm during the afternoon and was corrected with nutrient dosing. A later rise to 2.20\u00a0mS/cm triggered a dilution cycle. Water temperature remained steady near 25.2\u00a0°C, and reservoir volume declined from 41.8\u00a0L to 38.2\u00a0L but stayed safely above the 20\u00a0L pumpable minimum. Confirm the next stable EC reading before another correction is issued.',
      highlights: [
        'pH remained within its configured 5.5–6.5 target range.',
        'Water temperature stayed between 25.19 °C and 25.31 °C.',
        'Reservoir volume remained 18.2 L above the pumpable minimum at period end.',
      ],
      risks: ['The period ended with an EC-high correction; its post-mix response still needs confirmation.'],
      recommended_actions: ['Confirm that the next stable EC reading has returned to the 1.2–2.0 mS/cm target range.'],
      trend_notes: ['pH ranged from 5.95 to 6.43; EC ranged from 1.086 to 2.20 mS/cm.'],
      anomaly_events: [],
      dosing_events: [
        'EC Up 15.00 mL per component at cycle 49532.',
        'EC Down 6000.00 mL at cycle 49799.',
      ],
      reading_count: 597,
      dosing_event_count: 2,
      total_dose_ml: 6015,
      ph_min: 5.95,
      ph_max: 6.43,
      ec_min: 1.086,
      ec_max: 2.2,
      reservoir_min_liters: 38.19,
      reservoir_max_liters: 41.94,
      latest_reservoir_liters: 38.19,
      latest_reservoir_percent: 54.6,
      llm_used: true,
      model: 'gpt-4.1-nano',
    },
  }

  return { latestLog, cycle, history, notificationLogs, systemSettings, batchStatus, overviewSummary, capturedAt }
}

/** Browser-test fixture for a pending dose review and operator approval. */
export function createReviewFixture(): DashboardFixture {
  const data = createDashboardFixture()
  const pendingDecision = {
    decision: 'ph_high',
    pump_activated: 'ph_down',
    dose_ml: 14.96,
    duration_ms: 7418,
    reason: 'pH is above the target range. Review the bounded pH Down correction before execution.',
    metadata: { confidence: 0.82 },
  }
  const sensorSnapshot = {
    ph: data.latestLog.ph,
    ec: data.latestLog.ec,
    temperature: data.latestLog.temperature,
    reservoir_volume_liters: data.latestLog.reservoir_volume_liters,
  }

  data.latestLog = {
    ...data.latestLog,
    decision: 'wait_human_review',
    status: 'wait_human_review',
    decision_metadata: {
      ...(data.latestLog.decision_metadata as Record<string, unknown>),
      human_in_the_loop: {
        pending_decision: pendingDecision,
        sensor_snapshot: sensorSnapshot,
      },
    },
  }
  data.cycle = {
    ...data.cycle,
    status: 'wait_human_review',
    pump_activated: 'none',
    dose_ml: 0,
    duration_ms: 0,
    action_started_at: null,
    action_completed_at: null,
  }
  data.systemSettings = {
    ...data.systemSettings,
    hitl_enabled: true,
  }

  return data
}
