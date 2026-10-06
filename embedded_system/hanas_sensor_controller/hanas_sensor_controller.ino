#include "secrets.h"
#if __has_include("config.h")
#include "config.h"
#endif
#include <Adafruit_ADS1X15.h>
#include <DallasTemperature.h>
#include <DFRobot_EC.h>
#include <DFRobot_PH.h>
#include <EEPROM.h>
#include <HTTPClient.h>
#include <OneWire.h>
#include <WiFi.h>
#include <Wire.h>
#include <math.h>
#include <esp_timer.h>

/*
  HANAS ESP32 sensor and actuator controller

  HANAS: Hydroponic Agentic Nutrient Adjustment System

  Responsibilities:
    - Read calibrated pH, EC, water temperature, and water level sensors.
    - Convert water level to current reservoir volume for the HANAS payload.
    - Send readings to the HANAS API.
    - Execute the HANAS dosing decision and report control-cycle status.

  Expected secrets.h:
    #define WIFI_SSID "..."
    #define WIFI_PASSWORD "..."
    #define WIFI_BACKUP_SSID "..."       // Optional fallback Wi-Fi.
    #define WIFI_BACKUP_PASSWORD "..."   // Optional fallback Wi-Fi.
    #define SERVER_URL "https://<app-name>.azurewebsites.net/api/sensor-data"
    #define DEVICE_API_TOKEN "same-token-as-backend"

  Configurable values live in config.h:
    #define CONTROL_STRATEGY "agentic_ai"
    #define RESERVOIR_MAX_VOLUME_LITERS 70.0
    #define USE_FIXED_TEST_READINGS 0
*/

#ifndef RESERVOIR_MAX_VOLUME_LITERS
#define RESERVOIR_MAX_VOLUME_LITERS 70.0
#endif

#ifndef USE_FIXED_TEST_READINGS
#define USE_FIXED_TEST_READINGS 0
#endif

#ifndef CONTROL_STRATEGY
#define CONTROL_STRATEGY "agentic_ai"
#endif

#ifndef WIFI_BACKUP_SSID
#define WIFI_BACKUP_SSID ""
#endif

#ifndef WIFI_BACKUP_PASSWORD
#define WIFI_BACKUP_PASSWORD ""
#endif

#ifndef FIXED_TEST_TEMPERATURE_C
#define FIXED_TEST_TEMPERATURE_C 24.5
#endif

#ifndef FIXED_TEST_PH
#define FIXED_TEST_PH 6.2
#endif

#ifndef FIXED_TEST_EC
#define FIXED_TEST_EC 1.8
#endif

#ifndef FIXED_TEST_WATER_LEVEL_RAW
#define FIXED_TEST_WATER_LEVEL_RAW 0
#endif

#ifndef FIXED_TEST_WATER_LEVEL_VOLTAGE
#define FIXED_TEST_WATER_LEVEL_VOLTAGE 0.0
#endif

#ifndef FIXED_TEST_WATER_LEVEL_CURRENT_MA
#define FIXED_TEST_WATER_LEVEL_CURRENT_MA 0.0
#endif

#ifndef FIXED_TEST_WATER_LEVEL_CM
#define FIXED_TEST_WATER_LEVEL_CM 32.0
#endif

#ifndef FIXED_TEST_WATER_LEVEL_PERCENT
#define FIXED_TEST_WATER_LEVEL_PERCENT 78.0
#endif

#ifndef FIXED_TEST_RESERVOIR_VOLUME_LITERS
#define FIXED_TEST_RESERVOIR_VOLUME_LITERS ((FIXED_TEST_WATER_LEVEL_PERCENT / 100.0) * RESERVOIR_MAX_VOLUME_LITERS)
#endif

#define I2C_SDA_PIN 21
#define I2C_SCL_PIN 22
#define TEMP_SENSOR_PIN 27

#define PH_UP_PUMP_PIN 23
#define PH_DOWN_PUMP_PIN 19
#define EC_UP_A_PUMP_PIN 18
#define EC_UP_B_PUMP_PIN 17
#define EC_DOWN_PUMP_PIN 16

// ADS1115 configuration
const uint8_t ADS_ADDRESS = 0x48;
const uint8_t PH_CHANNEL = 0;
const uint8_t EC_CHANNEL = 1;
const uint8_t WATER_LEVEL_CHANNEL = 2;
const uint8_t SAMPLE_COUNT = 20;

const float ADS_MV_PER_BIT = 0.125;          // ADS1115 GAIN_ONE: +/-4.096V.
const float PH_DIVIDER_MULTIPLIER = 1.735;   // Matches pH calibration sketch divider.
const float EC_DIVIDER_MULTIPLIER = 1.735;   // Matches EC calibration sketch divider.
const float LEVEL_SENSE_RESISTOR_OHMS = 150.0;
const float TANK_HEIGHT_CM = 41.0;
// Water height of 19.1625 cm corresponds to the 70 L operating volume.
const float OPERATING_MAX_LEVEL_CM = 19.1625;
const float LEVEL_ZERO_CURRENT_MA = 4.0;
const float LEVEL_FULL_CURRENT_MA = 20.00;

const unsigned long READ_INTERVAL_MS = 60000; // Sends stable data to HANAS every 1 minute.
const unsigned long MONITORING_READ_INTERVAL_MS = 1000;
const unsigned long MIXING_TIME_MS = 300000;  // Phase 3 fallback: wait 5 minutes after dosing before the next decision.
#ifndef EC_UP_INTER_DOSE_MIXING_MS
#define EC_UP_INTER_DOSE_MIXING_MS \
  ((RESERVOIR_MAX_VOLUME_LITERS <= 25.0) ? 120000UL : 180000UL)
#endif
const unsigned long EC_UP_INTER_DOSE_MIXING_DURATION_MS = EC_UP_INTER_DOSE_MIXING_MS;
const unsigned long WAIT_STATUS_LOG_INTERVAL_MS = 10000;
const unsigned long MIXING_STATUS_LOG_INTERVAL_MS = 5000;
const unsigned long WIFI_RETRY_DELAY_MS = 1000;
const unsigned long WIFI_SWITCH_DELAY_MS = 250;
const uint8_t WIFI_PRIMARY_CONNECT_ROUNDS = 3;
const uint8_t WIFI_BACKUP_CONNECT_ROUNDS = 3;
const uint8_t WIFI_CONNECT_WAIT_ATTEMPTS_PER_ROUND = 20;
const unsigned long HTTP_TIMEOUT_MS = 180000; // Covers worst-case multi-agent LLM fallback before ESP32 gives up.
const unsigned long BACKEND_PRECHECK_TIMEOUT_MS = 5000;
const unsigned long EMERGENCY_STOP_HTTP_TIMEOUT_MS = 1200;
const uint8_t BACKEND_PRECHECK_RETRY_COUNT = 3;
const unsigned long HITL_PENDING_COMMAND_POLL_MS = 5000;
const unsigned long EMERGENCY_STOP_POLL_MS = 1000;
const unsigned long SENSOR_STABLE_REQUIRED_MS = 30000;
const unsigned long SENSOR_STABILITY_TIMEOUT_MS = 180000;
const unsigned long SENSOR_STABILITY_SAMPLE_INTERVAL_MS = 1000;
// Keep live thresholds aligned with backend stability checks.
// Filtering reduces probe noise before the strict threshold is evaluated.
const float PH_STABLE_DELTA = 0.03;
const float EC_STABLE_DELTA = 0.03;
const float PH_FILTER_ALPHA = 0.20; // Matches calibration sketch smoothing for live stability checks.
const float EC_FILTER_ALPHA = 0.20;
const uint8_t UNSTABLE_RESET_COUNT = 3;

const char* primarySsid = WIFI_SSID;
const char* primaryPassword = WIFI_PASSWORD;
const char* backupSsid = WIFI_BACKUP_SSID;
const char* backupPassword = WIFI_BACKUP_PASSWORD;
const char* serverUrl = SERVER_URL;

Adafruit_ADS1115 ads;
DFRobot_PH ph;
DFRobot_EC ec;
OneWire oneWire(TEMP_SENSOR_PIN);
DallasTemperature tempSensors(&oneWire);

struct WaterLevelReading {
  int16_t raw;
  float voltage;
  float currentMa;
  float levelCm;
  float percent;
  float reservoirVolumeLiters;
};

struct SensorSnapshot {
  float temperature;
  float ph;
  float ec;
  unsigned long phStableForSeconds;
  unsigned long ecStableForSeconds;
  float phStabilityThreshold;
  float ecStabilityThreshold;
  WaterLevelReading waterLevel;
};

struct StabilityWindow {
  float referenceValue;
  unsigned long startedAt;
  uint8_t unstableReadingCount;
  bool initialized;
  bool reported;
};

struct BackendDecisionSummary {
  String status;
  String receivedStrategy;
  String executedStrategy;
  String decision;
  String pumpActivated;
  String agenticMode;
  String actuationSource;
  String agenticAction;
  String safetyGateSource;
  String reasoningSource;
  String orchestratorRoute;
  String orchestratorAction;
  String orchestratorReason;
  String skippedAgents;
  String samePumpInterpretation;
  String samePumpReason;
  String latestSamePumpStatus;
  String riskFlags;
  String deliveryIssueReason;
  String deliveryIssuePump;
  String deliveryIssueMetric;
  String deliveryIssuePotentialCauses;
  String reason;
  float doseMl;
  float confidence;
  float latestSamePumpDoseMl;
  float recommendedDoseFactor;
  float fallbackReferenceFactor;
  float targetRangeHeadroomFactor;
  float baselineDoseMl;
  float doseBeforeFactorMl;
  float appliedDoseFactor;
  float deliveryIssuePreviousValue;
  float deliveryIssueCurrentValue;
  float deliveryIssueExpectedDelta;
  float deliveryIssueMinimumDelta;
  unsigned long durationMs;
  unsigned long baselineDurationMs;
  unsigned long durationBeforeFactorMs;
  unsigned long mixingTimeMs;
  int recentLogCount;
  int unresolvedSamePumpDoseCount;
  int logId;
  int controlCycleId;
  bool samePumpAvailable;
  bool latestSamePumpHitPumpCap;
  bool latestSamePumpHitCapAndUnresolved;
  bool deliveryIssueDetected;
};

enum ControllerMode {
  MODE_MONITORING,
  MODE_FULL_CONTROL
};

ControllerMode controllerMode = (STARTUP_CONTROLLER_MODE == 1) ? MODE_FULL_CONTROL : MODE_MONITORING;
unsigned long lastReadTime = 0;
unsigned long lastMonitoringReadTime = 0;
unsigned long mixingStartedAt = 0;
unsigned long currentMixingTimeMs = 0;
unsigned long lastMixingStatusLogTime = 0;
unsigned long lastMixingBackendRetryTime = 0;
unsigned long lastReadWaitStatusLogTime = 0;
unsigned long lastHitlPendingCommandPollTime = 0;
int currentMixingControlCycleId = 0;
bool mixingStatusReported = false;
bool emergencyStopLatched = false;
portMUX_TYPE dosingCutoffMux = portMUX_INITIALIZER_UNLOCKED;
volatile bool dosingCutoffExpired = false;
bool isMixing = false;
bool adsReady = false;
float monitoringFilteredPh = NAN;
float monitoringFilteredEc = NAN;
unsigned long monitoringSampleNumber = 0;
StabilityWindow monitoringPhWindow;
StabilityWindow monitoringEcWindow;

