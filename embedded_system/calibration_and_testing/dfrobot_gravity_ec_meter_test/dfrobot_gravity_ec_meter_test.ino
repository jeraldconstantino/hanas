#include <Wire.h>
#include <Adafruit_ADS1X15.h>
#include <DallasTemperature.h>
#include "DFRobot_EC.h"
#include <EEPROM.h>
#include <OneWire.h>

/*
  DFRobot Gravity Analog Electrical Conductivity Sensor calibration test
  Source guide: https://wiki.dfrobot.com/dfr0300/docs/20349
  Analog isolator guide: https://wiki.dfrobot.com/dfr0504/docs/19034

  Wiring:
    EC board +       -> Analog isolator SEN +
    EC board -       -> Analog isolator SEN -
    EC board A       -> Analog isolator SEN A
    Isolator MCU +   -> 5V
    Isolator MCU -   -> system GND
    Isolator MCU A   -> voltage divider -> ADS1115 A1
    ADS1115 SDA      -> ESP32 GPIO21
    ADS1115 SCL      -> ESP32 GPIO22
    ADS1115 VDD      -> ESP32 3.3V
    DS18B20 data     -> ESP32 GPIO27 with 4.7k pull-up to 3.3V
    DS18B20 VDD      -> ESP32 3.3V
    DS18B20 GND      -> system GND

  The isolator is needed when EC and pH probes are in the same container.
  Do not connect SEN-side GND to MCU/system GND; that defeats the isolation.

  The isolator MCU-side output can reach 5V. Since the ADS1115 is powered from
  3.3V, do not connect the isolator analog output directly to ADS1115 A1.

  Recommended divider:
    Isolator MCU A -> 10k resistor -> 4.7k resistor -> ADS1115 A1
    ADS1115 A1     -> 20k resistor -> GND

  This scales 5V down to about 2.88V. The code multiplies the ADS reading back up
  before giving it to the DFRobot EC library.

  Calibration:
    1. Open Serial Monitor at 115200 baud, line ending: Both NL & CR.
    2. Rinse the probe with distilled water and blot off water drops.
    3. Put the probe in 1413us/cm buffer. Stir gently and wait for a stable reading.
    4. Send: enterec
    5. Send: calec
    6. Send: exitec
       The calibration is saved only after exitec.
    7. Rinse the probe with distilled water and blot off water drops.
    8. Put the probe in 12.88ms/cm buffer. Stir gently and wait for a stable reading.
    9. Send: enterec
    10. Send: calec
    11. Send: exitec

  Do not touch the black platinum surface inside the probe. Rinse only with
  distilled water, and do not leave the probe immersed for long periods.
*/

const uint8_t ADS_ADDRESS = 0x48;
const uint8_t EC_CHANNEL = 1;               // ADS1115 A1
const uint8_t TEMP_SENSOR_PIN = 27;         // DS18B20 data pin
const uint8_t SAMPLE_COUNT = 20;
const unsigned long READ_INTERVAL_MS = 1000;
const unsigned long STABLE_REQUIRED_MS = 30000;
const float ADS_MV_PER_BIT = 0.125;         // GAIN_ONE: +/-4.096V range
const float EC_DIVIDER_MULTIPLIER = 1.735;  // (10k + 4.7k + 20k) / 20k
const float EC_STABLE_DELTA = 0.03;         // ms/cm
const uint8_t UNSTABLE_RESET_COUNT = 3;

Adafruit_ADS1115 ads;
DFRobot_EC ec;
OneWire oneWire(TEMP_SENSOR_PIN);
DallasTemperature tempSensors(&oneWire);

float voltage, ecValue, temperature = 25.0;
float stableReferenceEcValue = NAN;
unsigned long stableStartTime = 0;
bool stableReported = false;
uint8_t unstableReadingCount = 0;

float readECmV()
{
    float sum = 0.0;

    for (int i = 0; i < SAMPLE_COUNT; i++) {
        int16_t raw = ads.readADC_SingleEnded(EC_CHANNEL);

        sum += raw * ADS_MV_PER_BIT * EC_DIVIDER_MULTIPLIER;
        delay(10);
    }

    return sum / (float)SAMPLE_COUNT;
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

    ads.setGain(GAIN_ONE);   // +/-4.096V range, 0.125mV per bit
    tempSensors.begin();

    EEPROM.begin(32);
    ec.begin();

    Serial.println("DFRobot Gravity EC Sensor via ADS1115 A1");
    Serial.println("DS18B20 temperature sensor on GPIO27");
    Serial.println("Commands: enterec, calec, exitec");
    Serial.println("Raw ADS, Voltage(mV), Temperature(C), EC(ms/cm), Stability");
    Serial.print("Stable when EC changes by <= ");
    Serial.print(EC_STABLE_DELTA, 3);
    Serial.print(" ms/cm for ");
    Serial.print(STABLE_REQUIRED_MS / 1000);
    Serial.println(" seconds.");
    Serial.print("Stability resets after ");
    Serial.print(UNSTABLE_RESET_COUNT);
    Serial.println(" consecutive out-of-range readings.");
}

void loop()
{
    static unsigned long timepoint = millis();

    if (millis() - timepoint > READ_INTERVAL_MS) {
        timepoint = millis();

        temperature = readTemperature();
        int16_t raw = ads.readADC_SingleEnded(EC_CHANNEL);
        voltage = readECmV();
        ecValue = ec.readEC(voltage, temperature);
        updateStability(ecValue);

        Serial.print("Raw ADS:");
        Serial.print(raw);
        Serial.print("  Voltage:");
        Serial.print(voltage, 2);
        Serial.print("mV  Temperature:");
        Serial.print(temperature, 1);
        Serial.print("C  EC:");
        Serial.print(ecValue, 3);
        Serial.print(" ms/cm");
        printStability();
    }

    ec.calibration(voltage, temperature);
}

void updateStability(float currentEcValue)
{
    unsigned long now = millis();

    if (isnan(stableReferenceEcValue)) {
        stableReferenceEcValue = currentEcValue;
        stableStartTime = now;
        stableReported = false;
        unstableReadingCount = 0;
        return;
    }

    if (abs(currentEcValue - stableReferenceEcValue) > EC_STABLE_DELTA) {
        unstableReadingCount++;

        if (unstableReadingCount >= UNSTABLE_RESET_COUNT) {
            stableReferenceEcValue = currentEcValue;
            stableStartTime = now;
            stableReported = false;
            unstableReadingCount = 0;
        }
    } else {
        unstableReadingCount = 0;
    }
}

void printStability()
{
    unsigned long stableForMs = millis() - stableStartTime;
    bool isStable = stableForMs >= STABLE_REQUIRED_MS;

    Serial.print("  Stable:");
    Serial.print(isStable ? "yes" : "no");
    Serial.print("  StableFor:");
    Serial.print(stableForMs / 1000);
    Serial.print("s");
    Serial.print("  OutOfRangeCount:");
    Serial.print(unstableReadingCount);

    if (isStable && !stableReported) {
        Serial.print("  FirstStableAfter:");
        Serial.print(stableForMs / 1000);
        Serial.print("s");
        stableReported = true;
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
