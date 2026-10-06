#include <Wire.h>
#include <Adafruit_ADS1X15.h>
#include <DallasTemperature.h>
#include "DFRobot_EC.h"
#include "DFRobot_PH.h"
#include <EEPROM.h>
#include <OneWire.h>
#include <string.h>

/*
  DFRobot Gravity pH + EC calibration and test sketch
  pH source guide: https://wiki.dfrobot.com/sen0161-v2/docs/20941
  EC source guide: https://wiki.dfrobot.com/dfr0300/docs/20349
  Analog isolator guide: https://wiki.dfrobot.com/dfr0504/docs/19034

  Wiring:
    pH board +       -> pH analog isolator SEN +
    pH board -       -> pH analog isolator SEN -
    pH board A       -> pH analog isolator SEN A
    pH isolator MCU + -> 5V
    pH isolator MCU - -> system GND
    pH isolator MCU A -> voltage divider -> ADS1115 A0

    EC board +       -> EC analog isolator SEN +
    EC board -       -> EC analog isolator SEN -
    EC board A       -> EC analog isolator SEN A
    EC isolator MCU + -> 5V
    EC isolator MCU - -> system GND
    EC isolator MCU A -> voltage divider -> ADS1115 A1

    ADS1115 SDA      -> ESP32 GPIO21
    ADS1115 SCL      -> ESP32 GPIO22
    ADS1115 VDD      -> ESP32 3.3V
    DS18B20 data     -> ESP32 GPIO27 with 4.7k pull-up to 3.3V
    DS18B20 VDD      -> ESP32 3.3V
    DS18B20 GND      -> system GND

  Do not connect either isolator SEN-side GND to MCU/system GND; that defeats
  the isolation.

  Each isolator MCU-side output can reach 5V. Since the ADS1115 is powered from
  3.3V, do not connect either isolator analog output directly to the ADS1115.

  Recommended divider for each isolator:
    Isolator MCU A -> 10k resistor -> 4.7k resistor -> ADS1115 analog input
    ADS1115 input  -> 20k resistor -> GND

  This scales 5V down to about 2.88V. The code multiplies each ADS reading back
  up before giving it to the DFRobot pH or EC library.

  Serial Monitor:
    Baud rate: 115200
    Line ending: Both NL & CR

  pH calibration:
    1. Put the pH probe in pH 7.0 buffer and wait for stable pH.
    2. Send: enterph
    3. Send: calph
    4. Send: exitph
    5. Rinse/blot, then repeat with pH 4.0 buffer.

  EC calibration:
    1. Put the EC probe in 1413us/cm buffer and wait for stable EC.
    2. Send: enterec
    3. Send: calec
    4. Send: exitec
    5. Rinse/blot, then repeat with 12.88ms/cm buffer.
*/

const uint8_t ADS_ADDRESS = 0x48;
const uint8_t PH_CHANNEL = 0;               // ADS1115 A0
const uint8_t EC_CHANNEL = 1;               // ADS1115 A1
const uint8_t TEMP_SENSOR_PIN = 27;         // DS18B20 data pin
const uint8_t SAMPLE_COUNT = 20;
const unsigned long READ_INTERVAL_MS = 1000;
const unsigned long STABLE_REQUIRED_MS = 30000;
const float ADS_MV_PER_BIT = 0.125;         // GAIN_ONE: +/-4.096V range
const float PH_DIVIDER_MULTIPLIER = 1.735;  // (10k + 4.7k + 20k) / 20k
const float EC_DIVIDER_MULTIPLIER = 1.735;  // (10k + 4.7k + 20k) / 20k
const float PH_STABLE_DELTA = 0.03;
const float EC_STABLE_DELTA = 0.03;         // ms/cm
const float PH_FILTER_ALPHA = 0.20;         // Lower is smoother, higher reacts faster.
const float EC_FILTER_ALPHA = 0.20;
const uint8_t UNSTABLE_RESET_COUNT = 5;
const uint8_t COMMAND_BUFFER_SIZE = 24;
const bool MULTILINE_LOGS = true;
const unsigned long START_READING_WINDOW_MS = 300000;  // 5-minute baseline drift window.
const float PH_START_DRIFT_DELTA = 0.08;
const float EC_START_DRIFT_DELTA = 0.03;    // ms/cm

Adafruit_ADS1115 ads;
DFRobot_PH ph;
DFRobot_EC ec;
OneWire oneWire(TEMP_SENSOR_PIN);
DallasTemperature tempSensors(&oneWire);

struct StabilityState {
    float referenceValue;
    unsigned long startedAt;
    bool reported;
    bool initialized;
    uint8_t unstableReadingCount;
};

