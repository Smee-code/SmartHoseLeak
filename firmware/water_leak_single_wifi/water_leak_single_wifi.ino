/*
  WATER LEAK DETECTION - single-sensor, WiFi-only board
  ------------------------------------------------------
  One ESP32 on one household's hose line: a pressure sensor, a flow sensor,
  and a motorised ball valve. It connects to the household WiFi and posts
  readings to the Smart Hose Leak Django server, which owns the households,
  the history and the notifications.

  The board DECIDES the status itself (NORMAL / WATER_RUNNING / POSSIBLE_LEAK)
  so it keeps protecting the line when the WiFi drops. The server only turns a
  reported status into texts and emails. The figures the board decides against
  live on the server (admin console) and ride back on each upload reply, so
  they can be retuned with no re-flashing; the board caches the last set it saw
  and uses that while offline.

  TWO DETECTIONS
    1. Continuous flow (staged):
         flowing non-stop >= warn minutes -> WATER_RUNNING (owner warned)
         flowing non-stop >= leak minutes -> POSSIBLE_LEAK, and (if enabled)
           the valve auto-closes to stop the waste, then auto-opens after a
           cooldown.
    2. Nightly sealed-line pressure test:
         at the quiet hour, close the valve to seal the line, wait the grace
         period, and if the pressure fell more than the threshold, that is a
         POSSIBLE_LEAK. The valve auto-opens when the test ends.

  SETUP (one time)
    1. Power the board. With no saved WiFi it opens a hotspot named
       "LeakDetect-Setup" (password leakdetect123).
    2. Join it, open http://192.168.4.1/ , enter your home WiFi, the server
       URL (e.g. https://smarthoseleak.pythonanywhere.com) and the setup code.

  WIRING (change the pins below if yours differ)
    Pressure sensor (analog)   -> GPIO 34   (input only)
    Flow sensor (pulse)        -> GPIO 27
    Valve relay OPEN           -> GPIO 25
    Valve relay CLOSE          -> GPIO 26
    Relays here are ACTIVE LOW (ON = LOW). Flip RELAY_ON/OFF if yours differ.
*/

#include <WiFi.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include <Preferences.h>
#include <time.h>

// ---------------------------------------------------------------- pins
#define PRESSURE_PIN 34
#define FLOW_PIN     27
#define RELAY_OPEN   25
#define RELAY_CLOSE  26
#define RELAY_ON     LOW      // active-low relay board; set HIGH if active-high
#define RELAY_OFF    HIGH

// ---------------------------------------------------- setup access point
#define SETUP_AP_SSID "LeakDetect-Setup"
#define SETUP_AP_PASS "leakdetect123"

// ------------------------------------------------- calibration (TODO: tune)
#define FLOW_PULSES_PER_LITER 450.0f   // YF-S201 is ~450
#define PRESSURE_ADC_AT_ZERO  0.0f
#define PRESSURE_ADC_AT_MAX   4095.0f
#define PRESSURE_BAR_AT_MAX   1.2f

// Flow at or above this (L/min) counts as "water is running". Below it the
// line is treated as idle. Set a little above your sensor's drip noise.
#define FLOW_RUNNING_LPM 0.5f          // TODO: tune

// After an auto-close for a continuous-flow leak, hold the valve shut this
// long, then auto-open so water is not cut off indefinitely. If the leak is
// real it will be detected and closed again.
#define FLOW_LEAK_REOPEN_MINUTES 10    // TODO: tune

// A few seconds after the valve seals for the nightly test, the line settles;
// the pressure read then is the baseline the drop is measured from.
#define TEST_SETTLE_SECONDS 8

// ------------------------------------------------- time (Philippines UTC+8)
#define NTP_SERVER   "pool.ntp.org"
#define TZ_OFFSET_SEC 28800
#define DST_OFFSET_SEC 0

