#define PH_UP_PUMP_PIN      23
#define PH_DOWN_PUMP_PIN    19
#define EC_UP_A_PUMP_PIN    18
#define EC_UP_B_PUMP_PIN    17
#define EC_DOWN_PUMP_PIN    16

const bool PUMP_ON_STATE = HIGH;
const bool PUMP_OFF_STATE = LOW;

const unsigned long PRIME_TEST_MS = 60000;
const unsigned long IDLE_STATUS_INTERVAL_MS = 5000;

struct Pump {
  const char* name;
  int pin;
};

Pump pumps[] = {
  {"pH Up", PH_UP_PUMP_PIN},
  {"pH Down", PH_DOWN_PUMP_PIN},
  {"EC Up Solution A", EC_UP_A_PUMP_PIN},
  {"EC Up Solution B", EC_UP_B_PUMP_PIN},
  {"EC Down", EC_DOWN_PUMP_PIN},
};

const int PUMP_COUNT = sizeof(pumps) / sizeof(pumps[0]);

int activePumpIndex = -1;
unsigned long pumpStartTime = 0;
unsigned long pumpDurationMs = 0;
unsigned long lastStatusTime = 0;
unsigned long lastIdleStatusTime = 0;

void stopAllPumps();
void startPump(int pumpIndex, unsigned long durationMs);
void printPumpStatus();
void printIdleStatus();
void printStartupBanner();
void printMenu();

void setup() {
  Serial.begin(115200);
  Serial.setTimeout(50);
  delay(1000);

  for (int i = 0; i < PUMP_COUNT; i++) {
    pinMode(pumps[i].pin, OUTPUT);
  }

  stopAllPumps();

  printStartupBanner();
  printMenu();
}

void loop() {
  if (activePumpIndex >= 0) {
    printPumpStatus();
  } else {
    printIdleStatus();
  }

  if (activePumpIndex >= 0 && millis() - pumpStartTime >= pumpDurationMs) {
    Serial.print("Stopped ");
    Serial.println(pumps[activePumpIndex].name);
    Serial.println();
    stopAllPumps();
    printMenu();
  }

  if (!Serial.available()) {
    return;
  }

  String command = Serial.readStringUntil('\n');
  command.trim();

  if (command.length() == 0) {
    return;
  }

  if (command.length() == 1 && command[0] >= '1' && command[0] <= '5') {
    startPump(command[0] - '1', PRIME_TEST_MS);
    return;
  }

  if (command.equalsIgnoreCase("s") || command.equalsIgnoreCase("stop")) {
    stopAllPumps();
    Serial.println("All pumps stopped.");
    printMenu();
    return;
  }

  if (command == "?" || command.equalsIgnoreCase("h") || command.equalsIgnoreCase("help") || command.equalsIgnoreCase("menu")) {
    printMenu();
    return;
  }

  if (command.equalsIgnoreCase("status")) {
    if (activePumpIndex >= 0) {
      lastStatusTime = 0;
      printPumpStatus();
    } else {
      lastIdleStatusTime = 0;
      printIdleStatus();
    }
    return;
  }

  Serial.print("Unknown command: ");
  Serial.println(command);
  Serial.println("Send HELP, MENU, or ? for the command list.");
}

void stopAllPumps() {
  for (int i = 0; i < PUMP_COUNT; i++) {
    digitalWrite(pumps[i].pin, PUMP_OFF_STATE);
  }

  activePumpIndex = -1;
  pumpStartTime = 0;
  pumpDurationMs = 0;
  lastStatusTime = 0;
}

void startPump(int pumpIndex, unsigned long durationMs) {
  if (pumpIndex < 0 || pumpIndex >= PUMP_COUNT) {
    return;
  }

  stopAllPumps();

  Serial.print("Running ");
  Serial.print(pumps[pumpIndex].name);
  Serial.print(" on GPIO");
  Serial.print(pumps[pumpIndex].pin);
  Serial.print(" for ");
  Serial.print(durationMs / 1000);
  Serial.println(" seconds.");
  Serial.println("Watch the tube. Expected flow is left to right.");
  Serial.println("If the roller/head is visible, note whether it turns clockwise or counterclockwise.");
  Serial.println("Send S to stop immediately.");

  digitalWrite(pumps[pumpIndex].pin, PUMP_ON_STATE);
  activePumpIndex = pumpIndex;
  pumpStartTime = millis();
  pumpDurationMs = durationMs;
  lastStatusTime = 0;
  lastIdleStatusTime = 0;
  printPumpStatus();
}

void printPumpStatus() {
  unsigned long now = millis();

  if (lastStatusTime != 0 && now - lastStatusTime < 1000) {
    return;
  }

  lastStatusTime = now;

  unsigned long elapsedMs = now - pumpStartTime;
  unsigned long remainingMs = 0;

  if (elapsedMs < pumpDurationMs) {
    remainingMs = pumpDurationMs - elapsedMs;
  }

  Serial.print("Running ");
  Serial.print(pumps[activePumpIndex].name);
  Serial.print(" | elapsed: ");
  Serial.print(elapsedMs / 1000);
  Serial.print("s");
  Serial.print(" | remaining: ");
  Serial.print((remainingMs + 999) / 1000);
  Serial.println("s");
}

void printIdleStatus() {
  unsigned long now = millis();

  if (lastIdleStatusTime != 0 && now - lastIdleStatusTime < IDLE_STATUS_INTERVAL_MS) {
    return;
  }

  lastIdleStatusTime = now;

  Serial.println("Pump test is running and idle. Send 1-5 to run a pump, S to stop, or ? for help.");
}

void printStartupBanner() {
  Serial.println();
  Serial.println("==============================================");
  Serial.println("PERISTALTIC PUMP PRIME AND DIRECTION TEST");
  Serial.println("Serial baud: 115200");
  Serial.println("Status: sketch is running");
  Serial.println("Safety: all pump outputs set to OFF at startup");
  Serial.println();
  Serial.println("Pump pin map:");

  for (int i = 0; i < PUMP_COUNT; i++) {
    Serial.print("  ");
    Serial.print(i + 1);
    Serial.print(" - ");
    Serial.print(pumps[i].name);
    Serial.print(" on GPIO");
    Serial.println(pumps[i].pin);
  }

  Serial.println("==============================================");
  Serial.println();
}

void printMenu() {
  Serial.println("Serial commands:");
  Serial.print("  1 - run pH Up for ");
  Serial.print(PRIME_TEST_MS / 1000);
  Serial.println(" seconds");
  Serial.print("  2 - run pH Down for ");
  Serial.print(PRIME_TEST_MS / 1000);
  Serial.println(" seconds");
  Serial.print("  3 - run EC Up Solution A for ");
  Serial.print(PRIME_TEST_MS / 1000);
  Serial.println(" seconds");
  Serial.print("  4 - run EC Up Solution B for ");
  Serial.print(PRIME_TEST_MS / 1000);
  Serial.println(" seconds");
  Serial.print("  5 - run EC Down for ");
  Serial.print(PRIME_TEST_MS / 1000);
  Serial.println(" seconds");
  Serial.println("  S - stop all pumps immediately");
  Serial.println("  STOP - stop all pumps immediately");
  Serial.println("  STATUS - print current running/idle status");
  Serial.println("  H - help / show this menu");
  Serial.println("  HELP - help / show this menu");
  Serial.println("  MENU - help / show this menu");
  Serial.println("  ? - help / show this menu");
  Serial.println();
  Serial.println("Tips:");
  Serial.println("  Set Serial Monitor baud to 115200.");
  Serial.println("  Newline setting can be anything; blank newlines are ignored.");
  Serial.println();
}
