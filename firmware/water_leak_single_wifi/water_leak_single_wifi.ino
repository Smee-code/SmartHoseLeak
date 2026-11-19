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
#include <DNSServer.h>    // captive portal: the setup page opens by itself on the phone
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
// The server a board uses when nobody types one. The setup page comes up
// with this filled in, so the owner only picks a WiFi and types the code.
// Blank the field on the page ("Server address") to make the board look for
// a laptop server on its own network instead.
#define DEFAULT_SERVER "https://smarthoseleak.pythonanywhere.com"
// A network typed on the setup page is tried while the hotspot stays up, so
// the page can say how it went: this many tries, this long in all.
#define JOIN_TIMEOUT_MS 30000UL
#define JOIN_ATTEMPTS   3
#define JOIN_IDLE    0
#define JOIN_TRYING  1
#define JOIN_OK      2
#define JOIN_FAILED  3

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
DNSServer   dns;                               // answers every name with the board while the hotspot is up

String cfgSSID, cfgPass, cfgServer, cfgCode;   // provisioning
String devId, apiKey;                          // identity after claim
bool   apMode = false;
unsigned long apStartedMs = 0;                 // when the hotspot came up (for SETUP_HOTSPOT_MIN_MS)

// The guided join (20 Nov 2026). "Save" used to store the network and
// restart, blind: a wrong password or a 5 GHz-only router just meant the
// hotspot came back a minute later with no word why. Now the network is
// tried at once with the hotspot still up, and the page watches it happen.
uint8_t       joinState = JOIN_IDLE;
String        tryS, tryP, tryServer, tryCode;  // what the page asked for; saved only once it has joined
unsigned long joinStartedMs = 0, joinNextTryMs = 0;
uint8_t       joinAttempts = 0;
String        joinFailText;                    // why it failed, as a sentence for the page
volatile int  staReason  = 0;                  // why the station last dropped (from the WiFi event)
volatile bool staDropped = false;
String        claimResult;                     // the last claim that did not work, in words
String        scanCache;                       // the last list of networks, as JSON
unsigned long scanCacheMs = 0;

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
  if (code < 0) {
    if (uploadFailStreak < 255) uploadFailStreak++;
    claimResult = "The server at " + cfgServer + " did not answer. Is the internet working on this WiFi? Trying again.";
    maybeRediscover();
    return false;
  }
  uploadFailStreak = 0;

  // A used (409), expired (410), malformed (400) or unknown (404) code will
  // never work. Forget it so the board stops asking every two seconds; the
  // owner enters a fresh one on the setup page, which says which it was.
  if (code == 409 || code == 410 || code == 400 || code == 404) {
    if (code == 409)      claimResult = "That setup code was already used. Get a new one in your portal (Settings > My device), then enter it here.";
    else if (code == 410) claimResult = "That setup code has expired. Get a new one in your portal (Settings > My device), then enter it here.";
    else                  claimResult = "The server does not know that setup code. Check it against the one in your portal and enter it again.";
    cfgCode = "";
    prefs.begin("shl", false);
    prefs.putString("code", "");
    prefs.end();
    Serial.println("[CLAIM] that code will not work - get a new one and re-enter it.");
    return false;
  }
  if (code != 200) { claimResult = "The server answered HTTP " + String(code) + " to the setup code. Trying again."; return false; }

  String id, key;
  if (extractJsonString(resp, "device_id", id) &&
      extractJsonString(resp, "api_key", key)) {
    devId = id; apiKey = key;
    claimResult = "";
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

// ======================================================= joining a network
// Every join goes through here. Three of the stack's defaults are wrong for
// a house: (1) it refuses anything weaker than WPA2, so an older router or
// pocket WiFi still on WPA reads as "network not found"; (2) it takes the
// first access point that answers to the name, which with a repeater in the
// house can be the far one - so scan every channel and take the strongest;
// (3) a blank password has to mean an open network.
void wifiBegin(const String &ssid, const String &pass) {
  WiFi.setMinSecurity(pass.length() ? WIFI_AUTH_WPA_PSK : WIFI_AUTH_OPEN);
  WiFi.setScanMethod(WIFI_ALL_CHANNEL_SCAN);
  WiFi.setSortMethod(WIFI_CONNECT_AP_BY_SIGNAL);
  staDropped = false; staReason = 0;
  WiFi.begin(ssid.c_str(), pass.c_str());
}

// The WiFi stack says why a join failed; keep it, for the page and the log.
void onStaDisconnected(arduino_event_id_t event, arduino_event_info_t info) {
  int r = info.wifi_sta_disconnected.reason;
  if (r == WIFI_REASON_ASSOC_LEAVE) return;      // our own disconnect()
  staReason = r; staDropped = true;
}

bool reasonNoNetwork(int r)  { return r == WIFI_REASON_NO_AP_FOUND || (r >= 210 && r <= 212); }
bool reasonBadPassword(int r) {
  return r == WIFI_REASON_AUTH_FAIL || r == WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT || r == WIFI_REASON_HANDSHAKE_TIMEOUT;
}

// A few words for the serial monitor and the status line.
String staReasonText(int r) {
  if (r == 0) return "";
  if (reasonNoNetwork(r))   return "network not found - the board sees 2.4 GHz only";
  if (reasonBadPassword(r)) return "wrong password";
  if (r == WIFI_REASON_AUTH_EXPIRE || r == WIFI_REASON_ASSOC_EXPIRE || r == WIFI_REASON_BEACON_TIMEOUT)
    return "the router did not answer - weak signal?";
  return "could not join, reason " + String(r);
}

// A whole sentence for the setup page, saying what to do about it.
String joinFailSentence(int r) {
  String name = "\"" + tryS + "\"";
  if (reasonNoNetwork(r))
    return "The board cannot find a WiFi called " + name + ". It sees 2.4 GHz networks only. Check the name "
           "(capital letters matter), that the router's 2.4 GHz band is switched on, and that the board is within reach of it.";
  if (reasonBadPassword(r))
    return "The WiFi " + name + " refused the password. Type it again - capital letters and spaces matter. "
           "Tick Show password to check what you typed.";
  if (r == 0)
    return "The WiFi " + name + " did not answer in time. Move the board closer to the router and try again.";
  return "The board could not join " + name + " (" + staReasonText(r) + "). Move the board closer to the router and try again.";
}

// ======================================================= WiFi change
// Try a network the portal asked for. Blocking for up to 25 s each way; the
// line is not being watched meanwhile, which is acceptable once in a while
// for something a person just pressed. Falls back to the old network when
// the new one fails, so a typo cannot strand the board.
bool joinFor(const String &ssid, const String &pass, unsigned long ms) {
  WiFi.disconnect();
  delay(200);
  wifiBegin(ssid, pass);
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
         ".k{color:#5b6b7a;font-size:.9rem}input[type=checkbox]{width:auto;margin-right:.4rem}"
         ".net{display:block;width:100%;text-align:left;margin:.35rem 0 0;background:#eef4f8;color:#16232e;border:1px solid #c9d6df}"
         ".net.on{background:#0b6ea8;color:#fff;border-color:#0b6ea8}#nets a{display:inline-block;margin:.5rem 0}"
         ".step{margin:.5rem 0;padding:.65rem .75rem;border-radius:6px;background:#eef4f8}.step:empty{display:none}"
         ".step.ok{background:#e6f4ea;color:#14532d}.step.bad{background:#fdecea;color:#7a1a12}"
         "details{margin-top:.8rem}</style>" + inner;
}

// The setup page's script: asks the board which networks it can see and
// lists them to tap. Kept apart from the form so that a page cut short on a
// weak hotspot link still has a form that works by typing.
const char ROOT_JS[] PROGMEM = R"JS(
(function(){
var box=document.getElementById('nets'),inp=document.getElementById('ssid'),pw=document.getElementById('pass'),tries=0;
document.getElementById('show').onchange=function(){pw.type=this.checked?'text':'password'};
function sig(r){return r>-60?'strong':(r>-75?'fair':'weak')}
function pick(b,n){inp.value=n.ssid;var all=box.querySelectorAll('.net');for(var i=0;i<all.length;i++)all[i].className='net';b.className='net on';pw.focus()}
function show(d){
 box.textContent='';
 if(!d.nets.length){var p=document.createElement('div');p.textContent='The board sees no 2.4 GHz WiFi from here. Move it closer to the router, then scan again.';box.appendChild(p)}
 d.nets.forEach(function(n){var b=document.createElement('button');b.type='button';b.className='net'+(n.ssid==inp.value?' on':'');b.textContent=n.ssid+'  -  '+sig(n.rssi)+' signal'+(n.open?', no password':'');b.onclick=function(){pick(b,n)};box.appendChild(b)});
 var a=document.createElement('a');a.href='#';a.textContent='Scan again';a.onclick=function(e){e.preventDefault();tries=0;box.textContent='Looking for WiFi networks ...';load(1)};box.appendChild(a);
}
function load(fresh){
 fetch('/scan'+(fresh?'?fresh=1':''),{cache:'no-store'}).then(function(r){return r.json()}).then(function(d){
  if(d.scanning){if(++tries<20)setTimeout(function(){load(0)},1500);else box.textContent='Could not scan. Type the WiFi name below.';return}
  show(d);
 }).catch(function(){if(++tries<20)setTimeout(function(){load(0)},2000);else box.textContent='Could not scan. Type the WiFi name below.'});
}
load(0);
})();
)JS";