// ------------------------------------------------------------- timing
#define UPLOAD_INTERVAL_MS      2000UL
#define HTTP_TIMEOUT_MS         8000
#define WIFI_CONNECT_TIMEOUT_MS 20000UL

// ------------------------------------------------------------- state
Preferences prefs;
WebServer   web(80);

String cfgSSID, cfgPass, cfgServer, cfgCode;   // provisioning
String devId, apiKey;                          // identity after claim
bool   apMode = false;

// Detection settings from the server (cached in NVS; defaults until a reply).
int   dsFlowWarnMin   = 30;
int   dsFlowLeakMin   = 60;
int   dsTestHour      = 2;
int   dsTestGraceMin  = 60;
float dsTestDropBar   = 0.15f;
bool  dsAutoClose     = true;

volatile unsigned long flowPulses = 0;
unsigned long lastUploadMs   = 0;
unsigned long lastPulseReset = 0;
float  lastPressureBar = 0.0f;
float  lastFlowLpm     = 0.0f;
String lastUploadResult = "not sent yet";
String reportStatus    = "NORMAL";

// Valve ownership so the two detections never fight over it.
//   ""      -> open / nobody holds it
//   "flow"  -> closed by the continuous-flow leak
//   "test"  -> closed by the nightly pressure test
String valveReason = "";
String valveState  = "idle";

// Continuous-flow tracking.
unsigned long flowStartMs  = 0;        // when non-stop flow began (0 = idle)
unsigned long flowClosedAt = 0;        // when the flow-leak close happened

// Nightly pressure test.
enum TestPhase { TEST_IDLE, TEST_SETTLING, TEST_RUNNING };
TestPhase     testPhase       = TEST_IDLE;
unsigned long testSettleUntil = 0;
unsigned long testEndMs       = 0;
float         testBaselineBar = 0.0f;
int           lastTestDay     = -1;    // tm_yday the test last ran, so once/day
bool          pressureTestLeak = false; // latched until the next test

void IRAM_ATTR onFlowPulse() { flowPulses++; }

// ======================================================= config storage
void loadConfig() {
  prefs.begin("shl", true);
  cfgSSID   = prefs.getString("ssid", "");
  cfgPass   = prefs.getString("pass", "");
  cfgServer = prefs.getString("server", "");
  cfgCode   = prefs.getString("code", "");
  devId     = prefs.getString("devid", "");
  apiKey    = prefs.getString("apikey", "");
  dsFlowWarnMin  = prefs.getInt("warnmin", 30);
  dsFlowLeakMin  = prefs.getInt("leakmin", 60);
  dsTestHour     = prefs.getInt("testhr", 2);
  dsTestGraceMin = prefs.getInt("gracemin", 60);
  dsTestDropBar  = prefs.getFloat("dropbar", 0.15f);
  dsAutoClose    = prefs.getBool("autoclose", true);
  // A valve an admin sealed by hand must stay sealed across a power cut, so
  // the "manual" ownership is remembered rather than lost on reboot.
  valveReason    = prefs.getString("valvern", "");
  prefs.end();
}

void saveNetwork(const String &ssid, const String &pass,
                 const String &server, const String &code) {
  prefs.begin("shl", false);
  prefs.putString("ssid", ssid);
  prefs.putString("pass", pass);
  prefs.putString("server", server);
  prefs.putString("code", code);
  prefs.end();
}

void saveIdentity(const String &id, const String &key) {
  prefs.begin("shl", false);
  prefs.putString("devid", id);
  prefs.putString("apikey", key);
  prefs.putString("code", "");                  // single-use; forget it
  prefs.end();
}

void saveDetectionSettings() {
  prefs.begin("shl", false);
  prefs.putInt("warnmin", dsFlowWarnMin);
  prefs.putInt("leakmin", dsFlowLeakMin);
  prefs.putInt("testhr", dsTestHour);
  prefs.putInt("gracemin", dsTestGraceMin);
  prefs.putFloat("dropbar", dsTestDropBar);
  prefs.putBool("autoclose", dsAutoClose);
  prefs.end();
}

