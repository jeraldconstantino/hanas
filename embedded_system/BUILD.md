# Verified firmware build

Tested board: `esp32:esp32:esp32`, ESP32 core 3.3.8.
Install Arduino CLI and that board core, then install these sensor libraries:

- Adafruit ADS1X15 2.6.2
- Adafruit BusIO 1.17.4
- DFRobot_PH 1.0.0
- DallasTemperature 4.0.6
- OneWire 2.3.8

Copy `hanas_sensor_controller/secrets.example.h` to `secrets.h` and set your own
credentials. Compile from the package root:

```sh
arduino-cli compile --fqbn esp32:esp32:esp32 embedded_system/hanas_sensor_controller
```