// The progress page's script: asks the board every 1.5 s where it is, and
// says it in three lines - the WiFi, the server, the first reading.
const char PROGRESS_JS[] PROGMEM = R"JS(
(function(){
var miss=0,done=false;
function el(i){return document.getElementById(i)}
function set(i,cls,t){var e=el(i);e.className='step '+cls;e.textContent=t}
function tick(){
 fetch('/status',{cache:'no-store'}).then(function(r){return r.json()}).then(function(d){
  miss=0;el('hint').textContent='';
  if(d.join==1){set('s1','','Joining '+d.try_ssid+' ... this takes up to half a minute.');set('s2','','');set('s3','','')}
  else if(d.join==3){set('s1','bad',d.fail);set('s2','','');set('s3','','');el('back').textContent='Try again';done=true}
  else if(!d.wifi){set('s1','','Not on WiFi yet. Trying '+d.ssid+' ...'+(d.why?' Last try: '+d.why+'.':''));set('s2','','');set('s3','','')}
  else{
   set('s1','ok','Joined '+d.ssid+'. The board is on your WiFi.');
   if(d.device){
    set('s2','ok','The server knows this board as '+d.device+'.');
    if(d.upload=='OK'){set('s3','ok','Readings are reaching the server. All done.');el('hint').textContent='Put your phone back on your own WiFi. This setup network closes by itself in a few minutes.';done=true}
    else set('s3','','Sending the first reading ... ('+d.upload+')');
   }
   else if(d.claim){set('s2','bad',d.claim);set('s3','','')}
   else if(!d.server){set('s2','','Looking for the server on this network ... ('+d.search+')');set('s3','','')}
   else if(d.has_code){set('s2','','Asking the server which household this board belongs to ...');set('s3','','')}
   else{set('s2','bad','The board is on WiFi, but no setup code was entered, so it belongs to no household yet. Go back and enter the code.');set('s3','','')}
  }
 }).catch(function(){
  if(++miss>=4)el('hint').textContent='Lost contact with the board. If your phone left the setup network, join it again - or look at Settings > My device in your portal, which shows Connected once the board is through.';
 }).then(function(){if(!done)setTimeout(tick,1500)});
}
tick();
})();
)JS";