// Remember who owns the valve, so a manual close survives a reboot.
void saveValveReason() {
  prefs.begin("shl", false);
  prefs.putString("valvern", valveReason);
  prefs.end();
}

String normaliseServer(String url) {
  url.trim();
  while (url.endsWith("/")) url.remove(url.length() - 1);
  if (url.endsWith("/api/ingest")) url.remove(url.length() - String("/api/ingest").length());
  return url;
}

// ======================================================= tiny JSON reads
// Finds the position just after "key": , skipping any whitespace the server
// puts after the colon. Django's JSON has a space there ("api_key": "...").
int jsonValueStart(const String &body, const char *key) {
  String needle = String("\"") + key + "\":";
  int at = body.indexOf(needle);
  if (at < 0) return -1;
  int p = at + needle.length();
  while (p < (int)body.length() && (body[p] == ' ' || body[p] == '\t')) p++;
  return p;
}

bool extractJsonString(const String &body, const char *key, String &out) {
  int p = jsonValueStart(body, key);
  if (p < 0 || p >= (int)body.length() || body[p] != '"') return false;
  int start = p + 1;
  int end = body.indexOf('"', start);
  if (end < 0) return false;
  out = body.substring(start, end);
  return true;
}

// Reads a bare numeric/bool value: "key": 123   "key": 1.5   "key": true
bool extractJsonRaw(const String &body, const char *key, String &out) {
  int start = jsonValueStart(body, key);
  if (start < 0) return false;
  int end = start;
  while (end < (int)body.length()) {
    char c = body[end];
    if (c == ',' || c == '}' || c == ']') break;
    end++;
  }
  out = body.substring(start, end);
  out.trim();
  return out.length() > 0;
}

bool extractJsonInt(const String &body, const char *key, int &out) {
  String v; if (!extractJsonRaw(body, key, v)) return false; out = v.toInt(); return true;
}
bool extractJsonFloat(const String &body, const char *key, float &out) {
  String v; if (!extractJsonRaw(body, key, v)) return false; out = v.toFloat(); return true;
}
bool extractJsonBool(const String &body, const char *key, bool &out) {
  String v; if (!extractJsonRaw(body, key, v)) return false; out = (v == "true"); return true;
}

// Adopt the detection settings the server sent on the upload reply, and cache
// them so they survive a reboot and are used while offline.
void applyServerConfig(const String &resp) {
  bool changed = false;
  int iv; float fv; bool bv;
  if (extractJsonInt(resp,   "flow_warn_minutes", iv) && iv != dsFlowWarnMin)  { dsFlowWarnMin = iv;  changed = true; }
  if (extractJsonInt(resp,   "flow_leak_minutes", iv) && iv != dsFlowLeakMin)  { dsFlowLeakMin = iv;  changed = true; }
  if (extractJsonInt(resp,   "valve_test_hour",   iv) && iv != dsTestHour)     { dsTestHour = iv;     changed = true; }
  if (extractJsonInt(resp,   "valve_test_grace_minutes", iv) && iv != dsTestGraceMin) { dsTestGraceMin = iv; changed = true; }
  if (extractJsonFloat(resp, "valve_test_drop_bar", fv) && fv != dsTestDropBar){ dsTestDropBar = fv;  changed = true; }
  if (extractJsonBool(resp,  "auto_close_on_flow_leak", bv) && bv != dsAutoClose){ dsAutoClose = bv;  changed = true; }
  if (changed) {
    saveDetectionSettings();
    Serial.printf("[CFG] warn=%d leak=%d testHr=%d grace=%d drop=%.2f auto=%d\n",
                  dsFlowWarnMin, dsFlowLeakMin, dsTestHour, dsTestGraceMin, dsTestDropBar, dsAutoClose);
  }

  // Manual valve command from the admin console. A close takes ownership
  // ("manual") so the flow and nightly-test logic leave it sealed; an open
  // releases it back to automatic. Acted on only when the valve is not
  // already where the command wants it, so the command riding out on every
  // reply until the server sees it does not re-pulse the relay each cycle.
  String vcmd;
  if (extractJsonString(resp, "valve_command", vcmd)) {
    if (vcmd == "close" && valveState != "closed") {
      valveCloseNow(); valveReason = "manual"; saveValveReason();
      Serial.println("[VALVE] manual close (admin)");
    } else if (vcmd == "open" && valveState != "open") {
      valveOpenNow(); valveReason = ""; flowStartMs = 0; pressureTestLeak = false;
      saveValveReason();
      Serial.println("[VALVE] manual open (admin)");
    }
  }
}

