/*
  ============================================================================
  WATER LEAK DETECTION - PER-HOUSEHOLD DEVICE
  One ESP32 on one household's hose line, with a pressure sensor, a flow
  sensor, a motorised ball valve, a WiFi dashboard of its own, and a SIM800L
  kept as a last resort.
  ----------------------------------------------------------------------------
  Undergraduate Thesis Reference Implementation

  WHAT THIS BOARD IS FOR
    It watches one hose, from the main source to one house, and answers one
    question all day: is water leaving this line when nobody is using any?

    It decides that itself. The server owns the households, the history, the
    notifications and the SMS, but it is not always reachable - so the
    decision has to be made here, and the board keeps working through an
    outage with only its report delayed.

  HOW IT DECIDES  (four tests, no calibration)
    1. Burst        flow jumps high AND pressure collapses, together, and
                    stays that way. Normal use does not crash the pressure.
    2. Night flow   any flow for several minutes between 01:00 and 04:00,
                    when nobody should be drawing water. Needs NTP, and is
                    silent until the clock is actually set.
    3. Sustained    a steady trickle that never returns to zero for half an
       low flow     hour. Nobody runs a tap at 0.5 L/min that long - but a
                    tap left slightly open looks identical, so the alert
                    this raises asks the owner to check their faucets first.
    4. Valve-closed after ten minutes of no flow, shut the valve, wait, and
       decay        watch the pressure. A sealed line holds it; a hole bleeds
                    it away. This is the only test that finds a pinhole, and
                    it is why the valve is here.

    Low pressure with nothing flowing is reported as LOW SUPPLY, not a leak:
    the source is not delivering, and there is no hole to go and find.

    The Q = K*sqrt(P) method from the earlier version is gone. It needed a
    calibration that could not be done reliably in the field, and it only
    ever described a line whose conductance had not changed since the day it
    was measured.

  IDENTITY - NOTHING IS COMPILED IN
    The board is told its device ID and API key through its own setup page,
    and keeps them in flash. One firmware build serves every household.

    Setting one up:
      1. The facilitator adds the household on the dashboard, which registers
         a board and shows a short claim code, e.g. K7M-4Q2.
      2. Join the board's "LeakDetect-Setup" hotspot.
      3. Enter the WiFi network, its password, and the claim code.
      4. Save. The board reboots, joins the network, hands the code to the
         server, and receives its real ID and key. The code is then dead.

    The key itself is never typed and never leaves the server except to the
    board that claimed it. Re-flashing does not lose the identity; flash is a
    separate area.

  HARDWARE WIRING
    Flow sensor signal   -> GPIO 27   (interrupt, INPUT_PULLUP)
    Pressure sensor      -> GPIO 34   (ADC1 - see the note below)
    Valve relay OPEN     -> GPIO 25
    Valve relay CLOSE    -> GPIO 26
    GSM module RX / TX   -> GPIO 17 / GPIO 16, common ground

    ADC1 IS NOT OPTIONAL. The pressure pin must be in the 32-39 range. ADC2
    is taken over by the WiFi radio, and analogRead() on an ADC2 pin simply
    blocks once WiFi is up: the board looks alive and reports nothing.

    The pressure sensor outputs 0.5-4.5 V and needs its own voltage divider
    in front of the ADC pin. DIVIDER_RATIO below assumes 10k/20k.

    The valve runs on 12 V and draws about 6 W while turning. Drive it
    through a 2-channel relay module, NEVER from a GPIO directly. Most relay
    boards sold locally are ACTIVE LOW; see VALVE_RELAY_ACTIVE_LOW.

    Do NOT power a SIM800L from the ESP32. It draws bursts near 2 A and will
    brown the board out. Give it its own 3.7-4.2 V supply, grounds tied.

  WHAT IT SERVES
    http://<ip>/        live status, valve state, and the manual controls
    http://<ip>/status  the same as JSON
    http://<ip>/wifi    network, claim code and server address; behind
                        CONFIG_USERNAME / CONFIG_PASSWORD

  SERIAL COMMANDS (115200 baud)
    Z    zero the pressure sensor - do this once, with the line EMPTY.
         Until it is done the board reports its own resting voltage as
         pressure, about 0.45 bar on a dry hose, and every threshold is
         being compared against a meaningless number.
    V    run a valve test now (still waits for the line to be idle)
    O    open the valve, abandoning any test
    N    force night mode on or off, to test the night-flow window
    S/R  simulated readings on / off
  ============================================================================
*/

#include <math.h>
#include <HardwareSerial.h>
#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <Preferences.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>  // HTTPS to a hosted server
#include <time.h>              // NTP, for the night-flow window
#include <WiFiUDP.h>

// ============================================================================
// USER CONFIGURATION
// ============================================================================

// One board serves one household, so there is one sensor pair. The loops
// below still run over NUM_MODULES so the sampling and filtering code did not
// have to be rewritten - it simply runs once.
#define NUM_MODULES 1

// ---- Pin assignments -------------------------------------------------------
//
//   Flow sensor  (YF-S201 signal)      GPIO 27
//   Pressure     (transducer, divided) GPIO 34   <- must stay on ADC1
//   Valve relay  OPEN                  GPIO 25
//   Valve relay  CLOSE                 GPIO 26
//   GSM module   RX / TX               GPIO 16 / 17  (see below)
//
// The pressure pin must be an ADC1 pin (32-39). ADC2 is taken over by the WiFi
// radio, and analogRead() on an ADC2 pin simply blocks once WiFi is up - the
// board looks alive and reports nothing.
//
// 25 and 26 are free now that modules 2 and 3 are gone, and they are safe
// choices for outputs: neither is a strapping pin, so holding one at a level
// during reset cannot stop the board booting.
const uint8_t FLOW_PINS[NUM_MODULES] = {27};
const uint8_t PRESSURE_PINS[NUM_MODULES] = {34};

// ---- Motorised ball valve --------------------------------------------------
//
// Driven through a 2-channel relay module, never straight from a GPIO: the
// valve runs on 12 V and draws about 6 W while turning, which no ESP32 pin can
// supply and which would destroy it trying.
//
// Most relay boards sold here are ACTIVE LOW - the relay closes when the input
// is pulled to ground. That matters at power-on: a pin floats for a moment
// before setup() runs, and on an active-low board a floating pin can energise
// the relay. Both pins are therefore driven to the de-energised level as the
// very first thing setup() does. Set this to false for an active-high board.
#define VALVE_PIN_OPEN 25
#define VALVE_PIN_CLOSE 26
#define VALVE_RELAY_ACTIVE_LOW true

// How long the actuator takes to travel end to end, plus a margin. The relay
// is de-energised after this whatever happens: a relay left on is how the
// motor burns out, and a stuck state machine must not be able to cause that.
#define VALVE_TRAVEL_MS 8000

// ---- Flow sensor calibration (shared across identical sensor models) ----
#define PULSES_PER_LITER 450.0 // YF-S201 typical - confirm per datasheet

// The sensor's own ceiling. A YF-S201 tops out near 30 L/min, which is about
// 225 pulses a second; during testing a module reported 538 L/min, or roughly
// 4,000 pulses a second, which is electrical noise on the signal line rather
// than water. With one sensor per board there is no second reading to show
// the first is nonsense, so an impossible figure is discarded here.
#define FLOW_MAX_PLAUSIBLE_LPM 40.0

// Two pulses closer together than this cannot both be real at that ceiling.
// Cheap insurance against a long or unshielded signal cable.
#define FLOW_MIN_PULSE_GAP_US 2000

// ---- Pressure sensor calibration (shared across identical sensor models) ----
#define SENSOR_VOUT_MIN 0.5
#define SENSOR_VOUT_MAX 4.5
#define PRESSURE_MAX_BAR 12.0
#define DIVIDER_RATIO (20000.0 / (10000.0 + 20000.0)) // R2/(R1+R2)
#define ADC_MAX_COUNT 4095.0
#define ADC_VREF 3.3

// The ESP32's ADC is noisy enough that one reading is not worth much; these
// are averaged per sample, the same approach as sensor_test.ino.
#define PRESSURE_SAMPLES 16
#define PRESSURE_SAMPLE_GAP_US 500

// Raw-count thresholds for spotting a mis-wired sensor. A live transducer
// idles around 414 counts (0.5 V through the divider), so a reading near zero
// means no sensor rather than no pressure.
#define ADC_DEAD_BELOW 20
#define ADC_SATURATED_ABOVE 4075

// ---- Timing ----
#define SAMPLE_INTERVAL_MS 1000
#define FILTER_HISTORY_SIZE 5
#define EMA_ALPHA 0.2

// ---- Detection thresholds --------------------------------------------------
//
// A leak is water leaving the line when nobody is using any. The board only
// measures flow and pressure, so it tells use from loss by the PATTERN over
// time, never by one reading: household use is a large rush that stops, a
// leak is a small trickle that does not.
//
// Every figure here is a starting value. The ones that matter are pushed down
// from the dashboard and kept in flash, so a line is tuned to its site
// without anyone reflashing a board.

// Below this the line is reported as low supply. 0.3 bar is about 3 m of
// head, the minimum residual pressure gravity-fed rural systems are designed
// around. The old 1.5 bar suited a pressurised municipal main and reported a
// perfectly healthy gravity line as low supply forever.
#define P_LOW_THRESHOLD_BAR_DEFAULT 0.3
#define LOW_THRESHOLD_MIN_BAR 0.05
#define LOW_THRESHOLD_MAX_BAR 12.0