struct StartWindow {
    float referenceValue;
    unsigned long startedAt;
    bool initialized;
    const char* resetReason;
    float lastResetDrift;
    uint8_t driftOutOfRangeCount;
};

float phVoltage, ecVoltage, phValue, ecValue, temperature = 25.0;
float filteredPhValue = NAN;
float filteredEcValue = NAN;
StabilityState phStability = {NAN, 0, false, false, 0};
StabilityState ecStability = {NAN, 0, false, false, 0};
bool hasSensorReading = false;
unsigned long sampleNumber = 0;
StartWindow phStartWindow = {NAN, 0, false, "initializing", 0.0, 0};
StartWindow ecStartWindow = {NAN, 0, false, "initializing", 0.0, 0};

float readAverageAdsMilliVolts(uint8_t channel, float multiplier = 1.0);
float filterReading(float previousValue, float currentValue, float alpha);
float readTemperature();
bool readSerialCommand(char* command, size_t commandSize);
bool isPHCommand(const char* command);
bool isECCommand(const char* command);
bool isCalibrationCommand(const char* command);
bool isStable(const StabilityState& state);
void updateStartWindow(StartWindow& window, float value, bool stable, float driftDelta, const char* stableResetReason, const char* driftResetReason);
void resetStartWindow(StartWindow& window, const char* reason, float lastResetDrift);
void printCalibrationWarning(const char* sensorName, const StabilityState& state);
void printSensorReport(int16_t rawPh, int16_t rawEc);
void printStartCandidateStatus(bool multiline);
void updateStability(StabilityState& state, float currentValue, float stableDelta);
void printStability(bool multiline);

float readAverageAdsMilliVolts(uint8_t channel, float multiplier)
{
    float sum = 0.0;

    for (int i = 0; i < SAMPLE_COUNT; i++) {
        int16_t raw = ads.readADC_SingleEnded(channel);
        sum += raw * ADS_MV_PER_BIT * multiplier;
        delay(10);
    }

    return sum / (float)SAMPLE_COUNT;
}

float filterReading(float previousValue, float currentValue, float alpha)
{
    if (isnan(previousValue)) {
        return currentValue;
    }

    return previousValue + alpha * (currentValue - previousValue);
}

void setup()
{
    Serial.begin(115200);
    delay(1000);

    Wire.begin(21, 22);  // SDA = GPIO21, SCL = GPIO22

    if (!ads.begin(ADS_ADDRESS)) {
        Serial.println("ADS1115 not detected. Check wiring.");
        while (true) {
            delay(1000);
        }
    }

    ads.setGain(GAIN_ONE);
    tempSensors.begin();

    EEPROM.begin(32);
    ph.begin();
    ec.begin();

    Serial.println("DFRobot Gravity pH + EC calibration/test via ADS1115");
    Serial.println("pH: ADS1115 A0, EC: ADS1115 A1, DS18B20: GPIO27");
    Serial.println("Commands: enterph, calph, exitph, enterec, calec, exitec");
    Serial.println("Use pH 7.0/4.0 buffers for pH calibration and EC standards for EC calibration.");
    Serial.println("Keep probes away from direct air bubbles while checking stability.");
    Serial.print("Stable when pH changes by <= ");
    Serial.print(PH_STABLE_DELTA, 2);
    Serial.print(" and EC changes by <= ");
    Serial.print(EC_STABLE_DELTA, 3);
    Serial.print(" ms/cm for ");
    Serial.print(STABLE_REQUIRED_MS / 1000);
    Serial.println(" seconds.");
}