// ======================================================= HTTP helper
bool beginRequest(HTTPClient &http, WiFiClientSecure &tls, const String &url) {
  http.setTimeout(HTTP_TIMEOUT_MS);
  if (url.startsWith("https://")) { tls.setInsecure(); return http.begin(tls, url); }
  return http.begin(url);
}

// ======================================================= sensors
void readSensors() {
  int raw = analogRead(PRESSURE_PIN);
  float span = PRESSURE_ADC_AT_MAX - PRESSURE_ADC_AT_ZERO;
  float bar = span > 0 ? (raw - PRESSURE_ADC_AT_ZERO) / span * PRESSURE_BAR_AT_MAX : 0.0f;
  lastPressureBar = bar < 0 ? 0 : bar;

  noInterrupts();
  unsigned long pulses = flowPulses;
  flowPulses = 0;
  interrupts();
  unsigned long now = millis();
  unsigned long elapsedMs = now - lastPulseReset;
  lastPulseReset = now;
  float minutes = elapsedMs / 60000.0f;
  lastFlowLpm = minutes > 0 ? (pulses / FLOW_PULSES_PER_LITER) / minutes : 0.0f;
}

// ======================================================= valve
void valveStop() {
  digitalWrite(RELAY_OPEN, RELAY_OFF);
  digitalWrite(RELAY_CLOSE, RELAY_OFF);
  valveState = (valveReason == "") ? "idle" : valveState;
}
void valveOpenNow() {
  valveStop(); delay(200);
  digitalWrite(RELAY_OPEN, RELAY_ON);
  delay(600);                      // let the actuator travel, then release
  digitalWrite(RELAY_OPEN, RELAY_OFF);
  valveState = "open";
}
void valveCloseNow() {
  valveStop(); delay(200);
  digitalWrite(RELAY_CLOSE, RELAY_ON);
  delay(600);
  digitalWrite(RELAY_CLOSE, RELAY_OFF);
  valveState = "closed";
}

// ======================================================= detection
// Continuous-flow staging and the auto-close/auto-open around a flow leak.
void updateFlowDetection() {
  unsigned long now = millis();
  bool flowing = (lastFlowLpm >= FLOW_RUNNING_LPM);

  // Reopen a flow-leak close once the cooldown has passed.
  if (valveReason == "flow" &&
      now - flowClosedAt >= (unsigned long)FLOW_LEAK_REOPEN_MINUTES * 60000UL) {
    valveOpenNow();
    valveReason = "";
    flowStartMs = 0;               // give the line a fresh start
  }

  if (flowing) {
    if (flowStartMs == 0) flowStartMs = now;
    unsigned long mins = (now - flowStartMs) / 60000UL;
    if (mins >= (unsigned long)dsFlowLeakMin) {
      if (dsAutoClose && valveReason == "") {    // auto-close on the flow leak
        valveCloseNow();
        valveReason = "flow";
        flowClosedAt = now;
      }
    }
  } else {
    // Idle again. If nothing is holding the valve shut, the flow run is over.
    if (valveReason != "flow") flowStartMs = 0;
  }
}

