# EC Up execution timestamps

`20261025_ec_up_stage_timestamps.sql` adds nullable stage clocks to an existing
installation. Fresh installations include them in `init_dev.sql` and
`init_prod.sql`. Select the intended database and schema explicitly before
applying the migration. Stop control execution before coordinating the schema,
backend, and firmware updates. This change does not apply the migration or flash
any controller automatically. Backend startup/readiness rejects a schema missing
these stage columns.

The device callback contract is:

| Callback | Status | Meaning | First timestamp recorded |
| --- | --- | --- | --- |
| action-started | dosing | Before Pump A activation | action_started_at and ec_up_a_started_at |
| action-started | inter_dose_mixing | Pump A off; begin inter-dose mixing | inter_dose_mixing_started_at |
| action-started | ec_up_b_dosing | Inter-dose mixing ended; before Pump B activation | ec_up_b_started_at |
| action-completed | mixing | Pump B off; begin post-dose mixing | action_completed_at |
| complete | completed | Post-dose mixing ended | completed_at |
| complete | error / emergency_stopped | Sequence interrupted | completed_at; status indicates failure |

Timestamps reflect the backend's first accepted callback, using its clock.
Retries preserve the recorded timestamps and never move status backward.
A failed stage aborts execution; firmware must not replay physical doses on a
callback retry. If shutdown delivery fails, the device retries that callback
before attempting successful cycle completion.

`action_completed_at - action_started_at` is total sequence elapsed time,
including the inter-dose interval. `duration_ms` remains the commanded runtime
per nutrient pump. Pump A dosing ends at `inter_dose_mixing_started_at`,
inter-dose mixing ends at `ec_up_b_started_at`, and Pump B dosing ends at
`action_completed_at`. `completed_at - action_completed_at` represents
post-dose mixing for a successful cycle.

Historical overwritten sequence starts cannot be reconstructed reliably; this
migration leaves stage timestamps null. An EC Up cycle already in progress
under older firmware must be interrupted rather than resumed through the new
stage contract.
