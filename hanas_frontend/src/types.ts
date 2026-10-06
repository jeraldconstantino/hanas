export type Tone = 'good' | 'warn' | 'danger' | 'info' | 'neutral'

export type Page =
  | 'Overview'
  | 'Reservoir'
  | 'Dosing'
  | 'Trends'
  | 'Camera'
  | 'AI Reasoning'
  | 'Logs & Alerts'
  | 'Help'
  | 'Settings'

export type ConnectionState = 'connected' | 'connecting' | 'offline' | 'empty'

export type JsonMap = Record<string, unknown>

export type SensorHistoryEntry = {
  control_cycle_id: number | null
  timestamp: string
  action_started_at: string | null
  action_completed_at: string | null
  control_strategy: 'baseline' | 'agentic_ai' | null
  ph: number
  ec: number
  temperature: number | null
  reservoir_volume_liters: number | null
  ph_stable_for_seconds: number | null
  ec_stable_for_seconds: number | null
  decision: string | null
  pump_activated: string | null
  dose_ml: number | null
  duration_ms: number | null
  ph_deviation: number | null
  ec_deviation: number | null
  status: string | null
  decision_metadata: JsonMap
}

export type LatestLog = {
  log_id: number
  control_cycle_id: number
  timestamp: string
  ph: number
  ec: number
  temperature: number
  reservoir_volume_liters: number
  ph_stable_for_seconds: number | null
  ec_stable_for_seconds: number | null
  control_strategy: 'baseline' | 'agentic_ai'
  decision: string
  pump_activated: string
  dose_ml: number
  duration_ms: number
  mixing_time_ms: number
  ph_deviation: number
  ec_deviation: number
  status: string
  decision_metadata: JsonMap
}

export type RuntimeSettingsUpdate = {
  hitl_enabled?: boolean
  sms_enabled?: boolean
  maintenance_mode_enabled?: boolean
  monitoring_mode_enabled?: boolean
  emergency_stop_enabled?: boolean
  full_agentic_mode_enabled?: boolean
  crop_variety?: string
  crop_transplant_date?: string | null
  crop_harvest_start_day?: number
  crop_harvest_end_day?: number
}

export type HumanReviewPayload = {
  action: 'approve' | 'reject' | 'override'
  reviewer?: string
  reason: string
  decision?: string
  pump_activated?: 'ph_up' | 'ph_down' | 'ec_up' | 'ec_down' | 'none'
  dose_ml?: number
  duration_ms?: number
  mixing_time_ms?: number
}

export type NotificationLogEntry = {
  id: number
  timestamp: string
  provider: string
  recipient_number: string
  sender_id: string | null
  alert_type: string
  message_body: string
  status: 'submitted' | 'pending' | 'queued' | 'sent' | 'failed' | 'refunded' | 'disabled' | 'suppressed'
  provider_response: string | null
  provider_message_id?: number | null
  latest_provider_response?: string | null
  provider_status_updated_at?: string | null
  status_checked_at?: string | null
  status_check_count?: number
  error_message: string | null
  system_log_id: number | null
  control_cycle_id: number | null
  created_at: string
}

export type SystemSettings = {
  app_version: string
  app_env: string
  db_schema: string
  build_sha: string
  build_label: string
  default_reservoir_max_volume_liters: number
  minimum_pumpable_reservoir_volume_liters: number
  fixed_reservoir_volume_liters: number
  force_fixed_reservoir_volume: boolean
  hitl_enabled: boolean
  sms_enabled: boolean
  maintenance_mode_enabled: boolean
  monitoring_mode_enabled: boolean
  emergency_stop_enabled: boolean
  full_agentic_mode_enabled: boolean
  openai_api_key_configured: boolean
  agentic_ai_model: string
  sensor_sampling_interval_seconds: number
  batch_analysis_interval_seconds: number
  sms_cooldown_seconds: number
  control_strategy: string
  ph_target_min: number
  ph_target_max: number
  ec_target_min: number
  ec_target_max: number
  crop_type: string
  crop_variety: string
  crop_transplant_date: string | null
  crop_harvest_start_day: number
  crop_harvest_end_day: number
  active_experiment_run?: {
    id: number
    run_name: string
    control_strategy: string
    start_time: string
    last_activity_at: string | null
    cycle_count: number
  } | null
  experiment_preflight_required?: boolean
  experiment_reset_blocked?: boolean
  experiment_reset_blocked_reason?: string | null
}