bool initializeAds();
bool ensureWifiConnected();
String sensorEndpoint();
String apiBaseUrl();
String backendHealthEndpoint();
bool precheckBackendConnection();

float readAverageAdsMilliVolts(uint8_t channel, float multiplier = 1.0);
float filterReading(float previousValue, float currentValue, float alpha);
float readPHValue(float temperature);
float readECValue(float temperature);
float readWaterTemperature();
WaterLevelReading readWaterLevel();
bool readStableSensorSnapshot(SensorSnapshot& snapshot);
void resetStabilityWindow(StabilityWindow& window);
bool updateStabilityWindow(
  StabilityWindow& window,
  float value,
  float stableDelta,
  const char* label
);
void processSerialCommands();
void handleSerialCommand(String command);
void waitWithSerialCommands(unsigned long durationMs);
bool waitForFullControl(unsigned long durationMs);
void enterMonitoringMode();
void enterFullControlMode();
void printCommandHelp();
void resetMonitoringState();
void runMonitoringMode(unsigned long now);
void printMonitoringSample(
  int16_t rawPh,
  int16_t rawEc,
  float phVoltageMv,
  float ecVoltageMv,
  float rawPhValue,
  float rawEcValue,
  const SensorSnapshot& snapshot,
  bool phValid,
  bool ecValid,
  bool phStable,
  bool ecStable
);

bool isValidTemperature(float temperature);
bool isValidPH(float phValue);
bool isValidEC(float ecValue);
float clampFloat(float value, float minValue, float maxValue);

String buildSensorPayload(const SensorSnapshot& snapshot);
bool requestDosingDecision(
  const String& payload,
  BackendDecisionSummary& decision
);
bool requestPendingHumanCommand(BackendDecisionSummary& decision);
bool backendEmergencyStopActive();
bool executeDosingDecision(BackendDecisionSummary& decision);
bool markControlCycleActionStarted(int controlCycleId, const char* status);
bool markControlCycleActionCompleted(int controlCycleId, const char* status);
bool markControlCycleComplete(int controlCycleId, const char* status);
bool markControlCycleCompleteWithRetry(int controlCycleId, const char* status);
bool postControlCycleStatus(int controlCycleId, const char* action, const char* status, const char* label);
String extractStringValue(const String& json, const String& key);
String extractDecisionValue(const String& json);
String extractObjectValue(const String& json, const String& objectKey);
String extractStringValueFromObject(const String& json, const String& objectKey, const String& key);
String extractArrayValue(const String& json, const String& key);
bool extractBoolValue(const String& json, const String& key);
int extractIntValue(const String& json, const String& key);
float extractFloatValue(const String& json, const String& key);

void pumpOn(int pin);
void pumpOff(int pin);
void stopAllPumps();
int pumpPinForName(const String& pumpName);
bool runDosingCountdown(int pin, unsigned long durationMs, const String& pumpName);
bool runInterDoseMixingCountdown(unsigned long durationMs);
void runPumpForDuration(int pin, unsigned long durationMs, unsigned long mixingTimeMs, const String& pumpName);
void runEcUpSequence(int controlCycleId, unsigned long durationMs, unsigned long mixingTimeMs);
void startMixingPeriod(unsigned long mixingTimeMs);
void printStatus(float pHValue, float ecValue, float tempC, const WaterLevelReading& waterLevel);
void printPayloadSummary(const SensorSnapshot& snapshot, const String& payload);
void printDecisionSummary(const BackendDecisionSummary& decision);
void printAgentFlow(const BackendDecisionSummary& decision);
void printHistoryContext(const BackendDecisionSummary& decision);
void printHistoryBasis(const BackendDecisionSummary& decision);

void setup() {
  Serial.begin(115200);
  Serial.setTimeout(20);
  delay(1000);

  Serial.println();
  Serial.println("HANAS ESP32 SENSOR CONTROLLER");
  Serial.println();

  EEPROM.begin(32);
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  adsReady = initializeAds();

  ph.begin();
  ec.begin();
  tempSensors.begin();

  pinMode(PH_UP_PUMP_PIN, OUTPUT);
  pinMode(PH_DOWN_PUMP_PIN, OUTPUT);
  pinMode(EC_UP_A_PUMP_PIN, OUTPUT);
  pinMode(EC_UP_B_PUMP_PIN, OUTPUT);
  pinMode(EC_DOWN_PUMP_PIN, OUTPUT);
  stopAllPumps();

  Serial.println("System initialized.");
  Serial.print("Sensor endpoint: ");
  Serial.println(sensorEndpoint());
  Serial.print("Backend health endpoint: ");
  Serial.println(backendHealthEndpoint());
  resetMonitoringState();
  printCommandHelp();
#if USE_FIXED_TEST_READINGS
  Serial.println("Sensor mode: fixed test readings.");
#else
  Serial.println("Sensor mode: live sensor readings.");
#endif
#if STARTUP_CONTROLLER_MODE == 1
  Serial.println("Starting mode: FULL CONTROL. Backend submission and dosing are armed.");
  Serial.println("Type M to drop to monitoring mode.");
#else
  Serial.println("Starting mode: MONITORING. Type FULL to arm backend control and pumps.");
  Serial.println("Wi-Fi/backend pre-check runs when FULL control mode is requested.");
#endif
  Serial.print("EC Up A/B inter-dose mixing: ");
  Serial.print(EC_UP_INTER_DOSE_MIXING_DURATION_MS / 1000);
  Serial.print(" seconds (");
  Serial.print(EC_UP_INTER_DOSE_MIXING_DURATION_MS);
  Serial.println(" ms).");
  Serial.println();
}

void loop() {
  processSerialCommands();

  if (controllerMode == MODE_MONITORING) {
    runMonitoringMode(millis());
    return;
  }

  unsigned long now = millis();

  if (isMixing) {
    if (
      currentMixingControlCycleId > 0
      && !mixingStatusReported
      && (
        lastMixingBackendRetryTime == 0
        || now - lastMixingBackendRetryTime >= 5000
      )
    ) {
      lastMixingBackendRetryTime = now;
      mixingStatusReported = markControlCycleActionCompleted(
        currentMixingControlCycleId,
        "mixing"
      );
    }

    unsigned long mixingElapsedMs = now - mixingStartedAt;
    if (mixingElapsedMs < currentMixingTimeMs) {
      if (lastMixingStatusLogTime == 0 || now - lastMixingStatusLogTime >= MIXING_STATUS_LOG_INTERVAL_MS) {
        unsigned long remainingMs = currentMixingTimeMs - mixingElapsedMs;

        Serial.print("Mixing... elapsed ");
        Serial.print(mixingElapsedMs / 1000);
        Serial.print("s / ");
        Serial.print(currentMixingTimeMs / 1000);
        Serial.print("s, remaining ");
        Serial.print((remainingMs + 999) / 1000);
        Serial.println("s");

        lastMixingStatusLogTime = now;
      }

      if (!waitForFullControl(1000)) {
        return;
      }
      return;
    }

    isMixing = false;
    currentMixingTimeMs = 0;
    lastMixingStatusLogTime = 0;
    Serial.println("Mixing complete. Full control resumed.");
    Serial.println();

    if (currentMixingControlCycleId > 0) {
      if (markControlCycleCompleteWithRetry(currentMixingControlCycleId, "completed")) {
        currentMixingControlCycleId = 0;
        mixingStatusReported = false;
        lastMixingBackendRetryTime = 0;
      } else {
        lastMixingBackendRetryTime = now;
        return;
      }
    }
  } else if (currentMixingControlCycleId > 0) {
    if (
      lastMixingBackendRetryTime == 0
      || now - lastMixingBackendRetryTime >= 5000
    ) {
      lastMixingBackendRetryTime = now;
      if (markControlCycleCompleteWithRetry(currentMixingControlCycleId, "completed")) {
        currentMixingControlCycleId = 0;
        mixingStatusReported = false;
        lastMixingBackendRetryTime = 0;
      }
    }
    if (currentMixingControlCycleId > 0) {
      return;
    }
  }

  if (lastHitlPendingCommandPollTime == 0 || now - lastHitlPendingCommandPollTime >= HITL_PENDING_COMMAND_POLL_MS) {
    lastHitlPendingCommandPollTime = now;

    if (ensureWifiConnected()) {
      BackendDecisionSummary reviewedDecision;
      if (requestPendingHumanCommand(reviewedDecision)) {
        executeDosingDecision(reviewedDecision);
        return;
      }
    }
  }

  if (now - lastReadTime < READ_INTERVAL_MS) {
    if (lastReadWaitStatusLogTime == 0 || now - lastReadWaitStatusLogTime >= WAIT_STATUS_LOG_INTERVAL_MS) {
      unsigned long remainingMs = READ_INTERVAL_MS - (now - lastReadTime);

      Serial.print("Waiting for next reading... remaining ");
      Serial.print((remainingMs + 999) / 1000);
      Serial.println("s");

      lastReadWaitStatusLogTime = now;
    }

    waitForFullControl(100);
    return;
  }
  lastReadTime = now;
  lastReadWaitStatusLogTime = 0;

  Serial.println("Starting sensor cycle.");

  if (!adsReady) {
    adsReady = initializeAds();
    if (!adsReady) {
      Serial.println("ADS1115 unavailable. Skipping this reading.");
      waitForFullControl(1000);
      return;
    }
  }

  if (!ensureWifiConnected()) {
    if (controllerMode == MODE_MONITORING) {
      Serial.println("Wi-Fi connect interrupted by monitoring mode.");
      Serial.println();
      return;
    }

    Serial.println("Wi-Fi unavailable. Skipping this reading.");
    waitForFullControl(WIFI_RETRY_DELAY_MS);
    return;
  }

  SensorSnapshot snapshot;

#if USE_FIXED_TEST_READINGS
  snapshot.temperature = FIXED_TEST_TEMPERATURE_C;
  snapshot.ph = FIXED_TEST_PH;
  snapshot.ec = FIXED_TEST_EC;
  snapshot.waterLevel.raw = FIXED_TEST_WATER_LEVEL_RAW;
  snapshot.waterLevel.voltage = FIXED_TEST_WATER_LEVEL_VOLTAGE;
  snapshot.waterLevel.currentMa = FIXED_TEST_WATER_LEVEL_CURRENT_MA;
  snapshot.waterLevel.levelCm = FIXED_TEST_WATER_LEVEL_CM;
  snapshot.waterLevel.percent = FIXED_TEST_WATER_LEVEL_PERCENT;
  snapshot.waterLevel.reservoirVolumeLiters = FIXED_TEST_RESERVOIR_VOLUME_LITERS;
  snapshot.phStableForSeconds = SENSOR_STABLE_REQUIRED_MS / 1000;
  snapshot.ecStableForSeconds = SENSOR_STABLE_REQUIRED_MS / 1000;
  snapshot.phStabilityThreshold = PH_STABLE_DELTA;
  snapshot.ecStabilityThreshold = EC_STABLE_DELTA;
#else
  if (!readStableSensorSnapshot(snapshot)) {
    Serial.println("Stable pH/EC readings were not reached. Skipping HANAS submission.");
    Serial.println();
    return;
  }
#endif

  float temperature = snapshot.temperature;
  float pHValue = snapshot.ph;
  float ecValue = snapshot.ec;
  WaterLevelReading waterLevel = snapshot.waterLevel;

  if (!isValidPH(pHValue) || !isValidEC(ecValue)) {
    Serial.println("Invalid pH or EC reading. Skipping HANAS submission.");
    printStatus(pHValue, ecValue, temperature, waterLevel);
    Serial.println();
    return;
  }

  printStatus(pHValue, ecValue, temperature, waterLevel);

  BackendDecisionSummary decision;
  String payload = buildSensorPayload(snapshot);

  printPayloadSummary(snapshot, payload);

  if (!requestDosingDecision(payload, decision)) {
    Serial.println("No valid dosing decision received.");
    Serial.println();
    return;
  }

  processSerialCommands();
  if (controllerMode == MODE_MONITORING) {
    markControlCycleComplete(decision.controlCycleId, "error");
    Serial.println("Control cycle interrupted before dosing decision was applied.");
    Serial.println();
    return;
  }

  executeDosingDecision(decision);
}

