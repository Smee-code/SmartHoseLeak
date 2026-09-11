/*
  ============================================================================
  WATER LEAK DETECTION SYSTEM - 3 MODULES + GSM SMS ALERT + WIFI DASHBOARD
  + SMART HOSE LEAK SERVER UPLOAD
  Water Pressure Sensor + Water Flow Sensor x3 on a single ESP32, with a
  SIM800L-type GSM module for SMS notification to the owner on leak, PLUS
  a WiFi web dashboard / JSON API for live remote monitoring.
  ----------------------------------------------------------------------------
  Undergraduate Thesis Reference Implementation

  WHAT CHANGED FROM THE GSM-ONLY VERSION:
    - ESP32 now also connects to a WiFi network (WIFI_SSID / WIFI_PASSWORD
      below) and runs a small built-in web server (WebServer.h, part of the
      ESP32 Arduino core - no extra library install needed).
    - Two endpoints are served:
        GET /          -> a simple auto-refreshing HTML dashboard showing
                           pressure, flow, K, and status for all 3 modules
        GET /status     -> the same data as JSON, for logging / integration
                           with something else (a phone app, ThingSpeak
                           relay, Google Sheets script, etc.)
    - WiFi connection is attempted in setup() with a timeout; if it fails,
      the system prints a warning and continues running normally on GSM +
      Serial only (WiFi is a bonus feature, not a dependency of the core
      leak-detection logic).
    - A watchdog-style reconnect check runs in loop(): if WiFi drops, it
      retries every WIFI_RETRY_INTERVAL_MS instead of blocking forever.
    - GSM SMS alerting logic is UNCHANGED - WiFi is purely additive.

  SERVER UPLOAD (Smart Hose Leak Django app):
    - Every UPLOAD_INTERVAL_MS the board POSTs all three modules' readings
      as one JSON batch to SERVER_URL (/api/ingest/), authenticated with
      DEVICE_API_KEY in the X-Device-Key header.
    - The server owns the history, the household accounts, the complaints
      and the in-app notifications. This board owns the DECISION: the leak
      classification and the SMS happen here and do not depend on the
      server, the WiFi, or the internet being available.
    - Uploads are skipped in setup-AP mode and while WiFi is down. The most
      recent result is shown on the dashboard and in /status as
      "server_upload", so a misconfigured key or IP is visible at a glance.
    - Set SERVER_URL and DEVICE_API_KEY below before flashing.

  IMPORTANT ADC NOTE (still applies):
    - All 3 pressure inputs (GPIO34/35/32) are ADC1 channels. This was
      already required in the GSM-only version and matters even more now:
      ADC2 pins (which overlap with WiFi hardware) cannot be read reliably
      while WiFi is active on the ESP32. Do NOT move the pressure pins to
      ADC2 pins (0,2,4,12-15,25-27) now that WiFi is enabled.

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
    - Pressure pins MUST be ADC1 channels (32-39). See ADC note above.
    - Each pressure sensor (0.5-4.5V output) still needs its OWN voltage
      divider in front of its ADC pin, exactly as in the single-module
      version. DIVIDER_RATIO below assumes identical resistor values on
      all 3 dividers - update per-channel math if you use different
      resistors per module.

  HARDWARE WIRING - GSM MODULE (SIM800L or similar):
    ESP32 GPIO17 (TX2)  -> GSM module RX
    ESP32 GPIO16 (RX2)  -> GSM module TX
    ESP32 GND           -> GSM module GND (COMMON GROUND, required)

    IMPORTANT: Do NOT power a SIM800L from the ESP32's 3.3V or 5V pin.
    It draws current spikes up to ~2A during transmission bursts, which
    will brown out the ESP32. Use a dedicated 3.7-4.2V regulated supply
    (e.g. a buck converter or LiPo + charger) capable of that peak current,
    with grounds tied together.

    Update OWNER_PHONE_NUMBER below to the real number, in international
    format with country code (e.g. "+639171234567" for PH), before
    deploying.

  WIFI SETUP - NO RECOMPILING NEEDED:
    Credentials are NOT hardcoded. They are entered through the device's own
    setup page and stored in flash (NVS), so the same firmware runs on any
    network.

    First boot (or whenever the saved network is unreachable):
      1. The ESP32 raises its own access point, "LeakDetect-Setup"
         (password "leakdetect123" - change SETUP_AP_PASSWORD below).
      2. Join it with a phone or laptop. A captive-portal prompt should
         appear; if it does not, open http://192.168.4.1/ manually.
      3. Pick the network from the scanned list (or type a hidden SSID),
         enter its password, and press Save. The device reboots and joins.

    Once it is on a network:
      - The dashboard is at http://<ip>/ - the Serial Monitor prints the
        address at 115200 baud, e.g.
            [WIFI] Connected. Dashboard: http://192.168.1.42/
      - http://<ip>/status is the same data as JSON.
      - http://<ip>/wifi changes the network. That page is behind HTTP basic
        auth (CONFIG_USERNAME / CONFIG_PASSWORD below) so that not everyone
        on the LAN can knock the device offline; the read-only dashboard
        stays open. In setup-AP mode the auth prompt is skipped, because the
        AP's own WPA2 password is already the gate.
      - "Forget saved network" wipes the stored credentials and reboots back
        into setup mode.

    If the network drops, the device retries WIFI_FAILS_BEFORE_AP times and
    then reopens the setup AP rather than retrying a dead SSID forever.
    Leak detection, SMS alerts and Serial output are unaffected throughout -
    WiFi remains a bonus feature, not a dependency.
  ============================================================================
*/

#include <math.h>
#include <HardwareSerial.h>
#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <Preferences.h>
#include <HTTPClient.h>

// ============================================================================
// USER CONFIGURATION
// ============================================================================

#define NUM_MODULES 3

// ---- Pin assignments per module ----
const uint8_t FLOW_PINS[NUM_MODULES] = {27, 26, 25};
const uint8_t PRESSURE_PINS[NUM_MODULES] = {34, 35, 32};

// ---- Flow sensor calibration (shared across identical sensor models) ----
#define PULSES_PER_LITER 450.0 // YF-S201 typical - confirm per datasheet

// ---- Pressure sensor calibration (shared across identical sensor models) ----
#define SENSOR_VOUT_MIN 0.5
#define SENSOR_VOUT_MAX 4.5
#define PRESSURE_MAX_BAR 12.0
#define DIVIDER_RATIO (20000.0 / (10000.0 + 20000.0)) // R2/(R1+R2)
#define ADC_MAX_COUNT 4095.0
#define ADC_VREF 3.3

// ---- Timing ----
#define SAMPLE_INTERVAL_MS 1000
#define FILTER_HISTORY_SIZE 5
#define EMA_ALPHA 0.2

// ---- Detection thresholds (tune per rig / per module if needed) ----
#define P_LOW_THRESHOLD_BAR 1.5
#define RESIDUAL_LEAK_THRESHOLD 0.18
#define FLOW_ZERO_THRESHOLD_LPM 0.3
#define STATIC_TEST_WINDOW_S 30
#define STATIC_DECAY_THRESHOLD_BAR 0.05
#define REQUIRED_CONSECUTIVE 15

