/*
  ============================================================================
  WATER LEAK DETECTION SYSTEM - 3 MODULES + GSM SMS ALERT + SERVER UPLOAD
  Water Pressure Sensor + Water Flow Sensor x3 on a single ESP32, with a
  SIM800L-type GSM module for SMS notification to the owner on leak, and a
  WiFi uplink that posts every sample to the Smart Hose Leak Django server.
  ----------------------------------------------------------------------------
  Undergraduate Thesis Reference Implementation

  WHAT CHANGED FROM THE PREVIOUS (3-MODULE + GSM) VERSION:
    - Added WiFi. The board joins WIFI_SSID and POSTs a JSON batch of all
      three modules' readings to the Django server every UPLOAD_INTERVAL_MS.
      That is what makes the readings appear on the Facilitator Dashboard and
      the Owner Portal.
    - The POST carries DEVICE_API_KEY in the X-Device-Key header. The server
      matches it against the Device row and rejects anything else with a 401,
      so a stranger on the same network cannot inject fake readings.
    - Detection, filtering, calibration, hysteresis and the SMS logic are
      UNCHANGED. The server is a display and record layer; the decision that
      a leak exists is still made here on the device, exactly as before. If
      the WiFi or the server goes down, the SIM800L still sends its SMS.
    - Upload is decoupled from sampling: sampling stays at 1 s so the
      hysteresis counter still needs REQUIRED_CONSECUTIVE consecutive
      seconds, while uploads are throttled to UPLOAD_INTERVAL_MS.
    - A status CHANGE forces an immediate upload rather than waiting for the
      next interval, so an alert reaches the dashboard without delay.

  DETECTION STRATEGY (unchanged, applied per module):
    1. Calibrated Pressure-Flow curve: Q_expected = K * sqrt(P_measured).
    2. Flow abnormally HIGHER than expected for the pressure -> leak.
    3. Zero-flow pressure decay ("minimum night flow" test) -> leak.
    4. Hysteresis (N consecutive samples) before flagging/clearing.

  HARDWARE WIRING - 3 SENSOR SETS:
    Module 1: Flow -> GPIO27 (interrupt)   | Pressure -> GPIO34 (ADC1_CH6)
    Module 2: Flow -> GPIO26 (interrupt)   | Pressure -> GPIO35 (ADC1_CH7)
    Module 3: Flow -> GPIO25 (interrupt)   | Pressure -> GPIO32 (ADC1_CH4)

    NOTES:
    - Flow pins must be interrupt-capable AND support INPUT_PULLUP, so they
      must NOT be from the 34-39 input-only range. 25/26/27 are fine.
    - Pressure pins MUST be ADC1 channels (32-39). ADC2 pins cannot be read
      reliably while WiFi is active on the ESP32 - and WiFi is now always
      active, so this constraint is now mandatory rather than merely advised.
    - Each pressure sensor (0.5-4.5V output) still needs its OWN voltage
      divider in front of its ADC pin. DIVIDER_RATIO below assumes identical
      resistor values on all 3 dividers.

  HARDWARE WIRING - GSM MODULE (SIM800L or similar):
    ESP32 GPIO17 (TX2)  -> GSM module RX
    ESP32 GPIO16 (RX2)  -> GSM module TX
    ESP32 GND           -> GSM module GND (COMMON GROUND, required)

    IMPORTANT: Do NOT power a SIM800L from the ESP32's 3.3V or 5V pin. It
    draws current spikes up to ~2A during transmission bursts, which will
    brown out the ESP32 - and a brownout now also drops the WiFi link. Use a
    dedicated 3.7-4.2V regulated supply capable of that peak current, with
    grounds tied together.

  BEFORE DEPLOYING - UPDATE THESE:
    OWNER_PHONE_NUMBER  - facilitator number in international format
    WIFI_SSID / WIFI_PASSWORD
    SERVER_URL          - the LAN IP of the machine running Django, NOT
                          127.0.0.1 (that would mean the ESP32 itself)
    DEVICE_API_KEY      - printed by `python manage.py setup_demo`, and
                          visible in the Django admin under Devices
  ============================================================================
*/

#include <math.h>
#include <HardwareSerial.h>
#include <WiFi.h>
#include <HTTPClient.h>

// ============================================================================
// USER CONFIGURATION
// ============================================================================

#define NUM_MODULES                3