void handleRoot() {
  web.client().setNoDelay(true);   // push the page out in one go
  bool connected = (WiFi.status() == WL_CONNECTED);
  String s = "<h1>Smart Hose Leak - board setup</h1>";
  s += "<p class=k>";
  if (connected)             s += "Connected to " + htmlEscape(cfgSSID) + " (" + WiFi.localIP().toString() + ").";
  else if (cfgSSID.length()) s += "Not connected. Still trying \"" + htmlEscape(cfgSSID) + "\"" +
                                  (staReason ? " - last try: " + staReasonText(staReason) : String("")) +
                                  ". Pick the network again below.";
  else                       s += "Not connected to any WiFi yet. Fill in the three steps below.";
  s += "</p>";
  if (joinState != JOIN_IDLE) s += "<p><a href=/progress>See how the last setup went</a></p>";

  // The owner portal can link here with the setup code and the server in the
  // query string, so those come up filled in. The WiFi is picked on this
  // page, from the networks the board itself can see.
  bool fromPortal = web.hasArg("ssid") || web.hasArg("code");
  String fSsid   = (fromPortal && web.arg("ssid").length()) ? web.arg("ssid") : cfgSSID;
  String fPass   = fromPortal ? web.arg("pass")   : String("");
  String fServer = fromPortal ? web.arg("server") : (cfgServer.length() ? cfgServer : String(DEFAULT_SERVER));
  String fCode   = fromPortal ? web.arg("code")   : String("");

  s += "<form method=POST action=/save>"
       "<label>1. Your home WiFi</label>"
       "<div id=nets class=k>Looking for WiFi networks ...</div>"
       "<input name=ssid id=ssid value=\"" + htmlEscape(fSsid) + "\" placeholder='WiFi name' autocapitalize=none autocomplete=off>"
       "<span class=k>Tap your WiFi above, or type its name. Only 2.4 GHz networks are listed - the board "
       "cannot use 5 GHz. If yours is missing, move the board closer to the router.</span>"
       "<label>2. WiFi password</label>"
       "<input name=pass id=pass type=password value=\"" + htmlEscape(fPass) + "\" autocomplete=off placeholder='" +
       (cfgPass.length() && !fromPortal ? "leave blank to keep the saved one" : "") + "'>"
       "<label class=k style='font-weight:400'><input type=checkbox id=show>Show password</label>"
       "<label>3. Setup code</label>"
       "<input name=code value=\"" + htmlEscape(fCode) + "\" autocapitalize=characters autocomplete=off placeholder='" +
       (devId.length() ? "leave blank to stay " + devId : String("e.g. K7M-4Q2")) + "'>" +
       (devId.length() ? "<span class=k>Enter a code only to claim this board as a different household.</span>"
                       : "<span class=k>The code texted to you, also shown in your portal under Settings &gt; My device.</span>") +
       "<details><summary class=k>Server address (leave as it is)</summary>"
       "<input name=server value=\"" + htmlEscape(fServer) + "\" placeholder='blank: find a laptop server on this WiFi'></details>"
       "<button type=submit>Save &amp; connect</button></form>";

  s += "<p class=k style='margin-top:1.2rem'>Identity: " + (devId.length() ? devId : String("not claimed")) +
       " &middot; Server: " + (cfgServer.length() ? cfgServer : String("not set")) +
       "<br>Last upload: " + lastUploadResult +
       "<br>Last restart: " + bootReason +
       " &middot; Server search: " + lastDiscoveryResult +
       "<br>Uploads every " + String(dsUploadSec) + " s" +
       (cmdPollSec > 0 ? " &middot; asking for valve commands every " + String(cmdPollSec) + " s" : "") +
       "<br>Status: " + reportStatus +
       " &middot; Pressure: " + String(lastPressureBar, 2) + " bar &middot; Flow: " +
       String(lastFlowLpm, 2) + " L/min &middot; Valve: " + valveState +
       " (" + (valveReason == "" ? "auto" : valveReason) + ")</p>";

  s += "<p class=k>Valve: "
       "<a href=/valve?cmd=open>open</a> &middot; "
       "<a href=/valve?cmd=close>close</a></p>";
  s += "<p class=k><a href=/reset>Reset this board to a fresh one</a> - forgets the WiFi, "
       "server, identity and valve state, as if just flashed.</p>";
  s += "<script>";
  s += FPSTR(ROOT_JS);
  s += "</script>";

  web.send(200, "text/html", page(s));
}