bool executeDosingDecision(BackendDecisionSummary& decision) {
  emergencyStopLatched = false;
  printDecisionSummary(decision);

  bool isEcUpSequence = decision.pumpActivated == "ec_up";
  int pumpPin = isEcUpSequence ? -1 : pumpPinForName(decision.pumpActivated);
  if ((!isEcUpSequence && pumpPin < 0) || decision.durationMs == 0) {
    if (decision.decision == "wait_human_review") {
      Serial.println("Decision: Waiting for human review. No pump activated.");
      Serial.println("The controller will poll for an approved or overridden command.");
      Serial.println();
      return false;
    }

    if (decision.decision == "within_range") {
      Serial.println("Decision: No dosing needed.");
    } else {
      Serial.print("Decision: No pump activated (");
      Serial.print(decision.decision);
      Serial.println(").");
    }
    markControlCycleComplete(decision.controlCycleId, "completed");
    Serial.println();
    return false;
  }

  Serial.println("Decision: Dosing required.");
  Serial.print("Dose volume target: ");
  Serial.print(decision.doseMl, 2);
  Serial.println(" ml");
  Serial.print("Pump runtime target: ");
  Serial.print(decision.durationMs);
  Serial.println(" ms");
  Serial.print("Post-dose mixing target: ");
  Serial.print(decision.mixingTimeMs / 1000);
  Serial.println(" seconds");

  if (!markControlCycleActionStarted(decision.controlCycleId, "dosing")) {
    Serial.println("Unable to report dosing start. Skipping pump activation.");
    markControlCycleComplete(decision.controlCycleId, "error");
    Serial.println();
    return false;
  }

  processSerialCommands();
  if (controllerMode == MODE_MONITORING) {
    markControlCycleComplete(decision.controlCycleId, emergencyStopLatched ? "emergency_stopped" : "error");
    Serial.println("Control cycle interrupted before pump activation.");
    Serial.println();
    return false;
  }

  if (isEcUpSequence) {
    runEcUpSequence(decision.controlCycleId, decision.durationMs, decision.mixingTimeMs);
  } else {
    runPumpForDuration(pumpPin, decision.durationMs, decision.mixingTimeMs, decision.pumpActivated);
  }

  if (controllerMode == MODE_MONITORING) {
    markControlCycleComplete(decision.controlCycleId, emergencyStopLatched ? "emergency_stopped" : "error");
    Serial.println("Control cycle interrupted before completion.");
    Serial.println();
    return false;
  }

  if (isMixing) {
    currentMixingControlCycleId = decision.controlCycleId;
    lastMixingBackendRetryTime = millis();
    mixingStatusReported = markControlCycleActionCompleted(
      currentMixingControlCycleId,
      "mixing"
    );
    if (!mixingStatusReported) {
      Serial.println("WARNING: Mixing status was not confirmed; retrying while mixing continues.");
    }
    return true;
  }

  markControlCycleCompleteWithRetry(decision.controlCycleId, "completed");
  return true;
}

bool initializeAds() {
  if (!ads.begin(ADS_ADDRESS)) {
    Serial.println("ADS1115 not detected. Check wiring.");
    return false;
  }

  ads.setGain(GAIN_ONE);
  Serial.println("ADS1115 initialized.");
  return true;
}

bool hasWifiCredential(const char* value) {
  return value != nullptr && value[0] != '\0';
}

bool connectWifiNetwork(
  const char* label,
  const char* networkSsid,
  const char* networkPassword,
  uint8_t rounds
) {
  if (!hasWifiCredential(networkSsid)) {
    return false;
  }

  for (uint8_t round = 1; round <= rounds; round++) {
    WiFi.disconnect();
    if (!waitForFullControl(WIFI_SWITCH_DELAY_MS)) {
      return false;
    }

    WiFi.mode(WIFI_STA);
    WiFi.begin(networkSsid, networkPassword);
    Serial.print("Connecting to ");
    Serial.print(label);
    Serial.print(" Wi-Fi");

    for (uint8_t attempt = 0; attempt < WIFI_CONNECT_WAIT_ATTEMPTS_PER_ROUND; attempt++) {
      if (WiFi.status() == WL_CONNECTED) {
        Serial.println();
        Serial.print("Wi-Fi connected via ");
        Serial.print(label);
        Serial.print(". IP: ");
        Serial.println(WiFi.localIP());
        Serial.print("Server URL: ");
        Serial.println(serverUrl);
        return true;
      }

      Serial.print(".");
      if (!waitForFullControl(WIFI_RETRY_DELAY_MS)) {
        Serial.println();
        return false;
      }
    }

    Serial.println();
    Serial.print(label);
    Serial.print(" Wi-Fi connection attempt ");
    Serial.print(round);
    Serial.print(" of ");
    Serial.print(rounds);
    Serial.println(" failed.");
  }

  return false;
}

bool ensureWifiConnected() {
  if (WiFi.status() == WL_CONNECTED) {
    return true;
  }

  if (connectWifiNetwork(
        "primary",
        primarySsid,
        primaryPassword,
        WIFI_PRIMARY_CONNECT_ROUNDS
      )) {
    return true;
  }

  if (hasWifiCredential(backupSsid)) {
    Serial.println("Primary Wi-Fi unavailable after retries. Trying backup Wi-Fi.");
    if (connectWifiNetwork(
          "backup",
          backupSsid,
          backupPassword,
          WIFI_BACKUP_CONNECT_ROUNDS
        )) {
      return true;
    }
  } else {
    Serial.println("Primary Wi-Fi unavailable and no backup Wi-Fi is configured.");
  }

  Serial.println("Wi-Fi connection failed on all configured networks.");
  return false;
}

String sensorEndpoint() {
  String url = String(serverUrl);
  url.trim();

  if (url.endsWith("/")) {
    url.remove(url.length() - 1);
  }

  if (url.endsWith("/api/sensor-data")) {
    return url;
  }

  if (url.endsWith("/sensor-data")) {
    url.remove(url.length() - String("/sensor-data").length());
  }

  if (url.endsWith("/api")) {
    return url + "/sensor-data";
  }

  return url + "/api/sensor-data";
}

String apiBaseUrl() {
  String url = sensorEndpoint();
  int endpointIndex = url.indexOf("/api/sensor-data");

  if (endpointIndex >= 0) {
    return url.substring(0, endpointIndex);
  }

  return url;
}

String backendHealthEndpoint() {
  String url = apiBaseUrl();
  if (!url.endsWith("/")) {
    url += "/";
  }
  return url;
}

bool precheckBackendConnection() {
  Serial.println("Checking backend connection before full control...");

  if (!ensureWifiConnected()) {
    Serial.println("Backend pre-check failed: Wi-Fi is not connected.");
    return false;
  }

  String healthUrl = backendHealthEndpoint();
  Serial.print("Backend health endpoint: ");
  Serial.println(healthUrl);

  for (uint8_t attempt = 1; attempt <= BACKEND_PRECHECK_RETRY_COUNT; attempt++) {
    HTTPClient http;
    http.setTimeout(BACKEND_PRECHECK_TIMEOUT_MS);
    http.begin(healthUrl);

    int responseCode = http.GET();
    String response = responseCode > 0 ? http.getString() : "";

    Serial.print("Backend pre-check attempt ");
    Serial.print(attempt);
    Serial.print("/");
    Serial.print(BACKEND_PRECHECK_RETRY_COUNT);
    Serial.print(" response code: ");
    Serial.println(responseCode);

    if (responseCode > 0 && responseCode < 400) {
      Serial.println("Backend pre-check passed. Backend is reachable.");
      if (response.length() > 0) {
        Serial.print("Backend health response: ");
        Serial.println(response);
      }
      http.end();
      return true;
    }

    if (responseCode <= 0) {
      Serial.print("Backend pre-check error: ");
      Serial.println(http.errorToString(responseCode));
    } else if (response.length() > 0) {
      Serial.print("Backend pre-check response: ");
      Serial.println(response);
    }

    http.end();

    if (attempt < BACKEND_PRECHECK_RETRY_COUNT && !waitForFullControl(WIFI_RETRY_DELAY_MS)) {
      Serial.println("Backend pre-check interrupted.");
      return false;
    }
  }

  Serial.println("Backend pre-check failed. Confirm backend is running and SERVER_URL is correct.");
  return false;
}

float readAverageAdsMilliVolts(uint8_t channel, float multiplier) {
  float sum = 0.0;

  for (int i = 0; i < SAMPLE_COUNT; i++) {
    int16_t raw = ads.readADC_SingleEnded(channel);
    sum += raw * ADS_MV_PER_BIT * multiplier;
    delay(10);
  }

  return sum / (float)SAMPLE_COUNT;
}

float filterReading(float previousValue, float currentValue, float alpha) {
  if (isnan(previousValue)) {
    return currentValue;
  }

  return previousValue + alpha * (currentValue - previousValue);
}

float readPHValue(float temperature) {
  float voltageMv = readAverageAdsMilliVolts(PH_CHANNEL, PH_DIVIDER_MULTIPLIER);
  return ph.readPH(voltageMv, temperature);
}

float readECValue(float temperature) {
  float voltageMv = readAverageAdsMilliVolts(EC_CHANNEL, EC_DIVIDER_MULTIPLIER);
  return ec.readEC(voltageMv, temperature);
}

float readWaterTemperature() {
  tempSensors.requestTemperatures();
  return tempSensors.getTempCByIndex(0);
}