// Anything under this counts as "no water moving". Above the sensor's own
// noise floor, below the smallest real trickle it can register.
#define FLOW_ZERO_THRESHOLD_LPM 0.3

// --- Test 1: burst. Flow jumps high AND pressure collapses, together, and
// stays that way. Normal use does not crash the pressure; a split hose does.
#define BURST_FLOW_LPM 3.0
#define BURST_PRESSURE_DROP_RATIO 0.5   // below half the line's usual pressure
#define BURST_CONFIRM_S 60

// --- Test 2: sustained low flow. A steady trickle that never returns to
// zero. Nobody runs a tap at 0.5 L/min for half an hour.
#define SUSTAINED_FLOW_MIN_LPM 0.2
#define SUSTAINED_FLOW_MAX_LPM 2.0
#define SUSTAINED_MINUTES_DEFAULT 30

// --- Test 3: night flow. Any flow at all, for long enough, when the whole
// barangay should be asleep. Needs real time, so it is silent until NTP has
// answered - guessing the hour would raise alarms at random.
#define NIGHT_START_HOUR 1
#define NIGHT_END_HOUR 4
#define NIGHT_FLOW_MINUTES 5

// --- Test 4: valve-closed pressure decay. The only test that finds a leak
// too small for the flow sensor to feel. Seal the line and watch: intact hose
// holds its pressure, a hole bleeds it away.
#define VALVE_TEST_IDLE_MINUTES 10      // no flow for this long before testing
#define VALVE_TEST_INTERVAL_MINUTES 60  // how often to try
#define VALVE_SETTLE_S 30               // let the pressure stop swinging
#define VALVE_HOLD_S 300                // 5 min: long enough to see a drip
#define DECAY_MINOR_BAR 0.03            // anything smaller is sensor noise
#define DECAY_MAJOR_BAR 0.10

// The line's own resting pressure, learned slowly so that a burst can be
// judged against what this hose normally does rather than a fixed number.
#define BASELINE_ALPHA 0.002

// Samples agreeing before a status actually changes. At one sample a second
// this is 15 s, which is long enough to ride out a tap being opened and short
// enough that a burst is still reported almost immediately.
#define REQUIRED_CONSECUTIVE 15


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
// The board's default address. Used until something is saved through the
// setup page, and - importantly - used to CLAIM, because a blank board has to
// reach the server before it can be told anything, and it cannot ask the
// server where the server is.
//
// While the server is a laptop this is its LAN IP, and it has to be re-saved
// whenever that laptop moves. Once the system is hosted it becomes one fixed
// https:// address that never changes and nobody ever types.
//
// "localhost" and 127.0.0.1 will NOT work - those mean the ESP32 itself.
#define DEFAULT_SERVER_URL "http://192.168.1.21:8000/api/ingest/"

// ---- Device identity -------------------------------------------------------
//
// Nothing is compiled in. The board is told who it is through its own setup
// page, exactly like the WiFi credentials, and keeps it in flash.
//
// The alternative - a DEVICE_ID and DEVICE_API_KEY baked into the sketch -
// means one firmware build per household. Six houses is six builds, and
// flashing the wrong one to the wrong house gives two boards reporting as the
// same household, overwriting each other's readings with nothing on screen to
// say so. It also puts a live credential in the source repository and makes
// replacing a dead board a job that needs a laptop and the project folder.
//
// The key is never typed. A claim code is: six characters, single use, worth
// nothing afterwards. The board trades it for its real identity the first
// time it reaches the server.

#define UPLOAD_INTERVAL_MS 2000 // how often to POST readings
#define HTTP_TIMEOUT_MS 5000    // give up on a POST after this long

// ---- Finding the server by itself ----
// The laptop running the dashboard gets its address from whatever router it
// joins, so that address changes. Rather than being retyped every time, the
// board asks the network where the server is and remembers the answer.
#define DISCOVERY_PORT 18830
#define DISCOVERY_PROBE "SHL-DISCOVER?"
#define DISCOVERY_REPLY_WAIT_MS 700
// Three failures is about six seconds - long enough not to react to one
// dropped packet, short enough to recover before anyone notices.
#define DISCOVERY_AFTER_FAILURES 3
// Never faster than this, so a genuinely absent server means one quiet probe
// every half minute rather than a broadcast storm.
#define DISCOVERY_MIN_GAP_MS 30000

WebServer webServer(WEB_SERVER_PORT);
DNSServer dnsServer;
Preferences wifiPrefs;

String cfgSSID = ""; // credentials currently in use (from NVS or defaults)
String cfgPassword = "";
String cfgServerUrl = ""; // where readings are POSTed (from NVS or the default)

// Who this board is, as issued by the server and stored in flash. Empty means
// the board has never been claimed and has nothing to authenticate with.
String cfgDeviceId = "";
String cfgApiKey = "";

// A claim code typed on the setup page, kept until the board is on a network
// and can actually use it. It cannot be used while the setup page is open -
// the board is serving its own hotspot then, with no route to the server.
String pendingClaimCode = "";
String lastClaimResult = "not claimed yet";

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

// How bad the board judges a leak to be, from the flow rate or from how fast
// a sealed line loses pressure. Sent to the server, which decides what to do
// about it - the board does not know who lives here or how to reach them.
enum LeakSeverity
{
  SEVERITY_NONE,
  SEVERITY_MINOR,
  SEVERITY_MODERATE,
  SEVERITY_MAJOR
};

// Which test fired. Worth carrying all the way to the dashboard, because the
// tests answer different questions and the right response differs: sustained
// low flow is as likely to be a tap left open as a leak, while flow at two in
// the morning on a line the family says was idle is hard to explain away.
enum LeakTrigger
{
  TRIGGER_NONE,
  TRIGGER_BURST,
  TRIGGER_SUSTAINED,
  TRIGGER_NIGHT,
  TRIGGER_DECAY,
  TRIGGER_LOW_SUPPLY
};

// The sealed-line test, one stage at a time. It has to be a state machine
// rather than a blocking routine: the valve is shut while it runs, and
// everything else - the web server, uploads, watching for somebody opening a
// tap - has to keep working throughout.
enum ValveStage
{
  VALVE_IDLE,
  VALVE_CLOSING,
  VALVE_SETTLING,
  VALVE_HOLDING,
  VALVE_OPENING
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

  // Simulation / injection mode for testing logic without real hardware
  bool simulationMode;
  float simPressure;
  float simFlow;

  // What this hose normally sits at, learned slowly. A burst is judged
  // against the line's own habits rather than a number chosen in advance,
  // because a line's resting pressure depends on how far below the source it
  // runs and nobody knows that until it is built.
  float baselinePressure;
  bool baselineReady;

  // Flow over time, which is the whole basis of the first three tests.
  unsigned long flowStartedMillis;   // when the current unbroken flow began
  unsigned long lastZeroFlowMillis;  // when water last actually stopped
  bool flowing;

  // How long the current candidate leak has looked like one, so a burst is
  // only called after it has persisted rather than on a single spike.
  unsigned long burstSinceMillis;

  // The result of the last sealed-line test, kept until the next one runs.
  LeakSeverity decaySeverity;
  float lastDecayBar;

  // Status + hysteresis
  SystemStatus currentStatus;
  SystemStatus candidateStatus;
  uint16_t consecutiveCount;

  // What the current status is blamed on, and how bad it looks.
  LeakSeverity severity;
  LeakTrigger trigger;

  // Prevents sending one SMS per sample while an alarm condition persists
  bool leakNotified;

  // Raw ADC state, kept so the sensor can be diagnosed without a multimeter.
  // A correctly wired transducer reads ~0.5 V at zero pressure, which converts
  // to 0.00 bar - exactly what an unplugged pin also reports. Only the raw
  // count tells those two apart, so it is carried through to the serial line,
  // the device's own page and /status.
  int lastAdcRaw;
  float lastVAdc;
  float lastVSensor;
  uint8_t adcWarnState; // 0 = fine, 1 = reads ~0, 2 = saturated

  // This sensor's own reading with no water in the line, used as its 0 bar.
  // Defaults to the datasheet's 0.5 V until 'Z' measures the real one.
  float zeroVSensor;

  // Below this pressure the line is reported as LOW SUPPLY. Owned by the
  // server (see applyServerConfig) and mirrored in flash, so the board keeps
  // using the facilitator's figure through a reboot or a WiFi outage rather
  // than silently reverting to a compiled-in default.
  float lowPressureBar;

  // The filter history starts empty; seeding it with the first real reading
  // stops the first few samples being dragged toward zero by the blanks.
  bool primed;
};

ModuleState modules[NUM_MODULES];


unsigned long lastSampleMillis = 0;
unsigned long lastUploadMillis = 0;

// Outcome of the most recent upload attempt, surfaced on the device's own
// dashboard and in /status so a failing uplink is visible without a laptop.
String lastUploadResult = "not attempted yet";
bool lastUploadOk = false;

// Consecutive failed uploads, and when we last asked the network where the
// server is. Together these decide when to go looking for a moved server.
uint8_t uploadFailStreak = 0;
unsigned long lastDiscoveryMillis = 0;
String lastDiscoveryResult = "not needed yet";