void loop()
{
    static unsigned long timepoint = millis();

    if (millis() - timepoint > READ_INTERVAL_MS) {
        timepoint = millis();

        temperature = readTemperature();

        int16_t rawPh = ads.readADC_SingleEnded(PH_CHANNEL);
        int16_t rawEc = ads.readADC_SingleEnded(EC_CHANNEL);
        phVoltage = readAverageAdsMilliVolts(PH_CHANNEL, PH_DIVIDER_MULTIPLIER);
        ecVoltage = readAverageAdsMilliVolts(EC_CHANNEL, EC_DIVIDER_MULTIPLIER);
        phValue = ph.readPH(phVoltage, temperature);
        ecValue = ec.readEC(ecVoltage, temperature);
        filteredPhValue = filterReading(filteredPhValue, phValue, PH_FILTER_ALPHA);
        filteredEcValue = filterReading(filteredEcValue, ecValue, EC_FILTER_ALPHA);
        hasSensorReading = true;
        sampleNumber++;

        updateStability(phStability, filteredPhValue, PH_STABLE_DELTA);
        updateStability(ecStability, filteredEcValue, EC_STABLE_DELTA);
        updateStartWindow(phStartWindow, filteredPhValue, isStable(phStability), PH_START_DRIFT_DELTA, "pH stable became no", "pH drift exceeded limit");
        updateStartWindow(ecStartWindow, filteredEcValue, isStable(ecStability), EC_START_DRIFT_DELTA, "EC stable became no", "EC drift exceeded limit");

        printSensorReport(rawPh, rawEc);
    }

    char command[COMMAND_BUFFER_SIZE];
    if (readSerialCommand(command, sizeof(command))) {
        if (!hasSensorReading) {
            Serial.println("Waiting for first pH/EC reading before calibration command.");
            return;
        }

        Serial.print("Command received: ");
        Serial.println(command);

        if (isPHCommand(command)) {
            if (isCalibrationCommand(command) && !isStable(phStability)) {
                printCalibrationWarning("pH", phStability);
            }
            ph.calibration(phVoltage, temperature, command);
            Serial.println("pH calibration command processed.");
            return;
        }

        if (isECCommand(command)) {
            if (isCalibrationCommand(command) && !isStable(ecStability)) {
                printCalibrationWarning("EC", ecStability);
            }
            ec.calibration(ecVoltage, temperature, command);
            Serial.println("EC calibration command processed.");
            return;
        }

        Serial.println("Unknown command. Use enterph, calph, exitph, enterec, calec, or exitec.");
    }
}

bool readSerialCommand(char* command, size_t commandSize)
{
    if (!Serial.available()) {
        return false;
    }

    size_t index = 0;
    while (Serial.available() && index < commandSize - 1) {
        char received = (char)Serial.read();
        if (received == '\r' || received == '\n') {
            if (index > 0) {
                break;
            }
            continue;
        }
        command[index++] = received;
        delay(2);
    }
    command[index] = '\0';

    return index > 0;
}

bool isPHCommand(const char* command)
{
    return strcmp(command, "enterph") == 0
        || strcmp(command, "calph") == 0
        || strcmp(command, "exitph") == 0;
}

bool isECCommand(const char* command)
{
    return strcmp(command, "enterec") == 0
        || strcmp(command, "calec") == 0
        || strcmp(command, "exitec") == 0;
}

bool isCalibrationCommand(const char* command)
{
    return strcmp(command, "calph") == 0 || strcmp(command, "calec") == 0;
}

bool isStable(const StabilityState& state)
{
    return state.initialized && millis() - state.startedAt >= STABLE_REQUIRED_MS;
}

void printCalibrationWarning(const char* sensorName, const StabilityState& state)
{
    Serial.print("Warning: ");
    Serial.print(sensorName);
    Serial.print(" has only been stable for ");
    Serial.print((millis() - state.startedAt) / 1000);
    Serial.print("s. Recommended stable time is ");
    Serial.print(STABLE_REQUIRED_MS / 1000);
    Serial.println("s before cal.");
}

void printSensorReport(int16_t rawPh, int16_t rawEc)
{
    if (!MULTILINE_LOGS) {
        Serial.print("Sample:");
        Serial.print(sampleNumber);
        Serial.print(" RawPH:");
        Serial.print(rawPh);
        Serial.print(" pHVoltage:");
        Serial.print(phVoltage, 2);
        Serial.print("mV RawEC:");
        Serial.print(rawEc);
        Serial.print(" ECVoltage:");
        Serial.print(ecVoltage, 2);
        Serial.print("mV Temperature:");
        Serial.print(temperature, 1);
        Serial.print("C pH:");
        Serial.print(phValue, 2);
        Serial.print(" FilteredPH:");
        Serial.print(filteredPhValue, 2);
        Serial.print(" EC:");
        Serial.print(ecValue, 3);
        Serial.print("ms/cm FilteredEC:");
        Serial.print(filteredEcValue, 3);
        Serial.print("ms/cm");
        printStability(false);
        printStartCandidateStatus(false);
        return;
    }

    Serial.println();
    Serial.println("----- pH + EC sample -----");
    Serial.print("Sample: ");
    Serial.print(sampleNumber);
    Serial.print("  Uptime: ");
    Serial.print(millis() / 1000);
    Serial.println("s");

    Serial.print("Temperature: ");
    Serial.print(temperature, 1);
    Serial.println(" C");

    Serial.print("pH raw ADS: ");
    Serial.print(rawPh);
    Serial.print("  voltage: ");
    Serial.print(phVoltage, 2);
    Serial.println(" mV");
    Serial.print("pH: ");
    Serial.print(phValue, 2);
    Serial.print("  filtered: ");
    Serial.println(filteredPhValue, 2);

    Serial.print("EC raw ADS: ");
    Serial.print(rawEc);
    Serial.print("  voltage: ");
    Serial.print(ecVoltage, 2);
    Serial.println(" mV");
    Serial.print("EC: ");
    Serial.print(ecValue, 3);
    Serial.print(" ms/cm  filtered: ");
    Serial.print(filteredEcValue, 3);
    Serial.println(" ms/cm");

    printStability(true);
    printStartCandidateStatus(true);
    Serial.println("--------------------------");
}

