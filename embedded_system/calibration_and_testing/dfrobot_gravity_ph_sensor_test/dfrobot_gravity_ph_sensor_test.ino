#include <Wire.h>
#include <Adafruit_ADS1X15.h>
#include <DallasTemperature.h>
#include "DFRobot_PH.h"
#include <EEPROM.h>
#include <OneWire.h>

/*
  DFRobot Gravity Analog pH Sensor V2 calibration test
  Source guide: https://wiki.dfrobot.com/sen0161-v2/docs/20941
  Analog isolator guide: https://wiki.dfrobot.com/dfr0504/docs/19034

  Wiring:
    pH board +       -> Analog isolator SEN +
    pH board -       -> Analog isolator SEN -
    pH board A       -> Analog isolator SEN A
    Isolator MCU +   -> 5V
    Isolator MCU -   -> system GND
    Isolator MCU A   -> voltage divider -> ADS1115 A0

    ADS1115 SDA      -> ESP32 GPIO21
    ADS1115 SCL      -> ESP32 GPIO22
    ADS1115 VDD      -> ESP32 3.3V
    DS18B20 data     -> ESP32 GPIO27 with 4.7k pull-up to 3.3V
    DS18B20 VDD      -> ESP32 3.3V
    DS18B20 GND      -> system GND

  Do not connect SEN-side GND to MCU/system GND; that defeats the isolation.

  The isolator MCU-side output can reach 5V. Since the ADS1115 is powered from
  3.3V, do not connect the isolator analog output directly to ADS1115 A0.

  Recommended divider:
    Isolator MCU A -> 10k resistor -> 4.7k resistor -> ADS1115 A0
    ADS1115 A0     -> 20k resistor -> GND

  This scales 5V down to about 2.88V. The code multiplies the ADS reading back up
  before giving it to the DFRobot pH library.

  Calibration:
    1. Open Serial Monitor at 115200 baud, line ending: Both NL & CR.
    2. Rinse the probe with distilled water and blot off water drops.
    3. Put the probe in pH 7.0 buffer. Stir gently and wait for a stable reading.
    4. Send: enterph
    5. Send: calph
    6. Send: exitph
       The calibration is saved only after exitph.
    7. Rinse the probe with distilled water and blot off water drops.
    8. Put the probe in pH 4.0 buffer. Stir gently and wait for a stable reading.
    9. Send: enterph
    10. Send: calph
    11. Send: exitph

  Keep the BNC connector and signal board dry during calibration.
*/

const uint8_t ADS_ADDRESS = 0x48;
const uint8_t PH_CHANNEL = 0;           // ADS1115 A0
const uint8_t TEMP_SENSOR_PIN = 27;     // DS18B20 data pin
const uint8_t SAMPLE_COUNT = 20;
const unsigned long READ_INTERVAL_MS = 1000;
const unsigned long STABLE_REQUIRED_MS = 30000;
const float ADS_MV_PER_BIT = 0.125;     // GAIN_ONE: +/-4.096V range
const float PH_DIVIDER_MULTIPLIER = 1.735;  // (10k + 4.7k + 20k) / 20k
const float PH_STABLE_DELTA = 0.03;
const uint8_t UNSTABLE_RESET_COUNT = 3;

Adafruit_ADS1115 ads;
DFRobot_PH ph;
OneWire oneWire(TEMP_SENSOR_PIN);
DallasTemperature tempSensors(&oneWire);

float voltage, phValue, temperature = 25.0;
float stableReferencePhValue = NAN;
unsigned long stableStartTime = 0;
bool stableReported = false;
uint8_t unstableReadingCount = 0;

float readPHmV()
{
    float sum = 0.0;

    for (int i = 0; i < SAMPLE_COUNT; i++) {
        int16_t raw = ads.readADC_SingleEnded(PH_CHANNEL);
        sum += raw * ADS_MV_PER_BIT * PH_DIVIDER_MULTIPLIER;
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
    ph.begin();

    Serial.println("DFRobot Gravity pH Sensor V2 via ADS1115 A0");
    Serial.println("DS18B20 temperature sensor on GPIO27");
    Serial.println("Commands: enterph, calph, exitph");
    Serial.println("Calibration messages may appear between the 1-second data rows.");
    Serial.println("Raw ADS, Voltage(mV), Temperature(C), pH, Stability");
    Serial.print("Stable when pH changes by <= ");
    Serial.print(PH_STABLE_DELTA, 2);
    Serial.print(" for ");
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
        int16_t raw = ads.readADC_SingleEnded(PH_CHANNEL);
        voltage = readPHmV();
        phValue = ph.readPH(voltage, temperature);
        updateStability(phValue);

        Serial.print("Raw ADS:");
        Serial.print(raw);
        Serial.print("  Voltage:");
        Serial.print(voltage, 2);
        Serial.print("mV  Temperature:");
        Serial.print(temperature, 1);
        Serial.print("C  pH:");
        Serial.print(phValue, 2);
        printStability();
    }

    ph.calibration(voltage, temperature);
}

void updateStability(float currentPhValue)
{
    unsigned long now = millis();

    if (isnan(stableReferencePhValue)) {
        stableReferencePhValue = currentPhValue;
        stableStartTime = now;
        stableReported = false;
        unstableReadingCount = 0;
        return;
    }

    if (abs(currentPhValue - stableReferencePhValue) > PH_STABLE_DELTA) {
        unstableReadingCount++;

        if (unstableReadingCount >= UNSTABLE_RESET_COUNT) {
            stableReferencePhValue = currentPhValue;
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
