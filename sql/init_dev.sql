CREATE SCHEMA IF NOT EXISTS prod;
CREATE SCHEMA IF NOT EXISTS dev;

-- HANAS operator-facing timestamps use Philippine time. TIMESTAMPTZ still
-- stores an absolute instant; this setting controls how direct SQL sessions
-- display values when no client-side timezone is supplied.
SET TIME ZONE 'Asia/Manila';

-- Select the target schema before running this script.
-- Use prod for Phase 3 and dev for Phase 2.
SET search_path TO dev;


-- =========================================
-- CREATE TABLE: reference_ranges
-- =========================================

CREATE TABLE reference_ranges (
    id SERIAL PRIMARY KEY,
    reference_name VARCHAR(100) NOT NULL,
    calibration_stage VARCHAR(50) NOT NULL,
    calibration_source_volume_liters FLOAT NOT NULL,
    intended_reservoir_volume_liters FLOAT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT FALSE,
    crop_type VARCHAR(50) NOT NULL,
    growth_stage VARCHAR(100) NOT NULL,
    hydroponic_system_type VARCHAR(50) NOT NULL DEFAULT 'DFT',
    ph_target_min FLOAT NOT NULL,
    ph_target_max FLOAT NOT NULL,
    ec_target_min FLOAT NOT NULL,
    ec_target_max FLOAT NOT NULL,
    ph_up_dose_ml_per_liter_per_unit FLOAT NOT NULL,
    ph_down_dose_ml_per_liter_per_unit FLOAT NOT NULL,
    ec_up_dose_ml_per_liter_per_unit FLOAT NOT NULL,
    ec_down_dose_ml_per_liter_per_unit FLOAT NOT NULL,
    ph_pump_flow_ml_per_min FLOAT NOT NULL,
    ec_pump_flow_ml_per_min FLOAT NOT NULL,
    max_dose_ml_per_cycle FLOAT NOT NULL,
    ph_up_max_dose_ml_per_cycle FLOAT NOT NULL,
    ph_down_max_dose_ml_per_cycle FLOAT NOT NULL,
    ec_up_max_dose_ml_per_cycle FLOAT NOT NULL,
    ec_down_max_dose_ml_per_cycle FLOAT NOT NULL,
    ph_up_max_duration_ms INT NOT NULL,
    ph_down_max_duration_ms INT NOT NULL,
    ec_up_max_duration_ms INT NOT NULL,
    ec_down_max_duration_ms INT NOT NULL,
    notes TEXT,

    CONSTRAINT chk_reference_calibration_stage
        CHECK (
            calibration_stage IN (
                'bench_scale',
                'operating_volume',
                'validation'
            )
        )
);

-- =========================================
-- INSERT DEFAULT REFERENCE RANGE
-- =========================================

INSERT INTO reference_ranges (
    reference_name,
    calibration_stage,
    calibration_source_volume_liters,
    intended_reservoir_volume_liters,
    is_active,
    crop_type,
    growth_stage,
    hydroponic_system_type,
    ph_target_min,
    ph_target_max,
    ec_target_min,
    ec_target_max,
    ph_up_dose_ml_per_liter_per_unit,
    ph_down_dose_ml_per_liter_per_unit,
    ec_up_dose_ml_per_liter_per_unit,
    ec_down_dose_ml_per_liter_per_unit,
    ph_pump_flow_ml_per_min,
    ec_pump_flow_ml_per_min,
    max_dose_ml_per_cycle,
    ph_up_max_dose_ml_per_cycle,
    ph_down_max_dose_ml_per_cycle,
    ec_up_max_dose_ml_per_cycle,
    ec_down_max_dose_ml_per_cycle,
    ph_up_max_duration_ms,
    ph_down_max_duration_ms,
    ec_up_max_duration_ms,
    ec_down_max_duration_ms,
    notes
)
VALUES (
    'HANAS 3.5L Bench-Scale Reference',
    'bench_scale',
    3.5,
    3.5,
    FALSE,
    'Lettuce',
    'Vegetative stage after transplanting to full-strength nutrient solution',
    'DFT',
    5.5,
    6.5,
    1.2,
    2.0,
    0.71,
    0.48,
    5.82,
    660.0,
    121.0,
    125.0,
    10.0,
    10.0,
    10.0,
    100.0,
    100.0,
    10000,
    10000,
    60000,
    60000,
    'Preliminary 3.5L bench-scale reference retained for comparison before operating-volume recalibration.'
);

