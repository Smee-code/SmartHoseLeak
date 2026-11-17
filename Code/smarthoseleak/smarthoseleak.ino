/*
  WATER LEAK DETECTION - single-sensor, WiFi-only board
  ------------------------------------------------------
  One ESP32 on one household's hose line: a pressure sensor, a flow sensor,
  and a motorised ball valve. It connects to the household WiFi and posts
  readings to the Smart Hose Leak Django server, which owns the households,
  the history and the notifications.

  This is the data-path build: hotspot provisioning -> claim -> read sensors
  -> upload. It proves the board reaches the server and the server receives
  data. The automatic leak logic (continuous-flow staging and the nightly
  sealed-line pressure test) is layered on after the data path is confirmed.

  SETUP (one time)
    1. Power the board. With no saved WiFi it opens a hotspot named
       "LeakDetect-Setup" (password leakdetect123).
    2. Join that hotspot, open http://192.168.4.1/ in a browser.
    3. Enter your home WiFi name + password, the server URL
       (e.g. https://smarthoseleak.pythonanywhere.com) and the setup code
       the owner received. Save.
    4. The board reconnects to your WiFi, trades the code for its identity,
       and starts posting. The household's Settings page turns "Connected".

  WIRING (change the pins below if yours differ)
    Pressure sensor (analog)   -> GPIO 34   (input only)
    Flow sensor (pulse)        -> GPIO 27
    Valve relay OPEN           -> GPIO 25
    Valve relay CLOSE          -> GPIO 26
    Relays here are ACTIVE LOW (ON = LOW). Flip RELAY_ON/OFF if yours differ.

  PROTOCOL (must match the server)
    POST <server>/api/claim/   {"code":"..."} -> {device_id, api_key}
    POST <server>/api/ingest/  header X-Device-Key: <api_key>
                               {"device_id":"..","readings":[{...}]}
*/

#include <WiFi.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include <Preferences.h>

// ---------------------------------------------------------------- pins
#define PRESSURE_PIN 34
#define FLOW_PIN     27
#define RELAY_OPEN   25
#define RELAY_CLOSE  26
#define RELAY_ON     LOW      // active-low relay board; set HIGH if active-high
#define RELAY_OFF    HIGH

// ---------------------------------------------------- setup access point
// Match the server's BOARD_SETUP_SSID / BOARD_SETUP_PASSWORD so the owner's
// setup instructions name the same network.
#define SETUP_AP_SSID "LeakDetect-Setup"
#define SETUP_AP_PASS "leakdetect123"

// ------------------------------------------------- calibration (TODO: tune)
// Flow: how many pulses your sensor gives per litre. YF-S201 is ~450.
#define FLOW_PULSES_PER_LITER 450.0f
// Pressure: a straight line from ADC to bar. Read the raw ADC with no
// pressure and at a known pressure, then set these. 0..4095 is the ESP32 ADC.
#define PRESSURE_ADC_AT_ZERO 0.0f
#define PRESSURE_ADC_AT_MAX  4095.0f
#define PRESSURE_BAR_AT_MAX  1.2f

// ------------------------------------------------------------- timing
#define UPLOAD_INTERVAL_MS     2000UL
#define HTTP_TIMEOUT_MS        8000
#define WIFI_CONNECT_TIMEOUT_MS 20000UL

// ------------------------------------------------------------- state
Preferences prefs;
WebServer   web(80);

String cfgSSID, cfgPass, cfgServer, cfgCode;   // provisioning
String devId, apiKey;                          // identity after claim
bool   apMode = false;

volatile unsigned long flowPulses = 0;
unsigned long lastUploadMs   = 0;
unsigned long lastPulseReset = 0;
float  lastPressureBar = 0.0f;
float  lastFlowLpm     = 0.0f;
String lastUploadResult = "not sent yet";
String valveState = "idle";

void IRAM_ATTR onFlowPulse() { flowPulses++; }