#define MAX_CALIBRATION_POINTS 10

// ---- GSM module (SIM800L / SIM900 / A6, AT-command based) ----
#define GSM_RX_PIN 16 // ESP32 RX2 <- GSM module TX
#define GSM_TX_PIN 17 // ESP32 TX2 -> GSM module RX
#define GSM_BAUD 9600
#define OWNER_PHONE_NUMBER "+639171234567" // <-- UPDATE THIS
HardwareSerial GSM_SERIAL(2);              // ESP32 UART2

// ---- WiFi / web dashboard ----
// These two are only FALLBACK DEFAULTS, used on a brand-new board that has
// nothing saved in flash yet. Normal operation reads the credentials the user
// entered through the setup page (stored in NVS), so the same firmware runs on
// any network without recompiling. Leave both as "" to force setup mode on
// first boot.
#define DEFAULT_WIFI_SSID ""
#define DEFAULT_WIFI_PASSWORD ""

#define WIFI_CONNECT_TIMEOUT_MS 15000 // give up on initial connect after this long
#define WIFI_RETRY_INTERVAL_MS 30000  // if disconnected, retry this often (non-blocking)
#define WIFI_FAILS_BEFORE_AP 5        // consecutive failed retries -> reopen setup AP
#define WEB_SERVER_PORT 80

// ---- Setup access point (shown when the device cannot join a network) ----
// Join this AP with a phone and the captive portal opens the setup page.
// Its WPA2 password is what protects setup mode - anyone who can join the AP
// can configure the device, so do not leave it blank.
#define SETUP_AP_SSID "LeakDetect-Setup"
#define SETUP_AP_PASSWORD "leakdetect123" // >= 8 chars, or the AP will be open
#define DNS_PORT 53

// ---- Credentials for the /wifi config page while on a normal network ----
// The status dashboard stays open to anyone on the LAN; only the pages that
// can CHANGE the device's network are behind this login.
#define CONFIG_USERNAME "admin"
#define CONFIG_PASSWORD "leakadmin" // <-- CHANGE THIS

// ---- Where the saved credentials live in flash ----
#define WIFI_PREFS_NAMESPACE "wifi-cfg"

// ---- Smart Hose Leak server uplink ----
// The readings are POSTed as JSON to the Django server, which owns the
// history, the household accounts and the notifications. This is additive:
// if the server is unreachable, detection and the SMS alert are unaffected.
//
// SERVER_URL must be the LAN IP of the machine running Django, started as
//   python manage.py runserver 0.0.0.0:8000
// "localhost" and 127.0.0.1 will NOT work - those mean the ESP32 itself.
// DEVICE_API_KEY is printed by `python manage.py setup_demo` and is also in
// the Django admin under Devices.
// FALLBACK ONLY. The address actually used is the one saved through the setup
// page (stored in flash), so moving to a new network needs a phone, not a
// reflash. This value is used only until something is saved there, or after
// "Forget saved network" wipes it.
#define DEFAULT_SERVER_URL "http://192.168.1.21:8000/api/ingest/"
#define DEVICE_ID "esp32-01"
#define DEVICE_API_KEY "Tu-8U_8etJh_y4Fy_4Ao4vVzg431iCobAZs00KaKoog" // <-- UPDATE THIS

#define UPLOAD_INTERVAL_MS 2000 // how often to POST readings
#define HTTP_TIMEOUT_MS 5000    // give up on a POST after this long

WebServer webServer(WEB_SERVER_PORT);
DNSServer dnsServer;
Preferences wifiPrefs;

String cfgSSID = ""; // credentials currently in use (from NVS or defaults)
String cfgPassword = "";
String cfgServerUrl = ""; // where readings are POSTed (from NVS or the default)

unsigned long lastWifiRetryMillis = 0;
bool wifiEverConnected = false;
bool apMode = false; // true while the setup access point is running
uint8_t wifiFailCount = 0;

// ============================================================================
// GLOBAL STATE
// ============================================================================

enum SystemStatus
{
  STATUS_NORMAL,
  STATUS_LOW_SUPPLY,
  STATUS_POSSIBLE_LEAK
};