-- =========================================
-- INSERT PHASE 2 OPERATING-VOLUME REFERENCE RANGE
-- =========================================

INSERT INTO reference_ranges (
    reference_name,
    calibration_stage,
    calibration_source_volume_liters,
    intended_reservoir_volume_liters,
    is_active,
    crop_type,
    growth_stage,
    hydroponic_system_type,
    ph_target_min,
    ph_target_max,
    ec_target_min,
    ec_target_max,
    ph_up_dose_ml_per_liter_per_unit,
    ph_down_dose_ml_per_liter_per_unit,
    ec_up_dose_ml_per_liter_per_unit,
    ec_down_dose_ml_per_liter_per_unit,
    ph_pump_flow_ml_per_min,
    ec_pump_flow_ml_per_min,
    max_dose_ml_per_cycle,
    ph_up_max_dose_ml_per_cycle,
    ph_down_max_dose_ml_per_cycle,
    ec_up_max_dose_ml_per_cycle,
    ec_down_max_dose_ml_per_cycle,
    ph_up_max_duration_ms,
    ph_down_max_duration_ms,
    ec_up_max_duration_ms,
    ec_down_max_duration_ms,
    notes
)
VALUES (
    'HANAS Phase 2 20L Operating Reference',
    'operating_volume',
    20.0,
    20.0,
    CASE
        WHEN current_schema() = 'dev' THEN TRUE
        ELSE FALSE
    END,
    'Lettuce',
    'Vegetative stage after transplanting to full-strength nutrient solution',
    'DFT',
    5.5,
    6.5,
    1.2,
    2.0,
    0.80,
    0.71,
    4.45,
    592.12,
    121.0,
    125.0,
    10.0,
    10.0,
    10.0,
    100.0,
    2000.0,
    10000,
    10000,
    60000,
    960000,
    'Phase 2 20L operating reference recalibrated from Phase 2 operating-volume trials. The 3.5L bench reference row is retained for traceability.'
);

-- =========================================
-- INSERT PHASE 3 OPERATING-VOLUME REFERENCE RANGE
-- =========================================

INSERT INTO reference_ranges (
    reference_name,
    calibration_stage,
    calibration_source_volume_liters,
    intended_reservoir_volume_liters,
    is_active,
    crop_type,
    growth_stage,
    hydroponic_system_type,
    ph_target_min,
    ph_target_max,
    ec_target_min,
    ec_target_max,
    ph_up_dose_ml_per_liter_per_unit,
    ph_down_dose_ml_per_liter_per_unit,
    ec_up_dose_ml_per_liter_per_unit,
    ec_down_dose_ml_per_liter_per_unit,
    ph_pump_flow_ml_per_min,
    ec_pump_flow_ml_per_min,
    max_dose_ml_per_cycle,
    ph_up_max_dose_ml_per_cycle,
    ph_down_max_dose_ml_per_cycle,
    ec_up_max_dose_ml_per_cycle,
    ec_down_max_dose_ml_per_cycle,
    ph_up_max_duration_ms,
    ph_down_max_duration_ms,
    ec_up_max_duration_ms,
    ec_down_max_duration_ms,
    notes
)
VALUES (
    'HANAS Phase 3 70L Operating Reference',
    'operating_volume',
    70.0,
    70.0,
    CASE
        WHEN current_schema() = 'dev' THEN FALSE
        ELSE TRUE
    END,
    'Lettuce',
    'Vegetative stage after transplanting to full-strength nutrient solution',
    'DFT',
    5.5,
    6.5,
    1.2,
    2.0,
    0.80,
    0.71,
    4.45,
    592.12,
    121.0,
    125.0,
    10.0,
    20.0,
    20.0,
    20.0,
    6000.0,
    10000,
    10000,
    60000,
    2880000,
    'Phase 3 70L operating reference initialized with Phase 2 operating-volume coefficients. pH Up, pH Down, and EC Up are capped at 20 mL per cycle for production safety.'
);

-- =========================================
-- CREATE TABLE: experiment_runs
-- =========================================