// ======================================================= config storage
void loadConfig() {
  prefs.begin("shl", true);                     // read-only
  cfgSSID   = prefs.getString("ssid", "");
  cfgPass   = prefs.getString("pass", "");
  cfgServer = prefs.getString("server", "");
  cfgCode   = prefs.getString("code", "");
  devId     = prefs.getString("devid", "");
  apiKey    = prefs.getString("apikey", "");
  prefs.end();
}

void saveNetwork(const String &ssid, const String &pass,
                 const String &server, const String &code) {
  prefs.begin("shl", false);                    // read-write
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
  prefs.putString("code", "");                  // code is single-use; forget it
  prefs.end();
}

// Forgiving: strip trailing slash, and a trailing /api/ingest(/) if the
// person pasted the full ingest URL instead of the base.
String normaliseServer(String url) {
  url.trim();
  while (url.endsWith("/")) url.remove(url.length() - 1);
  if (url.endsWith("/api/ingest")) url.remove(url.length() - String("/api/ingest").length());
  return url;
}

// ======================================================= tiny JSON read
// The claim reply is small and flat; a full JSON library is not worth the
// flash for two string fields.
bool extractJsonString(const String &body, const char *key, String &out) {
  String needle = String("\"") + key + "\":\"";
  int at = body.indexOf(needle);
  if (at < 0) return false;
  int start = at + needle.length();
  int end = body.indexOf('"', start);
  if (end < 0) return false;
  out = body.substring(start, end);
  return true;
}

// ======================================================= HTTP helper
// A secure client for https, a plain one otherwise. setInsecure() skips
// certificate checks - fine for a hosted server behind HTTPS, and it means
// the board needs no CA bundle.
bool beginRequest(HTTPClient &http, WiFiClientSecure &tls, const String &url) {
  http.setTimeout(HTTP_TIMEOUT_MS);
  if (url.startsWith("https://")) {
    tls.setInsecure();
    return http.begin(tls, url);
  }
  return http.begin(url);
}

// ======================================================= sensors
float readPressureBar() {
  int raw = analogRead(PRESSURE_PIN);
  float span = PRESSURE_ADC_AT_MAX - PRESSURE_ADC_AT_ZERO;
  if (span <= 0) return 0.0f;
  float bar = (raw - PRESSURE_ADC_AT_ZERO) / span * PRESSURE_BAR_AT_MAX;
  if (bar < 0) bar = 0;
  return bar;
}

// Litres/minute from the pulses counted since the last call.
float readFlowLpm() {
  noInterrupts();
  unsigned long pulses = flowPulses;
  flowPulses = 0;
  interrupts();

  unsigned long now = millis();
  unsigned long elapsedMs = now - lastPulseReset;
  lastPulseReset = now;
  if (elapsedMs == 0) return 0.0f;

  float litres = pulses / FLOW_PULSES_PER_LITER;
  float minutes = elapsedMs / 60000.0f;
  return minutes > 0 ? litres / minutes : 0.0f;
}

// ======================================================= valve
void valveStop() {
  digitalWrite(RELAY_OPEN, RELAY_OFF);
  digitalWrite(RELAY_CLOSE, RELAY_OFF);
  valveState = "idle";
}
void valveOpen() {
  valveStop(); delay(200);
  digitalWrite(RELAY_OPEN, RELAY_ON);
  valveState = "opening";
}
void valveClose() {
  valveStop(); delay(200);
  digitalWrite(RELAY_CLOSE, RELAY_ON);
  valveState = "closing";
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
  if (code != 200) return false;

  String id, key;
  if (extractJsonString(resp, "device_id", id) &&
      extractJsonString(resp, "api_key", key)) {
    devId = id; apiKey = key;
    saveIdentity(id, key);
    Serial.printf("[CLAIM] device %s claimed.\n", id.c_str());
    return true;
  }
  return false;
}