// All per-module state lives here. One struct instance per physical
// pressure+flow sensor pair, so the sampling/filter/classification code
// below is written once and looped over NUM_MODULES instead of copy-pasted.
struct ModuleState
{
  uint8_t id; // 1, 2, 3 - for display/SMS messages
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

// Outcome of the most recent upload attempt, surfaced on the device's own
// dashboard and in /status so a failing uplink is visible without a laptop.
String lastUploadResult = "not attempted yet";
bool lastUploadOk = false;

// ============================================================================
// INTERRUPT SERVICE ROUTINES - one per module (ISRs can't take parameters)
// ============================================================================
void IRAM_ATTR flowPulseISR0() { modules[0].flowPulseCount++; }
void IRAM_ATTR flowPulseISR1() { modules[1].flowPulseCount++; }
void IRAM_ATTR flowPulseISR2() { modules[2].flowPulseCount++; }

// ============================================================================
// FILTERING FUNCTIONS (unchanged logic, now generic - operate on whichever
// module's history array is passed in)
// ============================================================================

float medianFilter(float *history, uint8_t size)
{
  float sorted[FILTER_HISTORY_SIZE];
  memcpy(sorted, history, sizeof(float) * size);
  for (uint8_t i = 1; i < size; i++)
  {
    float key = sorted[i];
    int j = i - 1;
    while (j >= 0 && sorted[j] > key)
    {
      sorted[j + 1] = sorted[j];
      j--;
    }
    sorted[j + 1] = key;
  }
  return sorted[size / 2];
}

float emaFilter(float previousEMA, float newSample, float alpha)
{
  if (previousEMA < 0)
    return newSample;
  return alpha * newSample + (1.0 - alpha) * previousEMA;
}

// ============================================================================
// SENSOR READING FUNCTIONS
// ============================================================================

float readPressureRaw(uint8_t pin)
{
  int adcCount = analogRead(pin);
  float vAtPin = (adcCount / ADC_MAX_COUNT) * ADC_VREF;
  float vSensorOut = vAtPin / DIVIDER_RATIO;

  float pressureBar = (vSensorOut - SENSOR_VOUT_MIN) *
                      (PRESSURE_MAX_BAR / (SENSOR_VOUT_MAX - SENSOR_VOUT_MIN));

  if (pressureBar < 0)
    pressureBar = 0;
  return pressureBar;
}

float computeFlowRateLPM(uint32_t pulses, unsigned long elapsedMillis)
{
  float liters = pulses / PULSES_PER_LITER;
  float minutes = elapsedMillis / 60000.0;
  if (minutes <= 0)
    return 0;
  return liters / minutes;
}

// ============================================================================
// CALIBRATION ROUTINE (per module)
// ============================================================================
// Usage: send "C1", "C2" or "C3" over Serial to start calibrating that
// module. Vary flow (open/close taps) and send 'A' to capture each stable
// (P,Q) point, then 'F' to finish and compute that module's K_CONDUCTANCE.

void handleCalibrationInput(ModuleState &m, char cmd)
{
  if (cmd == 'A')
  {
    if (m.calibCount < MAX_CALIBRATION_POINTS)
    {
      m.calibP[m.calibCount] = m.pressureEMA;
      m.calibQ[m.calibCount] = m.flowEMA;
      m.calibCount++;
      Serial.printf("[CALIBRATION M%d] Captured point %d: P=%.2f bar, Q=%.2f L/min\n",
                    m.id, m.calibCount, m.pressureEMA, m.flowEMA);
    }
    else
    {
      Serial.printf("[CALIBRATION M%d] Max calibration points reached.\n", m.id);
    }
  }
  else if (cmd == 'F')
  {
    float numerator = 0, denominator = 0;
    for (uint8_t i = 0; i < m.calibCount; i++)
    {
      if (m.calibP[i] > 0.01)
      {
        numerator += m.calibQ[i] * sqrt(m.calibP[i]);
        denominator += m.calibP[i];
      }
    }
    if (denominator > 0)
    {
      m.K_CONDUCTANCE = numerator / denominator;
      Serial.printf("[CALIBRATION M%d] Complete. K_CONDUCTANCE = %.4f\n", m.id, m.K_CONDUCTANCE);
    }
    else
    {
      Serial.printf("[CALIBRATION M%d] Not enough valid points to compute K.\n", m.id);
    }
    m.calibrationMode = false;
    activeCalibModule = -1;
  }
}

// ============================================================================
// LEAK CLASSIFICATION LOGIC (unchanged per-module logic)
// ============================================================================

SystemStatus classify(ModuleState &m, float pressure, float flow)
{
  bool haveCalibration = (m.K_CONDUCTANCE > 0);

  float expectedFlow = 0;
  float residualRatio = 0;
  if (haveCalibration)
  {
    expectedFlow = m.K_CONDUCTANCE * sqrt(max(pressure, 0.0f));
    if (expectedFlow > 0.05)
    {
      residualRatio = (flow - expectedFlow) / expectedFlow;
    }
  }

  // --- Static (zero-flow) pressure decay test - doesn't need calibration ---
  if (flow < FLOW_ZERO_THRESHOLD_LPM)
  {
    if (!m.inZeroFlowWindow)
    {
      m.inZeroFlowWindow = true;
      m.zeroFlowStartMillis = millis();
      m.pressureAtZeroFlowStart = pressure;
    }
    else
    {
      unsigned long elapsedS = (millis() - m.zeroFlowStartMillis) / 1000;
      if (elapsedS >= STATIC_TEST_WINDOW_S)
      {
        float drop = m.pressureAtZeroFlowStart - pressure;
        if (drop > STATIC_DECAY_THRESHOLD_BAR)
        {
          return STATUS_POSSIBLE_LEAK;
        }
        m.zeroFlowStartMillis = millis();
        m.pressureAtZeroFlowStart = pressure;
      }
    }
  }
  else
  {
    m.inZeroFlowWindow = false;
  }

  // --- Pressure/flow curve residual test (requires calibration) ---
  if (haveCalibration)
  {
    if (pressure < P_LOW_THRESHOLD_BAR)
    {
      if (fabs(residualRatio) < RESIDUAL_LEAK_THRESHOLD)
      {
        return STATUS_LOW_SUPPLY;
      }
      else if (residualRatio > RESIDUAL_LEAK_THRESHOLD)
      {
        return STATUS_POSSIBLE_LEAK;
      }
    }
    else
    {
      if (residualRatio > RESIDUAL_LEAK_THRESHOLD)
      {
        return STATUS_POSSIBLE_LEAK;
      }
    }
  }
  else
  {
    if (pressure < P_LOW_THRESHOLD_BAR)
    {
      return STATUS_LOW_SUPPLY;
    }
  }

  return STATUS_NORMAL;
}

void updateStatusWithHysteresis(ModuleState &m, SystemStatus newCandidate)
{
  if (newCandidate == m.candidateStatus)
  {
    m.consecutiveCount++;
  }
  else
  {
    m.candidateStatus = newCandidate;
    m.consecutiveCount = 1;
  }

  if (m.consecutiveCount >= REQUIRED_CONSECUTIVE && m.candidateStatus != m.currentStatus)
  {
    m.currentStatus = m.candidateStatus;
  }
}

const char *statusToString(SystemStatus s)
{
  switch (s)
  {
  case STATUS_NORMAL:
    return "NORMAL";
  case STATUS_LOW_SUPPLY:
    return "LOW WATER SUPPLY";
  case STATUS_POSSIBLE_LEAK:
    return "POSSIBLE LEAK";
  default:
    return "UNKNOWN";
  }
}

// ============================================================================
// GSM (SIM800L-style AT command set) - sends SMS to the owner
// ============================================================================

// Drains and returns whatever the GSM module sends back within timeoutMs.
// This is a simple blocking read, adequate for a thesis-scale project;
// a production system should parse specific response codes (OK/ERROR)
// and retry, and should check network registration (AT+CREG?) and signal
// quality (AT+CSQ) before attempting to send.
String gsmReadResponse(unsigned long timeoutMs)
{
  String response = "";
  unsigned long start = millis();
  while (millis() - start < timeoutMs)
  {
    while (GSM_SERIAL.available())
    {
      response += (char)GSM_SERIAL.read();
    }
  }
  return response;
}

bool gsmModulePresent()
{
  while (GSM_SERIAL.available())
    GSM_SERIAL.read(); // flush
  GSM_SERIAL.println("AT");
  String resp = gsmReadResponse(1000);
  return resp.indexOf("OK") >= 0;
}

bool sendSMS(const String &number, const String &message)
{
  while (GSM_SERIAL.available())
    GSM_SERIAL.read(); // flush stale data

  GSM_SERIAL.println("AT+CMGF=1"); // text mode (not PDU mode)
  gsmReadResponse(500);

  GSM_SERIAL.print("AT+CMGS=\"");
  GSM_SERIAL.print(number);
  GSM_SERIAL.println("\"");
  gsmReadResponse(500); // wait for the '>' prompt

  GSM_SERIAL.print(message);
  GSM_SERIAL.write(26);                // Ctrl+Z terminates and sends the SMS body
  String resp = gsmReadResponse(5000); // sending can take a few seconds

  bool ok = (resp.indexOf("OK") >= 0) || (resp.indexOf("+CMGS") >= 0);
  Serial.printf("[GSM] Send to %s: %s -- raw response: %s\n",
                number.c_str(), ok ? "OK" : "FAILED", resp.c_str());
  return ok;
}

// Checks each module's status transition and fires an SMS on
// NORMAL/LOW_SUPPLY -> POSSIBLE_LEAK, and again on POSSIBLE_LEAK -> NORMAL
// (leak cleared). leakNotified prevents re-sending every sample while the
// alarm condition is still active.
void checkAndNotifyLeak(ModuleState &m)
{
  if (m.currentStatus == STATUS_POSSIBLE_LEAK && !m.leakNotified)
  {
    String msg = "ALERT: Possible LEAK detected on Module " + String(m.id) +
                 ". Pressure=" + String(m.pressureEMA, 2) + " bar, Flow=" +
                 String(m.flowEMA, 2) + " L/min. Please check the pipeline.";
    Serial.printf("[LEAK] Module %d - sending SMS alert.\n", m.id);
    sendSMS(OWNER_PHONE_NUMBER, msg);
    m.leakNotified = true;
  }
  else if (m.currentStatus != STATUS_POSSIBLE_LEAK && m.leakNotified)
  {
    String msg = "UPDATE: Module " + String(m.id) +
                 " leak condition has CLEARED. Status is now " +
                 String(statusToString(m.currentStatus)) + ".";
    Serial.printf("[LEAK] Module %d - condition cleared, sending SMS.\n", m.id);
    sendSMS(OWNER_PHONE_NUMBER, msg);
    m.leakNotified = false;
  }
}

// ============================================================================
// SERVER UPLINK - posts readings to the Smart Hose Leak Django server
// ============================================================================

// Builds the JSON batch the server expects at /api/ingest/:
//   {"device_id":"esp32-01","readings":[{"module":1,...},{...},{...}]}
//
// Deliberately close to buildStatusJSON() below, but NOT the same shape: the
// server keys each reading by "module" (not "id") and the calibration constant
// by "k" (not "k_conductance"), and it wraps the batch in the device identity.
// The status tokens from statusToString() - "NORMAL", "LOW WATER SUPPLY",
// "POSSIBLE LEAK" - are already exactly the strings the server accepts.
String buildPayload()
{
  String json = "{\"device_id\":\"" DEVICE_ID "\",\"readings\":[";

  for (uint8_t i = 0; i < NUM_MODULES; i++)
  {
    ModuleState &m = modules[i];
    if (i > 0)
      json += ",";
    json += "{\"module\":" + String(m.id);
    json += ",\"pressure_bar\":" + String(m.pressureEMA, 3);
    json += ",\"flow_lpm\":" + String(m.flowEMA, 3);
    json += ",\"k\":" + String(m.K_CONDUCTANCE, 4);
    json += ",\"status\":\"" + String(statusToString(m.currentStatus)) + "\"";
    json += ",\"calibrating\":" + String(m.calibrationMode ? "true" : "false");
    json += ",\"simulated\":" + String(m.simulationMode ? "true" : "false");
    json += "}";
  }

  json += "]}";
  return json;
}

// Posts the current readings. Every failure is recorded in lastUploadResult
// and otherwise ignored: the leak decision and the SMS are made here on the
// device, so neither waits on the network or on the server being up.
//
// Skipped entirely in setup-AP mode - there the ESP32 has an IP address of its
// own but no route to the server, so attempting the POST would only stall the
// loop for HTTP_TIMEOUT_MS on every pass.
void uploadReadings()
{
  if (apMode)
  {
    lastUploadOk = false;
    lastUploadResult = "skipped - in WiFi setup mode";
    return;
  }
  if (WiFi.status() != WL_CONNECTED)
  {
    lastUploadOk = false;
    lastUploadResult = "skipped - WiFi not connected";
    return;
  }

  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT_MS);