WaterLevelReading readWaterLevel() {
  WaterLevelReading reading;
  reading.raw = ads.readADC_SingleEnded(WATER_LEVEL_CHANNEL);
  reading.voltage = reading.raw * ADS_MV_PER_BIT / 1000.0;
  reading.currentMa = (reading.voltage / LEVEL_SENSE_RESISTOR_OHMS) * 1000.0;
  reading.levelCm = ((reading.currentMa - LEVEL_ZERO_CURRENT_MA)
      / (LEVEL_FULL_CURRENT_MA - LEVEL_ZERO_CURRENT_MA)) * TANK_HEIGHT_CM;
  reading.levelCm = clampFloat(reading.levelCm, 0.0, TANK_HEIGHT_CM);
  reading.percent = (reading.levelCm / OPERATING_MAX_LEVEL_CM) * 100.0;
  reading.percent = max(0.0f, reading.percent);
  reading.reservoirVolumeLiters = (reading.percent / 100.0) * RESERVOIR_MAX_VOLUME_LITERS;
  return reading;
}

bool readStableSensorSnapshot(SensorSnapshot& snapshot) {
  StabilityWindow phWindow;
  StabilityWindow ecWindow;
  resetStabilityWindow(phWindow);
  resetStabilityWindow(ecWindow);
  float filteredPhValue = NAN;
  float filteredEcValue = NAN;

  unsigned long startedAt = millis();

  Serial.println("Waiting for stable pH and EC readings...");
  Serial.println("Using filtered pH/EC readings for stability and payload values.");
  Serial.print("pH stable threshold: +/-");
  Serial.print(PH_STABLE_DELTA, 2);
  Serial.print(" for ");
  Serial.print(SENSOR_STABLE_REQUIRED_MS / 1000);
  Serial.println(" seconds.");
  Serial.print("EC stable threshold: +/-");
  Serial.print(EC_STABLE_DELTA, 3);
  Serial.print(" mS/cm for ");
  Serial.print(SENSOR_STABLE_REQUIRED_MS / 1000);
  Serial.println(" seconds.");

  while (millis() - startedAt <= SENSOR_STABILITY_TIMEOUT_MS) {
    processSerialCommands();
    if (controllerMode == MODE_MONITORING) {
      Serial.println("Stable-read wait interrupted by monitoring mode.");
      return false;
    }

    snapshot.temperature = readWaterTemperature();
    if (!isValidTemperature(snapshot.temperature)) {
      Serial.println("Invalid temperature reading. Using fallback 25.0C.");
      snapshot.temperature = 25.0;
    }

    float rawPhValue = readPHValue(snapshot.temperature);
    float rawEcValue = readECValue(snapshot.temperature);
    snapshot.waterLevel = readWaterLevel();

    if (!isValidPH(rawPhValue) || !isValidEC(rawEcValue)) {
      Serial.println("Invalid pH or EC while waiting for stability.");
      if (!waitForFullControl(SENSOR_STABILITY_SAMPLE_INTERVAL_MS)) {
        return false;
      }
      continue;
    }

    filteredPhValue = filterReading(filteredPhValue, rawPhValue, PH_FILTER_ALPHA);
    filteredEcValue = filterReading(filteredEcValue, rawEcValue, EC_FILTER_ALPHA);
    snapshot.ph = filteredPhValue;
    snapshot.ec = filteredEcValue;

    bool phStable = updateStabilityWindow(phWindow, snapshot.ph, PH_STABLE_DELTA, "pH");
    bool ecStable = updateStabilityWindow(ecWindow, snapshot.ec, EC_STABLE_DELTA, "EC");

    Serial.print("Stability sample pH raw:");
    Serial.print(rawPhValue, 2);
    Serial.print(" filtered:");
    Serial.print(snapshot.ph, 2);
    Serial.print(" stable:");
    Serial.print(phStable ? "yes" : "no");
    Serial.print(" for:");
    Serial.print((millis() - phWindow.startedAt) / 1000);
    Serial.print("s EC raw:");
    Serial.print(rawEcValue, 3);
    Serial.print(" filtered:");
    Serial.print(snapshot.ec, 3);
    Serial.print(" stable:");
    Serial.print(ecStable ? "yes" : "no");
    Serial.print(" for:");
    Serial.print((millis() - ecWindow.startedAt) / 1000);
    Serial.println("s");

    if (phStable && ecStable) {
      Serial.println("pH and EC readings are stable.");
      snapshot.phStableForSeconds = (millis() - phWindow.startedAt) / 1000;
      snapshot.ecStableForSeconds = (millis() - ecWindow.startedAt) / 1000;
      snapshot.phStabilityThreshold = PH_STABLE_DELTA;
      snapshot.ecStabilityThreshold = EC_STABLE_DELTA;
      return true;
    }

    if (!waitForFullControl(SENSOR_STABILITY_SAMPLE_INTERVAL_MS)) {
      return false;
    }
  }

  Serial.println("Timed out while waiting for stable pH and EC readings.");
  return false;
}

void resetStabilityWindow(StabilityWindow& window) {
  window.referenceValue = NAN;
  window.startedAt = millis();
  window.unstableReadingCount = 0;
  window.initialized = false;
  window.reported = false;
}

bool updateStabilityWindow(
  StabilityWindow& window,
  float value,
  float stableDelta,
  const char* label
) {
  unsigned long now = millis();

  if (!window.initialized || isnan(window.referenceValue)) {
    window.referenceValue = value;
    window.startedAt = now;
    window.unstableReadingCount = 0;
    window.initialized = true;
    window.reported = false;
    return false;
  }

  if (fabs(value - window.referenceValue) > stableDelta) {
    window.unstableReadingCount++;

    if (window.unstableReadingCount >= UNSTABLE_RESET_COUNT) {
      Serial.print(label);
      Serial.println(" stability window reset.");
      window.referenceValue = value;
      window.startedAt = now;
      window.unstableReadingCount = 0;
      window.reported = false;
    }
  } else {
    window.unstableReadingCount = 0;
  }

  bool isStable = now - window.startedAt >= SENSOR_STABLE_REQUIRED_MS;
  if (isStable && !window.reported) {
    Serial.print(label);
    Serial.print(" first stable after ");
    Serial.print((now - window.startedAt) / 1000);
    Serial.println(" seconds.");
    window.reported = true;
  }

  return isStable;
}

void processSerialCommands() {
  while (Serial.available()) {
    String command = Serial.readStringUntil('\n');
    command.trim();
    command.replace("\r", "");

    if (command.length() > 0) {
      handleSerialCommand(command);
    }
  }
}

void handleSerialCommand(String command) {
  command.trim();
  command.toLowerCase();

  if (command == "m" || command == "monitor" || command == "monitoring") {
    enterMonitoringMode();
    return;
  }

  if (command == "full" || command == "f") {
    enterFullControlMode();
    return;
  }

  if (command == "s" || command == "stop") {
    enterMonitoringMode();
    Serial.println("All pumps stopped. Active control cycle aborted.");
    Serial.println();
    return;
  }

  if (command == "?" || command == "help") {
    printCommandHelp();
    return;
  }

  Serial.print("Unknown command: ");
  Serial.println(command);
  printCommandHelp();
}

bool waitForFullControl(unsigned long durationMs) {
  unsigned long startedAt = millis();

  while (millis() - startedAt < durationMs) {
    processSerialCommands();
    if (controllerMode != MODE_FULL_CONTROL) {
      return false;
    }

    delay(25);
  }

  return true;
}

void waitWithSerialCommands(unsigned long durationMs) {
  unsigned long startedAt = millis();

  while (millis() - startedAt < durationMs) {
    processSerialCommands();
    delay(25);
  }
}

void enterMonitoringMode() {
  if (currentMixingControlCycleId > 0) {
    int interruptedControlCycleId = currentMixingControlCycleId;
    const char* interruptedStatus = emergencyStopLatched
      ? "emergency_stopped"
      : (isMixing ? "error" : "completed");
    currentMixingControlCycleId = 0;
    mixingStatusReported = false;
    lastMixingBackendRetryTime = 0;
    markControlCycleCompleteWithRetry(interruptedControlCycleId, interruptedStatus);
  }

  controllerMode = MODE_MONITORING;
  isMixing = false;
  currentMixingTimeMs = 0;
  lastMixingStatusLogTime = 0;
  stopAllPumps();
  resetMonitoringState();

  Serial.println();
  Serial.println("MONITORING MODE");
  Serial.println("Live 1-second sensor readings only. Backend submission and pumps are disabled.");
  Serial.println("Type FULL when the trial is ready to start the full HANAS control cycle.");
  Serial.println();
}

void enterFullControlMode() {
  bool wasMonitoring = controllerMode == MODE_MONITORING;
  controllerMode = MODE_FULL_CONTROL;

  Serial.println();
  Serial.println("FULL CONTROL MODE REQUESTED");
  if (!precheckBackendConnection()) {
    Serial.println("Full control was not armed because the backend pre-check failed.");
    enterMonitoringMode();
    return;
  }

  if (wasMonitoring) {
    isMixing = false;
    lastReadWaitStatusLogTime = 0;
    lastReadTime = millis() - READ_INTERVAL_MS;
  }

  Serial.println();
  Serial.println("FULL CONTROL MODE");
  Serial.println("Backend pre-check passed.");
  Serial.println("HANAS will submit stable readings to the backend and execute dosing decisions.");
  Serial.println("Type M to return to monitoring mode and keep pumps disabled.");
  Serial.println();
}

void printCommandHelp() {
  Serial.println("Serial commands:");
  Serial.println("  M or MONITOR  - monitoring mode, 1-second local readings, no backend, no pumps");
  Serial.println("  FULL or F     - full experiment mode, backend decisions and dosing enabled");
  Serial.println("  S or STOP     - stop all pumps immediately");
  Serial.println("  ? or HELP     - show this menu");
  Serial.println();
}

void resetMonitoringState() {
  monitoringFilteredPh = NAN;
  monitoringFilteredEc = NAN;
  monitoringSampleNumber = 0;
  lastMonitoringReadTime = 0;
  resetStabilityWindow(monitoringPhWindow);
  resetStabilityWindow(monitoringEcWindow);
}