// ======================================================= guided join
// Start trying the network the page asked for. Nothing is saved yet: a
// typo must not overwrite a network that works.
void startJoin(const String &ssid, const String &pass, const String &server, const String &code) {
  tryS = ssid; tryP = pass; tryServer = server; tryCode = code;
  joinFailText = ""; claimResult = "";
  // Already on that very network (only the code or the server changed):
  // there is nothing to join.
  if (WiFi.status() == WL_CONNECTED && ssid == cfgSSID && pass == cfgPass) { commitJoin(); return; }
  joinAttempts = 1; joinStartedMs = millis(); joinNextTryMs = 0;
  joinState = JOIN_TRYING;
  startAP();                       // keep (or raise) the hotspot: the page that asked has to hear the answer
  apStartedMs = millis();          // ...for a fresh five minutes, however long it has been up
  Serial.printf("[JOIN] trying \"%s\" - asked for on the setup page\n", ssid.c_str());
  WiFi.disconnect();
  delay(150);
  wifiBegin(ssid, pass);
}

void failJoin(const String &why) {
  joinState = JOIN_FAILED;
  joinFailText = why;
  Serial.println("[JOIN] failed: " + why);
  WiFi.disconnect();
  lastWifiRetryMs = millis();      // the saved network, if there is one, is retried on its usual timer
}