// ======================================================= upload
void uploadReadings() {
  lastPressureBar = readPressureBar();
  lastFlowLpm     = readFlowLpm();

  if (apiKey.length() == 0) { lastUploadResult = "not claimed yet"; return; }
  if (WiFi.status() != WL_CONNECTED) { lastUploadResult = "WiFi down"; return; }

  String url = cfgServer + "/api/ingest/";
  String body = String("{\"device_id\":\"") + devId + "\",\"readings\":[{"
    "\"module\":1,"
    "\"pressure_bar\":" + String(lastPressureBar, 3) + ","
    "\"flow_lpm\":" + String(lastFlowLpm, 3) + ","
    "\"status\":\"NORMAL\""
    "}]}";

  WiFiClientSecure tls; HTTPClient http;
  if (!beginRequest(http, tls, url)) { lastUploadResult = "bad server URL"; return; }
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Key", apiKey);
  int code = http.POST(body);
  http.end();

  lastUploadResult = (code == 200) ? "OK" : ("HTTP " + String(code));
  Serial.printf("[UPLOAD] %s  P=%.3f bar  F=%.3f L/min\n",
                lastUploadResult.c_str(), lastPressureBar, lastFlowLpm);
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
  if (apMode)            s += "Hotspot mode. Enter your details below.";
  else if (connected)    s += "Connected to " + cfgSSID + " (" + WiFi.localIP().toString() + ").";
  else                   s += "Not connected to WiFi.";
  s += "</p>";

  s += "<p class=k>Identity: " + (devId.length() ? devId : String("not claimed")) +
       " &middot; Last upload: " + lastUploadResult +
       "<br>Pressure: " + String(lastPressureBar, 2) + " bar &middot; Flow: " +
       String(lastFlowLpm, 2) + " L/min &middot; Valve: " + valveState + "</p>";

  s += "<form method=POST action=/save>"
       "<label>Home WiFi name</label><input name=ssid value='" + cfgSSID + "'>"
       "<label>WiFi password</label><input name=pass type=password value=''>"
       "<label>Server URL</label><input name=server value='" +
       (cfgServer.length() ? cfgServer : String("https://smarthoseleak.pythonanywhere.com")) + "'>"
       "<label>Setup code</label><input name=code placeholder='e.g. K7M-4Q2'>"
       "<button type=submit>Save &amp; connect</button></form>";

  s += "<p class=k style='margin-top:1.2rem'>Valve: "
       "<a href=/valve?cmd=open>open</a> &middot; "
       "<a href=/valve?cmd=close>close</a> &middot; "
       "<a href=/valve?cmd=stop>stop</a></p>";

  web.send(200, "text/html", page(s));
}

void handleSave() {
  String ssid   = web.arg("ssid");
  String pass   = web.arg("pass");
  String server = normaliseServer(web.arg("server"));
  String code   = web.arg("code");
  if (pass.length() == 0) pass = cfgPass;        // keep the old password if blank
  saveNetwork(ssid, pass, server, code);
  web.send(200, "text/html",
           page("<h1>Saved</h1><p>The board is restarting and will connect to <b>" +
                ssid + "</b>, then claim its identity. Give it a minute, "
                "then check the household's Settings page.</p>"));
  delay(800);
  ESP.restart();
}

void handleValve() {
  String cmd = web.arg("cmd");
  if (cmd == "open") valveOpen();
  else if (cmd == "close") valveClose();
  else valveStop();
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
  if (!connectWiFi()) {
    startAP();
  } else if (apiKey.length() == 0 && cfgCode.length() > 0) {
    claimDevice();                               // first boot after setup
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
    if (c == "OPEN") valveOpen();
    else if (c == "CLOSE") valveClose();
    else if (c == "STOP") valveStop();
  }

  if (!apMode && millis() - lastUploadMs >= UPLOAD_INTERVAL_MS) {
    lastUploadMs = millis();
    // If WiFi dropped, try to bring it back without blocking for long.
    if (WiFi.status() != WL_CONNECTED) WiFi.reconnect();
    // Claim lazily if it hasn't happened yet (e.g. server was down at boot).
    if (apiKey.length() == 0 && cfgCode.length() > 0 && WiFi.status() == WL_CONNECTED) {
      claimDevice();
    }
    uploadReadings();
  }
}
