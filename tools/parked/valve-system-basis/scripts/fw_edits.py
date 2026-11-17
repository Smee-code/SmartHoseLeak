import io, sys, os

os.chdir(sys.argv[1])
p = 'firmware/water_leak_single_wifi/water_leak_single_wifi.ino'
t = io.open(p, encoding='utf-8', newline='').read()
nl = '\r\n' if '\r\n' in t else '\n'


def rep(old, new, count=1):
    global t
    old = old.replace('\n', nl)
    new = new.replace('\n', nl)
    assert t.count(old) == count, (old[:70], t.count(old))
    t = t.replace(old, new)


# ---- header: how the valve is decided now
rep('''    Wiring: supply + -> relay COM (both channels); relay 1 NO -> CLOSE wire;
    relay 2 NO -> OPEN wire; supply - -> valve N (common).
*/
''', '''    Wiring: supply + -> relay COM (both channels); relay 1 NO -> CLOSE wire;
    relay 2 NO -> OPEN wire; supply - -> valve N (common).

  WHO DECIDES THE VALVE
    The SYSTEM does. Every reply from the server carries valve_target
    ("open" or "closed"), the status the portal, the dashboard and the
    console show; the board drives the valve to it whenever its own
    position differs, so a reboot, a reflash or a dropped reply cannot
    leave the valve somewhere the system does not know. Two decisions stay
    the board's and the system adopts them: the nightly test and the flow
    hold (temporary, the system's "open" does not undo them) and a close or
    open made at the board itself (its page, serial CLOSE/OPEN), which the
    board numbers (valve_local_seq) so the system adopts each one once.
    An administrator's close stands even against the board's own page.
    On boot the valve is left where it is until the system has spoken, or
    BOOT_TARGET_WAIT_MS has passed; meanwhile it reports "unknown".
*/
''')

# ---- constants
rep('''#define WIFI_RESTART_AFTER_MS   (15UL * 60UL * 1000UL)
''', '''#define WIFI_RESTART_AFTER_MS   (15UL * 60UL * 1000UL)

// On boot the valve is left where it is until the system has said where it
// should be (valve_target on the first reply). If no word comes in this time
// - no WiFi, no server - the board falls back to its own memory.
#define BOOT_TARGET_WAIT_MS     60000UL
''')

# ---- state
rep('''String valveState  = "idle";
''', '''String valveState  = "idle";

// The SYSTEM's status for the valve ("open"/"closed"), as last heard on a
// reply. The system is the basis: the board drives the valve to it whenever
// its own position differs and nothing temporary (test, flow) holds it.
String        serverTarget     = "";
bool          heardTarget      = false;
bool          bootValveSettled = false;   // the boot wait has ended, one way or the other
unsigned long bootMs           = 0;
// Closes/opens made at the board itself (its page, serial CLOSE/OPEN),
// numbered so the system adopts each one once however many uploads repeat it.
uint32_t      localValveSeq    = 0;
''')

# ---- persistence
rep('''  valveReason    = prefs.getString("valvern", "");
''', '''  valveReason    = prefs.getString("valvern", "");
  localValveSeq  = prefs.getUInt("vseq", 0);
''')
rep('''// Remember who owns the valve, so a manual close survives a reboot.
void saveValveReason() {
  prefs.begin("shl", false);
  prefs.putString("valvern", valveReason);
  prefs.end();
}
''', '''// Remember who owns the valve, so a manual close survives a reboot.
void saveValveReason() {
  prefs.begin("shl", false);
  prefs.putString("valvern", valveReason);
  prefs.end();
}

// A close or open made at the board itself: number it so the system adopts
// it once, and report at once. A person at the board also ends the boot
// wait - they have decided.
void saveLocalValveSeq() {
  prefs.begin("shl", false);
  prefs.putUInt("vseq", localValveSeq);
  prefs.end();
}
void noteLocalValve() {
  localValveSeq++;
  saveLocalValveSeq();
  bootValveSettled = true;
  lastUploadMs = 0;
}
''')

# ---- following the system's status
rep('''void valveCloseNow() {
  valveDrive(RELAY_CLOSE, "closing");
  valveState = "closed";
}
''', '''void valveCloseNow() {
  valveDrive(RELAY_CLOSE, "closing");
  valveState = "closed";
}

// What the upload says about the valve. "unknown" until the boot wait has
// ended, so the screens never show a position the board has not taken.
String reportedValveState() {
  if (!bootValveSettled) return "unknown";
  return valveState == "closed" ? "closed" : "open";
}

// Drive the valve to the system's status, if it is not already there and
// nothing temporary of the board's own (the nightly test, a flow hold) is
// holding it. Called on every reply that carries valve_target.
void followServerTarget() {
  if (!heardTarget) return;
  if (valveReason == "test" || valveReason == "flow") return;
  if (serverTarget == "closed" && valveState != "closed") {
    valveCloseNow();
    if (valveReason == "decay") pressureTestLeak = true;      // still the leak the test found
    else { valveReason = "manual"; saveValveReason(); }
    Serial.println("[VALVE] the system says closed - closed");
  } else if (serverTarget == "open" && valveState != "open") {
    valveOpenNow();
    valveReason = ""; flowStartMs = 0; pressureTestLeak = false; saveValveReason();
    Serial.println("[VALVE] the system says open - opened");
  }
}

// End of the boot wait: the system has spoken, or the time is up.
void settleBootValve() {
  if (bootValveSettled) return;
  bootValveSettled = true;
  if (heardTarget) {
    Serial.println("[VALVE] boot: the system says " + serverTarget);
    followServerTarget();
    return;
  }
  // No word from the system: the board's own memory. A manual or leak-found
  // close stays shut; anything else is driven open.
  if (valveReason == "manual" || valveReason == "decay") {
    valveCloseNow();
    if (valveReason == "decay") pressureTestLeak = true;
    Serial.println("[VALVE] boot: no word from the system - restored " + valveReason + " close");
  } else {
    valveOpenNow();
    Serial.println("[VALVE] boot: no word from the system - opened");
  }
}
''')