// It joined: now the network is saved, and the rest (server, identity,
// first upload) follows from the loop exactly as after a boot.
void commitJoin() {
  // A new setup code means "this board is now that household": drop the old
  // identity so the claim runs again. A blank code keeps the identity, so a
  // board that only changed networks keeps its history.
  bool reclaim = tryCode.length() > 0 && apiKey.length() > 0;
  if (reclaim) { devId = ""; apiKey = ""; saveIdentity("", ""); }
  if (tryCode.length() == 0) tryCode = cfgCode;       // an unused code is kept
  cfgSSID = tryS; cfgPass = tryP; cfgServer = tryServer; cfgCode = tryCode;
  saveNetwork(cfgSSID, cfgPass, cfgServer, cfgCode);
  joinState = JOIN_OK;
  apStartedMs = millis();          // the phone may blink off while the radio changes channel; stay for it
  wifiDownSince = 0; uploadFailStreak = 0; unauthStreak = 0;
  lastUploadResult = "not sent yet";
  lastUploadMs = 0;                                   // upload as soon as there is an identity
  Serial.printf("[JOIN] joined \"%s\" - saved.\n", cfgSSID.c_str());
  onWiFiJoined();
}

// Called every loop while a join is being tried.
void serviceJoin() {
  if (joinState != JOIN_TRYING) return;
  unsigned long now = millis();
  if (WiFi.status() == WL_CONNECTED && WiFi.SSID() == tryS) { commitJoin(); return; }
  bool outOfTime = now - joinStartedMs > JOIN_TIMEOUT_MS;
  if (staDropped && now >= joinNextTryMs) {
    int r = staReason;
    staDropped = false;
    if (joinAttempts >= JOIN_ATTEMPTS || outOfTime) { failJoin(joinFailSentence(r)); return; }
    Serial.printf("[JOIN] try %u of %d: %s - trying again\n", joinAttempts, JOIN_ATTEMPTS, staReasonText(r).c_str());
    joinAttempts++;
    joinNextTryMs = now + 1500;
    wifiBegin(tryS, tryP);
    return;
  }
  if (outOfTime) failJoin(joinFailSentence(staReason));
}