CREATE TABLE experiment_runs (
    id SERIAL PRIMARY KEY,
    reference_range_id INT NOT NULL,
    run_name VARCHAR(100) NOT NULL,
    experiment_phase VARCHAR(20) NOT NULL,
    control_strategy VARCHAR(20) NOT NULL,
    reservoir_max_volume_liters FLOAT NOT NULL,
    sampling_interval_seconds INT NOT NULL,
    mixing_time_seconds INT NOT NULL,
    initial_confirmation_gap_seconds INT NOT NULL,
    stability_required_seconds INT NOT NULL,
    ph_stability_threshold FLOAT NOT NULL,
    ec_stability_threshold FLOAT NOT NULL,
    start_time TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    end_time TIMESTAMPTZ,
    notes TEXT,

    CONSTRAINT fk_reference
        FOREIGN KEY (reference_range_id)
        REFERENCES reference_ranges(id)
        ON DELETE CASCADE,

    CONSTRAINT chk_control_strategy
        CHECK (control_strategy IN ('baseline', 'agentic_ai')),

    CONSTRAINT chk_experiment_phase
        CHECK (experiment_phase IN ('phase2', 'phase3'))
);

-- =========================================
-- INSERT DEFAULT EXPERIMENT RUN
-- =========================================

INSERT INTO experiment_runs (
    reference_range_id,
    run_name,
    experiment_phase,
    control_strategy,
    reservoir_max_volume_liters,
    sampling_interval_seconds,
    mixing_time_seconds,
    initial_confirmation_gap_seconds,
    stability_required_seconds,
    ph_stability_threshold,
    ec_stability_threshold,
    notes
)
VALUES (
    CASE
        WHEN current_schema() = 'dev' THEN 2
        ELSE 3
    END,
    CASE
        WHEN current_schema() = 'dev' THEN 'HANAS Phase 2 Baseline Run'
        ELSE 'HANAS Phase 3 Live Run'
    END,
    CASE
        WHEN current_schema() = 'dev' THEN 'phase2'
        ELSE 'phase3'
    END,
    CASE
        WHEN current_schema() = 'dev' THEN 'baseline'
        ELSE 'agentic_ai'
    END,
    CASE
        WHEN current_schema() = 'dev' THEN 20.0
        ELSE 70.0
    END,
    60,
    120,
    30,
    30,
    0.03,
    0.03,
    'Default HANAS run for incoming ESP32 sensor readings'
);

-- =========================================
-- CREATE TABLE: trial_events
-- =========================================

CREATE TABLE trial_events (
    id SERIAL PRIMARY KEY,
    experiment_run_id INT NOT NULL,
    event_time TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    event_type VARCHAR(50) NOT NULL,
    disturbance_type VARCHAR(50),
    amount_added_ml FLOAT,
    target_metric VARCHAR(20),
    controller_type VARCHAR(20),
    severity_level VARCHAR(20),
    trial_number INT,
    episode_name VARCHAR(100),
    working_volume_liters FLOAT,
    before_ph FLOAT,
    before_ec FLOAT,
    before_temperature_c FLOAT,
    after_ph FLOAT,
    after_ec FLOAT,
    after_temperature_c FLOAT,
    notes TEXT,

    CONSTRAINT fk_trial_event_run
        FOREIGN KEY (experiment_run_id)
        REFERENCES experiment_runs(id)
        ON DELETE CASCADE,

    CONSTRAINT chk_trial_event_type
        CHECK (
            event_type IN (
                'disturbance',
                'reset',
                'calibration',
                'manual_note'
            )
        ),

    CONSTRAINT chk_trial_disturbance_type
        CHECK (
            disturbance_type IS NULL
            OR disturbance_type IN (
                'ph_low',
                'ph_high',
                'ec_low',
                'ec_high'
            )
        ),

    CONSTRAINT chk_trial_target_metric
        CHECK (
            target_metric IS NULL
            OR target_metric IN (
                'ph',
                'ec',
                'temperature',
                'reservoir_volume'
            )
        ),

    CONSTRAINT chk_trial_controller_type
        CHECK (
            controller_type IS NULL
            OR controller_type IN (
                'baseline',
                'agentic_ai'
            )
        ),

    CONSTRAINT chk_trial_severity_level
        CHECK (
            severity_level IS NULL
            OR severity_level IN (
                'mild',
                'moderate',
                'severe'
            )
        ),

    CONSTRAINT chk_trial_number
        CHECK (
            trial_number IS NULL
            OR trial_number > 0
        ),

    CONSTRAINT chk_trial_working_volume_liters
        CHECK (
            working_volume_liters IS NULL
            OR working_volume_liters > 0
        ),

    CONSTRAINT chk_trial_before_ph
        CHECK (
            before_ph IS NULL
            OR before_ph BETWEEN 0 AND 14
        ),

    CONSTRAINT chk_trial_after_ph
        CHECK (
            after_ph IS NULL
            OR after_ph BETWEEN 0 AND 14
        ),

    CONSTRAINT chk_trial_before_ec
        CHECK (
            before_ec IS NULL
            OR before_ec >= 0
        ),

    CONSTRAINT chk_trial_after_ec
        CHECK (
            after_ec IS NULL
            OR after_ec >= 0
        ),

    CONSTRAINT chk_trial_before_temperature_c
        CHECK (
            before_temperature_c IS NULL
            OR before_temperature_c BETWEEN -10 AND 80
        ),

    CONSTRAINT chk_trial_after_temperature_c
        CHECK (
            after_temperature_c IS NULL
            OR after_temperature_c BETWEEN -10 AND 80
        )
);