  if (!http.begin(cfgServerUrl))
  {
    lastUploadOk = false;
    lastUploadResult = "bad server address";
    Serial.printf("[UPLOAD] Failed to parse server address \"%s\".\n", cfgServerUrl.c_str());
    return;
  }

  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Key", DEVICE_API_KEY);

  int code = http.POST(buildPayload());

  if (code == 200)
  {
    lastUploadOk = true;
    lastUploadResult = "OK";
    Serial.println("[UPLOAD] OK");
  }
  else if (code == 401)
  {
    lastUploadOk = false;
    lastUploadResult = "401 - DEVICE_API_KEY rejected by server";
    Serial.println("[UPLOAD] REJECTED 401 - DEVICE_API_KEY does not match any device on the server.");
  }
  else if (code > 0)
  {
    lastUploadOk = false;
    lastUploadResult = "HTTP " + String(code);
    Serial.printf("[UPLOAD] Server returned %d: %s\n", code, http.getString().c_str());
  }
  else
  {
    lastUploadOk = false;
    lastUploadResult = String("no reply - ") + http.errorToString(code);
    Serial.printf("[UPLOAD] Failed: %s\n", http.errorToString(code).c_str());
  }

  http.end();
}

// ============================================================================
// WIFI CONNECTION + WEB DASHBOARD
// ============================================================================

// ---------------------------------------------------------------------------
// CREDENTIAL STORAGE (NVS / flash)
// ---------------------------------------------------------------------------
// Preferences writes to a flash partition that survives reboots, so a device
// configured once at a site stays configured. "Forget network" clears it.

void loadWiFiConfig()
{
  wifiPrefs.begin(WIFI_PREFS_NAMESPACE, true); // read-only
  cfgSSID = wifiPrefs.getString("ssid", "");
  cfgPassword = wifiPrefs.getString("pass", "");
  cfgServerUrl = wifiPrefs.getString("srv", "");
  wifiPrefs.end();

  // The server lives on a laptop with a DHCP address, so it moves whenever the
  // network changes. Keeping it in flash beside the credentials means a new
  // location needs a phone and the setup page, not a laptop and a reflash.
  if (cfgServerUrl.length() == 0)
  {
    cfgServerUrl = DEFAULT_SERVER_URL;
    Serial.printf("[UPLOAD] No saved server address - using compiled default %s\n",
                  cfgServerUrl.c_str());
  }
  else
  {
    Serial.printf("[UPLOAD] Loaded saved server address %s\n", cfgServerUrl.c_str());
  }

  if (cfgSSID.length() == 0)
  {
    cfgSSID = DEFAULT_WIFI_SSID;
    cfgPassword = DEFAULT_WIFI_PASSWORD;
    if (cfgSSID.length() == 0)
    {
      Serial.println("[WIFI] No saved network. Will start setup mode.");
    }
    else
    {
      Serial.printf("[WIFI] No saved network - falling back to compiled default \"%s\".\n",
                    cfgSSID.c_str());
    }
  }
  else
  {
    Serial.printf("[WIFI] Loaded saved network \"%s\".\n", cfgSSID.c_str());
  }
}

void saveWiFiConfig(const String &ssid, const String &password)
{
  wifiPrefs.begin(WIFI_PREFS_NAMESPACE, false); // read-write
  wifiPrefs.putString("ssid", ssid);
  wifiPrefs.putString("pass", password);
  wifiPrefs.end();
  Serial.printf("[WIFI] Saved network \"%s\" to flash.\n", ssid.c_str());
}

// Saved separately from the credentials: changing where readings go should not
// require retyping the WiFi password, and does not need a reboot.
void saveServerUrl(const String &url)
{
  wifiPrefs.begin(WIFI_PREFS_NAMESPACE, false);
  wifiPrefs.putString("srv", url);
  wifiPrefs.end();
  Serial.printf("[UPLOAD] Saved server address %s to flash.\n", url.c_str());
}

void clearWiFiConfig()
{
  wifiPrefs.begin(WIFI_PREFS_NAMESPACE, false);
  wifiPrefs.clear(); // credentials AND the saved server address
  wifiPrefs.end();
  Serial.println("[WIFI] Cleared saved network and server address from flash.");
}

