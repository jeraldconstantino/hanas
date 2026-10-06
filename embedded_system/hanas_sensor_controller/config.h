#ifndef HANAS_SENSOR_CONTROLLER_CONFIG_H
#define HANAS_SENSOR_CONTROLLER_CONFIG_H

// Backend dosing strategy requested in the sensor payload.
#define CONTROL_STRATEGY "agentic_ai"

// Reservoir capacity used to convert live water-level percent to liters.
// Phase 3 production uses the 70 L DFT reservoir.
#define RESERVOIR_MAX_VOLUME_LITERS 70.0


// Optional EC Up A/B separation delay override. If omitted, the controller uses
// 120s for 20L air-stone-only setups and 180s for larger circulated systems.
// #define EC_UP_INTER_DOSE_MIXING_MS 120000UL

// Startup controller mode.
// 0 = MONITORING only (reads sensors locally, no backend, no dosing — use for calibration).
// 1 = FULL CONTROL (connects to backend, sends data every minute, executes dosing decisions).
// Phase 3 production should use 1 so the ESP32 works standalone without a serial monitor.
#define STARTUP_CONTROLLER_MODE 1

// Set to 1 to send the fixed test readings below instead of live sensor values.
#define USE_FIXED_TEST_READINGS 0

#define FIXED_TEST_TEMPERATURE_C 24.5
#define FIXED_TEST_PH 6.2
#define FIXED_TEST_EC 1.8

#define FIXED_TEST_WATER_LEVEL_RAW 0
#define FIXED_TEST_WATER_LEVEL_VOLTAGE 0.0
#define FIXED_TEST_WATER_LEVEL_CURRENT_MA 0.0
#define FIXED_TEST_WATER_LEVEL_CM 32.0
#define FIXED_TEST_WATER_LEVEL_PERCENT 78.0

#endif