void updateStartWindow(StartWindow& window, float value, bool stable, float driftDelta, const char* stableResetReason, const char* driftResetReason)
{
    if (!stable) {
        resetStartWindow(window, stableResetReason, 0.0);
        return;
    }

    if (!window.initialized) {
        window.referenceValue = value;
        window.startedAt = millis();
        window.initialized = true;
        window.resetReason = "baseline started";
        window.lastResetDrift = 0.0;
        window.driftOutOfRangeCount = 0;
        return;
    }

    float drift = value - window.referenceValue;
    if (abs(drift) > driftDelta) {
        window.driftOutOfRangeCount++;

        if (window.driftOutOfRangeCount >= UNSTABLE_RESET_COUNT) {
            window.referenceValue = value;
            window.startedAt = millis();
            window.resetReason = driftResetReason;
            window.lastResetDrift = drift;
            window.driftOutOfRangeCount = 0;
        }
    } else {
        window.driftOutOfRangeCount = 0;
    }
}

void resetStartWindow(StartWindow& window, const char* reason, float lastResetDrift)
{
    window.referenceValue = NAN;
    window.startedAt = 0;
    window.initialized = false;
    window.resetReason = reason;
    window.lastResetDrift = lastResetDrift;
    window.driftOutOfRangeCount = 0;
}

void printStartCandidateStatus(bool multiline)
{
    unsigned long phWindowSeconds = 0;
    unsigned long ecWindowSeconds = 0;
    float phDrift = 0.0;
    float ecDrift = 0.0;
    bool pHStable = isStable(phStability);
    bool ecStable = isStable(ecStability);
    bool phHasFullWindow = false;
    bool ecHasFullWindow = false;
    bool logStartValues = false;

    if (phStartWindow.initialized) {
        phWindowSeconds = (millis() - phStartWindow.startedAt) / 1000;
        phDrift = filteredPhValue - phStartWindow.referenceValue;
        phHasFullWindow = millis() - phStartWindow.startedAt >= START_READING_WINDOW_MS;
    }

    if (ecStartWindow.initialized) {
        ecWindowSeconds = (millis() - ecStartWindow.startedAt) / 1000;
        ecDrift = filteredEcValue - ecStartWindow.referenceValue;
        ecHasFullWindow = millis() - ecStartWindow.startedAt >= START_READING_WINDOW_MS;
    }

    logStartValues = pHStable
        && ecStable
        && phHasFullWindow
        && ecHasFullWindow
        && abs(phDrift) <= PH_START_DRIFT_DELTA
        && abs(ecDrift) <= EC_START_DRIFT_DELTA;

    if (!multiline) {
        Serial.print(" RecordStart:");
        Serial.print(logStartValues ? "YES" : "NO");
        Serial.print(" pH:");
        Serial.print(filteredPhValue, 2);
        Serial.print(" EC:");
        Serial.print(filteredEcValue, 3);
        Serial.println();
        return;
    }

    if (logStartValues) {
        Serial.println("Record start now: YES");
        Serial.print("  Start pH: ");
        Serial.println(filteredPhValue, 2);
        Serial.print("  Start EC: ");
        Serial.println(filteredEcValue, 3);
        Serial.print("  Temp: ");
        Serial.print(temperature, 1);
        Serial.println(" C");
        return;
    }

    Serial.println("Record start now: NO");

    Serial.print("  pH baseline: ");
    if (!pHStable) {
        Serial.println("waiting for pH stable");
    } else if (!phHasFullWindow) {
        Serial.print(phWindowSeconds);
        Serial.print("s / ");
        Serial.print(START_READING_WINDOW_MS / 1000);
        Serial.print("s, last reset: ");
        Serial.print(phStartWindow.resetReason);
        if (phStartWindow.lastResetDrift != 0.0) {
            Serial.print(" ");
            Serial.print(phStartWindow.lastResetDrift, 2);
        }
        if (phStartWindow.driftOutOfRangeCount > 0) {
            Serial.print(", drift count ");
            Serial.print(phStartWindow.driftOutOfRangeCount);
            Serial.print("/");
            Serial.print(UNSTABLE_RESET_COUNT);
        }
        Serial.println();
    } else if (abs(phDrift) > PH_START_DRIFT_DELTA) {
        Serial.print("drifting ");
        Serial.println(phDrift, 2);
    } else {
        Serial.print("ready, current pH ");
        Serial.println(filteredPhValue, 2);
    }

    Serial.print("  EC baseline: ");
    if (!ecStable) {
        Serial.println("waiting for EC stable");
    } else if (!ecHasFullWindow) {
        Serial.print(ecWindowSeconds);
        Serial.print("s / ");
        Serial.print(START_READING_WINDOW_MS / 1000);
        Serial.print("s, last reset: ");
        Serial.print(ecStartWindow.resetReason);
        if (ecStartWindow.lastResetDrift != 0.0) {
            Serial.print(" ");
            Serial.print(ecStartWindow.lastResetDrift, 3);
        }
        if (ecStartWindow.driftOutOfRangeCount > 0) {
            Serial.print(", drift count ");
            Serial.print(ecStartWindow.driftOutOfRangeCount);
            Serial.print("/");
            Serial.print(UNSTABLE_RESET_COUNT);
        }
        Serial.println();
    } else if (abs(ecDrift) > EC_START_DRIFT_DELTA) {
        Serial.print("drifting ");
        Serial.println(ecDrift, 3);
    } else {
        Serial.print("ready, current EC ");
        Serial.println(filteredEcValue, 3);
    }
}