// ---- Pin assignments per module ----
const uint8_t FLOW_PINS[NUM_MODULES]     = {27, 26, 25};
const uint8_t PRESSURE_PINS[NUM_MODULES] = {34, 35, 32};

// ---- Flow sensor calibration (shared across identical sensor models) ----
#define PULSES_PER_LITER          450.0   // YF-S201 typical - confirm per datasheet

// ---- Pressure sensor calibration (shared across identical sensor models) ----
#define SENSOR_VOUT_MIN            0.5
#define SENSOR_VOUT_MAX            4.5
#define PRESSURE_MAX_BAR           12.0
#define DIVIDER_RATIO              (20000.0 / (10000.0 + 20000.0)) // R2/(R1+R2)
#define ADC_MAX_COUNT              4095.0
#define ADC_VREF                   3.3

// ---- Timing ----
#define SAMPLE_INTERVAL_MS         1000
#define FILTER_HISTORY_SIZE        5
#define EMA_ALPHA                  0.2

// ---- Detection thresholds (tune per rig / per module if needed) ----
#define P_LOW_THRESHOLD_BAR        1.5
#define RESIDUAL_LEAK_THRESHOLD    0.18
#define FLOW_ZERO_THRESHOLD_LPM    0.3
#define STATIC_TEST_WINDOW_S       30
#define STATIC_DECAY_THRESHOLD_BAR 0.05
#define REQUIRED_CONSECUTIVE       15

#define MAX_CALIBRATION_POINTS     10

// ---- GSM module (SIM800L / SIM900 / A6, AT-command based) ----
#define GSM_RX_PIN                 16      // ESP32 RX2 <- GSM module TX
#define GSM_TX_PIN                 17      // ESP32 TX2 -> GSM module RX
#define GSM_BAUD                   9600
#define OWNER_PHONE_NUMBER         "+639171234567"  // <-- UPDATE THIS
HardwareSerial GSM_SERIAL(2);               // ESP32 UART2

// ---- WiFi + server uplink ----
#define WIFI_SSID                  "YOUR_WIFI_NAME"        // <-- UPDATE THIS
#define WIFI_PASSWORD              "YOUR_WIFI_PASSWORD"    // <-- UPDATE THIS

// Use the LAN IP of the computer running `manage.py runserver 0.0.0.0:8000`.
// Find it with `ipconfig` on Windows and look for the IPv4 Address.
#define SERVER_URL                 "http://192.168.1.10:8000/api/ingest/"
#define DEVICE_ID                  "esp32-01"
#define DEVICE_API_KEY             "PASTE_THE_KEY_FROM_setup_demo_HERE"

#define UPLOAD_INTERVAL_MS         2000   // how often to POST readings
#define HTTP_TIMEOUT_MS            4000   // give up rather than stall sampling
#define WIFI_RETRY_INTERVAL_MS     15000  // how often to retry a dropped link

// ============================================================================
// GLOBAL STATE
// ============================================================================

enum SystemStatus {
  STATUS_NORMAL,
  STATUS_LOW_SUPPLY,
  STATUS_POSSIBLE_LEAK
};

// All per-module state lives here. One struct instance per physical
// pressure+flow sensor pair, so the sampling/filter/classification code
// below is written once and looped over NUM_MODULES instead of copy-pasted.
struct ModuleState {
  uint8_t id;              // 1, 2, 3 - for display/SMS/JSON messages
  uint8_t flowPin;
  uint8_t pressurePin;

  volatile uint32_t flowPulseCount; // incremented by this module's ISR

  float pressureHistory[FILTER_HISTORY_SIZE];
  float flowHistory[FILTER_HISTORY_SIZE];
  uint8_t historyIndex;

  float pressureEMA;
  float flowEMA;

  // Calibration: Q = K * sqrt(P)
  float K_CONDUCTANCE;
  float calibP[MAX_CALIBRATION_POINTS];
  float calibQ[MAX_CALIBRATION_POINTS];
  uint8_t calibCount;
  bool calibrationMode;

  // Simulation / injection mode for testing logic without real hardware
  bool simulationMode;
  float simPressure;
  float simFlow;

  // Static (zero-flow) pressure decay tracking
  unsigned long zeroFlowStartMillis;
  bool inZeroFlowWindow;
  float pressureAtZeroFlowStart;

  // Status + hysteresis
  SystemStatus currentStatus;
  SystemStatus candidateStatus;
  uint16_t consecutiveCount;

  // Prevents sending one SMS per sample while an alarm condition persists
  bool leakNotified;
};