// Minimal escaping so an SSID containing quotes or angle brackets cannot break
// (or inject into) the config page markup.
String htmlEscape(const String &in)
{
  String out;
  out.reserve(in.length() + 8);
  for (size_t i = 0; i < in.length(); i++)
  {
    char c = in[i];
    switch (c)
    {
    case '&':
      out += "&amp;";
      break;
    case '<':
      out += "&lt;";
      break;
    case '>':
      out += "&gt;";
      break;
    case '"':
      out += "&quot;";
      break;
    case '\'':
      out += "&#39;";
      break;
    default:
      out += c;
    }
  }
  return out;
}

// ---------------------------------------------------------------------------
// WIFI CONNECTION
// ---------------------------------------------------------------------------

// Blocking attempt with a timeout, called from setup(). Failure here does not
// halt the sketch - GSM + Serial keep working either way.
bool connectWiFi()
{
  if (cfgSSID.length() == 0)
    return false;

  Serial.printf("[WIFI] Connecting to \"%s\" ", cfgSSID.c_str());
  WiFi.mode(WIFI_STA);
  WiFi.begin(cfgSSID.c_str(), cfgPassword.c_str());

  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && (millis() - start) < WIFI_CONNECT_TIMEOUT_MS)
  {
    delay(300);
    Serial.print(".");
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED)
  {
    wifiEverConnected = true;
    apMode = false;
    wifiFailCount = 0;
    Serial.printf("[WIFI] Connected. Dashboard: http://%s/\n", WiFi.localIP().toString().c_str());
    return true;
  }

  Serial.println("[WIFI] WARNING: could not connect within timeout. "
                 "Continuing without WiFi (GSM + Serial still active).");
  return false;
}

// Brings up the device's own access point so a phone/laptop can configure it
// with no existing network available. AP_STA (rather than plain AP) keeps the
// station interface alive so the setup page can still scan for networks.
void startSetupAP()
{
  apMode = true;
  WiFi.mode(WIFI_AP_STA);

  // Stop the station from retrying the network we just gave up on. The driver
  // keeps re-associating in the background otherwise, and scanNetworks()
  // returns WIFI_SCAN_FAILED for as long as a connect is in progress - which
  // is what leaves the setup page's network list empty. false/false = do not
  // power down the radio (the AP needs it) and do not erase stored config.
  WiFi.disconnect(false, false);

  WiFi.softAP(SETUP_AP_SSID, SETUP_AP_PASSWORD);
  delay(200);

  IPAddress ip = WiFi.softAPIP();
  dnsServer.start(DNS_PORT, "*", ip); // captive portal: every lookup resolves here

  Serial.printf("[WIFI] SETUP MODE. Join \"%s\" (password \"%s\"), then open http://%s/\n",
                SETUP_AP_SSID, SETUP_AP_PASSWORD, ip.toString().c_str());
}

// Non-blocking: called every loop() iteration. If WiFi is down, retries at
// most once every WIFI_RETRY_INTERVAL_MS instead of stalling the sensor loop.
// After WIFI_FAILS_BEFORE_AP consecutive failures it stops retrying a network
// that is clearly gone and reopens setup mode so the device can be re-pointed.
void maintainWiFi()
{
  if (apMode)
  {
    dnsServer.processNextRequest();
    return;
  }

  if (WiFi.status() == WL_CONNECTED)
  {
    wifiFailCount = 0;
    return;
  }

  unsigned long now = millis();
  if (now - lastWifiRetryMillis < WIFI_RETRY_INTERVAL_MS)
    return;
  lastWifiRetryMillis = now;

  wifiFailCount++;
  if (wifiFailCount > WIFI_FAILS_BEFORE_AP || cfgSSID.length() == 0)
  {
    Serial.printf("[WIFI] %u failed retries - reopening setup access point.\n", wifiFailCount);
    startSetupAP();
    return;
  }

  Serial.printf("[WIFI] Not connected - retry %u/%u...\n", wifiFailCount, WIFI_FAILS_BEFORE_AP);
  WiFi.disconnect();
  WiFi.begin(cfgSSID.c_str(), cfgPassword.c_str());
}

// ---------------------------------------------------------------------------
// WIFI CONFIGURATION PAGE
// ---------------------------------------------------------------------------

// Gate for anything that can change the device's network settings.
// In setup-AP mode the AP's own WPA2 password is already the gate, so basic
// auth is skipped there - otherwise first-time setup would demand a second
// password the installer has no way to enter.
bool requireConfigAuth()
{
  if (apMode)
    return true;
  if (webServer.authenticate(CONFIG_USERNAME, CONFIG_PASSWORD))
    return true;
  webServer.requestAuthentication();
  return false;
}

// ---------------------------------------------------------------------------
// NEARBY-NETWORK SCAN
// ---------------------------------------------------------------------------
// scanNetworks() blocks for 2-4 seconds and stalls the sampling loop for that
// long, so results are cached and reused for SCAN_CACHE_TTL_MS. Reloading the
// setup page, or being bounced back to it by a validation error, then costs
// nothing. "Rescan" forces a fresh sweep.

#define SCAN_CACHE_SIZE 20
#define SCAN_CACHE_TTL_MS 30000

struct ScanEntry
{
  String ssid;
  int32_t rssi;
  bool open;
};

ScanEntry scanCache[SCAN_CACHE_SIZE];
uint8_t scanCacheCount = 0;
unsigned long scanCacheMillis = 0;
int lastScanStatus = 0; // raw scanNetworks() result: <0 failed, 0 none found

// Returns true when scanCache holds usable results.
bool refreshScanCache(bool force)
{
  unsigned long now = millis();
  if (!force && scanCacheCount > 0 && (now - scanCacheMillis) < SCAN_CACHE_TTL_MS)
  {
    return true;
  }

  // An association attempt in progress makes the scan fail outright. In setup
  // mode there is nothing worth keeping, so drop it first.
  if (apMode && WiFi.status() != WL_CONNECTED)
  {
    WiFi.disconnect(false, false);
    delay(80);
  }

  WiFi.scanDelete();
  int found = WiFi.scanNetworks();

  // WIFI_SCAN_FAILED (-2) is common on the first attempt right after the AP
  // comes up, because the radio is still settling. One retry clears it.
  if (found < 0)
  {
    Serial.printf("[WIFI] Scan failed (%d) - retrying once...\n", found);
    delay(250);
    WiFi.scanDelete();
    found = WiFi.scanNetworks();
  }

  lastScanStatus = found;
  Serial.printf("[WIFI] Scan result: %d network(s).\n", found);

  if (found <= 0)
  {
    scanCacheCount = 0;
    WiFi.scanDelete();
    return false;
  }

  scanCacheCount = 0;
  for (int i = 0; i < found && scanCacheCount < SCAN_CACHE_SIZE; i++)
  {
    String s = WiFi.SSID(i);
    if (s.length() == 0)
      continue; // hidden network - must be typed manually

    int32_t rssi = WiFi.RSSI(i);
    bool isOpen = (WiFi.encryptionType(i) == WIFI_AUTH_OPEN);

    // A repeater or mesh broadcasts the same SSID on several channels. Keep
    // one entry per name, carrying the strongest radio.
    int existing = -1;
    for (uint8_t j = 0; j < scanCacheCount; j++)
    {
      if (scanCache[j].ssid == s)
      {
        existing = j;
        break;
      }
    }
    if (existing >= 0)
    {
      if (rssi > scanCache[existing].rssi)
      {
        scanCache[existing].rssi = rssi;
        scanCache[existing].open = isOpen;
      }
      continue;
    }

    scanCache[scanCacheCount].ssid = s;
    scanCache[scanCacheCount].rssi = rssi;
    scanCache[scanCacheCount].open = isOpen;
    scanCacheCount++;
  }

  // Strongest first - insertion sort, at most SCAN_CACHE_SIZE entries.
  for (uint8_t a = 1; a < scanCacheCount; a++)
  {
    ScanEntry key = scanCache[a];
    int b = (int)a - 1;
    while (b >= 0 && scanCache[b].rssi < key.rssi)
    {
      scanCache[b + 1] = scanCache[b];
      b--;
    }
    scanCache[b + 1] = key;
  }

  scanCacheMillis = millis();
  WiFi.scanDelete();
  return scanCacheCount > 0;
}