// ---- Valve test state (one valve, so one set of these) ----
ValveStage valveStage = VALVE_IDLE;
unsigned long valveStageMillis = 0;
unsigned long lastValveTestMillis = 0;
float pressureAtHoldStart = 0;
bool valveIsOpen = true;             // driven open in setup(), before anything else
bool valveTestRequested = false;     // from the dashboard or the serial console
String lastValveResult = "no test yet";

// ---- Clock ----
// The night-flow test needs to know the hour, and an ESP32 has no clock of its
// own. Until NTP answers, that test simply does not run: guessing the time
// would raise alarms at random hours, which is worse than not running.
bool clockReady = false;
bool fakeNightMode = false;          // serial command, for testing the window

// ============================================================================
// INTERRUPT SERVICE ROUTINE
// ============================================================================
// One sensor, one ISR. Debounced in hardware terms: pulses arriving closer
// together than the sensor can physically produce them are noise picked up on
// the signal line, not water, and counting them is how a dry hose comes to
// report hundreds of litres a minute.
volatile unsigned long lastFlowPulseMicros = 0;

void IRAM_ATTR flowPulseISR0()
{
  unsigned long now = micros();
  if (now - lastFlowPulseMicros < FLOW_MIN_PULSE_GAP_US)
    return;
  lastFlowPulseMicros = now;
  modules[0].flowPulseCount++;
}

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

// Reads one module's pressure transducer and returns bar.
//
// Averages several ADC samples rather than taking one: a single ESP32 read
// can swing by hundreds of counts, and averaging is what makes the readings
// in sensor_test.ino steady. The raw count and both voltages are kept on the
// module so they can be reported - without them a dead sensor and a sensor at
// zero pressure are indistinguishable, because both end up as 0.00 bar.
float readPressureBar(ModuleState &m)
{
  long sumRaw = 0;
  long sumMilliVolts = 0;
  for (uint8_t i = 0; i < PRESSURE_SAMPLES; i++)
  {
    sumRaw += analogRead(m.pressurePin);
    // analogReadMilliVolts applies the calibration burned into the chip at
    // the factory. Converting the raw count by hand assumes a perfectly
    // linear 0-3.3 V ADC, which the ESP32's is not - it reads low, worst at
    // the bottom of the range, which is exactly where a pressure sensor
    // sits when there is no water.
    sumMilliVolts += analogReadMilliVolts(m.pressurePin);
    delayMicroseconds(PRESSURE_SAMPLE_GAP_US);
  }

  m.lastAdcRaw = (int)(sumRaw / (float)PRESSURE_SAMPLES + 0.5f);
  m.lastVAdc = (sumMilliVolts / (float)PRESSURE_SAMPLES) / 1000.0f;
  m.lastVSensor = m.lastVAdc / DIVIDER_RATIO;

  // Measured against this module's own zero point rather than the datasheet's
  // 0.5 V. Sensors vary, and a module resting slightly below 0.5 V would
  // otherwise have to climb past it before reporting any pressure at all,
  // losing the bottom of its range.
  float pressureBar = (m.lastVSensor - m.zeroVSensor) *
                      (PRESSURE_MAX_BAR / (SENSOR_VOUT_MAX - SENSOR_VOUT_MIN));

  if (pressureBar < 0)
    pressureBar = 0;
  return pressureBar;
}

// Records what each sensor reads with no water in the line, so that becomes
// its 0.00 bar. Run it with the system depressurised.
void zeroPressureSensors(int8_t onlyIndex)
{
  wifiPrefs.begin(WIFI_PREFS_NAMESPACE, false);
  for (uint8_t i = 0; i < NUM_MODULES; i++)
  {
    if (onlyIndex >= 0 && onlyIndex != (int8_t)i)
      continue;

    ModuleState &m = modules[i];
    readPressureBar(m); // refresh lastVSensor before taking it as the zero

    if (m.lastAdcRaw < ADC_DEAD_BELOW)
    {
      Serial.printf("[ZERO] M%d skipped - ADC reads %d, so there is no sensor to zero.\n",
                    m.id, m.lastAdcRaw);
      continue;
    }

    m.zeroVSensor = m.lastVSensor;
    char key[8];
    snprintf(key, sizeof(key), "z%u", (unsigned)m.id);
    wifiPrefs.putFloat(key, m.zeroVSensor);

    m.primed = false; // re-seed the filters against the new zero
    Serial.printf("[ZERO] M%d zero set at %.3f V (raw %d). Saved.\n",
                  m.id, m.zeroVSensor, m.lastAdcRaw);
  }
  wifiPrefs.end();
}

// ---------------------------------------------------------------------------
// TUNABLE THRESHOLDS (set from the dashboard, mirrored in flash)
// ---------------------------------------------------------------------------
// The right low-supply threshold is a property of the site, not of the code:
// a gravity line sits at roughly 1 bar per 10 m its source is above it, so
// the correct figure differs per line and is only knowable once the line
// exists. Holding it here - server-owned, flash-backed - means it can be
// tuned from a phone on the barangay's own WiFi, and the board still applies
// the last known figure while offline.

void loadThresholds()
{
  wifiPrefs.begin(WIFI_PREFS_NAMESPACE, true); // read-only
  for (uint8_t i = 0; i < NUM_MODULES; i++)
  {
    char key[8];
    snprintf(key, sizeof(key), "lp%u", (unsigned)modules[i].id);
    modules[i].lowPressureBar = wifiPrefs.getFloat(key, P_LOW_THRESHOLD_BAR_DEFAULT);
  }
  wifiPrefs.end();
}

// Saves only when the value actually moved. Flash has a finite erase budget
// and this runs off a reply the board receives every couple of seconds, so
// writing unconditionally would wear the partition out for no reason.
void storeThreshold(ModuleState &m, float bar)
{
  if (fabs(bar - m.lowPressureBar) < 0.001f)
    return;

  Serial.printf("[CONFIG] M%u low-supply threshold %.2f -> %.2f bar (from server)\n",
                (unsigned)m.id, m.lowPressureBar, bar);
  m.lowPressureBar = bar;

  char key[8];
  snprintf(key, sizeof(key), "lp%u", (unsigned)m.id);
  wifiPrefs.begin(WIFI_PREFS_NAMESPACE, false);
  wifiPrefs.putFloat(key, bar);
  wifiPrefs.end();
}

// Pulls one module's number out of the server's reply.
//
// A full JSON parser is not worth 30 KB of program space for one field, but
// a naive search of the whole body would be worse than useless: it would
// happily match a number that belongs to a different module. So this narrows
// to the object for this module first - "config" then "<id>": { ... } - and
// only searches for the key inside those braces.
// Pulls one string field out of a JSON reply. Same reasoning as
// extractModuleFloat: a full JSON parser is not worth 30 KB of program space
// for two fields, and the claim reply is a flat object we control.
bool extractJsonString(const String &body, const char *key, String &out)
{
  String needle = String("\"") + key + String("\":");
  int at = body.indexOf(needle);
  if (at < 0)
    return false;

  int open = body.indexOf('"', at + needle.length());
  if (open < 0)
    return false;
  int close = body.indexOf('"', open + 1);
  if (close < 0)
    return false;

  out = body.substring(open + 1, close);
  return out.length() > 0;
}

bool extractModuleFloat(const String &body, uint8_t moduleId, const char *key, float &out)
{
  int configAt = body.indexOf("\"config\"");
  if (configAt < 0)
    return false;

  String moduleKey = String("\"") + String((unsigned)moduleId) + String("\":");
  int moduleAt = body.indexOf(moduleKey, configAt);
  if (moduleAt < 0)
    return false;

  int openBrace = body.indexOf('{', moduleAt);
  int closeBrace = body.indexOf('}', openBrace);
  if (openBrace < 0 || closeBrace < 0)
    return false;

  String fieldKey = String("\"") + String(key) + String("\":");
  int fieldAt = body.indexOf(fieldKey, openBrace);
  if (fieldAt < 0 || fieldAt > closeBrace)
    return false;

  out = body.substring(fieldAt + fieldKey.length(), closeBrace).toFloat();
  return true;
}

// Applies whatever the server sent back with the last upload.
//
// Values are range-checked before being trusted. The board keeps running on
// its own for hours at a time, so a corrupted or truncated reply must not be
// able to leave it with a threshold of 0 (never alarms) or 99 (always does).
void applyServerConfig(const String &body)
{
  for (uint8_t i = 0; i < NUM_MODULES; i++)
  {
    float bar;
    if (!extractModuleFloat(body, modules[i].id, "low_pressure_bar", bar))
      continue;
    if (bar < LOW_THRESHOLD_MIN_BAR || bar > LOW_THRESHOLD_MAX_BAR)
    {
      Serial.printf("[CONFIG] M%u ignoring implausible threshold %.2f bar.\n",
                    (unsigned)modules[i].id, bar);
      continue;
    }
    storeThreshold(modules[i], bar);
  }
}

void loadZeroOffsets()
{
  wifiPrefs.begin(WIFI_PREFS_NAMESPACE, true);
  for (uint8_t i = 0; i < NUM_MODULES; i++)
  {
    char key[8];
    snprintf(key, sizeof(key), "z%u", (unsigned)modules[i].id);
    modules[i].zeroVSensor = wifiPrefs.getFloat(key, SENSOR_VOUT_MIN);
  }
  wifiPrefs.end();
}