ModuleState modules[NUM_MODULES];

// Which module is currently being calibrated via Serial ('C1'/'C2'/'C3'
// then 'A'/'F'). -1 = none active.
int8_t activeCalibModule = -1;

unsigned long lastSampleMillis = 0;
unsigned long lastUploadMillis = 0;
unsigned long lastWifiAttemptMillis = 0;
bool uploadDueNow = false;   // set on any status change to push immediately

// ============================================================================
// INTERRUPT SERVICE ROUTINES - one per module (ISRs can't take parameters)
// ============================================================================
void IRAM_ATTR flowPulseISR0() { modules[0].flowPulseCount++; }
void IRAM_ATTR flowPulseISR1() { modules[1].flowPulseCount++; }
void IRAM_ATTR flowPulseISR2() { modules[2].flowPulseCount++; }

// ============================================================================
// FILTERING FUNCTIONS (generic - operate on whichever module's history
// array is passed in)
// ============================================================================

float medianFilter(float *history, uint8_t size) {
  float sorted[FILTER_HISTORY_SIZE];
  memcpy(sorted, history, sizeof(float) * size);
  for (uint8_t i = 1; i < size; i++) {
    float key = sorted[i];
    int j = i - 1;
    while (j >= 0 && sorted[j] > key) {
      sorted[j + 1] = sorted[j];
      j--;
    }
    sorted[j + 1] = key;
  }
  return sorted[size / 2];
}

float emaFilter(float previousEMA, float newSample, float alpha) {
  if (previousEMA < 0) return newSample;
  return alpha * newSample + (1.0 - alpha) * previousEMA;
}

// ============================================================================
// SENSOR READING FUNCTIONS
// ============================================================================

float readPressureRaw(uint8_t pin) {
  int adcCount = analogRead(pin);
  float vAtPin = (adcCount / ADC_MAX_COUNT) * ADC_VREF;
  float vSensorOut = vAtPin / DIVIDER_RATIO;

  float pressureBar = (vSensorOut - SENSOR_VOUT_MIN) *
                       (PRESSURE_MAX_BAR / (SENSOR_VOUT_MAX - SENSOR_VOUT_MIN));

  if (pressureBar < 0) pressureBar = 0;
  return pressureBar;
}

float computeFlowRateLPM(uint32_t pulses, unsigned long elapsedMillis) {
  float liters = pulses / PULSES_PER_LITER;
  float minutes = elapsedMillis / 60000.0;
  if (minutes <= 0) return 0;
  return liters / minutes;
}

// ============================================================================
// CALIBRATION ROUTINE (per module)
// ============================================================================
// Usage: send "C1", "C2" or "C3" over Serial to start calibrating that
// module. Vary flow (open/close taps) and send 'A' to capture each stable
// (P,Q) point, then 'F' to finish and compute that module's K_CONDUCTANCE.

void handleCalibrationInput(ModuleState &m, char cmd) {
  if (cmd == 'A') {
    if (m.calibCount < MAX_CALIBRATION_POINTS) {
      m.calibP[m.calibCount] = m.pressureEMA;
      m.calibQ[m.calibCount] = m.flowEMA;
      m.calibCount++;
      Serial.printf("[CALIBRATION M%d] Captured point %d: P=%.2f bar, Q=%.2f L/min\n",
                    m.id, m.calibCount, m.pressureEMA, m.flowEMA);
    } else {
      Serial.printf("[CALIBRATION M%d] Max calibration points reached.\n", m.id);
    }
  } else if (cmd == 'F') {
    float numerator = 0, denominator = 0;
    for (uint8_t i = 0; i < m.calibCount; i++) {
      if (m.calibP[i] > 0.01) {
        numerator += m.calibQ[i] * sqrt(m.calibP[i]);
        denominator += m.calibP[i];
      }
    }
    if (denominator > 0) {
      m.K_CONDUCTANCE = numerator / denominator;
      Serial.printf("[CALIBRATION M%d] Complete. K_CONDUCTANCE = %.4f\n", m.id, m.K_CONDUCTANCE);
    } else {
      Serial.printf("[CALIBRATION M%d] Not enough valid points to compute K.\n", m.id);
    }
    m.calibrationMode = false;
    activeCalibModule = -1;
  }
}

// ============================================================================
// LEAK CLASSIFICATION LOGIC
// ============================================================================