// Plain-language signal strength - more useful on a phone than raw dBm.
const char *signalWord(int32_t rssi)
{
  if (rssi >= -60)
    return "strong";
  if (rssi >= -70)
    return "good";
  if (rssi >= -80)
    return "weak";
  return "very weak";
}

// Renders the setup form from the cached scan. forceRescan=true sweeps again.
// No default argument here on purpose: the IDE generates prototypes for .ino
// functions and drops defaults, which turns every one-argument call into a
// compile error.
String buildWifiPage(const String &message, bool forceRescan)
{
  bool haveScan = refreshScanCache(forceRescan);

  String html = "<!DOCTYPE html><html><head>";
  html += "<meta name='viewport' content='width=device-width,initial-scale=1'>";
  html += "<title>WiFi Setup - Water Leak Detection</title><style>";
  html += "body{font-family:Arial,sans-serif;background:#f4f6f8;margin:0;padding:20px;}";
  html += "h1{color:#1a3c5e;font-size:20px;}";
  html += "form{background:#fff;padding:16px;max-width:420px;box-shadow:0 1px 3px rgba(0,0,0,0.15);}";
  html += "label{display:block;margin:12px 0 4px;font-weight:bold;font-size:13px;}";
  html += "select,input[type=text],input[type=password]{width:100%;padding:8px;";
  html += "box-sizing:border-box;border:1px solid #ccc;}";
  html += "button{margin-top:16px;padding:10px 16px;background:#1a3c5e;color:#fff;border:0;";
  html += "cursor:pointer;}";
  html += ".msg{background:#fdecea;color:#c0392b;padding:10px;max-width:420px;margin-bottom:12px;}";
  html += ".warn{background:#fbeeda;color:#8a5a0b;padding:10px;font-size:13px;}";
  html += ".note{color:#555;font-size:12px;margin-top:14px;max-width:420px;}";
  html += "</style></head><body>";

  html += "<h1>WiFi Setup</h1>";
  if (message.length() > 0)
  {
    html += "<div class='msg'>" + htmlEscape(message) + "</div>";
  }

  html += "<p>Current: <b>";
  if (apMode)
  {
    html += "setup mode (not joined to any network)";
  }
  else if (WiFi.status() == WL_CONNECTED)
  {
    html += htmlEscape(cfgSSID) + "</b> at <b>" + WiFi.localIP().toString();
  }
  else
  {
    html += "disconnected (" + htmlEscape(cfgSSID) + ")";
  }
  html += "</b></p>";

  html += "<form method='POST' action='/wifi/save'>";
  html += "<label for='ssid_select'>Nearby networks</label>";

  if (haveScan)
  {
    html += "<select id='ssid_select' name='ssid_select'>";
    html += "<option value=''>-- select a network --</option>";
    for (uint8_t i = 0; i < scanCacheCount; i++)
    {
      html += "<option value='" + htmlEscape(scanCache[i].ssid) + "'>";
      html += htmlEscape(scanCache[i].ssid);
      html += " (" + String(signalWord(scanCache[i].rssi));
      html += scanCache[i].open ? ", open" : "";
      html += ")</option>";
    }
    html += "</select>";
  }
  else
  {
    // Previously this rendered an empty dropdown with no explanation, which
    // looked like "there are no networks here" even when the scan had simply
    // failed. Say which it was, and point at the field that always works.
    html += "<div class='warn'>";
    html += (lastScanStatus < 0)
                ? "The scan did not complete. This happens if the radio is busy; "
                  "press Rescan, or just type the network name below."
                : "No networks were found nearby. Move closer to the router, "
                  "press Rescan, or type the network name below.";
    html += "</div>";
  }
  html += "<p class='note' style='margin:6px 0 0'><a href='/wifi?rescan=1'>Rescan networks</a></p>";

  html += "<label for='ssid'>Or type the network name</label>";
  html += "<input type='text' id='ssid' name='ssid' ";
  html += "placeholder='leave blank to use the selection above'>";

  html += "<label for='password'>Password</label>";
  html += "<input type='password' id='password' name='password'>";

  // Where readings are sent. Kept on the same form so one visit can fix both
  // the network and the server after a move.
  html += "<hr style='margin:18px 0;border:0;border-top:1px solid #ddd'>";
  html += "<label for='server'>Server address</label>";
  html += "<input type='text' id='server' name='server' value='";
  html += htmlEscape(cfgServerUrl);
  html += "'>";
  html += "<p class='note' style='margin-top:6px'>The address of the computer running the ";
  html += "dashboard, ending in <b>/api/ingest/</b>. Open the dashboard on this phone and ";
  html += "the Device setup panel shows the exact line to paste. Leave the network fields ";
  html += "blank to change only this - the device will not reboot.</p>";

  html += "<button type='submit'>Save</button>";
  html += "</form>";

  html += "<form method='POST' action='/wifi/forget'>";
  html += "<button type='submit' style='background:#c0392b;'>Forget saved network</button>";
  html += "</form>";

  html += "<p class='note'>The device reboots after saving and tries the new network. ";
  html += "If it cannot join, it reopens the <b>" SETUP_AP_SSID "</b> setup access point ";
  html += "so you can try again. Leak detection, SMS alerts and Serial output keep running ";
  html += "either way - WiFi is only used for the dashboard.</p>";

  if (!apMode)
  {
    html += "<p class='note'><a href='/'>&larr; Back to dashboard</a></p>";
  }

  html += "</body></html>";

  WiFi.scanDelete();
  return html;
}

void handleWifiPage()
{
  if (!requireConfigAuth())
    return;
  // /wifi?rescan=1 bypasses the cache; a plain /wifi reuses it.
  webServer.send(200, "text/html", buildWifiPage("", webServer.hasArg("rescan")));
}