// The nightly sealed-line pressure test.
void updatePressureTest() {
  struct tm t;
  if (!getLocalTime(&t, 50)) return;             // no clock yet (NTP not synced)
  unsigned long now = millis();

  if (testPhase == TEST_IDLE) {
    bool dueNow = (t.tm_hour == dsTestHour) && (t.tm_yday != lastTestDay);
    if (dueNow && valveReason == "") {
      valveCloseNow();                           // seal the line
      valveReason = "test";
      testPhase = TEST_SETTLING;
      testSettleUntil = now + (unsigned long)TEST_SETTLE_SECONDS * 1000UL;
      lastTestDay = t.tm_yday;
      pressureTestLeak = false;                  // re-decide this run
      Serial.println("[TEST] sealed the line.");
    }
  } else if (testPhase == TEST_SETTLING) {
    if (now >= testSettleUntil) {
      testBaselineBar = lastPressureBar;         // baseline after it settles
      testEndMs = now + (unsigned long)dsTestGraceMin * 60000UL;
      testPhase = TEST_RUNNING;
      Serial.printf("[TEST] baseline %.3f bar, watching for %d min.\n",
                    testBaselineBar, dsTestGraceMin);
    }
  } else { // TEST_RUNNING
    if (now >= testEndMs) {
      float drop = testBaselineBar - lastPressureBar;
      pressureTestLeak = (drop > dsTestDropBar);
      Serial.printf("[TEST] drop %.3f bar -> %s\n",
                    drop, pressureTestLeak ? "LEAK" : "ok");
      valveOpenNow();                            // auto-open when the test ends
      valveReason = "";
      testPhase = TEST_IDLE;
    }
  }
}

// What to report this cycle. A latched pressure-test leak wins; otherwise the
// flow run's duration decides.
String computeReportStatus() {
  if (pressureTestLeak) return "POSSIBLE_LEAK";
  if (flowStartMs != 0) {
    unsigned long mins = (millis() - flowStartMs) / 60000UL;
    if (mins >= (unsigned long)dsFlowLeakMin) return "POSSIBLE_LEAK";
    if (mins >= (unsigned long)dsFlowWarnMin) return "WATER_RUNNING";
  }
  return "NORMAL";
}

// ======================================================= claim
bool claimDevice() {
  if (cfgServer.length() == 0 || cfgCode.length() == 0) return false;
  String url = cfgServer + "/api/claim/";
  String body = String("{\"code\":\"") + cfgCode + "\"}";

  WiFiClientSecure tls; HTTPClient http;
  if (!beginRequest(http, tls, url)) return false;
  http.addHeader("Content-Type", "application/json");
  int code = http.POST(body);
  String resp = http.getString();
  http.end();

  Serial.printf("[CLAIM] %d %s\n", code, resp.c_str());

  // A used (409) or expired (410) code will never work again. Forget it so
  // the board stops retrying; the owner enters a fresh code on the setup page.
  if (code == 409 || code == 410) {
    cfgCode = "";
    prefs.begin("shl", false);
    prefs.putString("code", "");
    prefs.end();
    Serial.println("[CLAIM] code is spent - get a new one and re-enter it.");
    return false;
  }
  if (code != 200) return false;

  String id, key;
  if (extractJsonString(resp, "device_id", id) &&
      extractJsonString(resp, "api_key", key)) {
    devId = id; apiKey = key;
    saveIdentity(id, key);
    Serial.printf("[CLAIM] device %s claimed.\n", id.c_str());
    return true;
  }
  Serial.println("[CLAIM] got 200 but could not read the identity - check the reply format.");
  return false;
}