SystemStatus classify(ModuleState &m, float pressure, float flow) {
  bool haveCalibration = (m.K_CONDUCTANCE > 0);

  float expectedFlow = 0;
  float residualRatio = 0;
  if (haveCalibration) {
    expectedFlow = m.K_CONDUCTANCE * sqrt(max(pressure, 0.0f));
    if (expectedFlow > 0.05) {
      residualRatio = (flow - expectedFlow) / expectedFlow;
    }
  }

  // --- Static (zero-flow) pressure decay test - doesn't need calibration ---
  if (flow < FLOW_ZERO_THRESHOLD_LPM) {
    if (!m.inZeroFlowWindow) {
      m.inZeroFlowWindow = true;
      m.zeroFlowStartMillis = millis();
      m.pressureAtZeroFlowStart = pressure;
    } else {
      unsigned long elapsedS = (millis() - m.zeroFlowStartMillis) / 1000;
      if (elapsedS >= STATIC_TEST_WINDOW_S) {
        float drop = m.pressureAtZeroFlowStart - pressure;
        if (drop > STATIC_DECAY_THRESHOLD_BAR) {
          return STATUS_POSSIBLE_LEAK;
        }
        m.zeroFlowStartMillis = millis();
        m.pressureAtZeroFlowStart = pressure;
      }
    }
  } else {
    m.inZeroFlowWindow = false;
  }

  // --- Pressure/flow curve residual test (requires calibration) ---
  if (haveCalibration) {
    if (pressure < P_LOW_THRESHOLD_BAR) {
      if (fabs(residualRatio) < RESIDUAL_LEAK_THRESHOLD) {
        return STATUS_LOW_SUPPLY;
      } else if (residualRatio > RESIDUAL_LEAK_THRESHOLD) {
        return STATUS_POSSIBLE_LEAK;
      }
    } else {
      if (residualRatio > RESIDUAL_LEAK_THRESHOLD) {
        return STATUS_POSSIBLE_LEAK;
      }
    }
  } else {
    if (pressure < P_LOW_THRESHOLD_BAR) {
      return STATUS_LOW_SUPPLY;
    }
  }

  return STATUS_NORMAL;
}

void updateStatusWithHysteresis(ModuleState &m, SystemStatus newCandidate) {
  if (newCandidate == m.candidateStatus) {
    m.consecutiveCount++;
  } else {
    m.candidateStatus = newCandidate;
    m.consecutiveCount = 1;
  }

  if (m.consecutiveCount >= REQUIRED_CONSECUTIVE && m.candidateStatus != m.currentStatus) {
    m.currentStatus = m.candidateStatus;
    // A confirmed transition is worth an immediate upload; do not sit on an
    // alert until the next scheduled POST.
    uploadDueNow = true;
  }
}

const char* statusToString(SystemStatus s) {
  switch (s) {
    case STATUS_NORMAL:        return "NORMAL";
    case STATUS_LOW_SUPPLY:    return "LOW WATER SUPPLY";
    case STATUS_POSSIBLE_LEAK: return "POSSIBLE LEAK";
    default:                   return "UNKNOWN";
  }
}

// The server expects these exact tokens (see DEVICE_STATUSES in core/api.py).
const char* statusToApiString(SystemStatus s) {
  switch (s) {
    case STATUS_NORMAL:        return "NORMAL";
    case STATUS_LOW_SUPPLY:    return "LOW_SUPPLY";
    case STATUS_POSSIBLE_LEAK: return "POSSIBLE_LEAK";
    default:                   return "NORMAL";
  }
}

// ============================================================================
// WIFI + SERVER UPLOAD
// ============================================================================