void updateStability(StabilityState& state, float currentValue, float stableDelta)
{
    unsigned long now = millis();

    if (!state.initialized) {
        state.referenceValue = currentValue;
        state.startedAt = now;
        state.reported = false;
        state.initialized = true;
        state.unstableReadingCount = 0;
        return;
    }

    if (abs(currentValue - state.referenceValue) > stableDelta) {
        state.unstableReadingCount++;

        if (state.unstableReadingCount >= UNSTABLE_RESET_COUNT) {
            state.referenceValue = currentValue;
            state.startedAt = now;
            state.reported = false;
            state.unstableReadingCount = 0;
        }
    } else {
        state.unstableReadingCount = 0;
    }
}

void printStability(bool multiline)
{
    unsigned long phStableForMs = millis() - phStability.startedAt;
    unsigned long ecStableForMs = millis() - ecStability.startedAt;
    bool phStable = phStability.initialized && phStableForMs >= STABLE_REQUIRED_MS;
    bool ecStable = ecStability.initialized && ecStableForMs >= STABLE_REQUIRED_MS;

    if (multiline) {
        Serial.println("Stability:");
        Serial.print("  pH stable: ");
        Serial.print(phStable ? "yes" : "no");
        Serial.print("  for: ");
        Serial.print(phStableForMs / 1000);
        Serial.print("s  out-of-range count: ");
        Serial.println(phStability.unstableReadingCount);
        Serial.print("  EC stable: ");
        Serial.print(ecStable ? "yes" : "no");
        Serial.print("  for: ");
        Serial.print(ecStableForMs / 1000);
        Serial.print("s  out-of-range count: ");
        Serial.println(ecStability.unstableReadingCount);
    } else {
        Serial.print(" pHStable:");
        Serial.print(phStable ? "yes" : "no");
        Serial.print(" pHStableFor:");
        Serial.print(phStableForMs / 1000);
        Serial.print("s pHOutOfRangeCount:");
        Serial.print(phStability.unstableReadingCount);
        Serial.print(" ECStable:");
        Serial.print(ecStable ? "yes" : "no");
        Serial.print(" ECStableFor:");
        Serial.print(ecStableForMs / 1000);
        Serial.print("s ECOutOfRangeCount:");
        Serial.print(ecStability.unstableReadingCount);
    }

    if (phStable && ecStable && (!phStability.reported || !ecStability.reported)) {
        Serial.print(multiline ? "Both stable: yes" : " BothStable:yes");
        phStability.reported = true;
        ecStability.reported = true;
    }

    Serial.println();
}

float readTemperature()
{
    tempSensors.requestTemperatures();

    float temperatureC = tempSensors.getTempCByIndex(0);

    if (temperatureC == DEVICE_DISCONNECTED_C) {
        Serial.println("DS18B20 not detected. Falling back to 25.0C.");
        return 25.0;
    }

    return temperatureC;
}
