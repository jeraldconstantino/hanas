#include <Wire.h>
#include <Adafruit_ADS1X15.h>

Adafruit_ADS1115 ads;

// Read the level sensor through ADS1115 input A2.
const int LEVEL_CHANNEL = 2;  // A2
const float SENSE_RESISTOR = 150.0;  // ohms
const float RANGE_CM = 41.0;         // configured sensor depth span
// The existing linear tank-volume model maps 19.1625 cm to 70 L.
const float OPERATING_MAX_LEVEL_CM = 19.1625;
const float RESERVOIR_MAX_VOLUME_LITERS = 70.0;
const float ZERO_CURRENT_MA = 3.10;  // measured dry-sensor current
const float FULL_CURRENT_MA = 20.00; // nominal full-scale current

void setup() {
  Serial.begin(115200);
  delay(2000);

  Wire.begin(21, 22);  // SDA = GPIO21, SCL = GPIO22

  if (!ads.begin(0x48)) {
    Serial.println("ADS1115 not detected. Check wiring.");
    while (true) {
      delay(1000);
    }
  }

  ads.setGain(GAIN_ONE); // +/-4.096V range, 0.125mV per bit

  Serial.println("Water Level Sensor via ADS1115 Started");
  Serial.println("Operating maximum: 70 L at 19.1625 cm.");
}

void loop() {
  int16_t raw = ads.readADC_SingleEnded(LEVEL_CHANNEL);

  float voltage = ads.computeVolts(raw);

  float current_mA = (voltage / SENSE_RESISTOR) * 1000.0;

  float level_cm =
      ((current_mA - ZERO_CURRENT_MA) / (FULL_CURRENT_MA - ZERO_CURRENT_MA)) * RANGE_CM;

  if (level_cm < 0) level_cm = 0;
  if (level_cm > RANGE_CM) level_cm = RANGE_CM;

  // Keep values above 100% visible so overfill can be detected.
  float percent = (level_cm / OPERATING_MAX_LEVEL_CM) * 100.0;
  if (percent < 0) percent = 0;
  float reservoir_liters = (percent / 100.0) * RESERVOIR_MAX_VOLUME_LITERS;

  Serial.println("--------------------------------------------------");

  Serial.print("ADC Raw Value      : ");
  Serial.println(raw);

  Serial.print("Measured Voltage   : ");
  Serial.print(voltage, 3);
  Serial.println(" V");

  Serial.print("Loop Current       : ");
  Serial.print(current_mA, 3);
  Serial.println(" mA");

  Serial.print("Water Level        : ");
  Serial.print(level_cm, 2);
  Serial.println(" cm");

  Serial.print("Operating Fill     : ");
  Serial.print(percent, 2);
  Serial.println(" %");

  Serial.print("Reservoir Volume   : ");
  Serial.print(reservoir_liters, 2);
  Serial.println(" L");

  if (percent > 100) {
    Serial.println("Status             : Above Operating Maximum");
  } else if (percent < 10) {
    Serial.println("Status             : Very Low Level");
  } else if (percent < 50) {
    Serial.println("Status             : Moderate Level");
  } else if (percent < 80) {
    Serial.println("Status             : High Level");
  } else {
    Serial.println("Status             : Near Full");
  }

  Serial.println("--------------------------------------------------\n");

  delay(1000);
}