// Non-blocking-ish reconnect. Called from loop(); only actually retries every
// WIFI_RETRY_INTERVAL_MS so a dead access point cannot stall the sampler.
void ensureWifi() {
  if (WiFi.status() == WL_CONNECTED) return;

  unsigned long now = millis();
  if (now - lastWifiAttemptMillis < WIFI_RETRY_INTERVAL_MS) return;
  lastWifiAttemptMillis = now;

  Serial.println("[WIFI] Not connected - attempting to reconnect...");
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

// Builds the JSON batch by hand. A JSON library would work too, but hand
// assembly keeps the sketch dependency-free and the payload is tiny and
// fixed in shape:
//   {"device_id":"esp32-01","readings":[{...},{...},{...}]}
String buildPayload() {
  String json = "{\"device_id\":\"";
  json += DEVICE_ID;
  json += "\",\"readings\":[";

  for (uint8_t i = 0; i < NUM_MODULES; i++) {
    ModuleState &m = modules[i];
    if (i) json += ",";
    json += "{\"module\":";
    json += m.id;
    json += ",\"pressure_bar\":";
    json += String(m.pressureEMA, 3);
    json += ",\"flow_lpm\":";
    json += String(m.flowEMA, 3);
    json += ",\"k\":";
    json += String(m.K_CONDUCTANCE, 4);
    json += ",\"status\":\"";
    json += statusToApiString(m.currentStatus);
    json += "\",\"calibrating\":";
    json += m.calibrationMode ? "true" : "false";
    json += ",\"simulated\":";
    json += m.simulationMode ? "true" : "false";
    json += "}";
  }

  json += "]}";
  return json;
}

// Posts the current readings. Failure here is logged and otherwise ignored:
// detection and the SMS alert do not depend on the server being reachable.
void uploadReadings() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[UPLOAD] Skipped - WiFi not connected.");
    return;
  }

  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT_MS);
  if (!http.begin(SERVER_URL)) {
    Serial.println("[UPLOAD] Failed to parse SERVER_URL.");
    return;
  }

  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Key", DEVICE_API_KEY);

  String payload = buildPayload();
  int code = http.POST(payload);

  if (code == 200) {
    Serial.println("[UPLOAD] OK");
  } else if (code == 401) {
    Serial.println("[UPLOAD] REJECTED 401 - DEVICE_API_KEY does not match any device on the server.");
  } else if (code > 0) {
    Serial.printf("[UPLOAD] Server returned %d: %s\n", code, http.getString().c_str());
  } else {
    Serial.printf("[UPLOAD] Failed: %s\n", http.errorToString(code).c_str());
  }

  http.end();
}

// ============================================================================
// GSM (SIM800L-style AT command set) - sends SMS to the owner
// ============================================================================

// Drains and returns whatever the GSM module sends back within timeoutMs.
// This is a simple blocking read, adequate for a thesis-scale project;
// a production system should parse specific response codes (OK/ERROR)
// and retry, and should check network registration (AT+CREG?) and signal
// quality (AT+CSQ) before attempting to send.
String gsmReadResponse(unsigned long timeoutMs) {
  String response = "";
  unsigned long start = millis();
  while (millis() - start < timeoutMs) {
    while (GSM_SERIAL.available()) {
      response += (char)GSM_SERIAL.read();
    }
  }
  return response;
}

bool gsmModulePresent() {
  while (GSM_SERIAL.available()) GSM_SERIAL.read(); // flush
  GSM_SERIAL.println("AT");
  String resp = gsmReadResponse(1000);
  return resp.indexOf("OK") >= 0;
}

bool sendSMS(const String &number, const String &message) {
  while (GSM_SERIAL.available()) GSM_SERIAL.read(); // flush stale data

  GSM_SERIAL.println("AT+CMGF=1");       // text mode (not PDU mode)
  gsmReadResponse(500);

  GSM_SERIAL.print("AT+CMGS=\"");
  GSM_SERIAL.print(number);
  GSM_SERIAL.println("\"");
  gsmReadResponse(500);                  // wait for the '>' prompt

  GSM_SERIAL.print(message);
  GSM_SERIAL.write(26);                  // Ctrl+Z terminates and sends the SMS body
  String resp = gsmReadResponse(5000);   // sending can take a few seconds

  bool ok = (resp.indexOf("OK") >= 0) || (resp.indexOf("+CMGS") >= 0);
  Serial.printf("[GSM] Send to %s: %s -- raw response: %s\n",
                number.c_str(), ok ? "OK" : "FAILED", resp.c_str());
  return ok;
}

// Checks each module's status transition and fires an SMS on
// NORMAL/LOW_SUPPLY -> POSSIBLE_LEAK, and again on POSSIBLE_LEAK -> NORMAL
// (leak cleared). leakNotified prevents re-sending every sample while the
// alarm condition is still active.
void checkAndNotifyLeak(ModuleState &m) {
  if (m.currentStatus == STATUS_POSSIBLE_LEAK && !m.leakNotified) {
    String msg = "ALERT: Possible LEAK detected on Module " + String(m.id) +
                 ". Pressure=" + String(m.pressureEMA, 2) + " bar, Flow=" +
                 String(m.flowEMA, 2) + " L/min. Please check the pipeline.";
    Serial.printf("[LEAK] Module %d - sending SMS alert.\n", m.id);
    sendSMS(OWNER_PHONE_NUMBER, msg);
    m.leakNotified = true;
  } else if (m.currentStatus != STATUS_POSSIBLE_LEAK && m.leakNotified) {
    String msg = "UPDATE: Module " + String(m.id) +
                 " leak condition has CLEARED. Status is now " +
                 String(statusToString(m.currentStatus)) + ".";
    Serial.printf("[LEAK] Module %d - condition cleared, sending SMS.\n", m.id);
    sendSMS(OWNER_PHONE_NUMBER, msg);
    m.leakNotified = false;
  }
}