export type BatchUnresolvedCommand = {
  control_cycle_id: number
  system_log_id: number
  timestamp: string | null
  status: string
  action_started_at: string | null
  action_completed_at: string | null
  decision: string
  pump_activated: string
  dose_ml: number
  duration_ms: number
  mixing_duration_seconds?: number
  triggered_by: string
  age_seconds: number
}

export type BatchLatestAnalysis = {
  control_cycle_id: number
  system_log_id: number
  timestamp: string | null
  status: string
  decision: string
  pump_activated: string
  dose_ml: number
  duration_ms: number
  triggered_by: string
  batch_trigger_mode: string | null
  batch_window_readings: number
  batch_candidate_rows_read: number
  decision_metadata: JsonMap
  age_seconds: number
}

export type BatchStatus = {
  batch_scheduler_configured: boolean
  batch_scheduler_running: boolean
  batch_scheduler_started_at: string | null
  batch_scheduler_next_run_at: string | null
  batch_scheduler_next_run_seconds: number | null
  batch_scheduler_last_run_started_at: string | null
  batch_scheduler_last_run_finished_at: string | null
  batch_scheduler_last_status: string
  batch_scheduler_last_message: string
  batch_last_skip_message: string
  batch_analysis_enabled: boolean
  full_agentic_mode_enabled: boolean
  batch_analysis_interval_seconds: number
  batch_analysis_window_readings: number
  batch_pending_command_expiry_seconds: number
  batch_physical_guard_window_seconds: number
  batch_has_recent_or_active_pump_command: boolean
  batch_recent_or_active_pump_command: BatchUnresolvedCommand | null
  batch_has_unresolved_command: boolean
  batch_unresolved_command: BatchUnresolvedCommand | null
  batch_latest_analysis: BatchLatestAnalysis | null
  openai_api_key_configured: boolean
  agentic_ai_use_llm: boolean
  agentic_ai_model: string
  app_env: string
  db_schema: string
}

export type OverviewSummary = {
  data_policy_version?: number
  generated_at: string
  period_start: string
  period_end: string
  title: string
  summary: string
  highlights: string[]
  risks: string[]
  recommended_actions: string[]
  trend_notes: string[]
  anomaly_events: string[]
  dosing_events: string[]
  reading_count: number
  dosing_event_count: number
  total_dose_ml: number
  ph_min: number | null
  ph_max: number | null
  ec_min: number | null
  ec_max: number | null
  reservoir_min_liters: number | null
  reservoir_max_liters: number | null
  latest_reservoir_liters: number | null
  latest_reservoir_percent: number | null
  llm_used: boolean
  model: string | null
}

export type OverviewSummaryStatus = {
  overview_summary_enabled: boolean
  overview_summary_interval_seconds: number
  overview_summary_scheduler_running: boolean
  overview_summary_scheduler_next_run_at: string | null
  overview_summary_scheduler_next_run_seconds: number | null
  overview_summary_scheduler_last_run_started_at: string | null
  overview_summary_scheduler_last_run_finished_at: string | null
  overview_summary_scheduler_last_status: string
  overview_summary_scheduler_last_message: string
  latest_summary: OverviewSummary | null
}

export type ControlCycle = {
  id: number
  status: string
  pump_activated: string
  dose_ml: number
  duration_ms: number
  mixing_duration_seconds: number
  mixing_elapsed_seconds: number
  action_started_at: string | null
  action_completed_at?: string | null
}