-- SAMPLE INSERT: PH LOW DISTURBANCE
-- INSERT INTO trial_events (
--     experiment_run_id,
--     event_type,
--     disturbance_type,
--     amount_added_ml,
--     target_metric,
--     controller_type,
--     severity_level,
--     trial_number,
--     episode_name,
--     working_volume_liters,
--     before_ph,
--     before_ec,
--     before_temperature_c,
--     after_ph,
--     after_ec,
--     after_temperature_c,
--     notes
-- )
-- VALUES (
--     1,
--     'disturbance',
--     'ph_low',
--     2.0,
--     'ph',
--     'baseline',
--     'moderate',
--     1,
--     'ph_low_benchmark',
--     20.0,
--     6.05,
--     1.62,
--     25.1,
--     5.12,
--     1.64,
--     25.2,
--     'Added 2 ml pH Down for moderate low-pH benchmark trial using baseline controller.'
-- );


-- =========================================
-- CREATE TABLE: control_cycles
-- =========================================

CREATE TABLE control_cycles (
    id SERIAL PRIMARY KEY,
    experiment_run_id INT NOT NULL,
    control_strategy VARCHAR(20) NOT NULL,
    started_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    deviation_detected_at TIMESTAMPTZ,
    action_started_at TIMESTAMPTZ,
    action_completed_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    status VARCHAR(64),

    CONSTRAINT fk_cycle_run
        FOREIGN KEY (experiment_run_id)
        REFERENCES experiment_runs(id)
        ON DELETE CASCADE,

    CONSTRAINT chk_control_cycle_strategy
        CHECK (control_strategy IN ('baseline', 'agentic_ai'))
);

-- =========================================
-- CREATE TABLE: system_logs
-- =========================================

CREATE TABLE system_logs (
    id SERIAL PRIMARY KEY,
    experiment_run_id INT NOT NULL,
    control_strategy VARCHAR(20) NOT NULL,
    timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,

    ph FLOAT,
    ec FLOAT,
    temperature FLOAT,
    reservoir_volume_liters FLOAT,
    ph_stable_for_seconds INT,
    ec_stable_for_seconds INT,
    ph_stability_threshold FLOAT,
    ec_stability_threshold FLOAT,

    decision VARCHAR(50),
    pump_activated VARCHAR(50),
    dose_ml FLOAT,
    duration_ms INT,
    mixing_base_seconds INT,
    mixing_effective_seconds INT,
    mixing_elapsed_seconds FLOAT,
    mixing_remaining_seconds FLOAT,
    mixing_adjustment_factor FLOAT,
    ph_within_range BOOLEAN,
    ec_within_range BOOLEAN,
    ph_deviation FLOAT,
    ec_deviation FLOAT,
    decision_reason TEXT,
    decision_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(64),
    control_cycle_id INT NOT NULL,

    CONSTRAINT fk_run
        FOREIGN KEY (experiment_run_id)
        REFERENCES experiment_runs(id)
        ON DELETE CASCADE,

    CONSTRAINT fk_control_cycle
        FOREIGN KEY (control_cycle_id)
        REFERENCES control_cycles(id)
        ON DELETE CASCADE,

    CONSTRAINT chk_system_log_strategy
        CHECK (control_strategy IN ('baseline', 'agentic_ai'))
);

