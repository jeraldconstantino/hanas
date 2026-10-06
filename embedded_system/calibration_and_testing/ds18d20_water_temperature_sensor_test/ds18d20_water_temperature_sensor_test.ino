#include <OneWire.h>
#include <DallasTemperature.h>

#define ONE_WIRE_BUS 27

OneWire oneWire(ONE_WIRE_BUS);
DallasTemperature sensors(&oneWire);

void setup() {
  Serial.begin(115200);
  Serial.println("DS18B20 Temperature Test");

  sensors.begin();
}

void loop() {
  sensors.requestTemperatures(); 

  float temperatureC = sensors.getTempCByIndex(0);

  Serial.print("Temperature: ");

  if (temperatureC == DEVICE_DISCONNECTED_C) {
    Serial.println("Sensor not detected!");
  } else {
    Serial.print(temperatureC);
    Serial.println(" °C");
  }

  delay(1000);
}