// ======================================================= upload
void uploadReadings() {
  if (apiKey.length() == 0) { lastUploadResult = "not claimed yet"; return; }
  if (WiFi.status() != WL_CONNECTED) { lastUploadResult = "WiFi down"; return; }

  String url = cfgServer + "/api/ingest/";
  String body = String("{\"device_id\":\"") + devId + "\",\"readings\":[{"
    "\"module\":1,"
    "\"pressure_bar\":" + String(lastPressureBar, 3) + ","
    "\"flow_lpm\":" + String(lastFlowLpm, 3) + ","
    "\"status\":\"" + reportStatus + "\","
    "\"valve_state\":\"" + String(valveState == "closed" ? "closed" : "open") + "\""
    "}]}";

  WiFiClientSecure tls; HTTPClient http;
  if (!beginRequest(http, tls, url)) { lastUploadResult = "bad server URL"; return; }
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Key", apiKey);
  int code = http.POST(body);
  String resp = (code == 200) ? http.getString() : "";
  http.end();

  lastUploadResult = (code == 200) ? "OK" : ("HTTP " + String(code));
  if (code == 200) applyServerConfig(resp);      // adopt any retuned settings
  Serial.printf("[UPLOAD] %s  %s  P=%.3f bar  F=%.3f L/min\n",
                lastUploadResult.c_str(), reportStatus.c_str(),
                lastPressureBar, lastFlowLpm);
}

// ======================================================= web pages
String page(const String &inner) {
  return "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
         "<style>body{font-family:system-ui,sans-serif;max-width:30rem;margin:1.5rem auto;padding:0 1rem;color:#16232e}"
         "h1{font-size:1.2rem}label{display:block;margin:.6rem 0 .2rem;font-weight:600}"
         "input{width:100%;padding:.5rem;font-size:16px;border:1px solid #ccc;border-radius:6px;box-sizing:border-box}"
         "button{margin-top:1rem;padding:.6rem 1rem;font-size:16px;border:0;border-radius:6px;background:#0b6ea8;color:#fff}"
         ".k{color:#5b6b7a;font-size:.9rem}</style>" + inner;
}

void handleRoot() {
  bool connected = (WiFi.status() == WL_CONNECTED);
  String s = "<h1>Smart Hose Leak - board setup</h1>";
  s += "<p class=k>";
  if (apMode)         s += "Hotspot mode. Enter your details below.";
  else if (connected) s += "Connected to " + cfgSSID + " (" + WiFi.localIP().toString() + ").";
  else                s += "Not connected to WiFi.";
  s += "</p>";

  s += "<p class=k>Identity: " + (devId.length() ? devId : String("not claimed")) +
       " &middot; Last upload: " + lastUploadResult + " &middot; Status: " + reportStatus +
       "<br>Pressure: " + String(lastPressureBar, 2) + " bar &middot; Flow: " +
       String(lastFlowLpm, 2) + " L/min &middot; Valve: " + valveState +
       " (" + (valveReason == "" ? "auto" : valveReason) + ")</p>";

  s += "<form method=POST action=/save>"
       "<label>Home WiFi name</label><input name=ssid value='" + cfgSSID + "'>"
       "<label>WiFi password</label><input name=pass type=password value=''>"
       "<label>Server URL</label><input name=server value='" +
       (cfgServer.length() ? cfgServer : String("https://smarthoseleak.pythonanywhere.com")) + "'>"
       "<label>Setup code</label><input name=code placeholder='e.g. K7M-4Q2'>"
       "<button type=submit>Save &amp; connect</button></form>";

  s += "<p class=k style='margin-top:1.2rem'>Valve: "
       "<a href=/valve?cmd=open>open</a> &middot; "
       "<a href=/valve?cmd=close>close</a></p>";

  web.send(200, "text/html", page(s));
}

void handleSave() {
  String ssid   = web.arg("ssid");
  String pass   = web.arg("pass");
  String server = normaliseServer(web.arg("server"));
  String code   = web.arg("code");
  if (pass.length() == 0) pass = cfgPass;
  saveNetwork(ssid, pass, server, code);
  web.send(200, "text/html",
           page("<h1>Saved</h1><p>The board is restarting and will connect to <b>" +
                ssid + "</b>, then claim its identity. Give it a minute, then check "
                "the household's Settings page.</p>"));
  delay(800);
  ESP.restart();
}