// Warns about the two wiring faults that look like a valid zero reading.
// Only fires when the condition changes, so a genuine fault is stated once
// instead of scrolling the monitor every second.
void checkPressureWiring(ModuleState &m)
{
  uint8_t state = 0;
  if (m.lastAdcRaw < ADC_DEAD_BELOW)
    state = 1;
  else if (m.lastAdcRaw > ADC_SATURATED_ABOVE)
    state = 2;

  if (state == m.adcWarnState)
    return;
  m.adcWarnState = state;

  if (state == 1)
  {
    Serial.printf("  ! M%d pressure ADC reads ~0 (raw %d). Sensor unpowered, signal wire "
                  "loose, or wrong pin. A working sensor idles near %.0f counts.\n",
                  m.id, m.lastAdcRaw,
                  SENSOR_VOUT_MIN * DIVIDER_RATIO / ADC_VREF * ADC_MAX_COUNT);
  }
  else if (state == 2)
  {
    Serial.printf("  ! M%d pressure ADC saturated (raw %d). Voltage above the 3.3 V the ADC "
                  "accepts - check the divider resistors.\n",
                  m.id, m.lastAdcRaw);
  }
  else
  {
    Serial.printf("  M%d pressure sensor reading normally again (raw %d).\n", m.id, m.lastAdcRaw);
  }
}

float computeFlowRateLPM(uint32_t pulses, unsigned long elapsedMillis)
{
  float liters = pulses / PULSES_PER_LITER;
  float minutes = elapsedMillis / 60000.0;
  if (minutes <= 0)
    return 0;

  float lpm = liters / minutes;

  // A reading the sensor cannot physically produce is noise on the signal
  // line, not water. Reporting it would be worse than dropping it: it drags
  // the filters and would trip the burst test on a dry hose.
  if (lpm > FLOW_MAX_PLAUSIBLE_LPM)
  {
    Serial.printf("[FLOW] Ignoring %.1f L/min - above the sensor's ceiling of %.0f. "
                  "Check the signal cable length and shielding.\n",
                  lpm, (float)FLOW_MAX_PLAUSIBLE_LPM);
    return 0;
  }
  return lpm;
}

// ============================================================================
// TIME OF DAY
// ============================================================================
// The night-flow test is the cheapest strong signal the board has: flow at two
// in the morning, on a line the family will tell you was idle, is hard to
// explain as anything but a leak. It needs the actual hour, and an ESP32 has
// no clock, so this stays silent until NTP answers rather than guessing.

bool isNightTime()
{
  if (fakeNightMode)
    return true;
  if (!clockReady)
    return false;

  struct tm now;
  if (!getLocalTime(&now, 0))
    return false;
  return now.tm_hour >= NIGHT_START_HOUR && now.tm_hour < NIGHT_END_HOUR;
}

void startClock()
{
  // UTC+8, no daylight saving. configTime does not block; clockReady is set
  // once an answer has actually arrived.
  configTime(8 * 3600, 0, "pool.ntp.org", "time.nist.gov");
}

void checkClock()
{
  if (clockReady)
    return;
  struct tm now;
  if (getLocalTime(&now, 0) && now.tm_year > 120) // 2020+, so not the epoch
  {
    clockReady = true;
    Serial.printf("[TIME] Clock set: %04d-%02d-%02d %02d:%02d local.\n",
                  now.tm_year + 1900, now.tm_mon + 1, now.tm_mday,
                  now.tm_hour, now.tm_min);
  }
}

// ============================================================================
// VALVE
// ============================================================================
// A motorised ball valve, driven through a relay. It is the only way to find a
// leak too small for the flow sensor to feel: seal the hose from the source
// and watch the pressure. An intact line holds it; a hole bleeds it away, and
// pressure falling in a sealed line can only mean water is leaving it.
//
// Everything here is written around one rule: never cut water off for longer
// than the test needs, and never leave a relay energised.

void valveRelaysOff()
{
  digitalWrite(VALVE_PIN_OPEN, VALVE_RELAY_ACTIVE_LOW ? HIGH : LOW);
  digitalWrite(VALVE_PIN_CLOSE, VALVE_RELAY_ACTIVE_LOW ? HIGH : LOW);
}

void valveDrive(bool open)
{
  valveRelaysOff();
  uint8_t pin = open ? VALVE_PIN_OPEN : VALVE_PIN_CLOSE;
  digitalWrite(pin, VALVE_RELAY_ACTIVE_LOW ? LOW : HIGH);
}

// Called from the state machine once the actuator has had time to travel. A
// relay left energised is how the motor burns out, so this runs on a timer
// rather than waiting for a limit switch the valve does not have.
void valveStopAndRecord(bool nowOpen)
{
  valveRelaysOff();
  valveIsOpen = nowOpen;
}

LeakSeverity severityFromDecay(float dropBar)
{
  if (dropBar >= DECAY_MAJOR_BAR)
    return SEVERITY_MAJOR;
  if (dropBar >= DECAY_MINOR_BAR)
    return SEVERITY_MODERATE;
  return SEVERITY_NONE;
}

LeakSeverity severityFromFlow(float lpm)
{
  if (lpm >= BURST_FLOW_LPM)
    return SEVERITY_MAJOR;
  if (lpm >= 1.0)
    return SEVERITY_MODERATE;
  return SEVERITY_MINOR;
}

// The sealed-line test, one stage per loop pass. Written as a state machine
// rather than a blocking routine because the valve is shut while it runs: the
// web server, the uploads and - most importantly - watching for somebody
// opening a tap all have to keep working throughout.
void runValveTest(ModuleState &m)
{
  unsigned long now = millis();

  switch (valveStage)
  {
  case VALVE_IDLE:
  {
    bool idleLongEnough =
        !m.flowing &&
        (now - m.lastZeroFlowMillis) >= (unsigned long)VALVE_TEST_IDLE_MINUTES * 60000UL;
    bool dueByInterval =
        (now - lastValveTestMillis) >= (unsigned long)VALVE_TEST_INTERVAL_MINUTES * 60000UL;

    // A test asked for by hand still waits for the line to be idle. The point
    // of the interval is to be unobtrusive; the point of the idle wait is to
    // never cut water off while somebody is using it, and that one is not
    // negotiable even for a demonstration.
    if (!idleLongEnough)
      return;
    if (!valveTestRequested && !dueByInterval)
      return;

    valveTestRequested = false;
    valveStage = VALVE_CLOSING;
    valveStageMillis = now;
    valveDrive(false);
    lastValveResult = "closing the valve";
    Serial.println("[VALVE] Closing to test the line.");
    break;
  }

  case VALVE_CLOSING:
    if (now - valveStageMillis >= VALVE_TRAVEL_MS)
    {
      valveStopAndRecord(false);
      valveStage = VALVE_SETTLING;
      valveStageMillis = now;
      lastValveResult = "settling";
    }
    break;

  case VALVE_SETTLING:
    // Shutting a valve sends a pressure surge down the hose. Measuring during
    // it would read the water hammer rather than the line.
    if (now - valveStageMillis >= (unsigned long)VALVE_SETTLE_S * 1000UL)
    {
      pressureAtHoldStart = m.pressureEMA;
      valveStage = VALVE_HOLDING;
      valveStageMillis = now;
      lastValveResult = "holding - watching for a pressure drop";
      Serial.printf("[VALVE] Sealed at %.3f bar. Holding %d s.\n",
                    pressureAtHoldStart, VALVE_HOLD_S);
    }
    break;

  case VALVE_HOLDING:
    // Somebody opened a tap. Their water matters more than the test, and the
    // reading would be meaningless anyway - the line is no longer sealed.
    if (m.flowEMA > FLOW_ZERO_THRESHOLD_LPM)
    {
      Serial.println("[VALVE] Flow during test - abandoning and reopening.");
      lastValveResult = "abandoned - a tap was opened";
      valveStage = VALVE_OPENING;
      valveStageMillis = now;
      valveDrive(true);
      break;
    }

    if (now - valveStageMillis >= (unsigned long)VALVE_HOLD_S * 1000UL)
    {
      float drop = pressureAtHoldStart - m.pressureEMA;
      m.lastDecayBar = drop;
      m.decaySeverity = severityFromDecay(drop);

      if (m.decaySeverity == SEVERITY_NONE)
        lastValveResult = "line holds pressure - no leak found";
      else
        lastValveResult = "pressure fell " + String(drop, 3) + " bar - leak in the sealed line";

      Serial.printf("[VALVE] Held %d s, pressure fell %.3f bar -> %s\n",
                    VALVE_HOLD_S, drop, lastValveResult.c_str());

      valveStage = VALVE_OPENING;
      valveStageMillis = now;
      valveDrive(true);
      lastValveTestMillis = now;
    }
    break;

  case VALVE_OPENING:
    if (now - valveStageMillis >= VALVE_TRAVEL_MS)
    {
      valveStopAndRecord(true);
      valveStage = VALVE_IDLE;
      lastValveTestMillis = now;
    }
    break;
  }
}