void handleSave() {
  String ssid   = web.arg("ssid");
  String pass   = web.arg("pass");
  String server = normaliseServer(web.arg("server"));
  String code   = web.arg("code");
  ssid.trim(); code.trim();
  if (ssid.length() == 0) ssid = cfgSSID;
  if (pass.length() == 0 && ssid == cfgSSID) pass = cfgPass;   // blank keeps the saved one, for the same network only
  if (ssid.length() == 0) {                                     // nothing to join
    web.sendHeader("Location", "/", true);
    web.send(303, "text/plain", "");
    return;
  }
  web.sendHeader("Location", "/progress", true);
  web.send(303, "text/plain", "");
  delay(300);                      // let the reply leave before the radio gets busy
  startJoin(ssid, pass, server, code);
}

void handleProgress() {
  String s = "<h1>Setting up the board</h1>"
             "<div class=step id=s1>Starting ...</div>"
             "<div class=step id=s2></div>"
             "<div class=step id=s3></div>"
             "<p class=k id=hint></p>"
             "<p><a href=/ id=back>Back to the setup page</a></p>"
             "<script>";
  s += FPSTR(PROGRESS_JS);
  s += "</script>";
  web.send(200, "text/html", page(s));
}

// Where the board is, for the progress page.
void handleStatus() {
  bool on = (WiFi.status() == WL_CONNECTED);
  String j = "{\"join\":" + String(joinState);
  j += ",\"try_ssid\":\"" + jsonEscape(joinState == JOIN_IDLE ? cfgSSID : tryS) + "\"";
  j += ",\"wifi\":" + String(on ? "true" : "false");
  j += ",\"ssid\":\"" + jsonEscape(cfgSSID) + "\"";
  j += ",\"ip\":\"" + (on ? WiFi.localIP().toString() : String("")) + "\"";
  j += ",\"why\":\"" + jsonEscape(on ? String("") : staReasonText(staReason)) + "\"";
  j += ",\"fail\":\"" + jsonEscape(joinFailText) + "\"";
  j += ",\"server\":\"" + jsonEscape(cfgServer) + "\"";
  j += ",\"device\":\"" + jsonEscape(devId) + "\"";
  j += ",\"has_code\":" + String(cfgCode.length() ? "true" : "false");
  j += ",\"claim\":\"" + jsonEscape(claimResult) + "\"";
  j += ",\"upload\":\"" + jsonEscape(lastUploadResult) + "\"";
  j += ",\"search\":\"" + jsonEscape(lastDiscoveryResult) + "\"}";
  web.sendHeader("Cache-Control", "no-store");
  web.send(200, "application/json", j);
}

// The networks the board can see, strongest first, one line per name. The
// scan runs in the background (it takes a few seconds and the hotspot must
// keep answering), so the page asks again until the list is ready.
void handleScan() {
  web.sendHeader("Cache-Control", "no-store");
  if (joinState == JOIN_TRYING) { web.send(200, "application/json", "{\"scanning\":true}"); return; }
  int n = WiFi.scanComplete();
  if (n == WIFI_SCAN_RUNNING) { web.send(200, "application/json", "{\"scanning\":true}"); return; }
  if (n < 0) {                                   // no scan waiting to be read
    if (!web.hasArg("fresh") && scanCache.length() && millis() - scanCacheMs < 30000UL) {
      web.send(200, "application/json", scanCache);
      return;
    }
    WiFi.scanNetworks(true);                     // in the background; the next ask collects it
    web.send(200, "application/json", "{\"scanning\":true}");
    return;
  }
  if (n > 40) n = 40;
  bool used[40];
  for (int i = 0; i < 40; i++) used[i] = false;
  String out = "{\"scanning\":false,\"nets\":[";
  int listed = 0;
  while (listed < 15) {
    int best = -1;
    for (int i = 0; i < n; i++)
      if (!used[i] && WiFi.SSID(i).length() && (best < 0 || WiFi.RSSI(i) > WiFi.RSSI(best))) best = i;
    if (best < 0) break;
    String name = WiFi.SSID(best);
    for (int i = 0; i < n; i++) if (WiFi.SSID(i) == name) used[i] = true;
    if (listed) out += ",";
    out += "{\"ssid\":\"" + jsonEscape(name) + "\",\"rssi\":" + String(WiFi.RSSI(best)) +
           ",\"open\":" + String(WiFi.encryptionType(best) == WIFI_AUTH_OPEN ? "true" : "false") + "}";
    listed++;
  }
  out += "]}";
  WiFi.scanDelete();
  scanCache = out; scanCacheMs = millis();
  web.send(200, "application/json", out);
}

