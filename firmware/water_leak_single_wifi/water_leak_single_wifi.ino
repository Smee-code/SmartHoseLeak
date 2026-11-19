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

  HOW OFTEN IT TALKS TO THE SERVER
    Sensors are read and the detections run every 2 s, online or not. A
    routine reading is uploaded every "upload interval" seconds (15 by
    default; set on the admin console's System page and carried back on
    the upload reply, so no re-flash). A change of status, of valve state or
    of valve reason is uploaded at once. A manual valve button does not wait
    for an upload either: while somebody is at a valve screen the server
    says so (command_poll_seconds) and the board asks /api/commands/ every
    two seconds for a pending open/close, so a press reaches the valve in
    under three seconds; when nobody is, it stops asking. This is what lets
    one hosted server carry many boards inside its daily CPU allowance.

  TWO DETECTIONS
    1. Continuous flow (staged):
         flowing non-stop >= warn minutes -> WATER_RUNNING (owner warned)
         flowing non-stop >= leak minutes -> POSSIBLE_LEAK, and (if enabled)
           the valve auto-closes to stop the waste, then auto-opens after a
           cooldown.
    2. Nightly sealed-line pressure test:
         at the quiet hour, close the valve to seal the line, wait the grace
         period, and if the pressure fell more than the threshold, that is a
         POSSIBLE_LEAK and the valve STAYS CLOSED until somebody opens it
         (portal, dashboard, console, this board's page, or serial OPEN).
         No drop: the valve auto-opens when the test ends.

  SETUP (one time)
    1. Power the board. With no saved WiFi it opens a hotspot named
       "LeakDetect-Setup" (password leakdetect123).
    2. Join it, open http://192.168.4.1/ , enter your home WiFi and the setup
       code. Leave the server box EMPTY when the server is the laptop on the
       same network - the board finds it by itself (see below). Type the
       address only for a server elsewhere (e.g. https://smarthoseleak.pythonanywhere.com).

  SETTING UP FROM THE OWNER PORTAL
    The portal's "Set up my board" gives the owner a link to this board's
    page with the WiFi, server and setup code already in the query string
    (http://192.168.4.1/?ssid=..&pass=..&server=..&code=..). The page shows
    them filled in and the owner only presses Save & connect.

  CHANGING WIFI WHILE ONLINE (from the portal)
    The upload reply can carry wifi_ssid / wifi_password / wifi_change. The
    board tries the new network for up to 25 s; if it joins, it saves it;
    if not, it goes back to the old one. Either way the next upload reports
    wifi_change (the id) and wifi_ok, once, and every upload reports the
    network it is on (wifi_ssid). The id is remembered so the same request
    is never tried twice.

  MOVING THE BOARD TO ANOTHER NETWORK
    The saved network is gone, so about a minute after power-up the board
    raises the hotspot again (while still trying the old network) and you
    repeat step 2. Leaving the password blank keeps the saved one; leaving
    the setup code blank keeps the board's identity, so the household's
    history continues. Once the new network is joined the hotspot closes.

  FINDING THE SERVER (laptop on the same WiFi)
    A laptop's address changes with every network it joins, so a board that
    simply remembered "http://192.168.1.21:8000" would post into the void
    after a move - the WiFi joins, nothing arrives. Instead the board asks:
    it broadcasts "SHL-DISCOVER?" on UDP 18830 and the server (started with
    manage.py runserver 0.0.0.0:8000) answers with its address, which the
    board adopts and saves. It asks at boot when it has no address or a
    laptop-style http:// one, and again whenever three uploads in a row get
    no reply (at most once per 30 s). A public https:// address is left alone.

  WIRING (change the pins below if yours differ)
    Pressure sensor (analog)   -> GPIO 34   (input only)
    Flow sensor (pulse)        -> GPIO 27
    Valve relay OPEN           -> GPIO 26
    Valve relay CLOSE          -> GPIO 25
    (Swapped 15 Nov 2026 to match the board as wired: the valve's CLOSE
    wire sits on the relay driven by GPIO 25. If a board is wired the
    other way round, swap these two numbers rather than the wires.)
    Relays here are ACTIVE LOW (ON = LOW). Flip RELAY_ON/OFF if yours differ.

  THE VALVE
    A three-wire motorised ball valve (ACARPS, DC 12-24 V, "Running Time
    15S": N = common, CLOSE, OPEN). It moves only WHILE power is held on the
    CLOSE or OPEN wire, and its own limit switch stops the motor at the end
    of travel - so each relay is held for VALVE_TRAVEL_MS (rated time plus a
    margin; holding past the limit switch is harmless). The board has no
    feedback from the valve: it reports "closed" because it held the close
    relay for the full travel, not because it measured the ball. A valve
    with a different running time: change VALVE_TRAVEL_MS.
    Wiring: supply + -> relay COM (both channels); relay 1 NO -> CLOSE wire;
    relay 2 NO -> OPEN wire; supply - -> valve N (common).
*/

#include <WiFi.h>
#include <WiFiUdp.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include <Preferences.h>
#include <time.h>
#include <esp_system.h>   // esp_reset_reason()

// ---------------------------------------------------------------- pins
#define PRESSURE_PIN 34
#define FLOW_PIN     27
#define RELAY_OPEN   26
#define RELAY_CLOSE  25
#define RELAY_ON     LOW      // active-low relay board; set HIGH if active-high
#define RELAY_OFF    HIGH
// How long a relay is held for one full open or close. The valve's rated
// running time is 15 s; the margin covers a cold motor and a low supply, and
// the valve's limit switch cuts the motor once it is there, so extra time
// costs nothing. 600 ms - the old value - turned the ball a few degrees and
// stopped, while the board still reported "closed".
#define VALVE_TRAVEL_MS 18000UL

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
// A flow-leak close reopens by itself after flow_leak_reopen_minutes (a
// console setting; 10 by default, 0 = never) and can be opened from the
// portal sooner. Until then the line stays POSSIBLE_LEAK and the valve shut.

// A few seconds after the valve seals for the nightly test, the line settles;
// the pressure read then is the baseline the drop is measured from.
#define TEST_SETTLE_SECONDS 8

// ------------------------------------------------- time (Philippines UTC+8)
#define NTP_SERVER   "pool.ntp.org"
#define TZ_OFFSET_SEC 28800
#define DST_OFFSET_SEC 0

// ------------------------------------------------------------- timing
#define SAMPLE_INTERVAL_MS      2000UL   // sensors + detections
#define NET_REPORT_MS          30000UL   // one plain "[NET] connected / NOT connected" line on the serial monitor
#define UNAUTH_BEFORE_RESET    3         // HTTP 401s in a row before the board forgets its identity
#define DEFAULT_UPLOAD_SEC      15       // until the server says otherwise
#define CHANGE_RETRY_MS         2000UL   // a changed state not yet accepted
#define HTTP_TIMEOUT_MS         8000
#define WIFI_CONNECT_TIMEOUT_MS 20000UL

// While running: retry a lost network every 10 s; after 6 misses in a row
// (about a minute) raise the setup hotspot beside the station so the board
// can be re-pointed without a power cycle. While the hotspot is up the saved
// network is still tried every minute, and the hotspot closes when it joins.
// Every try is a scan of all channels, during which the hotspot's own link
// stalls - a phone on it sees pages arrive cut short (the setup page came up
// without its Save & connect button, 19 Nov 2026). So while a phone is on
// the hotspot the tries are spaced out to every 5 minutes, and the WiFi
// stack's own background reconnect is switched off for as long as the
// hotspot is up (it is turned back on when the board joins).
#define WIFI_RETRY_INTERVAL_MS  10000UL
#define WIFI_FAILS_BEFORE_AP    6
#define WIFI_RETRY_IN_AP_MS     60000UL
#define WIFI_RETRY_WITH_PHONE_MS (5UL * 60UL * 1000UL)
// Once the setup hotspot is up it stays up for at least this long, and for
// as long as a phone is on it - even after the board has joined its network.
// It used to close the moment the board joined, which could be under a
// minute after a boot, before the person had even found it on their phone.
#define SETUP_HOTSPOT_MIN_MS     (5UL * 60UL * 1000UL)
// A board that has been without its network this long is restarted, unless
// somebody is on its hotspot. After a long outage (router rebooted, power
// back before the router was up) the ESP32's WiFi stack sometimes never
// re-associates however often begin() is called, while a fresh boot joins
// first time. Seen 14 Nov 2026: 26 h offline, fixed by a power cycle.
#define WIFI_RESTART_AFTER_MS   (15UL * 60UL * 1000UL)

// Server discovery (see the header). Matches core/discovery.py on the server.
#define DISCOVERY_PORT          18830
#define DISCOVERY_PROBE         "SHL-DISCOVER?"
#define DISCOVERY_REPLY_WAIT_MS 700
#define DISCOVERY_AFTER_FAILURES 3
#define DISCOVERY_MIN_GAP_MS    30000UL

// ------------------------------------------------------------- state
Preferences prefs;
WebServer   web(80);

String cfgSSID, cfgPass, cfgServer, cfgCode;   // provisioning
String devId, apiKey;                          // identity after claim
bool   apMode = false;
unsigned long apStartedMs = 0;                 // when the hotspot came up (for SETUP_HOTSPOT_MIN_MS)

uint8_t       wifiFailCount   = 0;
unsigned long lastWifiRetryMs = 0;
unsigned long wifiDownSince   = 0;             // millis() when the network was last seen to drop
uint8_t       uploadFailStreak = 0;            // transport failures in a row
uint8_t       unauthStreak     = 0;            // HTTP 401s in a row: the server no longer knows our key
unsigned long lastNetReportMs  = 0;
unsigned long lastDiscoveryMs  = 0;
String        lastDiscoveryResult = "not needed yet";
String        lastWifiChangeId;                 // the last change tried (persisted)
String        reportWifiId;                    // report this id on the next upload...
bool          reportWifiOk = false;            // ...with this outcome

// Detection settings from the server (cached in NVS; defaults until a reply).
int   dsFlowWarnMin   = 30;
int   dsFlowLeakMin   = 60;
int   dsTestHour      = 2;
int   dsUploadSec     = DEFAULT_UPLOAD_SEC;
int   dsTestGraceMin  = 60;
float dsTestDropBar   = 0.15f;
bool  dsAutoClose     = true;
int   dsReopenMin     = 10;      // a flow-leak close reopens by itself after this (0 = never)

volatile unsigned long flowPulses = 0;
unsigned long lastSampleMs   = 0;
unsigned long lastUploadMs   = 0;
unsigned long lastPollMs     = 0;
int           cmdPollSec     = 0;      // >0 while somebody is at a valve screen
// What the server last accepted, so a change is noticed and sent at once.
String sentStatus, sentValve, sentReason;
unsigned long lastPulseReset = 0;
float  lastPressureBar = 0.0f;
float  lastFlowLpm     = 0.0f;
String lastUploadResult = "not sent yet";
// Why the chip last started: "power-on", "brownout", "software restart",
// "watchdog", "crash"... Printed first thing on the serial monitor, shown on
// the board's page, and sent once per boot (boot_reason) so the server can
// say it on the device's row. A board that dies whenever the valve motor
// starts reports "brownout" here, which is the difference between a wiring
// fault and a mystery.
String bootReason   = "unknown";
bool   bootReported = false;                     // sent on the first accepted upload
String reportStatus    = "NORMAL";

// Valve ownership so the two detections never fight over it.
//   ""       -> open / nobody holds it
//   "flow"   -> closed by the continuous-flow leak; held shut until somebody opens it
//               or flow_leak_reopen_minutes pass (0 = never)
//   "test"   -> closed while the nightly pressure test runs
//   "decay"  -> the test found a leak; held shut until somebody opens it
//   "manual" -> closed by a person; held shut until somebody opens it
String valveReason = "";

// A test asked for from the dashboard ("Test valve"), run on the next cycle
// instead of waiting for the quiet hour.
bool testRequested = false;
String valveState  = "idle";

// Continuous-flow tracking.
unsigned long flowStartMs  = 0;        // when non-stop flow began (0 = idle)
bool          flowLeakHeld = false;    // the flow leak shut the valve; POSSIBLE_LEAK until opened
unsigned long flowClosedAt = 0;        // when that close happened (for the timed reopen)

// Nightly pressure test.
enum TestPhase { TEST_IDLE, TEST_SETTLING, TEST_RUNNING };
TestPhase     testPhase       = TEST_IDLE;
unsigned long testSettleUntil = 0;
unsigned long testEndMs       = 0;
float         testBaselineBar = 0.0f;
int           lastTestDay     = -1;    // tm_yday the test last ran, so once/day
bool          pressureTestLeak = false; // latched until the next test
float         lastTestDropBar  = 0.0f;  // how far the last test saw the pressure fall

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
  dsUploadSec    = prefs.getInt("upsec", DEFAULT_UPLOAD_SEC);
  dsTestGraceMin = prefs.getInt("gracemin", 60);
  dsTestDropBar  = prefs.getFloat("dropbar", 0.15f);
  dsAutoClose    = prefs.getBool("autoclose", true);
  dsReopenMin    = prefs.getInt("reopenmin", 10);
  // A valve an admin sealed by hand must stay sealed across a power cut, so
  // the "manual" ownership is remembered rather than lost on reboot.
  valveReason    = prefs.getString("valvern", "");
  lastWifiChangeId = prefs.getString("wifiid", "");
  prefs.end();
}

void saveWifiChangeId(const String &id) {
  prefs.begin("shl", false);
  prefs.putString("wifiid", id);
  prefs.end();
}

// A quote or a backslash in a WiFi name would break the JSON it rides in.
String jsonEscape(String v) {
  v.replace("\\", "\\\\"); v.replace("\"", "\\\""); return v;
}
// ...and in the setup page's value="..." attributes.
String htmlEscape(String v) {
  v.replace("&", "&amp;"); v.replace("\"", "&quot;"); v.replace("<", "&lt;"); v.replace("'", "&#39;"); return v;
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
  prefs.putInt("upsec", dsUploadSec);
  prefs.putInt("gracemin", dsTestGraceMin);
  prefs.putFloat("dropbar", dsTestDropBar);
  prefs.putBool("autoclose", dsAutoClose);
  prefs.putInt("reopenmin", dsReopenMin);
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

void saveServer(const String &server) {
  prefs.begin("shl", false);
  prefs.putString("server", server);
  prefs.end();
}

// A plain http:// address is a laptop on some local network, and a laptop's
// address moves. A public https:// address was typed on purpose and stays.
bool serverIsLocal() {
  return cfgServer.length() == 0 || cfgServer.startsWith("http://");
}

// ======================================================= server discovery
// Broadcasts a short probe; the server replies {"ingest":"http://.../api/ingest/"}.
// Returns true when a new address was adopted (and saved).
bool discoverServer() {
  if (WiFi.status() != WL_CONNECTED) return false;
  lastDiscoveryMs = millis();

  WiFiUDP udp;
  if (!udp.begin(0)) { lastDiscoveryResult = "could not open a socket"; return false; }

  IPAddress broadcast = WiFi.localIP();
  broadcast[3] = 255;                         // this subnet's broadcast address
  udp.beginPacket(broadcast, DISCOVERY_PORT);
  udp.print(DISCOVERY_PROBE);
  udp.endPacket();

  unsigned long deadline = millis() + DISCOVERY_REPLY_WAIT_MS;
  while ((long)(millis() - deadline) < 0) {
    int size = udp.parsePacket();
    if (size <= 0) { delay(10); continue; }
    char buffer[192];
    int n = udp.read(buffer, sizeof(buffer) - 1);
    if (n <= 0) continue;
    buffer[n] = '\0';

    String found;
    if (!extractJsonString(String(buffer), "ingest", found)) continue;
    found = normaliseServer(found);
    if (!found.startsWith("http://") && !found.startsWith("https://")) continue;
    udp.stop();

    uploadFailStreak = 0;
    if (found == cfgServer) { lastDiscoveryResult = "server confirmed " + found; return false; }
    Serial.printf("[DISCOVER] Server found at %s (was \"%s\")\n", found.c_str(), cfgServer.c_str());
    cfgServer = found;
    saveServer(found);
    lastDiscoveryResult = "adopted " + found;
    return true;
  }
  udp.stop();
  lastDiscoveryResult = "no server answered";
  Serial.println("[DISCOVER] No server answered on this network.");
  return false;
}

// Ask again when uploads keep getting no reply at all - the laptop moved.
// A 401 or a 404 is a reply, so it never triggers this; that is a key or a
// path problem, and asking the network would not fix it.
void maybeRediscover() {
  if (!serverIsLocal()) return;
  bool lost = uploadFailStreak >= DISCOVERY_AFTER_FAILURES;
  if (!lost && cfgServer.length() > 0) return;
  if (millis() - lastDiscoveryMs < DISCOVERY_MIN_GAP_MS && lastDiscoveryMs != 0) return;
  discoverServer();
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
  if (extractJsonInt(resp,   "flow_leak_reopen_minutes", iv) && iv >= 0 && iv != dsReopenMin) { dsReopenMin = iv; changed = true; }
  if (extractJsonInt(resp,   "upload_interval_seconds", iv) && iv >= 2 && iv != dsUploadSec) { dsUploadSec = iv; changed = true; }
  // Not saved: whether a person is at a valve screen right now is not worth
  // remembering across a reboot.
  if (extractJsonInt(resp,   "command_poll_seconds", iv)) {
    if (iv > 0 && cmdPollSec == 0) Serial.println("[POLL] somebody is at a valve screen - asking for commands every " + String(iv) + " s");
    if (iv == 0 && cmdPollSec > 0) Serial.println("[POLL] nobody at a valve screen - back to uploads only");
    cmdPollSec = iv;
  }
  if (changed) {
    saveDetectionSettings();
    Serial.printf("[CFG] warn=%d leak=%d testHr=%d grace=%d drop=%.2f auto=%d upload=%ds\n",
                  dsFlowWarnMin, dsFlowLeakMin, dsTestHour, dsTestGraceMin, dsTestDropBar, dsAutoClose, dsUploadSec);
  }

  // Manual valve command from the server - the administrator, the
  // facilitator or the household pressed a button; the server knows which
  // and says so on screen. A close takes ownership ("manual") so the flow
  // and nightly-test logic leave it sealed; an open releases it back to
  // automatic. Acted on only when the valve is not
  // already where the command wants it, so the command riding out on every
  // reply until the server sees it does not re-pulse the relay each cycle.
  // "Test valve" pressed on the dashboard: run the sealed-line test now.
  // Only when idle and nothing holds the valve - a line somebody sealed is
  // not reopened by a test, and a test is not restarted by its own echo
  // (the server keeps sending the flag until it sees the valve leave open).
  bool wantTest;
  if (extractJsonBool(resp, "run_valve_test", wantTest) && wantTest &&
      testPhase == TEST_IDLE && valveReason == "") {
    if (!testRequested) Serial.println("[TEST] requested from the dashboard.");
    testRequested = true;
  }

  // A WiFi change asked for from the portal, identified by wifi_change so
  // the same request is tried once even though it rides on every reply
  // until the server sees the report.
  String changeId;
  if (extractJsonString(resp, "wifi_change", changeId) && changeId.length() &&
      changeId != lastWifiChangeId) {
    String nssid, npass;
    extractJsonString(resp, "wifi_ssid", nssid);
    extractJsonString(resp, "wifi_password", npass);
    if (nssid.length()) changeWifi(changeId, nssid, npass);
  }

  String vcmd;
  if (extractJsonString(resp, "valve_command", vcmd)) {
    if (vcmd == "close" && valveState != "closed") {
      valveCloseNow(); valveReason = "manual"; saveValveReason();
      Serial.println("[VALVE] manual close (from the server)");
    } else if (vcmd == "open" && valveState != "open") {
      valveOpenNow(); valveReason = ""; flowStartMs = 0; pressureTestLeak = false; flowLeakHeld = false;
      saveValveReason();
      Serial.println("[VALVE] manual open (from the server)");
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
// Hold one relay for a full travel. Blocking on purpose: every caller (the
// manual command, the auto-close, the nightly test's seal and its settle
// timer, the restore after a reboot) relies on the valve having ARRIVED when
// this returns. The board's own page stays answerable meanwhile; readings
// and uploads pause for the 18 s, well inside the server's offline window.
void valveDrive(int relayPin, const char *verb) {
  valveStop(); delay(200);
  Serial.printf("[VALVE] %s - holding the relay for %lu s\n", verb, VALVE_TRAVEL_MS / 1000UL);
  digitalWrite(relayPin, RELAY_ON);
  unsigned long start = millis();
  while (millis() - start < VALVE_TRAVEL_MS) { delay(50); web.handleClient(); }
  digitalWrite(relayPin, RELAY_OFF);
}
void valveOpenNow() {
  valveDrive(RELAY_OPEN, "opening");
  valveState = "open";
}
void valveCloseNow() {
  valveDrive(RELAY_CLOSE, "closing");
  valveState = "closed";
}

// ======================================================= detection
// Continuous-flow staging and the auto-close/auto-open around a flow leak.
void updateFlowDetection() {
  unsigned long now = millis();
  bool flowing = (lastFlowLpm >= FLOW_RUNNING_LPM);

  // A flow-leak close reopens by itself after "reopen after" minutes (0 =
  // never; Console -> Leak detection), and anyone with the button can open
  // it sooner. The flow timer starts fresh, so water that is still running
  // is caught again: warning, then leak, then close.
  if (valveReason == "flow" && dsReopenMin > 0 &&
      now - flowClosedAt >= (unsigned long)dsReopenMin * 60000UL) {
    valveOpenNow(); valveReason = ""; flowLeakHeld = false; flowStartMs = 0; saveValveReason();
    Serial.printf("[FLOW] %d min since the auto-close - reopened\n", dsReopenMin);
    return;
  }

  if (flowing) {
    if (flowStartMs == 0) flowStartMs = now;
    unsigned long mins = (now - flowStartMs) / 60000UL;
    if (mins >= (unsigned long)dsFlowLeakMin) {
      if (dsAutoClose && valveReason == "") {    // auto-close on the flow leak
        valveCloseNow();
        valveReason = "flow";
        flowLeakHeld = true;                     // ...and hold it: POSSIBLE_LEAK until it reopens
        flowClosedAt = now;
        saveValveReason();                       // survives a power cut
        if (dsReopenMin > 0) Serial.printf("[FLOW] leak - closed the valve; reopens in %d min unless opened sooner\n", dsReopenMin);
        else                 Serial.println("[FLOW] leak - closed the valve; it stays shut until somebody opens it");
      }
    }
  } else {
    // Idle again: the flow run is over. A held close is remembered by
    // flowLeakHeld, not by the timer, so the timer can reset.
    flowStartMs = 0;
  }
}

// The nightly sealed-line pressure test.
void updatePressureTest() {
  struct tm t;
  if (!getLocalTime(&t, 50)) return;             // no clock yet (NTP not synced)
  unsigned long now = millis();

  if (testPhase == TEST_IDLE) {
    bool dueNow = (t.tm_hour == dsTestHour) && (t.tm_yday != lastTestDay);
    if ((dueNow || testRequested) && valveReason == "") {
      valveCloseNow();                           // seal the line
      valveReason = "test";
      testPhase = TEST_SETTLING;
      testSettleUntil = now + (unsigned long)TEST_SETTLE_SECONDS * 1000UL;
      if (dueNow) lastTestDay = t.tm_yday;       // a requested run does not use up tonight's
      testRequested = false;
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
      lastTestDropBar = drop;
      pressureTestLeak = (drop > dsTestDropBar);
      Serial.printf("[TEST] drop %.3f bar -> %s\n",
                    drop, pressureTestLeak ? "LEAK" : "ok");
      testPhase = TEST_IDLE;
      if (pressureTestLeak) {
        // The line is losing water with every tap shut. Opening it now would
        // only resume the leak, so the valve stays where the test put it,
        // saved so a power cut does not undo it. The household, the
        // facilitator and the admin all have an Open button for when it is
        // fixed; the status stays POSSIBLE_LEAK until then.
        valveReason = "decay";
        saveValveReason();
        Serial.println("[TEST] leak found - holding the valve closed.");
      } else {
        valveOpenNow();                          // no leak: give the water back
        valveReason = "";
      }
    }
  }
}

// What to report this cycle. A latched pressure-test leak wins; otherwise the
// flow run's duration decides.
String computeReportStatus() {
  if (pressureTestLeak || flowLeakHeld) return "POSSIBLE_LEAK";
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
  if (code < 0) { if (uploadFailStreak < 255) uploadFailStreak++; maybeRediscover(); return false; }
  uploadFailStreak = 0;

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
  if (WiFi.status() != WL_CONNECTED) { lastUploadResult = "WiFi down"; return; }
  if (cfgServer.length() == 0) { lastUploadResult = "no server address yet"; maybeRediscover(); return; }
  if (apiKey.length() == 0) { lastUploadResult = "not claimed yet"; return; }

  String url = cfgServer + "/api/ingest/";
  String body = String("{\"device_id\":\"") + devId + "\",\"readings\":[{"
    "\"module\":1,"
    "\"pressure_bar\":" + String(lastPressureBar, 3) + ","
    "\"flow_lpm\":" + String(lastFlowLpm, 3) + ","
    "\"status\":\"" + reportStatus + "\","
    "\"valve_state\":\"" + String(valveState == "closed" ? "closed" : "open") + "\","
    // Why it is closed - "flow", "test", "decay", "manual" or empty. The owner's and
    // the facilitator's screens say it in words, so a closed valve at night
    // reads as the leak test and not as a fault.
    "\"valve_reason\":\"" + valveReason + "\","
    // Which test tripped the status, so the alert can say so: "decay" is the
    // night pressure test, "sustained" is water that never stopped. With it,
    // how far the pressure fell in the last test - the alert quotes it.
    "\"trigger\":\"" + String(pressureTestLeak ? "decay" : (reportStatus == "NORMAL" ? "none" : "sustained")) + "\","
    "\"test_drop_bar\":" + String(lastTestDropBar, 3) +
    "}],"
    "\"wifi_ssid\":\"" + jsonEscape(cfgSSID) + "\"," +
    "\"lan_ip\":\"" + WiFi.localIP().toString() + "\"" +
    (bootReported ? String("") : ",\"boot_reason\":\"" + bootReason + "\"") +
    (reportWifiId.length() ? ",\"wifi_change\":\"" + reportWifiId + "\",\"wifi_ok\":" + String(reportWifiOk ? "true" : "false") : String("")) +
    "}";

  WiFiClientSecure tls; HTTPClient http;
  if (!beginRequest(http, tls, url)) { lastUploadResult = "bad server URL"; return; }
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Key", apiKey);
  int code = http.POST(body);
  String resp = (code == 200) ? http.getString() : "";
  http.end();

  // Any reply means we reached the right machine; only a transport failure
  // (code < 0) counts towards going to look for a moved server.
  if (code > 0) uploadFailStreak = 0;
  else if (uploadFailStreak < 255) uploadFailStreak++;

  if (code == 200)     lastUploadResult = "OK";
  else if (code > 0)   lastUploadResult = "HTTP " + String(code);
  else                 lastUploadResult = "no reply from " + cfgServer;

  // 401 means the server does not know our key any more: the household was
  // deleted (its board record goes with it) or the key was renewed. No
  // amount of retrying fixes that, so after a few in a row the board drops
  // its identity and waits for a new setup code, like a blank board.
  if (code == 401) {
    if (++unauthStreak >= UNAUTH_BEFORE_RESET) {
      Serial.println("[UPLOAD] the server no longer knows this board (HTTP 401 three times) - its household was deleted or its key renewed.");
      Serial.println("[UPLOAD] identity dropped. Enter a new setup code on the board's page to claim it again.");
      devId = ""; apiKey = ""; saveIdentity("", "");
      unauthStreak = 0;
      lastUploadResult = "the server does not know this board - enter a new setup code";
    }
  } else if (code > 0) unauthStreak = 0;

  if (code == 200) {
    sentStatus = reportStatus; sentValve = valveState; sentReason = valveReason;
    reportWifiId = "";                            // the outcome has been delivered
    bootReported = true;                          // the reset reason has been delivered
    applyServerConfig(resp);                     // adopt any retuned settings
  }
  Serial.printf("[UPLOAD] %s  %s  P=%.3f bar  F=%.3f L/min\n",
                lastUploadResult.c_str(), reportStatus.c_str(),
                lastPressureBar, lastFlowLpm);
  if (code < 0) maybeRediscover();
}

// ======================================================= WiFi change
// Try a network the portal asked for. Blocking for up to 25 s each way; the
// line is not being watched meanwhile, which is acceptable once in a while
// for something a person just pressed. Falls back to the old network when
// the new one fails, so a typo cannot strand the board.
bool joinFor(const String &ssid, const String &pass, unsigned long ms) {
  WiFi.disconnect();
  delay(200);
  WiFi.begin(ssid.c_str(), pass.c_str());
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < ms) { delay(250); web.handleClient(); }
  return WiFi.status() == WL_CONNECTED;
}

void changeWifi(const String &id, const String &ssid, const String &pass) {
  Serial.printf("[WIFI] portal asks to move to \"%s\" (change %s)\n", ssid.c_str(), id.c_str());
  String oldSsid = cfgSSID, oldPass = cfgPass;
  bool ok = joinFor(ssid, pass, 25000UL);
  if (ok) {
    cfgSSID = ssid; cfgPass = pass;
    saveNetwork(cfgSSID, cfgPass, cfgServer, cfgCode);
    Serial.printf("[WIFI] moved to \"%s\"\n", ssid.c_str());
    onWiFiJoined();
  } else {
    Serial.printf("[WIFI] \"%s\" did not answer - back to \"%s\"\n", ssid.c_str(), oldSsid.c_str());
    joinFor(oldSsid, oldPass, 25000UL);          // maintainWiFi takes over if this fails too
  }
  lastWifiChangeId = id; saveWifiChangeId(id);
  reportWifiId = id; reportWifiOk = ok;
  lastUploadMs = 0;                               // report at once
}

// ======================================================= command poll
// The cheap question "is there a valve command for me?", asked every
// cmdPollSec while the server says a person is at a valve screen. The reply
// carries the same valve_command / run_valve_test / command_poll_seconds keys
// as the upload reply, so applyServerConfig acts on it; a valve that moves as
// a result is a change, and the loop uploads it at once as the acknowledgement.
void pollCommands() {
  if (WiFi.status() != WL_CONNECTED || apiKey.length() == 0 || cfgServer.length() == 0) return;
  WiFiClientSecure tls; HTTPClient http;
  if (!beginRequest(http, tls, cfgServer + "/api/commands/")) return;
  http.addHeader("X-Device-Key", apiKey);
  int code = http.GET();
  String resp = (code == 200) ? http.getString() : "";
  http.end();
  if (code == 200) applyServerConfig(resp);
  else if (code < 0) { cmdPollSec = 0; }        // server gone: the upload path will rediscover
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
  web.client().setNoDelay(true);   // push the page out in one go; the button is at its end
  bool connected = (WiFi.status() == WL_CONNECTED);
  String s = "<h1>Smart Hose Leak - board setup</h1>";
  s += "<p class=k>";
  if (connected)      s += "Connected to " + cfgSSID + " (" + WiFi.localIP().toString() + ").";
  else if (cfgSSID.length()) s += "Hotspot mode. Still trying \"" + cfgSSID + "\" - enter the new network below.";
  else                s += "Hotspot mode. Enter your details below.";
  s += "</p>";

  s += "<p class=k>Identity: " + (devId.length() ? devId : String("not claimed")) +
       " &middot; Server: " + (cfgServer.length() ? cfgServer : String("not set - will search")) +
       "<br>Last upload: " + lastUploadResult +
       "<br>Last restart: " + bootReason +
       " &middot; Server search: " + lastDiscoveryResult +
       "<br>Uploads every " + String(dsUploadSec) + " s" +
       (cmdPollSec > 0 ? " &middot; asking for valve commands every " + String(cmdPollSec) + " s" : "") +
       "<br>Status: " + reportStatus +
       " &middot; Pressure: " + String(lastPressureBar, 2) + " bar &middot; Flow: " +
       String(lastFlowLpm, 2) + " L/min &middot; Valve: " + valveState +
       " (" + (valveReason == "" ? "auto" : valveReason) + ")</p>";

  // The owner portal links here with everything in the query string, so the
  // form comes up filled in and one press of Save does the rest.
  bool fromPortal = web.hasArg("ssid") || web.hasArg("code");
  String fSsid   = fromPortal ? web.arg("ssid")   : cfgSSID;
  String fPass   = fromPortal ? web.arg("pass")   : String("");
  String fServer = fromPortal ? web.arg("server") : cfgServer;
  String fCode   = fromPortal ? web.arg("code")   : String("");
  if (fromPortal) s += "<p class=k style='color:#0b6ea8'><b>Filled in from your portal.</b> Check the WiFi name, then press Save &amp; connect.</p>";

  s += "<form method=POST action=/save>"
       "<label>Home WiFi name</label><input name=ssid value=\"" + htmlEscape(fSsid) + "\">"
       "<label>WiFi password</label><input name=pass type=password value=\"" + htmlEscape(fPass) + "\" placeholder='" +
       (cfgPass.length() && !fromPortal ? "leave blank to keep the saved one" : "") + "'>"
       "<label>Server</label><input name=server value=\"" + htmlEscape(fServer) + "\" "
       "placeholder='leave blank: find the laptop server on this network'>"
       "<span class=k>Type an address only for a server elsewhere, e.g. "
       "https://smarthoseleak.pythonanywhere.com</span>"
       "<label>Setup code</label><input name=code value=\"" + htmlEscape(fCode) + "\" placeholder='" +
       (devId.length() ? "leave blank to stay " + devId : String("e.g. K7M-4Q2")) + "'>" +
       (devId.length() ? "<span class=k>Enter a code only to claim this board as a different household.</span>" : "") +
       "<button type=submit>Save &amp; connect</button></form>";

  s += "<p class=k style='margin-top:1.2rem'>Valve: "
       "<a href=/valve?cmd=open>open</a> &middot; "
       "<a href=/valve?cmd=close>close</a></p>";
  s += "<p class=k><a href=/reset>Reset this board to a fresh one</a> - forgets the WiFi, "
       "server, identity and valve state, as if just flashed.</p>";

  web.send(200, "text/html", page(s));
}

void handleSave() {
  String ssid   = web.arg("ssid");
  String pass   = web.arg("pass");
  String server = normaliseServer(web.arg("server"));
  String code   = web.arg("code");
  ssid.trim(); code.trim();
  if (ssid.length() == 0) ssid = cfgSSID;
  if (pass.length() == 0) pass = cfgPass;
  // A new setup code means "this board is now that household": drop the old
  // identity so the claim runs again. A blank code keeps the identity, so a
  // board that only changed networks keeps its history.
  bool reclaim = code.length() > 0 && apiKey.length() > 0;
  if (reclaim) saveIdentity("", "");
  if (code.length() == 0) code = cfgCode;       // an unused code is kept
  saveNetwork(ssid, pass, server, code);
  String note = "<h1>Saved</h1><p>The board is restarting and will connect to <b>" + ssid + "</b>";
  if (server.length() == 0) note += ", then look for the server on that network";
  note += (reclaim || apiKey.length() == 0) ? ", then claim its identity." : ".";
  note += " Give it a minute, then check the dashboard. The hotspot closes once it has joined.</p>";
  web.send(200, "text/html", page(note));
  delay(800);
  ESP.restart();
}

// Manual override from the status page. Clears any auto reason so a person is
// in charge until the next automatic action.
void handleValve() {
  String cmd = web.arg("cmd");
  // Local close latches the same way an admin close does, so a valve shut
  // from the board's own page also stays shut across a reboot.
  if (cmd == "open")  { valveOpenNow();  valveReason = ""; flowStartMs = 0; pressureTestLeak = false; flowLeakHeld = false; saveValveReason(); }
  else if (cmd == "close") { valveCloseNow(); valveReason = "manual"; saveValveReason(); }
  web.sendHeader("Location", "/");
  web.send(303, "text/plain", "");
}

// ======================================================= factory reset
// Everything the board remembers lives in one Preferences namespace, which
// a new sketch upload does NOT clear - so "I flashed it again" is not a
// fresh board. This is: WiFi, server, setup code, identity and key, valve
// reason, detection settings, all gone, then a restart into hotspot mode.
void factoryReset() {
  Serial.println("[RESET] forgetting everything - WiFi, server, identity, valve state - and restarting as a fresh board.");
  prefs.begin("shl", false);
  prefs.clear();
  prefs.end();
  WiFi.disconnect(true, true);     // drop the station and wipe the stack's own copy too
  delay(300);
  ESP.restart();
}

// GET shows a confirmation; POST does it. A link alone must not be enough,
// since a browser prefetch or a curious tap would wipe a working board.
void handleReset() {
  if (web.method() == HTTP_POST) {
    web.send(200, "text/html", page("<h1>Reset</h1><p>Everything is forgotten. The board is restarting; "
                                    "in about half a minute the <b>" SETUP_AP_SSID "</b> hotspot is back, "
                                    "and it can be set up like a new board.</p>"));
    delay(800);
    factoryReset();
    return;
  }
  web.send(200, "text/html", page(
    "<h1>Reset this board?</h1>"
    "<p class=k>This forgets the saved WiFi, the server address, the setup code, the board's identity "
    "and key, the valve state and the detection settings - as if the board had just been flashed. "
    "The household keeps its record on the server; to use this board again, enter a new setup code.</p>"
    "<form method=POST action=/reset><button type=submit style='background:#b42318'>Forget everything and restart</button></form>"
    "<p class=k><a href=/>Back</a></p>"));
}

// ======================================================= WiFi bring-up
// The hotspot runs beside the station (AP_STA), so the saved network is still
// retried while the setup page is up and the hotspot can close by itself.
void startAP() {
  if (apMode) return;
  apMode = true;
  apStartedMs = millis();
  WiFi.mode(WIFI_AP_STA);
  WiFi.softAP(SETUP_AP_SSID, SETUP_AP_PASS);
  // The stack's own reconnect would otherwise scan for the lost network over
  // and over in the background, and every scan knocks the hotspot off the
  // air for a moment. Our timed retries in maintainWiFi() do the trying.
  WiFi.setAutoReconnect(false);
  WiFi.disconnect();
  Serial.printf("[AP] %s at %s\n", SETUP_AP_SSID, WiFi.softAPIP().toString().c_str());
}

void stopAP() {
  if (!apMode) return;
  apMode = false;
  WiFi.softAPdisconnect(true);
  WiFi.mode(WIFI_STA);
  Serial.println("[AP] closed - the board is on the network.");
}

// Close the hotspot only once it has been up for SETUP_HOTSPOT_MIN_MS and
// nobody is on it. Called every cycle while the board is on its network.
void maybeStopAP() {
  if (!apMode) return;
  if (millis() - apStartedMs < SETUP_HOTSPOT_MIN_MS) return;
  if (WiFi.softAPgetStationNum() > 0) return;
  stopAP();
}

void onWiFiJoined() {
  wifiFailCount = 0;
  WiFi.setAutoReconnect(true);     // back on: a brief drop is handled by the stack
  Serial.printf("[WIFI] got %s\n", WiFi.localIP().toString().c_str());
  configTime(TZ_OFFSET_SEC, DST_OFFSET_SEC, NTP_SERVER);  // for the nightly test
  maybeStopAP();                   // the hotspot stays a while longer if it only just came up
  // A moved board carries the old laptop's address; ask before posting to it.
  if (serverIsLocal()) discoverServer();
}

// Called every cycle. Never blocks: a lost network is retried on a timer,
// and after enough misses the hotspot comes up beside the retries.
void maintainWiFi() {
  if (WiFi.status() == WL_CONNECTED) {
    wifiDownSince = 0;
    if (wifiFailCount > 0) onWiFiJoined();
    maybeStopAP();
    return;
  }
  if (cfgSSID.length() == 0) { startAP(); return; }
  if (wifiDownSince == 0) wifiDownSince = millis();
  // The last resort: a clean boot. Skipped while a phone is on the hotspot,
  // so a person typing in a new network is never cut off mid-form. A manual
  // or leak-found valve close is restored by setup(), so nothing is lost.
  if (millis() - wifiDownSince >= WIFI_RESTART_AFTER_MS && WiFi.softAPgetStationNum() == 0) {
    Serial.printf("[WIFI] no network for %lu min and nobody on the hotspot - restarting for a clean radio.\n",
                  WIFI_RESTART_AFTER_MS / 60000UL);
    delay(200);
    ESP.restart();
  }
  // A phone on the hotspot is somebody reading or filling in the setup page;
  // a scan now would cut their page short, so the tries are spaced out.
  unsigned long gap = !apMode ? WIFI_RETRY_INTERVAL_MS
                    : WiFi.softAPgetStationNum() > 0 ? WIFI_RETRY_WITH_PHONE_MS
                    : WIFI_RETRY_IN_AP_MS;
  if (millis() - lastWifiRetryMs < gap) return;
  lastWifiRetryMs = millis();
  if (wifiFailCount < 255) wifiFailCount++;
  if (!apMode && wifiFailCount > WIFI_FAILS_BEFORE_AP) {
    Serial.printf("[WIFI] %u misses - raising the setup hotspot.\n", wifiFailCount);
    startAP();
  }
  Serial.printf("[WIFI] retrying %s (%u)\n", cfgSSID.c_str(), wifiFailCount);
  WiFi.disconnect();
  WiFi.begin(cfgSSID.c_str(), cfgPass.c_str());
}

bool connectWiFi() {
  if (cfgSSID.length() == 0) return false;
  apMode = false;
  WiFi.persistent(false);          // our Preferences hold the network; keep the stack from rewriting flash
  WiFi.setAutoReconnect(true);     // let the stack retry on its own between our timed begin() calls
  WiFi.mode(WIFI_STA);
  WiFi.begin(cfgSSID.c_str(), cfgPass.c_str());
  Serial.printf("[WIFI] joining %s", cfgSSID.c_str());
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < WIFI_CONNECT_TIMEOUT_MS) {
    delay(400); Serial.print(".");
  }
  Serial.println();
  lastWifiRetryMs = millis();
  if (WiFi.status() == WL_CONNECTED) { onWiFiJoined(); return true; }
  wifiFailCount = 1;
  Serial.println("[WIFI] failed - opening setup hotspot (and still trying).");
  return false;
}

// ======================================================= network report
// One plain line every 30 s saying whether the board is on the network or
// not, and why not. Everything else on the serial monitor is event-driven
// (a retry, an upload), which can leave minutes of silence that read as
// "is it connected or not?".
void reportNetwork() {
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[NET] connected to \"%s\" (%s, %d dBm) - %s - last upload: %s\n",
                  cfgSSID.c_str(), WiFi.localIP().toString().c_str(), WiFi.RSSI(),
                  devId.length() ? devId.c_str() : "not claimed yet", lastUploadResult.c_str());
    if (apMode) Serial.printf("[NET] hotspot %s still up for setup (%d phone(s) on it)\n",
                              SETUP_AP_SSID, WiFi.softAPgetStationNum());
  } else if (cfgSSID.length() == 0) {
    Serial.printf("[NET] NOT connected - no WiFi saved yet. Join hotspot %s and open http://192.168.4.1/\n", SETUP_AP_SSID);
  } else if (apMode) {
    int phones = WiFi.softAPgetStationNum();
    unsigned long gap = phones > 0 ? WIFI_RETRY_WITH_PHONE_MS : WIFI_RETRY_IN_AP_MS;
    Serial.printf("[NET] NOT connected - hotspot %s is up (%d phone%s on it); trying \"%s\" every %lu s, %u misses so far\n",
                  SETUP_AP_SSID, phones, phones == 1 ? "" : "s", cfgSSID.c_str(), gap / 1000UL, wifiFailCount);
  } else {
    Serial.printf("[NET] NOT connected - trying \"%s\" (%u misses; the hotspot comes up after %d)\n",
                  cfgSSID.c_str(), wifiFailCount, WIFI_FAILS_BEFORE_AP);
  }
}

// ======================================================= reset reason
String resetReasonText() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON:   return "power-on";
    case ESP_RST_BROWNOUT:  return "brownout";
    case ESP_RST_SW:        return "software restart";
    case ESP_RST_PANIC:     return "crash";
    case ESP_RST_INT_WDT:
    case ESP_RST_TASK_WDT:
    case ESP_RST_WDT:       return "watchdog";
    case ESP_RST_DEEPSLEEP: return "deep sleep";
    case ESP_RST_EXT:       return "reset pin";
    default:                return "unknown";
  }
}

// ======================================================= setup / loop
void setup() {
  Serial.begin(115200);
  delay(200);
  bootReason = resetReasonText();
  Serial.println("[BOOT] reset reason: " + bootReason);

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
  if (valveReason == "manual" || valveReason == "decay" || valveReason == "flow") {
    valveCloseNow();
    if (valveReason == "decay") pressureTestLeak = true;   // keep reporting it
    if (valveReason == "flow")  { flowLeakHeld = true; flowClosedAt = millis(); }   // ...this one too; timer restarts
    Serial.println("[VALVE] restored " + valveReason + " close after reboot");
  } else {
    // Any other close (the nightly test, a flow-leak auto-close) does not
    // survive a reboot, so the valve is driven open now rather than left
    // wherever the power cut found it while the board reports "open".
    valveOpenNow();
    Serial.println("[VALVE] opened on boot");
  }
  if (!connectWiFi()) {
    startAP();
  } else if (apiKey.length() == 0 && cfgCode.length() > 0) {
    claimDevice();
  }

  web.on("/", handleRoot);
  web.on("/save", HTTP_POST, handleSave);
  web.on("/valve", handleValve);
  web.on("/reset", handleReset);
  web.begin();
  Serial.println("[WEB] setup/status page on port 80.");
}

void loop() {
  web.handleClient();

  // Manual serial valve control, handy on the bench.
  if (Serial.available()) {
    String c = Serial.readStringUntil('\n');
    c.trim(); c.toUpperCase();
    if (c == "OPEN")  { valveOpenNow();  valveReason = ""; flowStartMs = 0; pressureTestLeak = false; flowLeakHeld = false; saveValveReason(); }
    else if (c == "CLOSE") { valveCloseNow(); valveReason = "manual"; saveValveReason(); }
    else if (c == "TEST")  { testRequested = true; Serial.println("[TEST] requested from serial."); }
    else if (c == "FACTORY") factoryReset();        // back to a fresh board
  }

  if (millis() - lastNetReportMs >= NET_REPORT_MS) { lastNetReportMs = millis(); reportNetwork(); }

  // Sensors and detections every 2 s, online or not - and in hotspot mode
  // too, so a board waiting for its new network still protects the line.
  if (millis() - lastSampleMs >= SAMPLE_INTERVAL_MS) {
    lastSampleMs = millis();
    maintainWiFi();
    if (apiKey.length() == 0 && cfgCode.length() > 0 && cfgServer.length() > 0 &&
        WiFi.status() == WL_CONNECTED) {
      claimDevice();
    }
    readSensors();
    updateFlowDetection();
    updatePressureTest();
    reportStatus = computeReportStatus();
  }

  // Upload on the interval, or at once when something the server shows has
  // changed (status, valve position, valve reason). A change the server has
  // not accepted yet is retried every couple of seconds, not every loop.
  unsigned long sinceUpload = millis() - lastUploadMs;
  bool changed = reportStatus != sentStatus || valveState != sentValve || valveReason != sentReason;
  bool due = sinceUpload >= (unsigned long)dsUploadSec * 1000UL;
  if (due || (changed && sinceUpload >= CHANGE_RETRY_MS)) {
    lastUploadMs = millis();
    uploadReadings();          // sends the decided status; adopts any new config
  } else if (cmdPollSec > 0 && millis() - lastPollMs >= (unsigned long)cmdPollSec * 1000UL) {
    lastPollMs = millis();
    pollCommands();            // somebody is at a valve screen
  }
}