void runMonitoringMode(unsigned long now) {
  if (lastMonitoringReadTime != 0 && now - lastMonitoringReadTime < MONITORING_READ_INTERVAL_MS) {
    waitWithSerialCommands(25);
    return;
  }

  lastMonitoringReadTime = now;

  if (!adsReady) {
    adsReady = initializeAds();
    if (!adsReady) {
      Serial.println("ADS1115 unavailable. Monitoring sample skipped.");
      waitWithSerialCommands(1000);
      return;
    }
  }

  SensorSnapshot snapshot;
  snapshot.temperature = readWaterTemperature();
  if (!isValidTemperature(snapshot.temperature)) {
    Serial.println("Invalid temperature reading. Using fallback 25.0C.");
    snapshot.temperature = 25.0;
  }

  int16_t rawPh = ads.readADC_SingleEnded(PH_CHANNEL);
  int16_t rawEc = ads.readADC_SingleEnded(EC_CHANNEL);
  float phVoltageMv = readAverageAdsMilliVolts(PH_CHANNEL, PH_DIVIDER_MULTIPLIER);
  float ecVoltageMv = readAverageAdsMilliVolts(EC_CHANNEL, EC_DIVIDER_MULTIPLIER);
  float rawPhValue = ph.readPH(phVoltageMv, snapshot.temperature);
  float rawEcValue = ec.readEC(ecVoltageMv, snapshot.temperature);
  snapshot.waterLevel = readWaterLevel();

  bool phValid = isValidPH(rawPhValue);
  bool ecValid = isValidEC(rawEcValue);
  bool phStable = false;
  bool ecStable = false;

  if (phValid) {
    monitoringFilteredPh = filterReading(monitoringFilteredPh, rawPhValue, PH_FILTER_ALPHA);
    snapshot.ph = monitoringFilteredPh;
    phStable = updateStabilityWindow(monitoringPhWindow, snapshot.ph, PH_STABLE_DELTA, "pH");
    snapshot.phStableForSeconds = (millis() - monitoringPhWindow.startedAt) / 1000;
  } else {
    snapshot.ph = rawPhValue;
    snapshot.phStableForSeconds = 0;
  }

  if (ecValid) {
    monitoringFilteredEc = filterReading(monitoringFilteredEc, rawEcValue, EC_FILTER_ALPHA);
    snapshot.ec = monitoringFilteredEc;
    ecStable = updateStabilityWindow(monitoringEcWindow, snapshot.ec, EC_STABLE_DELTA, "EC");
    snapshot.ecStableForSeconds = (millis() - monitoringEcWindow.startedAt) / 1000;
  } else {
    snapshot.ec = rawEcValue;
    snapshot.ecStableForSeconds = 0;
  }

  snapshot.phStabilityThreshold = PH_STABLE_DELTA;
  snapshot.ecStabilityThreshold = EC_STABLE_DELTA;
  monitoringSampleNumber++;

  printMonitoringSample(
    rawPh,
    rawEc,
    phVoltageMv,
    ecVoltageMv,
    rawPhValue,
    rawEcValue,
    snapshot,
    phValid,
    ecValid,
    phStable,
    ecStable
  );
}

bool isValidTemperature(float temperature) {
  return temperature != DEVICE_DISCONNECTED_C && temperature > -10.0 && temperature < 80.0;
}

bool isValidPH(float phValue) {
  return phValue >= 0.0 && phValue <= 14.0;
}

bool isValidEC(float ecValue) {
  return ecValue >= 0.0 && ecValue <= 20.0;
}

float clampFloat(float value, float minValue, float maxValue) {
  if (value < minValue) {
    return minValue;
  }

  if (value > maxValue) {
    return maxValue;
  }

  return value;
}

String buildSensorPayload(const SensorSnapshot& snapshot) {
  String payload = "{";
  payload += "\"temperature\":" + String(snapshot.temperature, 2) + ",";
  payload += "\"ph\":" + String(snapshot.ph, 2) + ",";
  payload += "\"ec\":" + String(snapshot.ec, 3) + ",";
  payload += "\"reservoir_volume_liters\":" + String(snapshot.waterLevel.reservoirVolumeLiters, 2) + ",";
  payload += "\"ph_stable_for_seconds\":" + String(snapshot.phStableForSeconds) + ",";
  payload += "\"ec_stable_for_seconds\":" + String(snapshot.ecStableForSeconds) + ",";
  payload += "\"ph_stability_threshold\":" + String(snapshot.phStabilityThreshold, 2) + ",";
  payload += "\"ec_stability_threshold\":" + String(snapshot.ecStabilityThreshold, 3) + ",";
  payload += "\"control_strategy\":\"" + String(CONTROL_STRATEGY) + "\"";
  payload += "}";
  return payload;
}

bool requestDosingDecision(
  const String& payload,
  BackendDecisionSummary& decision
) {
  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.begin(sensorEndpoint());
  http.addHeader("Content-Type", "application/json");
#ifdef DEVICE_API_TOKEN
  http.addHeader("X-Device-Token", DEVICE_API_TOKEN);
#endif

  int responseCode = http.POST(payload);

  Serial.print("Sensor endpoint response code: ");
  Serial.println(responseCode);

  if (responseCode <= 0) {
    Serial.print("Sensor endpoint error: ");
    Serial.println(http.errorToString(responseCode));
    http.end();
    return false;
  }

  String response = http.getString();
  http.end();

  Serial.println("HANAS response:");
  Serial.println(response);

  decision.status = extractStringValue(response, "status");
  decision.receivedStrategy = extractStringValue(response, "control_strategy");
  decision.executedStrategy = extractStringValue(response, "executed_strategy");
  decision.decision = extractDecisionValue(response);
  decision.pumpActivated = extractStringValue(response, "pump_activated");
  String decisionContext = extractObjectValue(response, "decision");
  String metadataContext = extractObjectValue(decisionContext, "metadata");
  decision.agenticMode = extractStringValue(metadataContext, "agentic_mode");
  decision.actuationSource = extractStringValue(metadataContext, "actuation_source");
  decision.agenticAction = extractStringValue(response, "agentic_action");
  decision.safetyGateSource = extractStringValue(response, "safety_gate_source");
  decision.reasoningSource = extractStringValue(response, "reasoning_source");
  String orchestratorContext = extractObjectValue(metadataContext, "orchestrator_agent");
  decision.orchestratorRoute = extractStringValue(orchestratorContext, "route");
  decision.orchestratorAction = extractStringValue(orchestratorContext, "action");
  decision.orchestratorReason = extractStringValue(orchestratorContext, "reason");
  decision.skippedAgents = extractArrayValue(orchestratorContext, "skipped_agents");
  String samePumpContext = extractObjectValue(metadataContext, "same_pump_response");
  String deliveryIssueContext = extractObjectValue(metadataContext, "delivery_issue");
  decision.riskFlags = extractArrayValue(metadataContext, "risk_flags");
  decision.deliveryIssueDetected = extractBoolValue(deliveryIssueContext, "issue_detected");
  decision.deliveryIssueReason = extractStringValue(deliveryIssueContext, "reason");
  decision.deliveryIssuePump = extractStringValue(deliveryIssueContext, "current_pump");
  decision.deliveryIssueMetric = extractStringValue(deliveryIssueContext, "metric");
  decision.deliveryIssuePreviousValue = extractFloatValue(deliveryIssueContext, "previous_value");
  decision.deliveryIssueCurrentValue = extractFloatValue(deliveryIssueContext, "current_value");
  decision.deliveryIssueExpectedDelta = extractFloatValue(deliveryIssueContext, "expected_direction_delta");
  decision.deliveryIssueMinimumDelta = extractFloatValue(deliveryIssueContext, "minimum_expected_delta");
  decision.deliveryIssuePotentialCauses = extractArrayValue(deliveryIssueContext, "potential_causes");
  decision.recentLogCount = extractIntValue(response, "recent_log_count");
  decision.samePumpAvailable = extractBoolValue(samePumpContext, "available");
  decision.samePumpInterpretation = extractStringValue(samePumpContext, "interpretation");
  decision.samePumpReason = extractStringValue(samePumpContext, "reason");
  decision.latestSamePumpStatus = extractStringValue(samePumpContext, "latest_same_pump_status");
  decision.latestSamePumpDoseMl = extractFloatValue(samePumpContext, "latest_same_pump_dose_ml");
  decision.recommendedDoseFactor = extractFloatValue(samePumpContext, "recommended_dose_factor");
  decision.fallbackReferenceFactor = extractFloatValue(samePumpContext, "fallback_reference_factor");
  decision.targetRangeHeadroomFactor = extractFloatValue(samePumpContext, "target_range_headroom_factor");
  decision.unresolvedSamePumpDoseCount = extractIntValue(samePumpContext, "unresolved_same_pump_dose_count");
  decision.latestSamePumpHitPumpCap = extractBoolValue(samePumpContext, "latest_same_pump_hit_pump_cap");
  decision.latestSamePumpHitCapAndUnresolved = extractBoolValue(
    samePumpContext,
    "latest_same_pump_hit_cap_and_unresolved"
  );
  if (decision.recommendedDoseFactor == 0.0) {
    decision.recommendedDoseFactor = decision.fallbackReferenceFactor;
  }
  decision.baselineDoseMl = extractFloatValue(response, "baseline_dose_ml");
  decision.doseBeforeFactorMl = extractFloatValue(response, "dose_before_factor_ml");
  decision.appliedDoseFactor = extractFloatValue(response, "applied_dose_adjustment_factor");
  decision.baselineDurationMs = extractIntValue(response, "baseline_duration_ms");
  decision.durationBeforeFactorMs = extractIntValue(response, "duration_before_factor_ms");
  decision.reason = extractStringValue(response, "reason");
  decision.doseMl = extractFloatValue(response, "dose_ml");
  decision.confidence = extractFloatValue(response, "confidence");
  decision.durationMs = extractIntValue(response, "duration_ms");
  decision.mixingTimeMs = extractIntValue(response, "mixing_time_ms");
  if (decision.mixingTimeMs == 0 && decision.durationMs > 0) {
    decision.mixingTimeMs = MIXING_TIME_MS;
  }
  decision.logId = extractIntValue(response, "log_id");
  decision.controlCycleId = extractIntValue(response, "control_cycle_id");

  return responseCode >= 200
    && responseCode < 300
    && decision.pumpActivated.length() > 0
    && decision.controlCycleId > 0;
}

bool requestPendingHumanCommand(BackendDecisionSummary& decision) {
  if (WiFi.status() != WL_CONNECTED) {
    return false;
  }

  HTTPClient http;
  String url = apiBaseUrl() + "/api/control-cycles/pending-command";

  http.setTimeout(BACKEND_PRECHECK_TIMEOUT_MS);
  http.begin(url);
#ifdef DEVICE_API_TOKEN
  http.addHeader("X-Device-Token", DEVICE_API_TOKEN);
#endif

  int responseCode = http.GET();

  if (responseCode <= 0) {
    http.end();
    return false;
  }

  String response = http.getString();
  http.end();

  if (responseCode < 200 || responseCode >= 300) {
    Serial.print("Pending HITL command check failed: HTTP ");
    Serial.println(responseCode);
    return false;
  }

  String status = extractStringValue(response, "status");
  if (status == "emergency_stop") {
    emergencyStopLatched = true;
    enterMonitoringMode();
    Serial.println("Backend emergency stop is active. Pump outputs are locked off.");
    Serial.println();
    return false;
  }

  bool hasCommand = extractBoolValue(response, "has_command");
  if (!hasCommand) {
    return false;
  }

  decision.status = status;
  decision.receivedStrategy = "agentic_ai";
  decision.executedStrategy = "agentic_ai";
  decision.decision = extractStringValue(response, "decision");
  decision.pumpActivated = extractStringValue(response, "pump_activated");
  decision.agenticMode = extractStringValue(response, "agentic_mode");
  decision.actuationSource = extractStringValue(response, "actuation_source");
  decision.agenticAction = "human_review_command";
  decision.safetyGateSource = "human_review";
  decision.reasoningSource = "human_in_the_loop";
  decision.orchestratorRoute = "";
  decision.orchestratorAction = "";
  decision.orchestratorReason = "";
  decision.skippedAgents = "";
  decision.samePumpInterpretation = "";
  decision.samePumpReason = "";
  decision.latestSamePumpStatus = "";
  decision.riskFlags = "";
  decision.deliveryIssueReason = "";
  decision.deliveryIssuePump = "";
  decision.deliveryIssueMetric = "";
  decision.deliveryIssuePotentialCauses = "";
  decision.reason = extractStringValue(response, "message");
  decision.doseMl = extractFloatValue(response, "dose_ml");
  decision.confidence = 1.0;
  decision.durationMs = extractIntValue(response, "duration_ms");
  decision.mixingTimeMs = extractIntValue(response, "mixing_time_ms");
  if (decision.mixingTimeMs == 0 && decision.durationMs > 0) {
    decision.mixingTimeMs = MIXING_TIME_MS;
  }
  decision.logId = 0;
  decision.controlCycleId = extractIntValue(response, "control_cycle_id");

  return decision.pumpActivated.length() > 0
    && decision.pumpActivated != "none"
    && decision.controlCycleId > 0
    && decision.durationMs > 0;
}