// Manual override from the status page. Clears any auto reason so a person is
// in charge until the next automatic action.
void handleValve() {
  String cmd = web.arg("cmd");
  // Local close latches the same way an admin close does, so a valve shut
  // from the board's own page also stays shut across a reboot.
  if (cmd == "open")  { valveOpenNow();  valveReason = ""; flowStartMs = 0; pressureTestLeak = false; saveValveReason(); }
  else if (cmd == "close") { valveCloseNow(); valveReason = "manual"; saveValveReason(); }
  web.sendHeader("Location", "/");
  web.send(303, "text/plain", "");
}

// ======================================================= WiFi bring-up
void startAP() {
  apMode = true;
  WiFi.mode(WIFI_AP);
  WiFi.softAP(SETUP_AP_SSID, SETUP_AP_PASS);
  Serial.printf("[AP] %s at %s\n", SETUP_AP_SSID, WiFi.softAPIP().toString().c_str());
}

bool connectWiFi() {
  if (cfgSSID.length() == 0) return false;
  apMode = false;
  WiFi.mode(WIFI_STA);
  WiFi.begin(cfgSSID.c_str(), cfgPass.c_str());
  Serial.printf("[WIFI] joining %s", cfgSSID.c_str());
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < WIFI_CONNECT_TIMEOUT_MS) {
    delay(400); Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[WIFI] got %s\n", WiFi.localIP().toString().c_str());
    configTime(TZ_OFFSET_SEC, DST_OFFSET_SEC, NTP_SERVER);  // for the nightly test
    return true;
  }
  Serial.println("[WIFI] failed - opening setup hotspot.");
  return false;
}

// ======================================================= setup / loop
void setup() {
  Serial.begin(115200);
  delay(200);

  pinMode(PRESSURE_PIN, INPUT);
  pinMode(FLOW_PIN, INPUT_PULLUP);
  pinMode(RELAY_OPEN, OUTPUT);
  pinMode(RELAY_CLOSE, OUTPUT);
  valveStop();
  attachInterrupt(digitalPinToInterrupt(FLOW_PIN), onFlowPulse, FALLING);
  lastPulseReset = millis();

  loadConfig();
  // A line the admin sealed stays sealed through a power cut: re-close it
  // before anything else can decide the valve should be open.
  if (valveReason == "manual") {
    valveCloseNow();
    Serial.println("[VALVE] restored manual close after reboot");
  }
  if (!connectWiFi()) {
    startAP();
  } else if (apiKey.length() == 0 && cfgCode.length() > 0) {
    claimDevice();
  }

  web.on("/", handleRoot);
  web.on("/save", HTTP_POST, handleSave);
  web.on("/valve", handleValve);
  web.begin();
  Serial.println("[WEB] setup/status page on port 80.");
}

void loop() {
  web.handleClient();

  // Manual serial valve control, handy on the bench.
  if (Serial.available()) {
    String c = Serial.readStringUntil('\n');
    c.trim(); c.toUpperCase();
    if (c == "OPEN")  { valveOpenNow();  valveReason = ""; saveValveReason(); }
    else if (c == "CLOSE") { valveCloseNow(); valveReason = "manual"; saveValveReason(); }
  }

  if (!apMode && millis() - lastUploadMs >= UPLOAD_INTERVAL_MS) {
    lastUploadMs = millis();
    if (WiFi.status() != WL_CONNECTED) WiFi.reconnect();
    if (apiKey.length() == 0 && cfgCode.length() > 0 && WiFi.status() == WL_CONNECTED) {
      claimDevice();
    }

    // Detection runs every cycle, online or not, so the line stays protected
    // when the WiFi is down.
    readSensors();
    updateFlowDetection();
    updatePressureTest();
    reportStatus = computeReportStatus();

    uploadReadings();          // sends the decided status; adopts any new config
  }
}