// ============================================================================
// MODULE INITIALIZATION
// ============================================================================

void initModule(uint8_t index, uint8_t flowPin, uint8_t pressurePin, void (*isr)()) {
  ModuleState &m = modules[index];
  m.id = index + 1;
  m.flowPin = flowPin;
  m.pressurePin = pressurePin;
  m.flowPulseCount = 0;
  m.historyIndex = 0;
  m.pressureEMA = -1;
  m.flowEMA = -1;
  m.K_CONDUCTANCE = 0;
  m.calibCount = 0;
  m.calibrationMode = false;
  m.simulationMode = false;
  m.simPressure = 0;
  m.simFlow = 0;
  m.zeroFlowStartMillis = 0;
  m.inZeroFlowWindow = false;
  m.pressureAtZeroFlowStart = 0;
  m.currentStatus = STATUS_NORMAL;
  m.candidateStatus = STATUS_NORMAL;
  m.consecutiveCount = 0;
  m.leakNotified = false;

  pinMode(flowPin, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(flowPin), isr, FALLING);
}

// ============================================================================
// SETUP
// ============================================================================
void setup() {
  Serial.begin(115200);
  delay(500);

  analogReadResolution(12);
  analogSetAttenuation(ADC_11db);

  initModule(0, FLOW_PINS[0], PRESSURE_PINS[0], flowPulseISR0);
  initModule(1, FLOW_PINS[1], PRESSURE_PINS[1], flowPulseISR1);
  initModule(2, FLOW_PINS[2], PRESSURE_PINS[2], flowPulseISR2);

  GSM_SERIAL.begin(GSM_BAUD, SERIAL_8N1, GSM_RX_PIN, GSM_TX_PIN);

  // Bring up WiFi. Station mode only - the board must not act as an AP.
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("[WIFI] Connecting to ");
  Serial.print(WIFI_SSID);
  for (uint8_t i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) {
    delay(250);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("[WIFI] Connected. Device IP: ");
    Serial.println(WiFi.localIP());
    Serial.print("[WIFI] Posting readings to: ");
    Serial.println(SERVER_URL);
  } else {
    Serial.println("[WIFI] WARNING: could not connect. Detection and SMS still work;");
    Serial.println("       readings will not reach the dashboard until WiFi recovers.");
  }

  delay(3000); // SIM800L needs a few seconds to boot/register on the network

  lastSampleMillis = millis();
  lastUploadMillis = millis();

  Serial.println("============================================");
  Serial.println("  Water Leak Detection - 3 Modules + GSM + Server");
  Serial.println("============================================");
  if (gsmModulePresent()) {
    Serial.println("[GSM] Module detected and responding.");
  } else {
    Serial.println("[GSM] WARNING: module not responding to AT. Check wiring/power.");
  }
  Serial.println("Send 'C1'/'C2'/'C3' to calibrate that module.");
  Serial.println("  While calibrating: 'A' = capture point, 'F' = finish & compute K");
  Serial.println("Send 'S<module>,<pressure>,<flow>' e.g. S1,1.2,4.5 to inject");
  Serial.println("  simulated readings for testing. Send 'R<module>' or 'R' (all)");
  Serial.println("  to return to real sensor readings.");
  Serial.println("Send 'U' to force an immediate upload to the server.");
  Serial.println();
}

