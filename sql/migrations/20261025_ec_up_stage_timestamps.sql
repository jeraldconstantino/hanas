-- Run with the intended schema selected, e.g. PGOPTIONS='-c search_path=dev'.
-- Apply while control is stopped, before starting the updated backend/firmware.
-- Historical sequence starts cannot be recovered safely; do not backfill them.
BEGIN;
ALTER TABLE control_cycles
    ADD COLUMN IF NOT EXISTS ec_up_a_started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS inter_dose_mixing_started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS ec_up_b_started_at TIMESTAMPTZ;
COMMIT;