bool backendEmergencyStopActive() {
  if (WiFi.status() != WL_CONNECTED) {
    return false;
  }

  HTTPClient http;
  String url = apiBaseUrl() + "/api/control-cycles/pending-command";

  http.setTimeout(EMERGENCY_STOP_HTTP_TIMEOUT_MS);
  http.begin(url);
#ifdef DEVICE_API_TOKEN
  http.addHeader("X-Device-Token", DEVICE_API_TOKEN);
#endif

  int responseCode = http.GET();
  if (responseCode <= 0) {
    http.end();
    return false;
  }

  String response = http.getString();
  http.end();

  if (responseCode < 200 || responseCode >= 300) {
    return false;
  }

  return extractStringValue(response, "status") == "emergency_stop";
}

bool markControlCycleComplete(int controlCycleId, const char* status) {
  return postControlCycleStatus(controlCycleId, "complete", status, "complete");
}

bool markControlCycleCompleteWithRetry(int controlCycleId, const char* status) {
  const uint8_t maxAttempts = 3;

  for (uint8_t attempt = 1; attempt <= maxAttempts; attempt++) {
    if (WiFi.status() != WL_CONNECTED && !ensureWifiConnected()) {
      Serial.println("Unable to reconnect before reporting control cycle completion.");
    }

    if (markControlCycleComplete(controlCycleId, status)) {
      return true;
    }

    Serial.print("Control cycle completion report failed, attempt ");
    Serial.print(attempt);
    Serial.print(" of ");
    Serial.println(maxAttempts);

    if (attempt < maxAttempts && !waitForFullControl(1000)) {
      return false;
    }
  }

  Serial.println("WARNING: Control cycle completion was not confirmed by the backend.");
  return false;
}

bool markControlCycleActionStarted(int controlCycleId, const char* status) {
  return postControlCycleStatus(controlCycleId, "action-started", status, "action started");
}

bool markControlCycleActionCompleted(int controlCycleId, const char* status) {
  return postControlCycleStatus(controlCycleId, "action-completed", status, "action completed");
}

bool postControlCycleStatus(int controlCycleId, const char* action, const char* status, const char* label) {
  if (controlCycleId <= 0 || WiFi.status() != WL_CONNECTED) {
    return false;
  }

  HTTPClient http;
  String url = apiBaseUrl() + "/api/control-cycles/" + String(controlCycleId) + "/" + action;
  String payload = "{\"status\":\"" + String(status) + "\"}";

  http.setTimeout(HTTP_TIMEOUT_MS);
  http.begin(url);
  http.addHeader("Content-Type", "application/json");
#ifdef DEVICE_API_TOKEN
  http.addHeader("X-Device-Token", DEVICE_API_TOKEN);
#endif

  int responseCode = http.POST(payload);

  Serial.print("Control cycle ");
  Serial.print(label);
  Serial.print(" response code: ");
  Serial.println(responseCode);

  http.end();
  return responseCode >= 200 && responseCode < 300;
}

String extractStringValue(const String& json, const String& key) {
  String pattern = "\"" + key + "\":";
  int keyIndex = json.indexOf(pattern);

  if (keyIndex < 0) {
    return "";
  }

  int valueStart = json.indexOf("\"", keyIndex + pattern.length());
  if (valueStart < 0) {
    return "";
  }

  int valueEnd = json.indexOf("\"", valueStart + 1);
  if (valueEnd < 0) {
    return "";
  }

  return json.substring(valueStart + 1, valueEnd);
}

String extractDecisionValue(const String& json) {
  String decisionObjectPattern = "\"decision\":{";
  int objectIndex = json.indexOf(decisionObjectPattern);

  if (objectIndex < 0) {
    return extractStringValue(json, "decision");
  }

  String decisionValuePattern = "\"decision\":\"";
  int valueStart = json.indexOf(
    decisionValuePattern,
    objectIndex + decisionObjectPattern.length()
  );

  if (valueStart < 0) {
    return "";
  }

  valueStart += decisionValuePattern.length();
  int valueEnd = json.indexOf("\"", valueStart);

  if (valueEnd < 0) {
    return "";
  }

  return json.substring(valueStart, valueEnd);
}

String extractObjectValue(const String& json, const String& objectKey) {
  String pattern = "\"" + objectKey + "\":{";
  int objectStart = json.indexOf(pattern);

  if (objectStart < 0) {
    return "";
  }

  int braceStart = json.indexOf("{", objectStart + pattern.length() - 1);
  if (braceStart < 0) {
    return "";
  }

  int depth = 0;
  bool inString = false;

  for (int i = braceStart; i < json.length(); i++) {
    char current = json.charAt(i);
    char previous = i > 0 ? json.charAt(i - 1) : '\0';

    if (current == '"' && previous != '\\') {
      inString = !inString;
    }

    if (inString) {
      continue;
    }

    if (current == '{') {
      depth++;
    } else if (current == '}') {
      depth--;
      if (depth == 0) {
        return json.substring(braceStart, i + 1);
      }
    }
  }

  return "";
}

String extractStringValueFromObject(const String& json, const String& objectKey, const String& key) {
  String objectValue = extractObjectValue(json, objectKey);
  if (objectValue.length() == 0) {
    return "";
  }

  return extractStringValue(objectValue, key);
}

String extractArrayValue(const String& json, const String& key) {
  String pattern = "\"" + key + "\":[";
  int keyIndex = json.indexOf(pattern);

  if (keyIndex < 0) {
    return "";
  }

  int arrayStart = json.indexOf("[", keyIndex + pattern.length() - 1);
  int arrayEnd = json.indexOf("]", arrayStart);

  if (arrayStart < 0 || arrayEnd < 0) {
    return "";
  }

  String value = json.substring(arrayStart + 1, arrayEnd);
  value.replace("\"", "");
  value.replace(",", " -> ");
  value.trim();
  return value;
}

bool extractBoolValue(const String& json, const String& key) {
  String pattern = "\"" + key + "\":";
  int keyIndex = json.indexOf(pattern);

  if (keyIndex < 0) {
    return false;
  }

  int valueStart = keyIndex + pattern.length();
  while (valueStart < json.length() && json.charAt(valueStart) == ' ') {
    valueStart++;
  }

  return json.substring(valueStart, valueStart + 4) == "true";
}

int extractIntValue(const String& json, const String& key) {
  String pattern = "\"" + key + "\":";
  int keyIndex = json.indexOf(pattern);

  if (keyIndex < 0) {
    return 0;
  }

  int valueStart = keyIndex + pattern.length();
  while (valueStart < json.length() && json.charAt(valueStart) == ' ') {
    valueStart++;
  }

  int valueEnd = valueStart;
  while (valueEnd < json.length() && isDigit(json.charAt(valueEnd))) {
    valueEnd++;
  }

  return json.substring(valueStart, valueEnd).toInt();
}

float extractFloatValue(const String& json, const String& key) {
  String pattern = "\"" + key + "\":";
  int keyIndex = json.indexOf(pattern);

  if (keyIndex < 0) {
    return 0.0;
  }

  int valueStart = keyIndex + pattern.length();
  while (valueStart < json.length() && json.charAt(valueStart) == ' ') {
    valueStart++;
  }

  int valueEnd = valueStart;
  while (
    valueEnd < json.length()
    && (
      isDigit(json.charAt(valueEnd))
      || json.charAt(valueEnd) == '.'
      || json.charAt(valueEnd) == '-'
    )
  ) {
    valueEnd++;
  }

  return json.substring(valueStart, valueEnd).toFloat();
}

void pumpOn(int pin) {
  digitalWrite(pin, HIGH);
}

void pumpOff(int pin) {
  digitalWrite(pin, LOW);
}

void stopAllPumps() {
  pumpOff(PH_UP_PUMP_PIN);
  pumpOff(PH_DOWN_PUMP_PIN);
  pumpOff(EC_UP_A_PUMP_PIN);
  pumpOff(EC_UP_B_PUMP_PIN);
  pumpOff(EC_DOWN_PUMP_PIN);
}

int pumpPinForName(const String& pumpName) {
  if (pumpName == "ph_up") {
    return PH_UP_PUMP_PIN;
  }

  if (pumpName == "ph_down") {
    return PH_DOWN_PUMP_PIN;
  }

  if (pumpName == "ec_up_a") {
    return EC_UP_A_PUMP_PIN;
  }

  if (pumpName == "ec_up_b") {
    return EC_UP_B_PUMP_PIN;
  }

  if (pumpName == "ec_down") {
    return EC_DOWN_PUMP_PIN;
  }

  return -1;
}