void handleWifiSave()
{
  if (!requireConfigAuth())
    return;

  // The typed field wins if it was filled in, so hidden networks still work.
  String ssid = webServer.arg("ssid");
  ssid.trim();
  if (ssid.length() == 0)
    ssid = webServer.arg("ssid_select");
  String password = webServer.arg("password");

  String server = webServer.arg("server");
  server.trim();

  // Catch the two mistakes that produce a board which looks connected but
  // silently never delivers: a bare IP, and localhost. 127.0.0.1 means the
  // ESP32 itself, so it can never reach the laptop that way.
  if (server.length() > 0)
  {
    if (!server.startsWith("http://") && !server.startsWith("https://"))
    {
      webServer.send(400, "text/html",
                     buildWifiPage("The server address must start with http:// - for example "
                                   "http://192.168.1.21:8000/api/ingest/",
                                   false));
      return;
    }
    if (server.indexOf("127.0.0.1") >= 0 || server.indexOf("localhost") >= 0)
    {
      webServer.send(400, "text/html",
                     buildWifiPage("127.0.0.1 and localhost mean this device, not your computer. "
                                   "Use the computer's address on the network.",
                                   false));
      return;
    }
  }

  bool serverChanged = (server.length() > 0 && server != cfgServerUrl);
  if (serverChanged)
  {
    saveServerUrl(server);
    cfgServerUrl = server;
  }

  // Server-only change: nothing about the network moved, so there is no reason
  // to reboot and drop the readings mid-run.
  if (ssid.length() == 0)
  {
    if (!serverChanged)
    {
      webServer.send(400, "text/html",
                     buildWifiPage("Nothing to save. Pick a network, or enter a new server address.",
                                   false));
      return;
    }

    String ok = "<!DOCTYPE html><html><head>";
    ok += "<meta name='viewport' content='width=device-width,initial-scale=1'>";
    ok += "<title>Saved</title></head><body style='font-family:Arial,sans-serif;padding:20px;'>";
    ok += "<h2>Server address saved</h2><p>Readings now go to <b>" + htmlEscape(cfgServerUrl) + "</b>.</p>";
    ok += "<p>No reboot needed - the next upload uses it. Check the dashboard, or ";
    ok += "<a href='/'>this device's own page</a>, where the last upload result is shown.</p>";
    ok += "<p><a href='/wifi'>&larr; Back to settings</a></p></body></html>";
    webServer.send(200, "text/html", ok);
    return;
  }

  saveWiFiConfig(ssid, password);

  String html = "<!DOCTYPE html><html><head>";
  html += "<meta name='viewport' content='width=device-width,initial-scale=1'>";
  html += "<title>Saved</title></head><body style='font-family:Arial,sans-serif;padding:20px;'>";
  html += "<h2>Saved</h2><p>Rebooting and joining <b>" + htmlEscape(ssid) + "</b>.</p>";
  html += "<p>Check the Serial Monitor for the new IP address, or reconnect to your ";
  html += "network and look for the device there. If it cannot join, it will reopen the ";
  html += "<b>" SETUP_AP_SSID "</b> access point within a minute.</p></body></html>";
  webServer.send(200, "text/html", html);

  delay(1500); // let the response actually flush before the reset
  ESP.restart();
}

void handleWifiForget()
{
  if (!requireConfigAuth())
    return;

  clearWiFiConfig();
  webServer.send(200, "text/html",
                 "<!DOCTYPE html><html><body style='font-family:Arial,sans-serif;padding:20px;'>"
                 "<h2>Network forgotten</h2><p>Rebooting into setup mode. Join the "
                 "<b>" SETUP_AP_SSID "</b> access point to configure it again.</p></body></html>");

  delay(1500);
  ESP.restart();
}

// In setup mode, send every stray request to the config page - that is what
// makes phones pop up the "sign in to network" captive portal prompt.
void handleNotFound()
{
  if (apMode)
  {
    webServer.sendHeader("Location", String("http://") + WiFi.softAPIP().toString() + "/wifi", true);
    webServer.send(302, "text/plain", "");
    return;
  }
  webServer.send(404, "text/plain", "Not found");
}

// Builds a compact JSON snapshot of all module states.
String buildStatusJSON()
{
  String json = "{";
  json += "\"wifi_connected\":" + String(WiFi.status() == WL_CONNECTED ? "true" : "false") + ",";
  json += "\"setup_mode\":" + String(apMode ? "true" : "false") + ",";
  json += "\"ssid\":\"" + htmlEscape(cfgSSID) + "\",";
  json += "\"ip\":\"" + (apMode ? WiFi.softAPIP().toString() : WiFi.localIP().toString()) + "\",";
  json += "\"server_url\":\"" + htmlEscape(cfgServerUrl) + "\",";
  json += "\"server_upload_ok\":" + String(lastUploadOk ? "true" : "false") + ",";
  json += "\"server_upload\":\"" + htmlEscape(lastUploadResult) + "\",";
  json += "\"modules\":[";
  for (uint8_t i = 0; i < NUM_MODULES; i++)
  {
    ModuleState &m = modules[i];
    if (i > 0)
      json += ",";
    json += "{";
    json += "\"id\":" + String(m.id) + ",";
    json += "\"pressure_bar\":" + String(m.pressureEMA, 2) + ",";
    json += "\"flow_lpm\":" + String(m.flowEMA, 2) + ",";
    json += "\"k_conductance\":" + String(m.K_CONDUCTANCE, 4) + ",";
    json += "\"status\":\"" + String(statusToString(m.currentStatus)) + "\",";
    json += "\"calibrating\":" + String(m.calibrationMode ? "true" : "false") + ",";
    json += "\"simulated\":" + String(m.simulationMode ? "true" : "false");
    json += "}";
  }
  json += "]}";
  return json;
}

void handleWebStatus()
{
  webServer.send(200, "application/json", buildStatusJSON());
}