# ---- the reply: valve_target before the legacy one-shot command
rep('''  String vcmd;
  if (extractJsonString(resp, "valve_command", vcmd)) {
''', '''  // The system's status for the valve, restated on every reply. This is the
  // basis: drive the valve to it whenever the board's own position differs.
  // An older server sends none; the one-shot valve_command below still works.
  String vt;
  if (extractJsonString(resp, "valve_target", vt) && (vt == "open" || vt == "closed")) {
    serverTarget = vt; heardTarget = true;
    if (!bootValveSettled) settleBootValve();
    else followServerTarget();
  }

  String vcmd;
  if (extractJsonString(resp, "valve_command", vcmd)) {
''')

# ---- the upload: reported state and the local sequence number
rep('''    "\\"valve_state\\":\\"" + String(valveState == "closed" ? "closed" : "open") + "\\","
''', '''    "\\"valve_state\\":\\"" + reportedValveState() + "\\","
''')
rep('''    "\\"valve_reason\\":\\"" + valveReason + "\\""
    "}],"
''', '''    "\\"valve_reason\\":\\"" + valveReason + "\\","
    // Closes/opens made at the board itself, numbered (see noteLocalValve).
    "\\"valve_local_seq\\":" + String(localValveSeq) +
    "}],"
''')

# ---- the board's own temporary closes respect the system's status when they end
rep('''  if (valveReason == "flow" &&
      now - flowClosedAt >= (unsigned long)FLOW_LEAK_REOPEN_MINUTES * 60000UL) {
    valveOpenNow();
    valveReason = "";
    flowStartMs = 0;               // give the line a fresh start
  }
''', '''  if (valveReason == "flow" &&
      now - flowClosedAt >= (unsigned long)FLOW_LEAK_REOPEN_MINUTES * 60000UL) {
    if (serverTarget == "closed") {   // the system wants it shut: hand it over, leave it
      valveReason = "manual"; saveValveReason();
    } else {
      valveOpenNow();
      valveReason = "";
    }
    flowStartMs = 0;               // give the line a fresh start
  }
''')
rep('''        valveOpenNow();                          // no leak: give the water back
        valveReason = "";
''', '''        if (serverTarget == "closed") {          // the system wants it shut: leave it
          valveReason = "manual"; saveValveReason();
        } else {
          valveOpenNow();                        // no leak: give the water back
          valveReason = "";
        }
''')

# ---- boot: wait for the system
rep('''  // A line the admin sealed stays sealed through a power cut: re-close it
  // before anything else can decide the valve should be open.
  if (valveReason == "manual" || valveReason == "decay") {
    valveCloseNow();
    if (valveReason == "decay") pressureTestLeak = true;   // keep reporting it
    Serial.println("[VALVE] restored " + valveReason + " close after reboot");
  } else {
    // Any other close (the nightly test, a flow-leak auto-close) does not
    // survive a reboot, so the valve is driven open now rather than left
    // wherever the power cut found it while the board reports "open".
    valveOpenNow();
    Serial.println("[VALVE] opened on boot");
  }
''', '''  // The valve is left where it is until the system says where it should be:
  // settleBootValve() on the first reply, or when BOOT_TARGET_WAIT_MS is up
  // with no word (then the board's own memory decides). Until then the
  // uploads say "unknown".
  bootMs = millis();
  if (valveReason == "decay") pressureTestLeak = true;       // keep reporting it meanwhile
  Serial.println("[VALVE] boot: waiting for the system to say open or closed");
''')

# ---- loop: the boot wait's timer
rep('''    lastSampleMs = millis();
    maintainWiFi();
''', '''    lastSampleMs = millis();
    maintainWiFi();
    if (!bootValveSettled && millis() - bootMs >= BOOT_TARGET_WAIT_MS) settleBootValve();
''')

# ---- local commands are numbered
rep('''    if (c == "OPEN")  { valveOpenNow();  valveReason = ""; flowStartMs = 0; pressureTestLeak = false; saveValveReason(); }
    else if (c == "CLOSE") { valveCloseNow(); valveReason = "manual"; saveValveReason(); }
''', '''    if (c == "OPEN")  { valveOpenNow();  valveReason = ""; flowStartMs = 0; pressureTestLeak = false; saveValveReason(); noteLocalValve(); }
    else if (c == "CLOSE") { valveCloseNow(); valveReason = "manual"; saveValveReason(); noteLocalValve(); }
''')
rep('''  if (cmd == "open")  { valveOpenNow();  valveReason = ""; flowStartMs = 0; pressureTestLeak = false; saveValveReason(); }
  else if (cmd == "close") { valveCloseNow(); valveReason = "manual"; saveValveReason(); }
''', '''  if (cmd == "open")  { valveOpenNow();  valveReason = ""; flowStartMs = 0; pressureTestLeak = false; saveValveReason(); noteLocalValve(); }
  else if (cmd == "close") { valveCloseNow(); valveReason = "manual"; saveValveReason(); noteLocalValve(); }
''')

# ---- the board's page says what the system says
rep('''       " (" + (valveReason == "" ? "auto" : valveReason) + ")</p>";
''', '''       " (" + (valveReason == "" ? "auto" : valveReason) + ")" +
       (heardTarget ? " &middot; System says: " + serverTarget
                    : String(" &middot; System: no word yet")) + "</p>";
''')

io.open(p, 'w', encoding='utf-8', newline='').write(t)
print("firmware edits ok")