// ============================================================================
// MAIN LOOP
// ============================================================================
void loop() {
  // --- Handle Serial commands ---
  if (Serial.available()) {
    char cmd = Serial.read();

    if (cmd == 'C') {
      char digit = Serial.read();
      int idx = digit - '1';
      if (idx >= 0 && idx < NUM_MODULES) {
        modules[idx].calibrationMode = true;
        modules[idx].calibCount = 0;
        activeCalibModule = idx;
        Serial.printf("[CALIBRATION] Module %d - vary flow, 'A' to capture, 'F' to finish.\n", idx + 1);
      }
    } else if ((cmd == 'A' || cmd == 'F') && activeCalibModule >= 0) {
      handleCalibrationInput(modules[activeCalibModule], cmd);
    } else if (cmd == 'S') {
      char digit = Serial.read();
      int idx = digit - '1';
      if (Serial.peek() == ',') Serial.read();
      float p = Serial.parseFloat();
      if (Serial.peek() == ',') Serial.read();
      float q = Serial.parseFloat();
      if (idx >= 0 && idx < NUM_MODULES) {
        modules[idx].simPressure = p;
        modules[idx].simFlow = q;
        modules[idx].simulationMode = true;
        Serial.printf("[SIMULATION] Module %d - Injecting P=%.2f bar, Q=%.2f L/min\n", idx + 1, p, q);
      }
    } else if (cmd == 'R') {
      // Peek: a following digit scopes it to one module, otherwise resets all
      if (isDigit(Serial.peek())) {
        char digit = Serial.read();
        int idx = digit - '1';
        if (idx >= 0 && idx < NUM_MODULES) {
          modules[idx].simulationMode = false;
          Serial.printf("[SIMULATION] Module %d - returned to real sensor readings.\n", idx + 1);
        }
      } else {
        for (uint8_t i = 0; i < NUM_MODULES; i++) modules[i].simulationMode = false;
        Serial.println("[SIMULATION] All modules returned to real sensor readings.");
      }
    } else if (cmd == 'U') {
      uploadDueNow = true;
      Serial.println("[UPLOAD] Manual upload requested.");
    }
    // Any other/leftover character (e.g. newline after Enter) is simply
    // discarded by virtue of already having been consumed by Serial.read().
  }

  ensureWifi();

  unsigned long now = millis();
  if (now - lastSampleMillis >= SAMPLE_INTERVAL_MS) {
    unsigned long elapsed = now - lastSampleMillis;
    lastSampleMillis = now;

    for (uint8_t i = 0; i < NUM_MODULES; i++) {
      ModuleState &m = modules[i];

      // --- Read & filter pressure ---
      float rawPressure = m.simulationMode ? m.simPressure : readPressureRaw(m.pressurePin);
      m.pressureHistory[m.historyIndex] = rawPressure;
      float medianPressure = medianFilter(m.pressureHistory, FILTER_HISTORY_SIZE);
      m.pressureEMA = emaFilter(m.pressureEMA, medianPressure, EMA_ALPHA);

      // --- Read & filter flow ---
      noInterrupts();
      uint32_t pulses = m.flowPulseCount;
      m.flowPulseCount = 0;
      interrupts();

      float rawFlow = m.simulationMode ? m.simFlow : computeFlowRateLPM(pulses, elapsed);
      m.flowHistory[m.historyIndex] = rawFlow;
      float medianFlow = medianFilter(m.flowHistory, FILTER_HISTORY_SIZE);
      m.flowEMA = emaFilter(m.flowEMA, medianFlow, EMA_ALPHA);

      m.historyIndex = (m.historyIndex + 1) % FILTER_HISTORY_SIZE;

      // --- Classification + hysteresis ---
      SystemStatus candidate = classify(m, m.pressureEMA, m.flowEMA);
      updateStatusWithHysteresis(m, candidate);

      // --- GSM notification on status change ---
      checkAndNotifyLeak(m);

      // --- Report ---
      Serial.printf("[M%d] P=%.2f bar | Q=%.2f L/min | K=%.3f | Status: %s%s%s\n",
                    m.id, m.pressureEMA, m.flowEMA, m.K_CONDUCTANCE,
                    statusToString(m.currentStatus),
                    m.calibrationMode ? "  (CALIBRATING)" : "",
                    m.simulationMode ? "  (SIMULATED INPUT)" : "");
    }
    Serial.printf("--- WiFi: %s ---\n",
                  WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString().c_str() : "disconnected");
  }

  // --- Upload on its own schedule, or immediately after a status change ---
  if (uploadDueNow || (now - lastUploadMillis >= UPLOAD_INTERVAL_MS)) {
    // Do not upload before the first sample has produced real EMA values.
    if (modules[0].pressureEMA >= 0) {
      lastUploadMillis = now;
      uploadDueNow = false;
      uploadReadings();
    }
  }
}
