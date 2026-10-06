<picture>
  <source media="(prefers-color-scheme: dark)" srcset="hanas_frontend/public/hanas-mark-dark.svg">
  <img src="hanas_frontend/public/hanas-mark-light.svg" alt="HANAS logo" width="96" height="96">
</picture>

# HANAS

HANAS is the Hydroponic Agentic Nutrient Adjustment System. It monitors hydroponic
pH, EC, water temperature, and reservoir volume readings, records each control
cycle in PostgreSQL, and returns dosing decisions to an ESP32-based sensor and
actuator controller.

The backend supports both the rule-based `baseline` strategy and the
history-aware `agentic_ai` strategy. Both return the same ESP32 pump contract so
Phase 2 and Phase 3 runs can be compared with the same database schema.

## Requirements

Use Python 3.12, Node.js 24, PostgreSQL 16, and the Arduino ESP32 toolchain.
See [embedded_system/BUILD.md](embedded_system/BUILD.md) for firmware build requirements.

Initialize the database using the [Database](#database) instructions before starting
the backend. Then follow [Backend](#backend) and [Frontend](#frontend) to run the app.

## Experiment Phases

HANAS is organized around three research phases:

| Phase | Purpose | System setup |
| --- | --- | --- |
| Phase 1 | Calibrate and test individual sensors, pumps, dosing rates, and connectivity before running closed-loop experiments. | Use the sketches in `embedded_system/calibration_and_testing/`. |
| Phase 2 | Run controlled benchmark experiments comparing the rule-based `baseline` and proposed `agentic_ai` controller. | Set `APP_ENV=dev`; backend uses the `dev` schema and Phase 2 defaults. |
| Phase 3 | Run live hydroponic validation with plants and continue analysis using the same schema shape. | Set `APP_ENV=prod`; backend uses the `prod` schema and Phase 3 defaults. |

Phase 1 calibration scripts include pH, EC, water temperature, water level,
peristaltic pump, and Wi-Fi connectivity tests. Phase 2 records controlled
disturbance annotations in `trial_events` while the backend records automated
sensor, decision, and control-cycle logs. Phase 2 and Phase 3 use the same DDL
and analysis queries so results remain comparable across schemas.

Phase 2 response coefficients can be reused in Phase 3 as starting calibration
priors. Treat them as expected response estimates rather than fixed truth: Phase
3 compares expected pH/EC change from the Phase 2 coefficient with the
actual post-dose/post-mixing reading and record the residual. This avoids a
separate water-heavy Phase 3 coefficient experiment unless Phase 3 repeatedly
shows large prediction errors.

## Repository Layout

```text
hanas_backend/                            FastAPI backend for the HANAS API
hanas_frontend/                           React/Vite dashboard for operators
embedded_system/hanas_sensor_controller/  ESP32 sensor and actuator controller
camera_gateway/                           go2rtc camera gateway examples
sql/                                      Fresh dev/prod database initialization
research_analysis/                        Research notebooks and analysis scripts
```

## Backend

The backend exposes:

```text
GET  /
POST /api/sensor-data
POST /api/control-cycles/{control_cycle_id}/action-started
POST /api/control-cycles/{control_cycle_id}/action-completed
POST /api/control-cycles/{control_cycle_id}/complete
```

To run locally:

```bash
cd hanas_backend
python3.12 -m venv venv
venv/bin/pip install -r requirements.dev.txt
cp .env.example .env
./start.sh
```

Local database connection defaults are read from:

```text
hanas_backend/app/database/database.ini
```

Non-secret application defaults are read from:

```text
hanas_backend/app/core/config.ini
```

Sensitive credentials and deployment connection values can be set in
`hanas_backend/.env`, including `DB_SSLMODE` when your database requires a
specific SSL setting. The one non-secret selector in `.env` is `APP_ENV`, which
chooses a profile from `hanas_backend/app/core/config.ini`. Non-confidential
system behavior, such as schema, experiment phase, strategy, and timing
defaults, lives in `config.ini`.

## Frontend

The operator dashboard lives in `hanas_frontend/`.

Run locally:

```bash
cd hanas_frontend
npm ci
npm run dev
```

Build for production:

```bash
npm run build
```

Common local URLs:

```text
Frontend: http://localhost:5173
Backend:  http://localhost:8080
```

The dashboard backend URL can be changed from the frontend Settings page. For
build-time defaults, use:

```bash
VITE_API_BASE_URL=http://localhost:8080
VITE_CAMERA_STREAM_URL=https://camera-gateway.example/stream.html?src=lettuce_cam
```

`VITE_CAMERA_STREAM_URL` is the browser-safe go2rtc URL. Keep raw Tapo RTSP
URLs and camera credentials out of frontend configuration.

### Dashboard Operator Features

The dashboard is built for the Phase 3 operator workflow:

- Overview shows the current system state, the latest 12-hour operator recap,
  maintenance-mode status, and crop lifecycle progress.
- Monitoring Only keeps ESP32 readings flowing to the backend and dashboard
  while pausing control-agent analysis and new automatic pump commands. Full
  Agentic Mode can remain configured and resumes after operator confirmation.
  Monitoring Only does not interrupt a pump already running; Emergency Stop is
  the immediate physical-command lockout.
- Reservoir, Dosing, Trends, Camera, AI Reasoning, Logs & Alerts, Help,
  and Settings are available from the desktop sidebar and mobile bottom nav.
- Mobile and tablet views use blocking overlays for trend drilldowns,
  notifications, and the agent pipeline so taps on a dimmed background close the
  current panel before any underlying control can be activated.
- Trend charts support mouse hover on desktop and touch/drag inspection on
  mobile/tablet. Tooltips remain within the viewport, with detail panels opened
  from intentional chart selections.
- AI Reasoning includes a live state graph for the latest agentic decision. The
  graph uses icon nodes, selectable route highlighting, drag-to-rearrange, reset,
  mouse-wheel/trackpad panning, and click-hold-drag panning on empty canvas
  space. On desktop, the AI Pipeline side panel stays open unless the operator
  closes it; on tablet and mobile it behaves as a modal drawer.
- The dashboard fetches a cached seven-day history window for lifecycle
  summaries, then loads smaller selected ranges inside Trends. This keeps crop
  day tooltips useful without polling the largest history query every refresh.

### Crop Lifecycle

Crop age is configured from Settings under the Crop section. Set the transplant
date and the expected harvest window, for example day `30` to day `35` for
lettuce. Once saved, Overview shows the crop journey from transplant through
harvest and exposes milestone tooltips.

Lifecycle milestone tooltips summarize values found in the loaded history for
that crop day:

```text
Average pH
Average EC
Average water temperature
Average reservoir volume
Total dose
Reading count
```

The same crop lifecycle context is sent through backend settings for dosing
decisions and overview recaps. After harvest, or after a failed batch, clearing
or resetting the transplant date starts the next crop from the correct day.

### Camera View

HANAS can show a live grow-room camera through the Camera page. The current
recommended student-friendly setup is:

```text
Tapo C310 camera -> Raspberry Pi go2rtc -> Tailscale private IP -> HANAS browser
```

The Raspberry Pi remains responsible for camera access and 24-hour HDD
recording. HANAS only embeds the browser stream. The phone or laptop opening
the Azure-hosted HANAS frontend also needs access to the same Tailscale network,
because the browser loads the camera stream directly from the Pi.

For local HTTP development, use this camera URL format in frontend Settings or
`VITE_CAMERA_STREAM_URL`:

```text
http://<pi-tailscale-ip>:1984/stream.html?src=lettuce_cam
```

An HTTPS-hosted HANAS dashboard cannot embed that HTTP URL because browsers
block active mixed content. For the deployed dashboard, expose the private
go2rtc page through an HTTPS-enabled gateway or reverse proxy and configure its
`https://` URL. Opening the HTTP camera in a separate tab remains available as
a fallback, but it will not render inside the dashboard.

The HDD recorder on the Pi uses the local go2rtc RTSP relay:

```text
rtsp://127.0.0.1:8554/lettuce_cam
```

See `camera_gateway/README.md` for the Raspberry Pi and go2rtc setup.

### Agentic AI Control Flow

The `agentic_ai` strategy runs a structured graph coordinated by the
orchestrator:

```text
input_context
  -> orchestrator_agent
      -> monitoring_agent -> orchestrator_agent
      -> diagnostic_reasoning_agent -> orchestrator_agent
      -> decision_agent -> orchestrator_agent
      -> dose_planning_agent -> orchestrator_agent
      -> consistency_review -> orchestrator_agent
      -> safety_gate
      -> human_review_gate
      -> final backend decision
      -> ESP32 execution or no-action
      -> next reading/history
```

Agents communicate through structured graph state. The orchestrator asks only
the specialist needed for the current situation, receives structured feedback,
and then decides whether to ask another specialist, stop early, or hand off to
the deterministic safety gate. This keeps stable readings from spending tokens
on unnecessary stages while still preserving a trace when diagnosis or dose
planning is required.

The final safety gate is deterministic Python; it validates the agent plan,
enforces pump/dose/mixing bounds, and writes metadata explaining what was
accepted, bounded, skipped, or overridden. The Human Review Gate is represented
in the graph after the safety gate. Runtime operator holds remain backed by the
database control-cycle state so a pending command survives refreshes, browser
disconnects, and ESP32 polling intervals.

The response metadata includes the main trace fields used during Phase 2:

```text
baseline_shadow              Rule-based decision for comparison
agentic_primary_shadow       Agentic one-action correction before factor scaling
monitoring_agent             Stability/range/trend assessment
diagnostic_reasoning_agent   Classification and primary metric
decision_agent               Dose, wait, monitor, or fallback action
dose_planning_agent          Pump, dose factor, mixing factor, and reason
consistency_review           Cross-agent validation result
human_review_gate            Operator hold/release stage after safety bounds
same_pump_response           Recent same-pump history interpretation
mixing_window                Effective bounded post-dose mixing window
safety_gate_source           Deterministic safety gate marker
llm_trace                    Per-agent structured LLM outputs
```

#### Confirmation And History

Agentic AI waits for one fresh matching confirmation before dosing a new
out-of-range condition. This prevents a startup or transient sensor shock from
triggering a pump action on the first reading. Confirmation waits appear as
`wait_initial_confirmation`.

Recent same-pump history is used as advisory context for the Dose Planning
Agent and as safety metadata for the final decision. For example:

```text
previous_same_pump_resolved_or_changed_condition
previous_same_pump_still_unresolved
previous_same_pump_overshot_opposite_direction
```

Overshoot-risk history can reduce the fallback/reference dose factor. The agent
still chooses the requested dose factor, and the safety gate clamps it to the
configured safe range.

#### Adaptive Dosing

Agentic dosing starts from the bounded midpoint correction in
`agentic_primary_shadow`, then applies the Dose Planning Agent's
`dose_adjustment_factor`. The current safety range is:

```text
0.4 <= dose_adjustment_factor <= 2.0
```

A factor below `1.0` means a cautious/reduced correction. A factor of `1.0`
means the full bounded midpoint correction. A factor above `1.0` is allowed only
when severity, history, target headroom, and pump caps justify a stronger
follow-up. Pump-specific maximum dose and duration limits are still enforced
after the factor is applied.

The Dose Planning Agent can also request a `mixing_adjustment_factor`. The
safety gate bounds the final post-dose mixing window between the configured
minimum and maximum. Phase 2 commonly used longer mixing for persistent EC or
mixed disturbances.

#### Mixed Disturbances

When both pH and EC are out of range, Agentic AI performs one correction per
cycle and defers the other metric until after mixing and remeasurement.

For mixed disturbances:

- `diagnostic_reasoning_agent.classification` is `combined_disturbance`.
- `diagnostic_reasoning_agent.primary_metric` is either `ph` or `ec`.
- The Diagnostic Agent acts as the primary selector for non-severe mixed cases.
- The backend safety validator can force pH first when pH deviation is severe.
- The Dose Planning Agent selects the pump that matches the validated
  `agentic_primary_shadow`.

The current severe pH override is:

```text
abs(pH boundary deviation) >= 0.5
```

For example, with a pH target minimum of `5.5`, pH `5.0` or lower is severe on
the low side. With a pH target maximum of `6.5`, pH `7.0` or higher is severe on
the high side. In that case, pH is corrected first even if EC is also outside
range. Otherwise, the Diagnostic Agent can select EC first when nutrient or
dilution changes are likely to shift pH during mixing.

Final mixed-disturbance metadata records the selected first correction and notes
when the other out-of-range metric is deferred.

### Phase 3 Batch Mode And Overview Summary

In Phase 3, per-reading emergency guards remain deterministic while the
agentic pipeline runs every 10 minutes on the scheduled batch interval. The
frontend labels the pipeline drawer as a live run only while a backend stage is
active; otherwise it shows the latest saved batch result and the next
scheduled batch time.

Operators can opt into **Full Agentic Mode** from Settings. After an explicit
token-usage warning, the complete LLM pipeline runs for every incoming sensor
reading (normally every 60 seconds) and the scheduled 10-minute batch pauses.
The mode requires `default_control_strategy=agentic_ai` and `OPENAI_API_KEY`.
Deterministic dose limits, active-command and mixing locks, emergency stop, and
optional human review still gate physical pump commands. LLM failures and
overlapping agent cycles fail closed without deterministic fallback dosing.

The Agentic AI context includes crop lifecycle values when configured:

```text
crop type / variety
transplant date
crop age in days
growth stage label
days until harvest window
harvest window start and end day
```

This context helps dosing decisions and recaps distinguish young transplanted
lettuce, established growth, and harvest-window crops. The deterministic safety
gate still enforces pump, dose, duration, and mixing limits.

The Overview page also includes a 12-hour operator summary. It is anchored to
local Philippine time and runs at:

```text
12:00 AM
12:00 PM
```

When a summary exists, Overview displays the latest saved summary, readings
count, dosing total, pH/EC range, highlights, and watch items. If no summary has
been generated yet, the card shows the next fixed run time instead of a long
minute countdown.

## Database

HANAS uses PostgreSQL with the `prod` schema by default. Set `APP_ENV=dev` in
`hanas_backend/.env` to select the Phase 2 dev profile. Use `dev` for Phase 2
controlled benchmarking and `prod` for Phase 3 live hydroponic validation. Both
schemas use the same DDL so Phase 3 data can be analyzed with the same queries.

Create an empty database named `hanas`, then initialize the required schemas
from the release root:

```bash
psql -v ON_ERROR_STOP=1 -d hanas -f sql/init_dev.sql
psql -v ON_ERROR_STOP=1 -d hanas -f sql/init_prod.sql
```

Supply database connection credentials through your PostgreSQL client configuration.
These scripts create tables and seed reference settings. They do not reset existing
data or upgrade old schemas; rerunning them against populated schemas fails.
Set `APP_ENV=dev` or `APP_ENV=prod` to select the corresponding backend profile.

Optional manual metric SQL is excluded from this release. Use the retained research notebooks and scripts for result reproduction.


## ESP32 Controller

The Arduino sketch lives at:

```text
embedded_system/hanas_sensor_controller/hanas_sensor_controller.ino
```

Copy `embedded_system/hanas_sensor_controller/secrets.example.h` to
`embedded_system/hanas_sensor_controller/secrets.h`, then fill in Wi-Fi and API
settings:

```cpp
#define WIFI_SSID "your-wifi"
#define WIFI_PASSWORD "your-password"
// Optional fallback network used after primary Wi-Fi retries fail.
#define WIFI_BACKUP_SSID "your-backup-wifi"
#define WIFI_BACKUP_PASSWORD "your-backup-password"
// Use your local backend URL or your Azure App Service sensor endpoint.
#define SERVER_URL "https://<app-name>.azurewebsites.net/api/sensor-data"
#define DEVICE_API_TOKEN "same-token-as-backend"
```

If `DEVICE_API_TOKEN` is empty in the backend, the ESP32 token header is not
required for local testing. For Azure, set `DEVICE_API_TOKEN` in App Service and
use the same value in `secrets.h`.

For production dashboard write actions, set `OPERATOR_API_TOKEN` in App Service
and enter the same token in the frontend Settings page. This protects HITL
approval/override and runtime settings updates. Leave it empty only for local
testing.

If `WIFI_BACKUP_SSID` is omitted or empty, the controller only uses the primary
network. If it is configured, the controller tries the primary network for three
connection rounds before switching to the backup network. Both networks use the
same Azure `SERVER_URL`; the backup hotspot only changes connectivity, not the
backend endpoint.

The controller defaults to a 70 L reservoir for Phase 3. For Phase 2, compile
or edit the controller with a 20 L reservoir so the ESP32 payload matches the
backend `APP_ENV=dev` defaults:

```cpp
#define RESERVOIR_MAX_VOLUME_LITERS 20.0
```

For Phase 3, keep the default:

```cpp
#define RESERVOIR_MAX_VOLUME_LITERS 70.0
```

The controller sends `CONTROL_STRATEGY` with each reading. Use `baseline` for
the rule-based branch or `agentic_ai` for the history-aware branch:

```cpp
#define CONTROL_STRATEGY "agentic_ai"
```

After a pump action, the backend returns `mixing_time_ms` with the decision.
Baseline runs normally return the configured 120 second window, while
Phase 3 `agentic_ai` emergency corrections currently return a deterministic
300 second post-dose guard. Agentic batch decisions may still carry their own
bounded mixing window metadata. The ESP32 uses the backend response value and
uses its compiled `MIXING_TIME_MS` safety default if the response omits a
positive mixing duration.

The ESP32 also reports control-cycle action state back to the backend:

```text
POST /api/control-cycles/{control_cycle_id}/action-started
POST /api/control-cycles/{control_cycle_id}/action-completed
POST /api/control-cycles/{control_cycle_id}/complete
```

The ESP32 reports `action-completed` with status `mixing` as soon as the pump
stops, then reports `complete` only after the post-dose mixing timer finishes.
This keeps the dashboard countdown and the backend physical guard aligned with
the reservoir.

If power or Wi-Fi is lost after dosing and the cycle remains marked `dosing` or `mixing`,
the same complete endpoint can be called once when the action is known to have
finished:

```bash
curl -X POST "http://localhost:8080/api/control-cycles/<id>/complete" \
  -H "Content-Type: application/json" \
  -H "X-Device-Token: <configured-device-token>" \
  -d '{"status":"completed"}'
```

Use this only after confirming that both pump actuation and mixing have finished.
It marks the cycle and its dosing log as completed. The token header is required
when DEVICE_API_TOKEN is configured.

For EC-up dosing, the ESP32 doses Sol A first, waits, then doses Sol B. That
inter-dose wait is separate from the post-dose `mixing_time_ms`: by default it is
120 seconds for the 20 L Phase 2 air-stone-only controller configuration and 180
seconds for larger circulated systems such as the 70 L Phase 3 hydroponic loop.
Define `EC_UP_INTER_DOSE_MIXING_MS` in
`embedded_system/hanas_sensor_controller/config.h` to override it after wet
testing your circulation setup.

## Azure

Configure Azure App Service and Azure Database for PostgreSQL with these settings:

```text
DB_USERNAME=<azure-postgres-user>
DB_PASSWORD=<azure-postgres-password>
DB_HOST=<server-name>.postgres.database.azure.com
DB_NAME=hanas
DB_PORT=5432
DB_SSLMODE=require
APP_ENV=prod
DEVICE_API_TOKEN=<shared-device-token>
OPERATOR_API_TOKEN=<dashboard-operator-token>
OPENAI_API_KEY=<openai-api-key>
CORS_ALLOW_ORIGINS=https://<Azure frontend defaultHostName>
```

Set `VITE_API_BASE_URL` to the backend HTTPS address before building the frontend.
Deployment workflows are not included in this package. Configure both tokens and
set the CORS origin to the exact frontend origin.

## Research analysis

`research_analysis/` contains the calibration and experiment notebooks and analysis
scripts. Install `research_analysis/requirements.txt` in a separate Python 3.12
environment. Place the published workbooks in `research_analysis/raw_data/phase1`,
`phase2`, and `phase3`; see [research_analysis/README.md](research_analysis/README.md)
for the expected files and analysis commands.

## Tests

Run the backend test suite from the repository root after installing
`hanas_backend/requirements.dev.txt`:

```bash
hanas_backend/venv/bin/python -m pytest hanas_backend/tests
```

Run frontend checks from `hanas_frontend/`:

```bash
npm ci
npm run lint
npm run build
npx playwright install chromium
npm run test:e2e
```

Compile the main ESP32 controller sketch from the repository root:

```bash
arduino-cli compile --fqbn esp32:esp32:esp32 embedded_system/hanas_sensor_controller
```

Before production deployment, run the full hard-test checklist:

```bash
cd hanas_frontend && npm run lint && npm run build
cd ..
hanas_backend/venv/bin/python -m compileall hanas_backend/app
hanas_backend/venv/bin/python -m pytest hanas_backend/tests
arduino-cli compile --fqbn esp32:esp32:esp32 embedded_system/hanas_sensor_controller
```

See `VERIFICATION.json` for recorded check results and skipped tests.
Device testing and deployment verification remain separate from software checks.

### Monitoring recovery validation

The monitoring agent assesses natural recovery from chronological sensor readings
and separate pump events. Monitoring uses `agentic_ai_monitoring_model`
(`gpt-4.1-mini`). Diagnostic uses `agentic_ai_diagnostic_model`
(`gpt-4.1-mini`). Dose Planning uses `agentic_ai_dose_planning_model`
(`gpt-4.1-mini`); other stages retain `agentic_ai_model`. Restart the backend
after changing model configuration or prompts. Hard safety checks still govern
the pump contract. Conflicting recovery evidence triggers one model review; an
unresolved conflict holds the cycle without dosing.

Before unattended production use, review recorded decisions and complete a
supervised device trial. Verify that recovery produces a waiting response with
zero pump duration, loss of recovery permits the appropriate bounded correction,
and mixing, sensor faults, and model failures preserve the safe pump contract.
Software regression tests do not replace physical verification.

Dose Planning now materializes `candidate_dose_ml`, `candidate_duration_ms`, and
`candidate_mixing_time_seconds` using calibrated calculation after its factor
proposal. These values appear in the planning trace before Safety Gate executes.
Safety Gate independently recalculates and rejects missing or mismatched
candidates, including pump misalignment. The ESP32 response fields remain the
validated final dose, duration, and mixing contract.