// A phone that has just joined a network asks a well-known address whether
// the internet is there. While the hotspot is up every name resolves to the
// board (the DNS server), and answering those asks with a redirect to the
// setup page is what makes the phone open it by itself ("Sign in to
// network"), with no address to type.
void handleNotFound() {
  if (apMode) {
    web.sendHeader("Location", "http://" + WiFi.softAPIP().toString() + "/", true);
    web.send(302, "text/plain", "");
    return;
  }
  web.send(404, "text/plain", "not found");
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
  dns.start(53, "*", WiFi.softAPIP());            // every name -> the board, so the setup page opens by itself
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
  dns.stop();
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
  if (joinState == JOIN_TRYING) return;            // the setup page's join is in charge (serviceJoin)
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
  if (WiFi.scanComplete() == WIFI_SCAN_RUNNING) return;   // the setup page is listing networks; a retry would cut it off
  lastWifiRetryMs = millis();
  if (wifiFailCount < 255) wifiFailCount++;
  if (!apMode && wifiFailCount > WIFI_FAILS_BEFORE_AP) {
    Serial.printf("[WIFI] %u misses - raising the setup hotspot.\n", wifiFailCount);
    startAP();
  }
  Serial.printf("[WIFI] retrying %s (%u)%s%s\n", cfgSSID.c_str(), wifiFailCount,
                staReason ? " - last try: " : "", staReasonText(staReason).c_str());
  WiFi.disconnect();
  delay(100);
  wifiBegin(cfgSSID, cfgPass);
}

bool connectWiFi() {
  if (cfgSSID.length() == 0) return false;
  apMode = false;
  WiFi.persistent(false);          // our Preferences hold the network; keep the stack from rewriting flash
  WiFi.setAutoReconnect(true);     // let the stack retry on its own between our timed begin() calls
  WiFi.mode(WIFI_STA);
  wifiBegin(cfgSSID, cfgPass);
  Serial.printf("[WIFI] joining %s", cfgSSID.c_str());
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < WIFI_CONNECT_TIMEOUT_MS) {
    delay(400); Serial.print(".");
  }
  Serial.println();
  lastWifiRetryMs = millis();
  if (WiFi.status() == WL_CONNECTED) { onWiFiJoined(); return true; }
  wifiFailCount = 1;
  Serial.printf("[WIFI] failed%s%s - opening setup hotspot (and still trying).\n",
                staReason ? ": " : "", staReasonText(staReason).c_str());
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
  if (WiFi.status() != WL_CONNECTED && staReason)
    Serial.printf("[NET] why the last try failed: %s\n", staReasonText(staReason).c_str());
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
  // The radio comes on only now, after the valve has finished moving: the
  // motor's start and the WiFi's first burst together are what a marginal
  // supply cannot carry.
  WiFi.onEvent(onStaDisconnected, ARDUINO_EVENT_WIFI_STA_DISCONNECTED);
  if (!connectWiFi()) {
    startAP();
  } else if (apiKey.length() == 0 && cfgCode.length() > 0) {
    claimDevice();
  }

  web.on("/", handleRoot);
  web.on("/save", HTTP_POST, handleSave);
  web.on("/progress", handleProgress);
  web.on("/status", handleStatus);
  web.on("/scan", handleScan);
  web.on("/valve", handleValve);
  web.on("/reset", handleReset);
  web.onNotFound(handleNotFound);
  web.begin();
  Serial.println("[WEB] setup/status page on port 80.");
}

void loop() {
  web.handleClient();
  if (apMode) dns.processNextRequest();
  serviceJoin();

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
      if (claimDevice()) lastUploadMs = 0;      // claimed: send the first reading now, not in 15 s
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