CREATE INDEX idx_system_logs_run_timestamp
    ON system_logs (experiment_run_id, timestamp);

CREATE INDEX idx_system_logs_strategy_timestamp
    ON system_logs (control_strategy, timestamp);

CREATE INDEX idx_control_cycles_strategy_started
    ON control_cycles (control_strategy, started_at);

CREATE INDEX idx_trial_events_run_time
    ON trial_events (experiment_run_id, event_time);

CREATE INDEX idx_reference_ranges_stage_active
    ON reference_ranges (calibration_stage, is_active);

-- =========================================
-- CREATE TABLE: system_settings
-- =========================================

CREATE TABLE system_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- =========================================
-- CREATE TABLE: notification_logs
-- =========================================

CREATE TABLE notification_logs (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    provider TEXT NOT NULL,
    recipient_number TEXT NOT NULL,
    sender_id TEXT,
    alert_type TEXT NOT NULL,
    message_body TEXT NOT NULL,
    status TEXT NOT NULL,
    provider_response TEXT,
    provider_message_id BIGINT,
    latest_provider_response TEXT,
    provider_status_updated_at TIMESTAMPTZ,
    status_checked_at TIMESTAMPTZ,
    status_check_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    system_log_id BIGINT REFERENCES system_logs(id) ON DELETE SET NULL,
    control_cycle_id BIGINT REFERENCES control_cycles(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX notification_logs_timestamp_idx
    ON notification_logs (timestamp DESC);

CREATE INDEX notification_logs_status_alert_idx
    ON notification_logs (status, alert_type);


-- =========================================
-- COLUMN COMMENTS: reference_ranges
-- =========================================

COMMENT ON TABLE reference_ranges IS
'Stores shared crop and nutrient reference values used across HANAS experiment runs.';

COMMENT ON COLUMN reference_ranges.id IS
'Primary key for the reference range record.';

COMMENT ON COLUMN reference_ranges.reference_name IS
'Human-readable reference set name, such as HANAS 3.5L Bench-Scale Reference or HANAS Phase 2 20L Operating Reference.';

COMMENT ON COLUMN reference_ranges.calibration_stage IS
'Calibration stage for this reference set. Allowed values are bench_scale, operating_volume, and validation.';

COMMENT ON COLUMN reference_ranges.calibration_source_volume_liters IS
'Reservoir volume used to produce or recalibrate the coefficient set stored in this row.';

COMMENT ON COLUMN reference_ranges.intended_reservoir_volume_liters IS
'Reservoir volume this reference set is intended to support.';

COMMENT ON COLUMN reference_ranges.is_active IS
'Whether this reference set is intended to be selected for new experiment runs by default.';

COMMENT ON COLUMN reference_ranges.crop_type IS
'Crop used in the HANAS hydroponic experiment, such as lettuce.';

COMMENT ON COLUMN reference_ranges.growth_stage IS
'Plant growth stage associated with the recommended pH and EC ranges.';

COMMENT ON COLUMN reference_ranges.hydroponic_system_type IS
'Hydroponic system type used for this reference set, such as DFT for Deep Flow Technique.';

COMMENT ON COLUMN reference_ranges.ph_target_min IS
'Minimum recommended pH value for the crop and growth stage.';

COMMENT ON COLUMN reference_ranges.ph_target_max IS
'Maximum recommended pH value for the crop and growth stage.';

COMMENT ON COLUMN reference_ranges.ec_target_min IS
'Minimum recommended EC value for the crop and growth stage.';

COMMENT ON COLUMN reference_ranges.ec_target_max IS
'Maximum recommended EC value for the crop and growth stage.';

COMMENT ON COLUMN reference_ranges.ph_up_dose_ml_per_liter_per_unit IS
'Estimated pH Up dose, in milliliters per reservoir liter per pH unit below the target range.';

COMMENT ON COLUMN reference_ranges.ph_down_dose_ml_per_liter_per_unit IS
'Estimated pH Down dose, in milliliters per reservoir liter per pH unit above the target range.';

COMMENT ON COLUMN reference_ranges.ec_up_dose_ml_per_liter_per_unit IS
'Estimated EC Up nutrient dose, in milliliters per component per reservoir liter per EC unit below the target range.';

COMMENT ON COLUMN reference_ranges.ec_down_dose_ml_per_liter_per_unit IS
'Estimated EC Down dilution dose, in milliliters per reservoir liter per EC unit above the target range.';

COMMENT ON COLUMN reference_ranges.ph_pump_flow_ml_per_min IS
'Estimated flow rate of the pH dosing pumps in milliliters per minute.';

COMMENT ON COLUMN reference_ranges.ec_pump_flow_ml_per_min IS
'Estimated flow rate of the EC dosing pumps in milliliters per minute.';

COMMENT ON COLUMN reference_ranges.max_dose_ml_per_cycle IS
'Maximum dose per control cycle when no per-pump volume limit is configured.';

COMMENT ON COLUMN reference_ranges.ph_up_max_dose_ml_per_cycle IS
'Maximum allowed pH Up dose volume per control cycle for safety.';

COMMENT ON COLUMN reference_ranges.ph_down_max_dose_ml_per_cycle IS
'Maximum allowed pH Down dose volume per control cycle for safety.';

COMMENT ON COLUMN reference_ranges.ec_up_max_dose_ml_per_cycle IS
'Maximum allowed EC Up dose volume per component per control cycle for safety.';

COMMENT ON COLUMN reference_ranges.ec_down_max_dose_ml_per_cycle IS
'Maximum allowed EC Down dilution-water dose volume per control cycle for safety.';

COMMENT ON COLUMN reference_ranges.ph_up_max_duration_ms IS
'Maximum pH Up pump runtime per control cycle.';

COMMENT ON COLUMN reference_ranges.ph_down_max_duration_ms IS
'Maximum pH Down pump runtime per control cycle.';

COMMENT ON COLUMN reference_ranges.ec_up_max_duration_ms IS
'Maximum EC Up pump runtime per component per control cycle.';

COMMENT ON COLUMN reference_ranges.ec_down_max_duration_ms IS
'Maximum EC Down dilution-water pump runtime per control cycle.';

COMMENT ON COLUMN reference_ranges.notes IS
'Additional notes about the reference range or configuration.';


-- =========================================
-- COLUMN COMMENTS: experiment_runs
-- =========================================

COMMENT ON TABLE experiment_runs IS
'Stores individual experiment sessions and identifies the control strategy used.';

COMMENT ON COLUMN experiment_runs.id IS
'Primary key for the experiment run.';

COMMENT ON COLUMN experiment_runs.reference_range_id IS
'Foreign key linking the run to the shared pH and EC reference range.';

COMMENT ON COLUMN experiment_runs.run_name IS
'Human-readable name of the experiment run.';

COMMENT ON COLUMN experiment_runs.experiment_phase IS
'Experiment phase associated with the run. Phase 2 uses dev; Phase 3 uses prod.';

COMMENT ON COLUMN experiment_runs.control_strategy IS
'Control strategy used in the run. Allowed values are baseline and agentic_ai.';

COMMENT ON COLUMN experiment_runs.reservoir_max_volume_liters IS
'Reservoir capacity configured for this experiment run.';

COMMENT ON COLUMN experiment_runs.sampling_interval_seconds IS
'Expected interval between stable backend submissions for this experiment run.';

COMMENT ON COLUMN experiment_runs.mixing_time_seconds IS
'Expected post-dose mixing wait before the next control decision.';

COMMENT ON COLUMN experiment_runs.initial_confirmation_gap_seconds IS
'Minimum backend wall-clock gap between the first out-of-range agentic reading and a confirmation reading before dosing.';

COMMENT ON COLUMN experiment_runs.stability_required_seconds IS
'Required stable pH and EC duration before accepting a reading.';

COMMENT ON COLUMN experiment_runs.ph_stability_threshold IS
'pH stability threshold configured for this experiment run.';

COMMENT ON COLUMN experiment_runs.ec_stability_threshold IS
'EC stability threshold configured for this experiment run.';

COMMENT ON COLUMN experiment_runs.start_time IS
'Timestamp when the experiment run started.';

COMMENT ON COLUMN experiment_runs.end_time IS
'Timestamp when the experiment run ended.';

COMMENT ON COLUMN experiment_runs.notes IS
'Additional notes about the experiment run.';


-- =========================================
-- COLUMN COMMENTS: trial_events
-- =========================================

COMMENT ON TABLE trial_events IS
'Stores manually recorded benchmark-trial events such as reservoir disturbances, resets, calibrations, controller trial annotations, and experiment notes.';

COMMENT ON COLUMN trial_events.id IS
'Primary key for the manual trial event.';

COMMENT ON COLUMN trial_events.experiment_run_id IS
'Foreign key linking the event to the experiment run where it occurred.';

COMMENT ON COLUMN trial_events.event_time IS
'Timestamp when the manual trial event occurred. Defaults to insertion time, but can be manually backfilled.';

COMMENT ON COLUMN trial_events.event_type IS
'Manual event category. Allowed values are disturbance, reset, calibration, and manual_note.';

COMMENT ON COLUMN trial_events.disturbance_type IS
'Controlled disturbance type for benchmark episodes, such as ph_low, ph_high, ec_low, or ec_high.';

COMMENT ON COLUMN trial_events.amount_added_ml IS
'Manual amount added during the event, in milliliters, when applicable.';

COMMENT ON COLUMN trial_events.target_metric IS
'Primary metric affected or observed by the event, such as ph, ec, temperature, or reservoir_volume.';

COMMENT ON COLUMN trial_events.controller_type IS
'Controller used during the benchmark trial. Allowed values are baseline and agentic_ai.';

COMMENT ON COLUMN trial_events.severity_level IS
'Severity level of the controlled disturbance. Allowed values are mild, moderate, and severe.';

COMMENT ON COLUMN trial_events.trial_number IS
'Repeated trial number for a given controller, scenario, and severity level. For example, trial 1 or trial 2.';

COMMENT ON COLUMN trial_events.episode_name IS
'Human-readable benchmark episode name, such as ph_low_benchmark, ph_high_benchmark, ec_low_benchmark, or ec_high_benchmark.';

COMMENT ON COLUMN trial_events.working_volume_liters IS
'Actual reservoir working volume during the benchmark episode, such as 20 liters in a 34 liter reservoir.';

COMMENT ON COLUMN trial_events.before_ph IS
'Manual or observed pH immediately before the disturbance, reset, or calibration event.';

COMMENT ON COLUMN trial_events.before_ec IS
'Manual or observed EC immediately before the disturbance, reset, or calibration event.';

COMMENT ON COLUMN trial_events.before_temperature_c IS
'Manual or observed water temperature in degrees Celsius immediately before the disturbance, reset, or calibration event.';

COMMENT ON COLUMN trial_events.after_ph IS
'Manual or observed pH immediately after the disturbance was mixed enough to define the starting disturbed condition.';

COMMENT ON COLUMN trial_events.after_ec IS
'Manual or observed EC immediately after the disturbance was mixed enough to define the starting disturbed condition.';

COMMENT ON COLUMN trial_events.after_temperature_c IS
'Manual or observed water temperature in degrees Celsius immediately after the disturbance was mixed enough to define the starting disturbed condition.';

COMMENT ON COLUMN trial_events.notes IS
'Free-text details about the disturbance, reset, calibration, controller behavior, or manual observation.';


-- =========================================
-- COLUMN COMMENTS: control_cycles
-- =========================================

COMMENT ON TABLE control_cycles IS
'Groups logs belonging to one read-decide-act control cycle.';

COMMENT ON COLUMN control_cycles.id IS
'Primary key for the control cycle.';

COMMENT ON COLUMN control_cycles.experiment_run_id IS
'Foreign key linking the control cycle to a specific experiment run.';

COMMENT ON COLUMN control_cycles.control_strategy IS
'Control strategy used for this cycle, either baseline or agentic_ai.';

COMMENT ON COLUMN control_cycles.started_at IS
'Timestamp when the control cycle started.';

COMMENT ON COLUMN control_cycles.deviation_detected_at IS
'Timestamp when an out-of-range pH or EC deviation was detected for the control cycle.';

COMMENT ON COLUMN control_cycles.action_started_at IS
'Timestamp when the ESP32 reported that the dosing action physically started.';

COMMENT ON COLUMN control_cycles.action_completed_at IS
'Timestamp when the ESP32 reported that the dosing action physically completed.';

COMMENT ON COLUMN control_cycles.completed_at IS
'Timestamp when the control cycle completed, if tracked by the application.';

COMMENT ON COLUMN control_cycles.status IS
'Current high-level control-cycle status, such as within_range, dosing, mixing, completed, or error.';


-- =========================================
-- COLUMN COMMENTS: system_logs
-- =========================================

COMMENT ON TABLE system_logs IS
'Stores HANAS time-series sensor readings, decisions, and actuator actions during system operation.';

COMMENT ON COLUMN system_logs.id IS
'Primary key for each system log entry.';

COMMENT ON COLUMN system_logs.experiment_run_id IS
'Foreign key linking the log entry to a specific experiment run.';

COMMENT ON COLUMN system_logs.control_strategy IS
'Control strategy that produced the logged decision. This intentionally duplicates the run strategy so phase comparisons can filter without parsing JSON metadata.';

COMMENT ON COLUMN system_logs.timestamp IS
'Timestamp when the sensor reading or system event was recorded.';

COMMENT ON COLUMN system_logs.ph IS
'Measured pH value of the nutrient solution.';

COMMENT ON COLUMN system_logs.ec IS
'Measured electrical conductivity of the nutrient solution.';

COMMENT ON COLUMN system_logs.temperature IS
'Measured water temperature of the nutrient solution in degrees Celsius.';

COMMENT ON COLUMN system_logs.reservoir_volume_liters IS
'Estimated or configured nutrient reservoir volume in liters.';

COMMENT ON COLUMN system_logs.decision IS
'Decision generated by the control strategy, such as within_range, ph_low, ph_high, ec_low, ec_high, wait_for_mixing, or wait_initial_confirmation.';

COMMENT ON COLUMN system_logs.pump_activated IS
'Logical pump action requested during the control cycle, if any. For two-part nutrients, ec_up maps to separate EC Up A and EC Up B pumps on the controller.';

COMMENT ON COLUMN system_logs.dose_ml IS
'Estimated dose volume in milliliters. For ec_up, this is the per-component A/B dose; total nutrient concentrate added is recorded in decision_metadata.';

COMMENT ON COLUMN system_logs.duration_ms IS
'Pump activation duration in milliseconds.';

COMMENT ON COLUMN system_logs.mixing_base_seconds IS
'Base post-dose mixing window in seconds before agentic adjustment.';

COMMENT ON COLUMN system_logs.mixing_effective_seconds IS
'Bounded effective mixing window in seconds used by the agentic safety gate.';

COMMENT ON COLUMN system_logs.mixing_elapsed_seconds IS
'Elapsed seconds since the latest dose when this log evaluated a mixing wait.';

COMMENT ON COLUMN system_logs.mixing_remaining_seconds IS
'Remaining seconds in the effective mixing window when this log evaluated a mixing wait.';

COMMENT ON COLUMN system_logs.mixing_adjustment_factor IS
'Final bounded multiplier applied to the base mixing window.';

COMMENT ON COLUMN system_logs.ph_within_range IS
'Whether the pH reading was within the configured pH reference range.';

COMMENT ON COLUMN system_logs.ec_within_range IS
'Whether the EC reading was within the configured EC reference range.';

COMMENT ON COLUMN system_logs.ph_deviation IS
'Absolute distance of the pH reading from the configured pH reference range, or zero when within range.';

COMMENT ON COLUMN system_logs.ec_deviation IS
'Absolute distance of the EC reading from the configured EC reference range, or zero when within range.';

COMMENT ON COLUMN system_logs.decision_reason IS
'Human-readable explanation for the generated decision.';

COMMENT ON COLUMN system_logs.decision_metadata IS
'Structured metadata for comparing baseline and agentic_ai decisions.';

COMMENT ON COLUMN system_logs.status IS
'High-level system condition at the time of logging, such as within_range, dosing, mixing, confirming, waiting, unstable, sensor_anomaly, no_action, or error.';

COMMENT ON COLUMN system_logs.control_cycle_id IS
'Foreign key linking the log entry to a read-decide-act control cycle.';