bool runDosingCountdown(int pin, unsigned long durationMs, const String& pumpName) {
  unsigned long startedAt = millis();
  unsigned long lastDosingStatusLogTime = 0;
  unsigned long lastEmergencyStopPollTime = 0;

  // HTTP polling runs on the Arduino task and may block. The ESP timer
  // service turns this output off independently at the commanded deadline.
  esp_timer_handle_t cutoffTimer = nullptr;
  esp_timer_create_args_t cutoffArgs = {};
  cutoffArgs.callback = [](void* argument) {
    portENTER_CRITICAL(&dosingCutoffMux);
    dosingCutoffExpired = true;
    digitalWrite(static_cast<int>(reinterpret_cast<intptr_t>(argument)), LOW);
    portEXIT_CRITICAL(&dosingCutoffMux);
  };
  cutoffArgs.arg = reinterpret_cast<void*>(static_cast<intptr_t>(pin));
  cutoffArgs.dispatch_method = ESP_TIMER_TASK;
  cutoffArgs.name = "pump_cutoff";
  if (esp_timer_create(&cutoffArgs, &cutoffTimer) != ESP_OK) {
    stopAllPumps();
    enterMonitoringMode();
    Serial.println("Pump cutoff timer unavailable; actuation held.");
    return false;
  }
  portENTER_CRITICAL(&dosingCutoffMux);
  dosingCutoffExpired = false;
  portEXIT_CRITICAL(&dosingCutoffMux);
  if (esp_timer_start_once(cutoffTimer, static_cast<uint64_t>(durationMs) * 1000ULL) != ESP_OK) {
    esp_timer_delete(cutoffTimer);
    stopAllPumps();
    enterMonitoringMode();
    Serial.println("Pump cutoff timer could not start; actuation held.");
    return false;
  }
  startedAt = millis();
  // Serialize turn-on with expiry: a task delayed past its timer must never
  // turn the pump back on after the cutoff callback has already run.
  portENTER_CRITICAL(&dosingCutoffMux);
  bool canStart = !dosingCutoffExpired;
  if (canStart) pumpOn(pin);
  portEXIT_CRITICAL(&dosingCutoffMux);
  if (!canStart) {
    esp_timer_stop(cutoffTimer);
    esp_timer_delete(cutoffTimer);
    enterMonitoringMode();
    Serial.println("Pump start missed its cutoff deadline; actuation held.");
    return false;
  }

  while (millis() - startedAt < durationMs) {
    processSerialCommands();
    if (controllerMode == MODE_MONITORING) {
      Serial.println("Dosing interrupted by monitoring mode.");
      break;
    }

    unsigned long now = millis();
    if (now - lastEmergencyStopPollTime >= EMERGENCY_STOP_POLL_MS) {
      lastEmergencyStopPollTime = now;
      if (backendEmergencyStopActive()) {
        emergencyStopLatched = true;
        stopAllPumps();
        enterMonitoringMode();
        Serial.println("Emergency stop received from backend. Pump outputs forced off.");
        esp_timer_stop(cutoffTimer);
        esp_timer_delete(cutoffTimer);
        return false;
      }
    }

    if (lastDosingStatusLogTime == 0 || now - lastDosingStatusLogTime >= 1000) {
      unsigned long elapsedMs = now - startedAt;
      if (elapsedMs >= durationMs) {
        break;
      }

      unsigned long remainingMs = durationMs - elapsedMs;

      Serial.print("Dosing ");
      Serial.print(pumpName);
      Serial.print("... elapsed ");
      Serial.print(elapsedMs / 1000.0, 2);
      Serial.print("s / ");
      Serial.print(durationMs / 1000.0, 2);
      Serial.print("s, remaining ");
      Serial.print(remainingMs / 1000.0, 2);
      Serial.println("s");

      lastDosingStatusLogTime = now;
    }

    delay(50);
  }

  pumpOff(pin);
  esp_timer_stop(cutoffTimer);
  esp_timer_delete(cutoffTimer);
  return controllerMode == MODE_FULL_CONTROL;
}

bool runInterDoseMixingCountdown(unsigned long durationMs) {
  unsigned long startedAt = millis();
  unsigned long lastStatusLogTime = 0;
  unsigned long lastEmergencyStopPollTime = 0;

  while (millis() - startedAt < durationMs) {
    processSerialCommands();
    if (controllerMode != MODE_FULL_CONTROL) {
      Serial.println("EC Up A/B mixing interrupted by monitoring mode.");
      return false;
    }

    unsigned long now = millis();
    if (now - lastEmergencyStopPollTime >= EMERGENCY_STOP_POLL_MS) {
      lastEmergencyStopPollTime = now;
      if (backendEmergencyStopActive()) {
        emergencyStopLatched = true;
        stopAllPumps();
        enterMonitoringMode();
        Serial.println("Emergency stop received during EC Up A/B mixing. Pump outputs locked off.");
        return false;
      }
    }

    if (lastStatusLogTime == 0 || now - lastStatusLogTime >= 1000) {
      unsigned long elapsedMs = now - startedAt;
      if (elapsedMs >= durationMs) {
        break;
      }

      unsigned long remainingMs = durationMs - elapsedMs;

      Serial.print("Mixing between EC Up A and B... elapsed ");
      Serial.print(elapsedMs / 1000.0, 2);
      Serial.print("s / ");
      Serial.print(durationMs / 1000.0, 2);
      Serial.print("s, remaining ");
      Serial.print(remainingMs / 1000.0, 2);
      Serial.println("s");

      lastStatusLogTime = now;
    }

    delay(50);
  }

  return true;
}

void runPumpForDuration(
  int pin,
  unsigned long durationMs,
  unsigned long mixingTimeMs,
  const String& pumpName
) {
  stopAllPumps();

  Serial.print("Activating ");
  Serial.print(pumpName);
  Serial.print(" for ");
  Serial.print(durationMs);
  Serial.println(" ms");

  if (!runDosingCountdown(pin, durationMs, pumpName)) {
    stopAllPumps();
    return;
  }
  if (controllerMode == MODE_MONITORING) {
    stopAllPumps();
    return;
  }

  Serial.print("Action complete: ");
  Serial.println(pumpName);

  startMixingPeriod(mixingTimeMs);
}

void runEcUpSequence(int controlCycleId, unsigned long durationMs, unsigned long mixingTimeMs) {
  stopAllPumps();

  Serial.print("Activating ec_up_a for ");
  Serial.print(durationMs);
  Serial.println(" ms");

  if (!runDosingCountdown(EC_UP_A_PUMP_PIN, durationMs, "ec_up_a")) {
    stopAllPumps();
    return;
  }
  if (controllerMode == MODE_MONITORING) {
    stopAllPumps();
    return;
  }

  Serial.print("Mixing between EC Up A and B for ");
  Serial.print(EC_UP_INTER_DOSE_MIXING_DURATION_MS);
  Serial.println(" ms");
  if (!markControlCycleActionStarted(controlCycleId, "inter_dose_mixing")) {
    Serial.println("WARNING: Unable to publish EC Up A/B mixing status; continuing the safe mixing delay.");
  }
  if (!runInterDoseMixingCountdown(EC_UP_INTER_DOSE_MIXING_DURATION_MS)) {
    stopAllPumps();
    return;
  }

  Serial.print("Activating ec_up_b for ");
  Serial.print(durationMs);
  Serial.println(" ms");
  if (!markControlCycleActionStarted(controlCycleId, "ec_up_b_dosing")) {
    Serial.println("Unable to report EC Up B start. Stopping before the second nutrient pump.");
    enterMonitoringMode();
    return;
  }

  if (!runDosingCountdown(EC_UP_B_PUMP_PIN, durationMs, "ec_up_b")) {
    stopAllPumps();
    return;
  }
  if (controllerMode == MODE_MONITORING) {
    stopAllPumps();
    return;
  }

  Serial.println("Action complete: ec_up A then B");

  startMixingPeriod(mixingTimeMs);
}

void startMixingPeriod(unsigned long mixingTimeMs) {
  if (mixingTimeMs == 0) {
    mixingTimeMs = MIXING_TIME_MS;
  }

  isMixing = true;
  mixingStartedAt = millis();
  currentMixingTimeMs = mixingTimeMs;
  lastMixingStatusLogTime = 0;

  Serial.print("Mixing for ");
  Serial.print(mixingTimeMs / 1000);
  Serial.print(" seconds (");
  Serial.print(mixingTimeMs);
  Serial.println(" ms).");
  Serial.println();
}

void printMonitoringSample(
  int16_t rawPh,
  int16_t rawEc,
  float phVoltageMv,
  float ecVoltageMv,
  float rawPhValue,
  float rawEcValue,
  const SensorSnapshot& snapshot,
  bool phValid,
  bool ecValid,
  bool phStable,
  bool ecStable
) {
  Serial.println("----- monitoring sample -----");
  Serial.print("Sample: ");
  Serial.print(monitoringSampleNumber);
  Serial.print("  Uptime: ");
  Serial.print(millis() / 1000);
  Serial.println("s");

  Serial.print("pH raw: ");
  Serial.print(rawPh);
  Serial.print("  voltage: ");
  Serial.print(phVoltageMv, 2);
  Serial.print("mV  calculated pH: ");
  Serial.print(rawPhValue, 2);
  if (phValid) {
    Serial.print("  filtered pH: ");
    Serial.print(snapshot.ph, 2);
    Serial.println("  status: valid");
  } else {
    Serial.println("  status: INVALID (expected 0.00 to 14.00)");
  }

  Serial.print("EC raw: ");
  Serial.print(rawEc);
  Serial.print("  voltage: ");
  Serial.print(ecVoltageMv, 2);
  Serial.print("mV  calculated EC: ");
  Serial.print(rawEcValue, 3);
  Serial.print(" mS/cm");
  if (ecValid) {
    Serial.print("  filtered EC: ");
    Serial.print(snapshot.ec, 3);
    Serial.println(" mS/cm  status: valid");
  } else {
    Serial.println("  status: INVALID (expected 0.000 to 20.000 mS/cm)");
  }

  float temperatureF = (snapshot.temperature * 9.0 / 5.0) + 32.0;
  Serial.print("Water temperature: ");
  Serial.print(snapshot.temperature, 2);
  Serial.print(" C / ");
  Serial.print(temperatureF, 2);
  Serial.println(" F");
  Serial.println("Temperature compensation: applied to pH and EC readings");

  Serial.print("Reservoir volume: ");
  Serial.print(snapshot.waterLevel.reservoirVolumeLiters, 2);
  Serial.println(" L");

  Serial.print("pH stable: ");
  if (phValid) {
    Serial.print(phStable ? "yes" : "no");
    Serial.print(" for ");
    Serial.print(snapshot.phStableForSeconds);
    Serial.print("s");
  } else {
    Serial.print("n/a (invalid pH reading)");
  }

  Serial.print("  EC stable: ");
  if (ecValid) {
    Serial.print(ecStable ? "yes" : "no");
    Serial.print(" for ");
    Serial.print(snapshot.ecStableForSeconds);
    Serial.println("s");
  } else {
    Serial.println("n/a (invalid EC reading)");
  }
  Serial.println();

  Serial.print("Level voltage: ");
  Serial.print(snapshot.waterLevel.voltage, 3);
  Serial.println(" V");

  Serial.print("Level current: ");
  Serial.print(snapshot.waterLevel.currentMa, 2);
  Serial.println(" mA");

  Serial.print("Water level: ");
  Serial.print(snapshot.waterLevel.levelCm, 2);
  Serial.println(" cm");

  Serial.print("Tank fill: ");
  Serial.print(snapshot.waterLevel.percent, 2);
  Serial.println(" %");
}

void printStatus(float pHValue, float ecValue, float tempC, const WaterLevelReading& waterLevel) {
  Serial.println("--------------- CURRENT STATUS ----------------");

  Serial.print("pH: ");
  Serial.println(pHValue, 2);

  Serial.print("EC (mS/cm): ");
  Serial.println(ecValue, 3);

  Serial.print("Water Temperature (C): ");
  Serial.println(tempC, 2);

  Serial.print("Water Level (cm): ");
  Serial.println(waterLevel.levelCm, 2);

  Serial.print("Tank Fill (%): ");
  Serial.println(waterLevel.percent, 2);

  Serial.print("Reservoir Volume (L): ");
  Serial.println(waterLevel.reservoirVolumeLiters, 2);

  Serial.println("----------------------------------------------");
}

