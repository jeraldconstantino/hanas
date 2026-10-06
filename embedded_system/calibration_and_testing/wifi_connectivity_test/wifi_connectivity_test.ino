#include "secrets.h"
#include <WiFi.h>
#include <HTTPClient.h>

// Check connectivity without submitting sensor readings or pump commands.
const unsigned long CHECK_INTERVAL_MS = 5000;
const unsigned long CONNECT_TIMEOUT_MS = 15000;
unsigned long lastCheckTime = 0;

void setup() {
  Serial.begin(115200);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.println("Wi-Fi and backend readiness test. No sensor data is submitted.");
}

void loop() {
  unsigned long now = millis();
  if (now - lastCheckTime < CHECK_INTERVAL_MS) return;
  lastCheckTime = now;

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Connecting to Wi-Fi...");
    WiFi.reconnect();
    unsigned long startedAt = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - startedAt < CONNECT_TIMEOUT_MS) {
      delay(100);
    }
    if (WiFi.status() != WL_CONNECTED) {
      Serial.println("Connection timed out. Check credentials and signal strength.");
      return;
    }
  }

  Serial.print("ESP32 IP: ");
  Serial.println(WiFi.localIP());
  HTTPClient http;
  http.setConnectTimeout(5000);
  http.setTimeout(5000);
  if (!http.begin(HEALTH_URL)) {
    Serial.println("Unable to open the backend readiness URL.");
    return;
  }
  int status = http.GET();
  Serial.print("Readiness response: ");
  Serial.println(status);
  if (status > 0) Serial.println(http.getString());
  else Serial.println("Request failed. Check the backend URL and network.");
  http.end();
}