// ============================================================================
// LEAK CLASSIFICATION
// ============================================================================
// Four tests, in the order they should be trusted. None of them computes an
// expected flow from pressure: that method needed a calibration nobody could
// do reliably in the field, and it could only ever describe a line whose
// conductance had not changed since the day it was measured.

SystemStatus classify(ModuleState &m, float pressure, float flow)
{
  unsigned long now = millis();
  m.severity = SEVERITY_NONE;
  m.trigger = TRIGGER_NONE;

  // --- Track when water is and is not moving. Every test below is really a
  // question about how long something has been true. ---
  bool moving = flow > FLOW_ZERO_THRESHOLD_LPM;
  if (moving && !m.flowing)
  {
    m.flowing = true;
    m.flowStartedMillis = now;
  }
  else if (!moving && m.flowing)
  {
    m.flowing = false;
    m.lastZeroFlowMillis = now;
  }
  if (!moving)
    m.lastZeroFlowMillis = now;

  // The line's own resting pressure, learned only while nothing is drawn.
  // Learning it during use would drag the baseline down towards the pressure
  // a running tap causes, and a burst would then look normal.
  if (!moving && pressure > 0.05)
  {
    m.baselinePressure = m.baselineReady
                             ? m.baselinePressure + BASELINE_ALPHA * (pressure - m.baselinePressure)
                             : pressure;
    m.baselineReady = true;
  }

  // --- Test 1: burst. Flow high AND pressure collapsed, together, sustained.
  // Normal use does not crash the pressure; a split hose does. Checked first
  // because it is the only one where minutes matter. ---
  bool pressureCollapsed =
      m.baselineReady && pressure < m.baselinePressure * BURST_PRESSURE_DROP_RATIO;
  if (flow >= BURST_FLOW_LPM && pressureCollapsed)
  {
    if (m.burstSinceMillis == 0)
      m.burstSinceMillis = now;
    if (now - m.burstSinceMillis >= (unsigned long)BURST_CONFIRM_S * 1000UL)
    {
      m.severity = SEVERITY_MAJOR;
      m.trigger = TRIGGER_BURST;
      return STATUS_POSSIBLE_LEAK;
    }
  }
  else
  {
    m.burstSinceMillis = 0;
  }

  // --- Test 4's verdict, carried forward. The sealed-line test runs on its
  // own schedule, so its result is reported here until the next test replaces
  // it. This is the only test that finds a pinhole. ---
  if (m.decaySeverity != SEVERITY_NONE)
  {
    m.severity = m.decaySeverity;
    m.trigger = TRIGGER_DECAY;
    return STATUS_POSSIBLE_LEAK;
  }

  // --- Test 3: night flow. Any flow, for long enough, when nobody should be
  // using water. Cheap and strong - but silent until the clock is known. ---
  if (m.flowing && isNightTime())
  {
    if (now - m.flowStartedMillis >= (unsigned long)NIGHT_FLOW_MINUTES * 60000UL)
    {
      m.severity = severityFromFlow(flow);
      m.trigger = TRIGGER_NIGHT;
      return STATUS_POSSIBLE_LEAK;
    }
  }

  // --- Test 2: sustained low flow. A steady trickle that never returns to
  // zero. Nobody runs a tap at half a litre a minute for half an hour - but a
  // tap left slightly open looks exactly like this, which is why the alert
  // this raises asks the owner to check their faucets first. ---
  if (m.flowing && flow >= SUSTAINED_FLOW_MIN_LPM && flow <= SUSTAINED_FLOW_MAX_LPM)
  {
    if (now - m.flowStartedMillis >= (unsigned long)SUSTAINED_MINUTES_DEFAULT * 60000UL)
    {
      m.severity = severityFromFlow(flow);
      m.trigger = TRIGGER_SUSTAINED;
      return STATUS_POSSIBLE_LEAK;
    }
  }

  // --- Weak supply is not a leak. Low pressure with nothing flowing means
  // the source is not delivering, and sending somebody to look for a hole
  // would waste the trip. ---
  if (pressure < m.lowPressureBar)
  {
    m.trigger = TRIGGER_LOW_SUPPLY;
    return STATUS_LOW_SUPPLY;
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

const char *severityToString(LeakSeverity s)
{
  switch (s)
  {
  case SEVERITY_MINOR:    return "minor";
  case SEVERITY_MODERATE: return "moderate";
  case SEVERITY_MAJOR:    return "major";
  default:                return "none";
  }
}

const char *triggerToString(LeakTrigger t)
{
  switch (t)
  {
  case TRIGGER_BURST:      return "burst";
  case TRIGGER_SUSTAINED:  return "sustained";
  case TRIGGER_NIGHT:      return "night";
  case TRIGGER_DECAY:      return "decay";
  case TRIGGER_LOW_SUPPLY: return "low_supply";
  default:                 return "none";
  }
}

// What the server is told about the valve. "not_fitted" is deliberately
// distinct from "unknown": a line with no valve is working as designed, while
// a valve whose state cannot be read is a fault, and collapsing the two would
// hide every real fault behind the boards that have no valve yet.
const char *valveToString()
{
  switch (valveStage)
  {
  case VALVE_CLOSING:
  case VALVE_OPENING:
    return "moving";
  case VALVE_SETTLING:
  case VALVE_HOLDING:
    return "closed";
  default:
    return valveIsOpen ? "open" : "closed";
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
// SMS from the board is a LAST RESORT, not the normal path.
//
// The server owns the household database, so it is the only thing that can
// name a person and write a sentence they can act on, and it sends the real
// alert. This fires only when the board could not reach the server at all -
// otherwise the owner would get two texts for every leak, which is how people
// learn to ignore them.
//
// There is no "cleared" text from here either. An all-clear is only worth
// sending to somebody who was told about the problem, and the server knows
// who that was; the board does not.
void checkAndNotifyLeak(ModuleState &m)
{
  if (m.currentStatus == STATUS_POSSIBLE_LEAK && !m.leakNotified)
  {
    if (lastUploadOk)
    {
      // The server has it and will do this properly. Remember that we have
      // seen this leak, so a later outage does not re-announce it.
      m.leakNotified = true;
      return;
    }

    String who = cfgDeviceId.length() ? cfgDeviceId : String("this line");
    String msg = "LEAK ALERT " + who + ": possible leak (" +
                 String(triggerToString(m.trigger)) + ", " +
                 String(severityToString(m.severity)) + "). Please check all your faucets "
                 "are closed. If they are and water still runs, tell the facilitator.";
    Serial.println("[LEAK] Server unreachable - texting the owner directly.");
    sendSMS(OWNER_PHONE_NUMBER, msg);
    m.leakNotified = true;
  }
  else if (m.currentStatus != STATUS_POSSIBLE_LEAK && m.leakNotified)
  {
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
// server keys each reading by "module" (not "id"), and it wraps the batch in
// the device identity this board was issued when it claimed.
// The status tokens from statusToString() - "NORMAL", "LOW WATER SUPPLY",
// "POSSIBLE LEAK" - are already exactly the strings the server accepts.
String buildPayload()
{
  String json = "{\"device_id\":\"" + cfgDeviceId + "\",\"readings\":[";

  for (uint8_t i = 0; i < NUM_MODULES; i++)
  {
    ModuleState &m = modules[i];
    if (i > 0)
      json += ",";
    json += "{\"module\":" + String(m.id);
    json += ",\"pressure_bar\":" + String(m.pressureEMA, 3);
    json += ",\"flow_lpm\":" + String(m.flowEMA, 3);
    json += ",\"status\":\"" + String(statusToString(m.currentStatus)) + "\"";
    json += ",\"severity\":\"" + String(severityToString(m.severity)) + "\"";
    json += ",\"trigger\":\"" + String(triggerToString(m.trigger)) + "\"";
    json += ",\"valve_state\":\"" + String(valveToString()) + "\"";
    json += ",\"simulated\":" + String(m.simulationMode ? "true" : "false");
    json += "}";
  }

  json += "]}";
  return json;
}

// Asks the network where the dashboard is and adopts the answer.
//
// Broadcasts a short probe; the server replies with the URL to post to. The
// result is saved to flash, so the board only has to do this once per move
// and comes back up already pointed at the right place.
//
// Returns true when a new address was adopted.
bool discoverServer()
{
  if (apMode || WiFi.status() != WL_CONNECTED)
    return false;

  lastDiscoveryMillis = millis();

  WiFiUDP udp;
  if (!udp.begin(0)) // any free local port
  {
    lastDiscoveryResult = "could not open a socket";
    return false;
  }

  IPAddress broadcast = WiFi.localIP();
  broadcast[3] = 255; // this subnet's broadcast address

  udp.beginPacket(broadcast, DISCOVERY_PORT);
  udp.print(DISCOVERY_PROBE);
  udp.endPacket();

  unsigned long deadline = millis() + DISCOVERY_REPLY_WAIT_MS;
  while ((long)(millis() - deadline) < 0)
  {
    int size = udp.parsePacket();
    if (size <= 0)
    {
      delay(10);
      continue;
    }

    char buffer[192];
    int read = udp.read(buffer, sizeof(buffer) - 1);
    if (read <= 0)
      continue;
    buffer[read] = '\0';

    // The reply is {"ingest":"http://..."}. Pulling the one value out by
    // hand keeps the sketch free of a JSON library for a single field.
    String body(buffer);
    int keyAt = body.indexOf("\"ingest\"");
    if (keyAt < 0)
      continue;
    int start = body.indexOf('"', body.indexOf(':', keyAt)) + 1;
    int end = body.indexOf('"', start);
    if (start <= 0 || end <= start)
      continue;

    String found = body.substring(start, end);
    if (!found.startsWith("http://") && !found.startsWith("https://"))
      continue;

    udp.stop();

    if (found == cfgServerUrl)
    {
      lastDiscoveryResult = "server confirmed the address already in use";
      return false;
    }

    Serial.printf("[DISCOVER] Server found at %s (was %s)\n",
                  found.c_str(), cfgServerUrl.c_str());
    cfgServerUrl = found;
    saveServerUrl(found);
    lastDiscoveryResult = "adopted " + found;
    uploadFailStreak = 0;
    return true;
  }

  udp.stop();
  lastDiscoveryResult = "no server answered";
  Serial.println("[DISCOVER] No server answered on this network.");
  return false;
}

// Posts the current readings. Every failure is recorded in lastUploadResult
// and otherwise ignored: the leak decision and the SMS are made here on the
// device, so neither waits on the network or on the server being up.
//
// Skipped entirely in setup-AP mode - there the ESP32 has an IP address of its
// own but no route to the server, so attempting the POST would only stall the
// loop for HTTP_TIMEOUT_MS on every pass.
// The address of an endpoint alongside /api/ingest/ on the same server. The
// setup page asks for one URL, not three, because one is what a person can
// reasonably be asked to type correctly.
String serverEndpoint(const char *path)
{
  String base = cfgServerUrl;
  int marker = base.indexOf("/api/");
  if (marker > 0)
    base = base.substring(0, marker);
  else if (base.endsWith("/"))
    base.remove(base.length() - 1);
  return base + path;
}

// A secure client when the address says https, a plain one otherwise.
//
// setInsecure() skips certificate verification. That is a real limitation and
// should be stated as one: it protects the readings from being read in
// transit but not from a server impersonating the real one. Storing a CA
// bundle on the board is the fix, and it is out of scope here.
bool beginRequest(HTTPClient &http, WiFiClientSecure &tls, const String &url)
{
  http.setTimeout(HTTP_TIMEOUT_MS);
  if (url.startsWith("https://"))
  {
    tls.setInsecure();
    return http.begin(tls, url);
  }
  return http.begin(url);
}

// Trade a claim code for this board's identity.
//
// Runs once, after the board is on a network - never from the setup page,
// where the board is serving its own hotspot and has no route to anything.
// The code is single use and the server burns it, so a board that succeeds
// here can never be claimed out from under itself by somebody reusing it.
void tryClaim()
{
  if (pendingClaimCode.length() == 0)
    return;
  if (WiFi.status() != WL_CONNECTED)
    return;

  String url = serverEndpoint("/api/claim/");
  Serial.printf("[CLAIM] Trying code %s at %s\n", pendingClaimCode.c_str(), url.c_str());

  WiFiClientSecure tls;
  HTTPClient http;
  if (!beginRequest(http, tls, url))
  {
    lastClaimResult = "bad server address";
    return;
  }

  http.addHeader("Content-Type", "application/json");
  String payload = String("{\"code\":\"") + pendingClaimCode + "\"}";
  int code = http.POST(payload);
  String body = http.getString();
  http.end();

  if (code <= 0)
  {
    // No answer at all. Almost always the site's WiFi having no route to the
    // internet, which looks identical to a wrong code unless it is said out
    // loud - so it is.
    lastClaimResult = "could not reach the server - is this network online?";
    Serial.println("[CLAIM] " + lastClaimResult);
    return;
  }

  if (code != 200)
  {
    // The server explains itself; repeating its words is more useful than
    // inventing our own. The code is kept so a temporary problem can be
    // retried, unless the server says the code itself is finished.
    lastClaimResult = "server said: " + body;
    Serial.printf("[CLAIM] Rejected (%d): %s\n", code, body.c_str());
    if (code == 404 || code == 409 || code == 410)
    {
      pendingClaimCode = "";
      wifiPrefs.begin(WIFI_PREFS_NAMESPACE, false);
      wifiPrefs.remove("claim");
      wifiPrefs.end();
      lastClaimResult += " (ask the facilitator for a new code)";
    }
    return;
  }

  String id, key;
  if (!extractJsonString(body, "device_id", id) || !extractJsonString(body, "api_key", key))
  {
    lastClaimResult = "server replied but the answer made no sense";
    return;
  }

  cfgDeviceId = id;
  cfgApiKey = key;
  pendingClaimCode = "";

  wifiPrefs.begin(WIFI_PREFS_NAMESPACE, false);
  wifiPrefs.putString("did", cfgDeviceId);
  wifiPrefs.putString("key", cfgApiKey);
  wifiPrefs.remove("claim");
  wifiPrefs.end();

  lastClaimResult = "claimed";
  Serial.printf("[CLAIM] Success. This board is %s.\n", cfgDeviceId.c_str());
}

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

  // An unclaimed board has nothing to authenticate with. Posting anyway would
  // collect a 401 every two seconds forever, and on the serial monitor that
  // looks exactly like a wrong server address - so it says which it is.
  if (cfgApiKey.length() == 0)
  {
    lastUploadOk = false;
    lastUploadResult = "not claimed - enter a claim code on /wifi";
    return;
  }

  WiFiClientSecure tls;
  HTTPClient http;

  if (!beginRequest(http, tls, cfgServerUrl))
  {
    lastUploadOk = false;
    lastUploadResult = "bad server address";
    Serial.printf("[UPLOAD] Failed to parse server address \"%s\".\n", cfgServerUrl.c_str());
    return;
  }

  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Key", cfgApiKey);

  int code = http.POST(buildPayload());

  // A reply of any kind means we are talking to the right machine, so only a
  // transport failure counts toward going to look for a moved server. A 401
  // is the wrong key, not the wrong address, and rediscovering would not
  // help.
  if (code > 0)
    uploadFailStreak = 0;
  else if (uploadFailStreak < 255)
    uploadFailStreak++;

  if (code == 200)
  {
    lastUploadOk = true;
    lastUploadResult = "OK";
    // The reply carries the settings the facilitator edited in the dashboard.
    // Reading them here means there is no second request to make and no
    // polling interval to choose - config arrives with the acknowledgement.
    applyServerConfig(http.getString());
    Serial.println("[UPLOAD] OK");
  }
  else if (code == 401)
  {
    lastUploadOk = false;
    lastUploadResult = "401 - this board's key was rejected by the server";
    Serial.println("[UPLOAD] REJECTED 401 - the server does not recognise this board's key. Ask for a new claim code and set the board up again.");
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
  cfgDeviceId = wifiPrefs.getString("did", "");
  cfgApiKey = wifiPrefs.getString("key", "");
  pendingClaimCode = wifiPrefs.getString("claim", "");
  wifiPrefs.end();

  if (cfgDeviceId.length() && cfgApiKey.length())
  {
    lastClaimResult = "claimed";
    Serial.printf("[ID] Claimed as %s.\n", cfgDeviceId.c_str());
  }
  else if (pendingClaimCode.length())
  {
    Serial.printf("[ID] Not claimed. Will try claim code %s once WiFi is up.\n",
                  pendingClaimCode.c_str());
  }
  else
  {
    Serial.println("[ID] Not claimed. Enter a claim code on the setup page (/wifi).");
  }

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
  // Only the network. clear() would wipe the whole namespace, which also
  // holds the per-sensor zero offsets, the low-supply thresholds and - since
  // this board is told who it is rather than compiled that way - its identity.
  // "Forget saved network" must forget a network, not unclaim the board and
  // throw away a calibration nobody can redo without emptying the line.
  wifiPrefs.begin(WIFI_PREFS_NAMESPACE, false);
  wifiPrefs.remove("ssid");
  wifiPrefs.remove("pass");
  wifiPrefs.end();
  Serial.println("[WIFI] Cleared the saved network. Identity, server address, "
                 "zero offsets and thresholds are kept.");
}

// Deliberately separate, and not reachable from the setup page. Unclaiming a
// board is how it gets moved to a different household, and it should take a
// considered act rather than a mistap next to "Forget network".
void clearIdentity()
{
  wifiPrefs.begin(WIFI_PREFS_NAMESPACE, false);
  wifiPrefs.remove("did");
  wifiPrefs.remove("key");
  wifiPrefs.remove("claim");
  wifiPrefs.end();
  cfgDeviceId = "";
  cfgApiKey = "";
  pendingClaimCode = "";
  lastClaimResult = "identity cleared - enter a new claim code";
  Serial.println("[ID] Identity cleared. Enter a new claim code on /wifi.");
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

  // Who this board is. First after the network on purpose: on a new board it
  // is the only other thing that has to be entered, and everything below it
  // is either already correct or only needed on the bench.
  html += "<hr style='margin:18px 0;border:0;border-top:1px solid #ddd'>";
  if (cfgDeviceId.length())
  {
    html += "<label>This board</label>";
    html += "<p class='note' style='margin-top:0'><b>Claimed as ";
    html += htmlEscape(cfgDeviceId);
    html += "</b>. Nothing to do here.</p>";
  }
  else
  {
    html += "<label for='claim'>Claim code</label>";
    html += "<input type='text' id='claim' name='claim' placeholder='K7M-4Q2' ";
    html += "autocapitalize='characters' spellcheck='false' value='";
    html += htmlEscape(pendingClaimCode);
    html += "'>";
    html += "<p class='note' style='margin-top:6px'>From the dashboard, under "
            "<b>Lines &rarr; Devices</b>. Six characters, used once. The board fetches "
            "its own ID and key from the server - you never type the key.</p>";
    if (lastClaimResult != "not claimed yet")
    {
      html += "<p class='note' style='color:#b3261e'><b>Last attempt:</b> ";
      html += htmlEscape(lastClaimResult);
      html += "</p>";
    }
  }

  // Where readings are sent. Kept on the same form so one visit can fix both
  // the network and the server after a move.
  html += "<hr style='margin:18px 0;border:0;border-top:1px solid #ddd'>";
  html += "<label for='server'>Server address</label>";
  html += "<input type='text' id='server' name='server' value='";
  html += htmlEscape(cfgServerUrl);
  html += "'>";
  html += "<p class='note' style='margin-top:6px'>Already filled in - leave it alone unless "
          "you are told otherwise. It must end in <b>/api/ingest/</b>. Leave the network "
          "fields blank to change only this; the device will not reboot.</p>";

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

  String claim = webServer.arg("claim");
  claim.trim();
  claim.toUpperCase();

  // Catch the two mistakes that produce a board which looks connected but
  // silently never delivers: a bare IP, and localhost. 127.0.0.1 means the
  // ESP32 itself, so it can never reach the laptop that way.
  if (server.length() > 0)
  {
    if (!server.startsWith("http://") && !server.startsWith("https://"))
    {
      webServer.send(400, "text/html",
                     buildWifiPage("The server address must start with http:// or https:// - "
                                   "for example http://192.168.1.21:8000/api/ingest/",
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

  // The code is stored rather than used. The board is serving its own hotspot
  // at this moment and has no route to the server; it claims after the reboot,
  // once it is on the network that was just entered.
  bool claimChanged = false;
  if (claim.length() > 0 && cfgDeviceId.length() == 0)
  {
    pendingClaimCode = claim;
    wifiPrefs.begin(WIFI_PREFS_NAMESPACE, false);
    wifiPrefs.putString("claim", claim);
    wifiPrefs.end();
    claimChanged = true;
    Serial.printf("[CLAIM] Code %s saved. Will be used once WiFi is up.\n", claim.c_str());
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
    if (!serverChanged && !claimChanged)
    {
      webServer.send(400, "text/html",
                     buildWifiPage("Nothing to save. Pick a network, enter a claim code, "
                                   "or change the server address.",
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
  json += "\"server_discovery\":\"" + htmlEscape(lastDiscoveryResult) + "\",";
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
    json += "\"severity\":\"" + String(severityToString(m.severity)) + "\",";
    json += "\"trigger\":\"" + String(triggerToString(m.trigger)) + "\",";
    json += "\"low_pressure_bar\":" + String(m.lowPressureBar, 3) + ",";
    // Raw ADC on the status page too, so the sensors can be checked from a
    // phone on site without opening the Serial Monitor.
    json += "\"adc_raw\":" + String(m.lastAdcRaw) + ",";
    json += "\"v_sensor\":" + String(m.lastVSensor, 3) + ",";
    json += "\"status\":\"" + String(statusToString(m.currentStatus)) + "\",";
    json += "\"baseline_bar\":" + String(m.baselinePressure, 3) + ",";
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
  // The connection panel: labels down the left, values on the right.
  html += "table.info{margin-bottom:18px;}";
  html += "table.info th{width:130px;background:#eef3f8;color:#1a3c5e;}";
  html += ".muted{color:#666;font-size:13px;}";
  html += ".mono{font-family:Consolas,monospace;font-size:13px;word-break:break-all;}";
  html += ".good{color:#1a7a3c;} .bad{color:#c0392b;}";
  html += "a{color:#1a3c5e;}";
  html += "</style></head><body>";
  html += "<h1>Water Leak Detection - Live Status</h1>";

  // Which network the board is on, and where it is sending readings. Both
  // change without anyone touching the board - the network when it is moved,
  // the server address when the laptop's own address moves - so this is the
  // first thing to check when the dashboard stops updating.
  bool online = (WiFi.status() == WL_CONNECTED);
  html += "<table class='info'>";

  html += "<tr><th>Network</th><td>";
  if (online)
  {
    html += "<b>" + htmlEscape(WiFi.SSID()) + "</b>";
    long rssi = WiFi.RSSI();
    html += " &nbsp;<span class='muted'>" + String(signalWord(rssi));
    html += " signal, " + String(rssi) + " dBm</span>";
  }
  else
  {
    html += "<span class='bad'>Not connected</span>";
    if (cfgSSID.length())
      html += " <span class='muted'>(trying " + htmlEscape(cfgSSID) + ")</span>";
  }
  html += "</td></tr>";

  html += "<tr><th>This device</th><td>";
  html += online ? WiFi.localIP().toString() : String("-");
  html += " &nbsp;<span class='muted'>signal strength and address come from the router</span>";
  html += "</td></tr>";

  // Identity before anything else. On a board that has just been set up this
  // is the one thing somebody standing in a yard needs to confirm, and it
  // saves them opening the settings page to find out.
  html += "<tr><th>This board</th><td>";
  if (cfgDeviceId.length())
  {
    html += "<b class='good'>" + htmlEscape(cfgDeviceId) + "</b>";
    html += " &nbsp;<span class='muted'>claimed</span>";
  }
  else
  {
    html += "<b class='bad'>Not claimed</b>";
    html += " &nbsp;<span class='muted'>" + htmlEscape(lastClaimResult) + "</span>";
    html += " &nbsp;<a href='/wifi'>enter a claim code</a>";
  }
  html += "</td></tr>";

  html += "<tr><th>Valve</th><td>";
  html += "<b>" + String(valveToString()) + "</b>";
  html += " &nbsp;<span class='muted'>" + htmlEscape(lastValveResult) + "</span>";
  html += "</td></tr>";

  html += "<tr><th>Sending to</th><td>";
  html += "<span class='mono'>" + htmlEscape(cfgServerUrl) + "</span>";
  html += "</td></tr>";

  html += "<tr><th>Last upload</th><td><b class='";
  html += (lastUploadOk ? "good" : "bad");
  html += "'>" + htmlEscape(lastUploadResult) + "</b>";
  if (!lastUploadOk)
    html += " &nbsp;<span class='muted'>last search: " + htmlEscape(lastDiscoveryResult) + "</span>";
  html += "</td></tr>";

  html += "</table>";
  html += "<p class='muted'>Auto-refreshes every 3 seconds. "
          "<a href='/wifi'>Change network or server address</a></p>";
  // "Low below" is shown beside the live pressure on purpose: a LOW SUPPLY
  // verdict is only meaningful next to the figure it was judged against, and
  // seeing both is how anyone on site can tell a genuinely weak line from a
  // threshold set too high for it.
  html += "<table><tr><th>Line</th><th>Pressure (bar)</th><th>Low below</th>";
  html += "<th>Flow (L/min)</th><th>What fired</th><th>Status</th></tr>";

  for (uint8_t i = 0; i < NUM_MODULES; i++)
  {
    ModuleState &m = modules[i];
    String cssClass = "NORMAL";
    if (m.currentStatus == STATUS_LOW_SUPPLY)
      cssClass = "LOW";
    if (m.currentStatus == STATUS_POSSIBLE_LEAK)
      cssClass = "LEAK";

    html += "<tr><td>This household</td>";
    html += "<td>" + String(m.pressureEMA, 2) + "</td>";
    html += "<td class='muted'>" + String(m.lowPressureBar, 2) + "</td>";
    html += "<td>" + String(m.flowEMA, 2) + "</td>";
    html += "<td class='muted'>" + String(triggerToString(m.trigger));
    if (m.severity != SEVERITY_NONE)
      html += " / " + String(severityToString(m.severity));
    html += "</td>";
    html += "<td class='" + cssClass + "'>" + String(statusToString(m.currentStatus));
    if (m.simulationMode)
      html += " (simulated)";
    html += "</td></tr>";
  }

  html += "</table>";
  // The two manual controls the design calls for. Behind the config password,
  // because closing somebody's water from a phone on the same WiFi should not
  // be something any passer-by can do.
  html += "<form method='POST' action='/valve/test' style='display:inline'>";
  html += "<button type='submit' style='padding:8px 14px;margin-top:14px;margin-right:8px'>";
  html += "Run valve test now</button></form>";
  html += "<form method='POST' action='/valve/open' style='display:inline'>";
  html += "<button type='submit' style='padding:8px 14px;margin-top:14px'>Open valve</button></form>";
  html += "<p class='muted'>A test waits until the line has been idle for ";
  html += String(VALVE_TEST_IDLE_MINUTES);
  html += " minutes, so it never cuts water off while somebody is using it.</p>";

  html += "<p style='margin-top:16px;'><a href='/status'>Raw JSON</a>";
  html += " &nbsp;|&nbsp; <a href='/wifi'>WiFi settings</a></p>";
  html += "</body></html>";

  webServer.send(200, "text/html", html);
}

void handleValveTest()
{
  if (!requireConfigAuth())
    return;
  valveTestRequested = true;
  webServer.sendHeader("Location", "/", true);
  webServer.send(302, "text/plain", "");
}

void handleValveOpen()
{
  if (!requireConfigAuth())
    return;
  valveStage = VALVE_OPENING;
  valveStageMillis = millis();
  valveDrive(true);
  lastValveResult = "opened by hand";
  webServer.sendHeader("Location", "/", true);
  webServer.send(302, "text/plain", "");
}

void setupWebServer()
{
  webServer.on("/", handleWebRoot);
  webServer.on("/status", handleWebStatus);
  webServer.on("/wifi", HTTP_GET, handleWifiPage);
  webServer.on("/wifi/save", HTTP_POST, handleWifiSave);
  webServer.on("/wifi/forget", HTTP_POST, handleWifiForget);
  webServer.on("/valve/test", HTTP_POST, handleValveTest);
  webServer.on("/valve/open", HTTP_POST, handleValveOpen);
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
  m.simulationMode = false;
  m.simPressure = 0;
  m.simFlow = 0;
  m.baselinePressure = 0;
  m.baselineReady = false;
  m.flowStartedMillis = 0;
  m.lastZeroFlowMillis = millis();
  m.flowing = false;
  m.burstSinceMillis = 0;
  m.decaySeverity = SEVERITY_NONE;
  m.lastDecayBar = 0;
  m.severity = SEVERITY_NONE;
  m.trigger = TRIGGER_NONE;
  m.currentStatus = STATUS_NORMAL;
  m.candidateStatus = STATUS_NORMAL;
  m.consecutiveCount = 0;
  m.leakNotified = false;
  m.lastAdcRaw = 0;
  m.lastVAdc = 0;
  m.lastVSensor = 0;
  m.adcWarnState = 0;
  m.primed = false;
  m.zeroVSensor = SENSOR_VOUT_MIN;                     // replaced by loadZeroOffsets() if one was saved
  m.lowPressureBar = P_LOW_THRESHOLD_BAR_DEFAULT;      // replaced by loadThresholds(), then by the server

  pinMode(flowPin, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(flowPin), isr, FALLING);

  // Set per pin as well as globally, matching sensor_test.ino exactly, so the
  // attenuation cannot differ between the two sketches.
  analogSetPinAttenuation(pressurePin, ADC_11db);
}

// ============================================================================
// SETUP
// ============================================================================
void setup()
{
  Serial.begin(115200);
  delay(500);

  // The valve, before anything else. A pin floats for a moment between reset
  // and this line, and on an active-low relay board a floating pin can pull
  // the relay in - so both are driven to the de-energised level first, then
  // the valve is deliberately opened. A power cut must never be able to leave
  // a household with the water shut off.
  pinMode(VALVE_PIN_OPEN, OUTPUT);
  pinMode(VALVE_PIN_CLOSE, OUTPUT);
  valveRelaysOff();
  valveDrive(true);
  delay(VALVE_TRAVEL_MS);
  valveStopAndRecord(true);

  analogReadResolution(12);
  analogSetAttenuation(ADC_11db);

  initModule(0, FLOW_PINS[0], PRESSURE_PINS[0], flowPulseISR0);

  loadZeroOffsets();
  loadThresholds();

  GSM_SERIAL.begin(GSM_BAUD, SERIAL_8N1, GSM_RX_PIN, GSM_TX_PIN);
  delay(3000); // SIM800L needs a few seconds to boot/register on the network

  lastSampleMillis = millis();

  Serial.println("============================================");
  Serial.println("  Water Leak Detection - per-household device");
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
  // Nothing saved yet: ask the network rather than sitting on the
  // compiled-in default, which is only right by luck. Pointless on a hosted
  // deployment - a broadcast does not cross the internet - so it is skipped
  // when the address is not a plain http:// one on the local network.
  if (cfgServerUrl == DEFAULT_SERVER_URL && cfgServerUrl.startsWith("http://"))
    discoverServer();

  // Real time, for the night-flow test. Non-blocking; the test stays silent
  // until an answer actually arrives.
  startClock();

  // If a claim code was entered on the setup page before the last reboot,
  // this is the first moment the board can actually use it.
  tryClaim();

  setupWebServer(); // dashboard on a normal network, setup page on the AP

  Serial.println("Send 'C1'/'C2'/'C3' to calibrate that module.");
  Serial.println("  While calibrating: 'A' = capture point, 'F' = finish & compute K");
  Serial.println("Send 'Z' with NO WATER in the line to set the zero point for all");
  Serial.println("  modules, or 'Z2' for one. Do this once per sensor; it is saved.");
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

  // Real time arrives some seconds after WiFi does; the night-flow test stays
  // silent until it has.
  checkClock();

  // A claim code entered on the setup page is used here, once the board is on
  // a network. tryClaim() returns immediately when there is nothing to do.
  tryClaim();

  // The sealed-line test, advanced one stage per pass. It must run outside the
  // sampling timer: the valve is shut while it holds, and noticing that
  // somebody opened a tap cannot wait a second.
  runValveTest(modules[0]);

  // --- Handle Serial commands ---
  if (Serial.available())
  {
    char cmd = Serial.read();

    if (cmd == 'V')
    {
      // Ask for a sealed-line test now. It still waits for the line to be
      // idle: cutting water off while somebody is using it is not something
      // a demonstration gets to override.
      valveTestRequested = true;
      Serial.println("[VALVE] Test requested. It starts once the line has been idle.");
    }
    else if (cmd == 'O')
    {
      // Manual override - open the valve and abandon any test in progress.
      valveStage = VALVE_OPENING;
      valveStageMillis = millis();
      valveDrive(true);
      Serial.println("[VALVE] Opening (manual override).");
    }
    else if (cmd == 'N')
    {
      fakeNightMode = !fakeNightMode;
      Serial.printf("[TIME] Night mode forced %s (for testing the night-flow window).\n",
                    fakeNightMode ? "ON" : "OFF");
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
    else if (cmd == 'Z')
    {
      // 'Z' zeroes every module, 'Z2' just that one. Run with no water in
      // the line - whatever the sensors read now becomes their 0.00 bar.
      int8_t only = -1;
      if (isDigit(Serial.peek()))
      {
        char digit = Serial.read();
        only = digit - '1';
        if (only < 0 || only >= NUM_MODULES)
          only = -1;
      }
      zeroPressureSensors(only);
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

      // --- Read pressure and flow ---
      float rawPressure = m.simulationMode ? m.simPressure : readPressureBar(m);

      noInterrupts();
      uint32_t pulses = m.flowPulseCount;
      m.flowPulseCount = 0;
      interrupts();

      float rawFlow = m.simulationMode ? m.simFlow : computeFlowRateLPM(pulses, elapsed);

      // First real sample fills the whole history. Without this the median of
      // a part-empty buffer is 0 for the first few seconds, so the board
      // reports - and uploads - pressures well below what it is measuring.
      if (!m.primed)
      {
        for (uint8_t h = 0; h < FILTER_HISTORY_SIZE; h++)
        {
          m.pressureHistory[h] = rawPressure;
          m.flowHistory[h] = rawFlow;
        }
        m.primed = true;
      }

      // --- Filter ---
      m.pressureHistory[m.historyIndex] = rawPressure;
      float medianPressure = medianFilter(m.pressureHistory, FILTER_HISTORY_SIZE);
      m.pressureEMA = emaFilter(m.pressureEMA, medianPressure, EMA_ALPHA);

      m.flowHistory[m.historyIndex] = rawFlow;
      float medianFlow = medianFilter(m.flowHistory, FILTER_HISTORY_SIZE);
      m.flowEMA = emaFilter(m.flowEMA, medianFlow, EMA_ALPHA);

      m.historyIndex = (m.historyIndex + 1) % FILTER_HISTORY_SIZE;

      if (!m.simulationMode)
        checkPressureWiring(m);

      // --- Classification + hysteresis ---
      SystemStatus candidate = classify(m, m.pressureEMA, m.flowEMA);
      updateStatusWithHysteresis(m, candidate);

      // --- GSM notification on status change ---
      checkAndNotifyLeak(m);

      // --- Report ---
      // The raw ADC count and the sensor voltage are on the line because
      // 0.00 bar is what both a working sensor at rest and a disconnected one
      // report. raw tells them apart: ~414 means wired and idle, ~0 means no
      // signal at all.
      Serial.printf("[M%d] P=%.2f bar (%.1f psi) | Q=%.2f L/min | raw=%4d  Vadc=%.3f  Vsens=%.3f | "
                    "base=%.2f | valve=%s | %s (%s/%s)%s\n",
                    m.id, m.pressureEMA, m.pressureEMA * 14.5038, m.flowEMA,
                    m.lastAdcRaw, m.lastVAdc, m.lastVSensor,
                    m.baselinePressure, valveToString(),
                    statusToString(m.currentStatus),
                    triggerToString(m.trigger), severityToString(m.severity),
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

    // Uploads failing to reach anything usually means the dashboard's
    // machine changed address. Ask the network where it went, rather than
    // posting into the void until somebody notices.
    if (uploadFailStreak >= DISCOVERY_AFTER_FAILURES &&
        (now - lastDiscoveryMillis) >= DISCOVERY_MIN_GAP_MS)
    {
      discoverServer();
    }
  }
}