// Simple auto-refreshing HTML page; refreshes itself every 3s via meta tag
// so no JavaScript polling is required. Good enough for a thesis demo.
void handleWebRoot()
{
  // In setup mode there are no meaningful readings to show yet, and the only
  // useful thing the visitor can do is pick a network.
  if (apMode)
  {
    handleWifiPage();
    return;
  }

  String html = "<!DOCTYPE html><html><head><meta http-equiv='refresh' content='3'>";
  html += "<title>Water Leak Detection Dashboard</title>";
  html += "<style>";
  html += "body{font-family:Arial,sans-serif;background:#f4f6f8;margin:0;padding:20px;}";
  html += "h1{color:#1a3c5e;}";
  html += "table{border-collapse:collapse;width:100%;max-width:700px;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,0.15);}";
  html += "th,td{padding:10px 14px;text-align:left;border-bottom:1px solid #e0e0e0;}";
  html += "th{background:#1a3c5e;color:#fff;}";
  html += ".NORMAL{color:#1a7a3c;font-weight:bold;}";
  html += ".LOW{color:#b8860b;font-weight:bold;}";
  html += ".LEAK{color:#c0392b;font-weight:bold;background:#fdecea;}";
  html += "</style></head><body>";
  html += "<h1>Water Leak Detection - Live Status</h1>";
  html += "<p>Auto-refreshes every 3 seconds. WiFi: ";
  html += (WiFi.status() == WL_CONNECTED ? "Connected" : "Disconnected");
  html += " &nbsp;|&nbsp; Server upload: <b style=\"color:";
  html += (lastUploadOk ? "#1a7a3c" : "#c0392b");
  html += "\">" + htmlEscape(lastUploadResult) + "</b>";
  html += "</p>";
  html += "<table><tr><th>Module</th><th>Pressure (bar)</th><th>Flow (L/min)</th><th>K</th><th>Status</th></tr>";

  for (uint8_t i = 0; i < NUM_MODULES; i++)
  {
    ModuleState &m = modules[i];
    String cssClass = "NORMAL";
    if (m.currentStatus == STATUS_LOW_SUPPLY)
      cssClass = "LOW";
    if (m.currentStatus == STATUS_POSSIBLE_LEAK)
      cssClass = "LEAK";

    html += "<tr><td>Module " + String(m.id) + "</td>";
    html += "<td>" + String(m.pressureEMA, 2) + "</td>";
    html += "<td>" + String(m.flowEMA, 2) + "</td>";
    html += "<td>" + String(m.K_CONDUCTANCE, 3) + "</td>";
    html += "<td class='" + cssClass + "'>" + String(statusToString(m.currentStatus));
    if (m.calibrationMode)
      html += " (calibrating)";
    if (m.simulationMode)
      html += " (simulated)";
    html += "</td></tr>";
  }

  html += "</table>";
  html += "<p style='margin-top:16px;'><a href='/status'>Raw JSON</a>";
  html += " &nbsp;|&nbsp; <a href='/wifi'>WiFi settings</a></p>";
  html += "</body></html>";

  webServer.send(200, "text/html", html);
}

void setupWebServer()
{
  webServer.on("/", handleWebRoot);
  webServer.on("/status", handleWebStatus);
  webServer.on("/wifi", HTTP_GET, handleWifiPage);
  webServer.on("/wifi/save", HTTP_POST, handleWifiSave);
  webServer.on("/wifi/forget", HTTP_POST, handleWifiForget);
  webServer.onNotFound(handleNotFound);
  webServer.begin();
  Serial.println("[WEB] Web server started.");
}

// ============================================================================
// MODULE INITIALIZATION
// ============================================================================

void initModule(uint8_t index, uint8_t flowPin, uint8_t pressurePin, void (*isr)())
{
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
void setup()
{
  Serial.begin(115200);
  delay(500);

  analogReadResolution(12);
  analogSetAttenuation(ADC_11db);

  initModule(0, FLOW_PINS[0], PRESSURE_PINS[0], flowPulseISR0);
  initModule(1, FLOW_PINS[1], PRESSURE_PINS[1], flowPulseISR1);
  initModule(2, FLOW_PINS[2], PRESSURE_PINS[2], flowPulseISR2);

  GSM_SERIAL.begin(GSM_BAUD, SERIAL_8N1, GSM_RX_PIN, GSM_TX_PIN);
  delay(3000); // SIM800L needs a few seconds to boot/register on the network

  lastSampleMillis = millis();

  Serial.println("============================================");
  Serial.println("  Water Leak Detection - 3 Modules + GSM + WiFi");
  Serial.println("============================================");
  if (gsmModulePresent())
  {
    Serial.println("[GSM] Module detected and responding.");
  }
  else
  {
    Serial.println("[GSM] WARNING: module not responding to AT. Check wiring/power.");
  }

  loadWiFiConfig();
  if (!connectWiFi())
  {
    startSetupAP(); // no saved network, or it is out of range
  }
  setupWebServer(); // dashboard on a normal network, setup page on the AP

  Serial.println("Send 'C1'/'C2'/'C3' to calibrate that module.");
  Serial.println("  While calibrating: 'A' = capture point, 'F' = finish & compute K");
  Serial.println("Send 'S<module>,<pressure>,<flow>' e.g. S1,1.2,4.5 to inject");
  Serial.println("  simulated readings for testing. Send 'R<module>' or 'R' (all)");
  Serial.println("  to return to real sensor readings.");
  Serial.println();
}

// ============================================================================
// MAIN LOOP
// ============================================================================
void loop()
{
  // --- Keep WiFi alive + serve web requests ---
  // handleClient() runs in both modes: on a normal network it serves the
  // dashboard, and in setup mode it serves the configuration page over the AP.
  maintainWiFi();
  webServer.handleClient();

  // --- Handle Serial commands ---
  if (Serial.available())
  {
    char cmd = Serial.read();

    if (cmd == 'C')
    {
      char digit = Serial.read();
      int idx = digit - '1';
      if (idx >= 0 && idx < NUM_MODULES)
      {
        modules[idx].calibrationMode = true;
        modules[idx].calibCount = 0;
        activeCalibModule = idx;
        Serial.printf("[CALIBRATION] Module %d - vary flow, 'A' to capture, 'F' to finish.\n", idx + 1);
      }
    }
    else if ((cmd == 'A' || cmd == 'F') && activeCalibModule >= 0)
    {
      handleCalibrationInput(modules[activeCalibModule], cmd);
    }
    else if (cmd == 'S')
    {
      char digit = Serial.read();
      int idx = digit - '1';
      if (Serial.peek() == ',')
        Serial.read();
      float p = Serial.parseFloat();
      if (Serial.peek() == ',')
        Serial.read();
      float q = Serial.parseFloat();
      if (idx >= 0 && idx < NUM_MODULES)
      {
        modules[idx].simPressure = p;
        modules[idx].simFlow = q;
        modules[idx].simulationMode = true;
        Serial.printf("[SIMULATION] Module %d - Injecting P=%.2f bar, Q=%.2f L/min\n", idx + 1, p, q);
      }
    }
    else if (cmd == 'R')
    {
      // Peek: a following digit scopes it to one module, otherwise resets all
      if (isDigit(Serial.peek()))
      {
        char digit = Serial.read();
        int idx = digit - '1';
        if (idx >= 0 && idx < NUM_MODULES)
        {
          modules[idx].simulationMode = false;
          Serial.printf("[SIMULATION] Module %d - returned to real sensor readings.\n", idx + 1);
        }
      }
      else
      {
        for (uint8_t i = 0; i < NUM_MODULES; i++)
          modules[i].simulationMode = false;
        Serial.println("[SIMULATION] All modules returned to real sensor readings.");
      }
    }
    // Any other/leftover character (e.g. newline after Enter) is simply
    // discarded by virtue of already having been consumed by Serial.read().
  }

  unsigned long now = millis();
  if (now - lastSampleMillis >= SAMPLE_INTERVAL_MS)
  {
    unsigned long elapsed = now - lastSampleMillis;
    lastSampleMillis = now;

    for (uint8_t i = 0; i < NUM_MODULES; i++)
    {
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
    Serial.println("--------------------------------------------");
  }

  // --- Upload to the Django server on its own, slower timer ---
  // Kept separate from the sampling timer so the POST rate can be tuned
  // without changing how often the sensors are read and classified.
  if (now - lastUploadMillis >= UPLOAD_INTERVAL_MS)
  {
    lastUploadMillis = now;
    uploadReadings();
  }
}