void printPayloadSummary(const SensorSnapshot& snapshot, const String& payload) {
  Serial.println("--------------- HANAS REQUEST ----------------");
  Serial.print("Endpoint: ");
  Serial.println(sensorEndpoint());
  Serial.print("Requested control strategy: ");
  Serial.println(CONTROL_STRATEGY);
  Serial.print("Payload pH: ");
  Serial.println(snapshot.ph, 2);
  Serial.print("Payload EC (mS/cm): ");
  Serial.println(snapshot.ec, 3);
  Serial.print("Payload temperature (C): ");
  Serial.println(snapshot.temperature, 2);
  Serial.print("Payload reservoir volume (L): ");
  Serial.println(snapshot.waterLevel.reservoirVolumeLiters, 2);
  Serial.print("pH stable for (s): ");
  Serial.println(snapshot.phStableForSeconds);
  Serial.print("EC stable for (s): ");
  Serial.println(snapshot.ecStableForSeconds);
  Serial.print("pH stability threshold: +/-");
  Serial.println(snapshot.phStabilityThreshold, 2);
  Serial.print("EC stability threshold: +/-");
  Serial.println(snapshot.ecStabilityThreshold, 3);
  Serial.println("Raw JSON payload:");
  Serial.println(payload);
  Serial.println("----------------------------------------------");
}

void printDecisionSummary(const BackendDecisionSummary& decision) {
  Serial.println("--------------- HANAS DECISION ---------------");
  Serial.print("Backend status: ");
  Serial.println(decision.status);
  Serial.print("Received strategy: ");
  Serial.println(decision.receivedStrategy);
  Serial.print("Executed strategy: ");
  if (decision.executedStrategy.length() > 0) {
    Serial.println(decision.executedStrategy);
  } else {
    Serial.println(decision.receivedStrategy);
  }
  Serial.print("Decision: ");
  Serial.println(decision.decision);
  Serial.print("Agentic mode: ");
  if (decision.agenticMode.length() > 0) {
    Serial.println(decision.agenticMode);
  } else {
    Serial.println("n/a");
  }
  Serial.print("Actuation source: ");
  if (decision.actuationSource.length() > 0) {
    Serial.println(decision.actuationSource);
  } else {
    Serial.println("n/a");
  }
  Serial.print("Agentic action: ");
  if (decision.agenticAction.length() > 0) {
    Serial.println(decision.agenticAction);
  } else {
    Serial.println("n/a");
  }
  Serial.print("Safety gate source: ");
  if (decision.safetyGateSource.length() > 0) {
    Serial.println(decision.safetyGateSource);
  } else {
    Serial.println("n/a");
  }
  printAgentFlow(decision);
  printHistoryContext(decision);
  printHistoryBasis(decision);
  Serial.print("Pump activated: ");
  Serial.println(decision.pumpActivated);
  Serial.print("Dose ml: ");
  Serial.println(decision.doseMl, 2);
  if (decision.doseBeforeFactorMl > 0.0 || decision.appliedDoseFactor > 0.0) {
    Serial.print("Dose basis: before_factor_ml=");
    Serial.print(decision.doseBeforeFactorMl, 2);
    Serial.print(" | applied_factor=");
    Serial.print(decision.appliedDoseFactor, 2);
    Serial.print(" | final_ml=");
    Serial.print(decision.doseMl, 2);
    Serial.print(" | baseline_ml=");
    Serial.print(decision.baselineDoseMl, 2);
    Serial.println();
  }
  Serial.print("Duration ms: ");
  Serial.println(decision.durationMs);
  if (decision.durationBeforeFactorMs > 0 || decision.baselineDurationMs > 0) {
    Serial.print("Duration basis: before_factor_ms=");
    Serial.print(decision.durationBeforeFactorMs);
    Serial.print(" | final_ms=");
    Serial.print(decision.durationMs);
    Serial.print(" | baseline_ms=");
    Serial.print(decision.baselineDurationMs);
    Serial.println();
  }
  Serial.print("Mixing time ms: ");
  Serial.println(decision.mixingTimeMs);
  Serial.print("Mixing time seconds: ");
  Serial.println(decision.mixingTimeMs / 1000);
  Serial.print("Log ID: ");
  Serial.println(decision.logId);
  Serial.print("Control cycle ID: ");
  Serial.println(decision.controlCycleId);
  Serial.print("Confidence: ");
  Serial.println(decision.confidence, 2);
  Serial.print("Reason: ");
  Serial.println(decision.reason);
  Serial.println("----------------------------------------------");
}

void printAgentFlow(const BackendDecisionSummary& decision) {
  Serial.print("Agent flow: ");

  if (decision.orchestratorRoute == "safety_gate") {
    Serial.print("orchestrator -> safety_gate");
  } else if (decision.orchestratorRoute == "monitoring_agent") {
    Serial.print("orchestrator -> monitoring -> orchestrator");
    if (decision.skippedAgents.indexOf("diagnostic_reasoning_agent") >= 0) {
      Serial.print(" -> safety_gate");
    } else {
      Serial.print(" -> diagnostic -> orchestrator -> decision -> orchestrator");
      if (decision.skippedAgents.indexOf("dose_planning_agent") < 0) {
        Serial.print(" -> dose_planning -> orchestrator");
      }
      if (decision.skippedAgents.indexOf("consistency_review") < 0) {
        Serial.print(" -> consistency_review -> orchestrator");
      }
      Serial.print(" -> safety_gate");
    }
  } else {
    Serial.print("n/a");
  }

  if (decision.orchestratorAction.length() > 0) {
    Serial.print(" | orchestrator_action=");
    Serial.print(decision.orchestratorAction);
  }

  if (decision.reasoningSource.length() > 0) {
    Serial.print(" | source=");
    Serial.print(decision.reasoningSource);
  }

  if (decision.skippedAgents.length() > 0) {
    Serial.print(" | skipped=");
    Serial.print(decision.skippedAgents);
  }

  Serial.println();

  if (decision.riskFlags.length() > 0) {
    Serial.print("Risk flags: ");
    Serial.println(decision.riskFlags);
  }

  if (decision.deliveryIssueDetected) {
    Serial.println("Delivery issue: possible pump/dosing failure detected");
    Serial.print("Delivery issue reason: ");
    Serial.println(decision.deliveryIssueReason);
    Serial.print("Delivery issue pump: ");
    Serial.println(decision.deliveryIssuePump);
    Serial.print("Delivery issue metric: ");
    Serial.println(decision.deliveryIssueMetric);
    Serial.print("Delivery issue values: previous=");
    Serial.print(decision.deliveryIssuePreviousValue, 4);
    Serial.print(" current=");
    Serial.print(decision.deliveryIssueCurrentValue, 4);
    Serial.print(" expected_delta=");
    Serial.print(decision.deliveryIssueExpectedDelta, 4);
    Serial.print(" minimum_expected_delta=");
    Serial.println(decision.deliveryIssueMinimumDelta, 4);
    if (decision.deliveryIssuePotentialCauses.length() > 0) {
      Serial.print("Potential causes: ");
      Serial.println(decision.deliveryIssuePotentialCauses);
    }
  }

  if (decision.orchestratorReason.length() > 0) {
    Serial.print("Orchestrator reason: ");
    Serial.println(decision.orchestratorReason);
  }
}

void printHistoryContext(const BackendDecisionSummary& decision) {
  Serial.print("History context: recent_logs=");
  Serial.print(decision.recentLogCount);
  Serial.print(" | same_pump=");

  if (decision.samePumpAvailable) {
    Serial.print(decision.samePumpInterpretation);
  } else {
    Serial.print("none");
  }

  if (decision.latestSamePumpDoseMl > 0.0) {
    Serial.print(" | latest_same_pump_dose_ml=");
    Serial.print(decision.latestSamePumpDoseMl, 2);
  }

  if (decision.latestSamePumpStatus.length() > 0) {
    Serial.print(" | latest_same_pump_status=");
    Serial.print(decision.latestSamePumpStatus);
  }

  if (decision.recommendedDoseFactor > 0.0) {
    Serial.print(" | history_fallback_factor=");
    Serial.print(decision.recommendedDoseFactor, 2);
  }

  if (decision.targetRangeHeadroomFactor > 0.0) {
    Serial.print(" | target_range_headroom_factor=");
    Serial.print(decision.targetRangeHeadroomFactor, 2);
  }

  if (decision.unresolvedSamePumpDoseCount > 0) {
    Serial.print(" | unresolved_same_pump_doses=");
    Serial.print(decision.unresolvedSamePumpDoseCount);
  }

  if (decision.latestSamePumpHitPumpCap) {
    Serial.print(" | latest_same_pump_hit_cap=yes");
  }

  Serial.println();
}

void printHistoryBasis(const BackendDecisionSummary& decision) {
  Serial.print("History basis: ");

  if (decision.samePumpInterpretation == "previous_same_pump_still_unresolved") {
    Serial.print("Previous same-pump dose is still unresolved");
    if (decision.latestSamePumpDoseMl > 0.0) {
      Serial.print(" after ");
      Serial.print(decision.latestSamePumpDoseMl, 2);
      Serial.print(" ml");
    }
    if (decision.latestSamePumpHitCapAndUnresolved) {
      Serial.print(" and that dose reached its cycle cap");
    }
    Serial.print("; history fallback factor is ");
    Serial.print(decision.recommendedDoseFactor, 2);
    if (decision.appliedDoseFactor > 0.0) {
      Serial.print(", final applied dose factor is ");
      Serial.print(decision.appliedDoseFactor, 2);
    }
    Serial.println(".");
    return;
  }

  if (decision.samePumpInterpretation == "previous_same_pump_resolved_or_changed_condition") {
    Serial.print("Previous same-pump correction returned to range or the condition changed");
    Serial.print("; history fallback factor is ");
    Serial.print(decision.recommendedDoseFactor, 2);
    if (decision.appliedDoseFactor > 0.0) {
      Serial.print(", final applied dose factor is ");
      Serial.print(decision.appliedDoseFactor, 2);
    }
    Serial.println("; remeasure after mixing.");
    return;
  }

  if (decision.samePumpInterpretation == "previous_same_pump_overshot_opposite_direction") {
    Serial.print("Recent same-pump history indicates overshoot risk");
    Serial.print("; history fallback factor is ");
    Serial.print(decision.recommendedDoseFactor, 2);
    if (decision.appliedDoseFactor > 0.0) {
      Serial.print(", final applied dose factor is ");
      Serial.print(decision.appliedDoseFactor, 2);
    }
    Serial.println(".");
    return;
  }

  if (decision.samePumpReason == "No recent same-pump dose exists inside the fresh history window.") {
    Serial.print("No fresh same-pump dose is available");
    Serial.print("; history fallback factor is ");
    Serial.print(decision.recommendedDoseFactor, 2);
    if (decision.appliedDoseFactor > 0.0) {
      Serial.print(", final applied dose factor is ");
      Serial.print(decision.appliedDoseFactor, 2);
    }
    Serial.println("; remeasure after mixing.");
    return;
  }

  if (decision.samePumpReason.length() > 0) {
    Serial.println(decision.samePumpReason);
    return;
  }

  Serial.println("No specific same-pump history adjustment was reported.");
}
