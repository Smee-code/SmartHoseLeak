# Smart Hose Leak Detection System

A web-based and mobile-integrated automated water supply monitoring system for the
community water system of **Brgy. Lower Dakong Patag, Sison, Surigao del Norte**.

Capstone project — College of Information Technology Education, Department of Computer
Studies, North Eastern Mindanao State University, Cantilan Campus.

- **Backend:** Django 5.2 (Python), SQLite by default, PostgreSQL-ready
- **Frontend:** plain HTML, CSS and JavaScript — no build step, no framework
- **Device:** ESP32 with 3 flow sensors, 3 pressure sensors and a SIM800L GSM module

---

## Quick start

```bash
# 1. Activate the virtual environment
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS / Linux

# 2. Apply the database schema
python manage.py migrate

# 3. Create the device, the three supply lines, the facilitator and sample households
python manage.py setup_demo

# 4. Run the server
python manage.py runserver
```

Open <http://127.0.0.1:8000/>.

`setup_demo` prints the facilitator login, every owner login, and the **device API key**.
Copy that key down — the ESP32 needs it, and it is also visible any time in the Django
admin under **Devices**.

### See it working without the hardware

In a second terminal, with the server running:

```bash
python tools/simulator.py --key <device api key>            # all lines normal
python tools/simulator.py --key <device api key> --leak 2   # force a leak on Line 2
python tools/simulator.py --key <device api key> --random-events   # hands-off demo
```

The simulator reproduces the firmware's own detection maths — the same
pressure–flow residual test and the same 15-sample hysteresis — so what appears on
the dashboard is what the real rig would produce.

---

## Connecting the real ESP32

The sketch is at [`firmware/smart_hose_leak/smart_hose_leak.ino`](firmware/smart_hose_leak/smart_hose_leak.ino).
It is your original 3-module + GSM sketch with a WiFi uplink added; the detection,
filtering, calibration, hysteresis and SMS logic are unchanged.

1. **Edit the four config values near the top of the sketch:**

   | Setting | Value |
   |---|---|
   | `WIFI_SSID` / `WIFI_PASSWORD` | Your WiFi network |
   | `SERVER_URL` | `http://<your PC LAN IP>:8000/api/ingest/` — **not** `127.0.0.1` |
   | `DEVICE_API_KEY` | The key printed by `setup_demo` |
   | `OWNER_PHONE_NUMBER` | The facilitator's number, e.g. `+639171234567` |

   Find your LAN IP with `ipconfig` (look for *IPv4 Address*, usually `192.168.x.x`).

2. **Run the server so the network can reach it**, not just your own machine:

   ```bash
   python manage.py runserver 0.0.0.0:8000
   ```

   Windows Firewall will usually prompt the first time — allow it on private networks.
   The ESP32 and the PC must be on the **same WiFi network**.

3. **Upload the sketch** and open the Arduino Serial Monitor at 115200 baud. You should
   see `[WIFI] Connected` followed by `[UPLOAD] OK` every couple of seconds. Readings
   appear on the dashboard immediately.

**If you see `[UPLOAD] REJECTED 401`** the `DEVICE_API_KEY` does not match. Copy it again
from the Django admin. **If uploads time out**, the ESP32 cannot reach the PC — check that
both are on the same network and that the firewall is allowing port 8000.

### Calibrate before trusting the alerts

The leak test compares measured flow against `Q = K · √P`, and `K` starts at zero — until
each module is calibrated the system falls back to a plain low-pressure check only. Over the
Serial Monitor, per module:

```
C1        start calibrating module 1
A         capture a stable (pressure, flow) point — repeat at several flow rates
F         finish and compute K for that module
```

Repeat with `C2` and `C3`. Each hose run has its own diameter, length and fittings, so each
needs its own `K`.

---

## The two portals

### Facilitator Dashboard (web)

| Page | What it does |
|---|---|
| **Dashboard** | Live tiles per supply line, leak map, open alerts, history chart |
| **Pressure** | Every registered household's pressure in one sortable table |
| **Households** | The household database — add, edit, search, export, issue logins |
| **Alerts** | Full event log, acknowledge alerts, CSV export |
| **Complaints** | Owner complaints inbox; replying also notifies the owner |
| **Notices** | Send a message to one household, one supply line, or everyone |
| **Lines** | Distance markers, map coordinates, maintenance mode, device status |
| **Reports** | 30-day summary per line and CSV exports for LGU records |

### Owner Portal (mobile, installable)

Registered household owners get only: their own line's pressure and history, notices from
the facilitator, and complaint submission with status tracking. It is a **PWA** — on a phone,
open the site in Chrome or Safari and choose *Add to Home Screen* to install it as an app.

Owner accounts are created by the facilitator when adding a household; there is no public
sign-up. A temporary password is shown **once** on screen — write it down. If it is lost, use
*Issue a new temporary password* on the household record.

---

## How a leak becomes an alert

1. The ESP32 samples all three modules once a second and filters each reading
   (median then EMA).
2. It flags a module when flow runs more than 18% above the calibrated pressure–flow
   curve, or when pressure decays while no tap is open.
3. Hysteresis requires **15 consecutive** samples before the status actually changes,
   which is what keeps noise out of the alert log.
4. On a confirmed change the SIM800L sends the facilitator an SMS **directly from the
   device**, and the board POSTs the new status to the server.
5. The server records a `LeakEvent`, sets the segment status, and raises an in-app
   notification for every active household assigned to that supply line.

The device decides; the server displays and records. **If the WiFi or the server is down,
the SMS alert still goes out** — that path does not depend on the network.

### Leak location

Each of the three modules covers one stretch of hose, configured with a start and end
distance marker measured from the main valve. Defaults are **0–15 m**, **15–30 m** and
**30–50 m**; edit them under *Lines* or in the Django admin to match the real installation.

A leak on module 2 is therefore reported as *"between 15 m and 30 m from the main valve"*.
This is the honest resolution three sensor pairs can deliver — a claim of ±1 m would need a
sensor pair per metre, or acoustic/correlation equipment. Narrow the markers and the reported
span narrows with them.

### The leak map

The dashboard picks its map renderer automatically:

- **Strong connection** and coordinates set on at least one line → an interactive
  **Leaflet** map with OpenStreetMap tiles.
- **Weak or no connection**, or no coordinates set → an **inline SVG schematic** of the
  hose run, drawn from the distance markers, needing no network at all.

The pill in the top bar shows which was detected, and the *Switch view* button overrides
the choice. The schematic is the safe default for a field demo on a weak signal.

---

## Data retention

The device sends 3 readings a second. Storing every raw sample would be roughly 260,000 rows
a day, so:

- **`LatestReading`** — the current value per line, overwritten each time.
- **`ReadingRollup`** — one averaged row per line per minute, which the charts and reports
  read. Samples are folded in as they arrive.
- **`LeakEvent`** — every confirmed status change, kept permanently.

Trim old rollups with `python manage.py prune_rollups` (default: keep 90 days).

To record outages that happen while nobody is watching, schedule
`python manage.py mark_offline` every minute via Windows Task Scheduler or cron. The
dashboard already shows a stale feed as offline on its own; this only adds the audit trail.

---

## Switching to PostgreSQL

Create a `.env` file next to `manage.py` (see `.env.example`):

```
DB_ENGINE=postgres
DB_NAME=smarthoseleak
DB_USER=postgres
DB_PASSWORD=yourpassword
DB_HOST=127.0.0.1
DB_PORT=5432
```

Then `pip install psycopg[binary]` and `python manage.py migrate`. No model changes needed.

---

## Before deploying for real

`DEBUG=True` and the development server are for building and demonstrating only. For an
actual deployment set in `.env`:

```
DEBUG=False
SECRET_KEY=<a long random string>
ALLOWED_HOSTS=your-domain-or-ip
CSRF_TRUSTED_ORIGINS=https://your-domain
```

and serve through a real web server. Note that the ingest endpoint is plain HTTP inside the
LAN: the device key stops casual spoofing, but anyone sniffing that network can read it. For
anything beyond the campus deployment, put the server behind HTTPS.

---

## Project layout

```
config/          Django settings and root URLs
core/
  models.py      Device, Segment, LatestReading, ReadingRollup, Household,
                 LeakEvent, Notification, Complaint, User
  api.py         /api/ingest/ (device), /api/live/ and /api/history/ (dashboards)
  views.py       Both portals, with role enforcement
  forms.py       Household, complaint, notice and supply line forms
  admin.py       Django admin, including the distance marker editor
  management/commands/
    setup_demo.py    bootstrap device, lines, facilitator, sample households
    prune_rollups.py trim old history
    mark_offline.py  log device outages
templates/       All HTML, plus the PWA manifest and service worker
static/          app.css, app.js, map.js, chart.js, icons
firmware/        The ESP32 sketch
tools/simulator.py   Stand-in for the device
```

## Troubleshooting

| Symptom | Cause |
|---|---|
| Tiles show `--` and *Offline* | Nothing is posting. Start the simulator or the device. |
| `[UPLOAD] REJECTED 401` | `DEVICE_API_KEY` does not match the Devices row. |
| Upload times out | ESP32 and PC on different networks, or firewall blocking port 8000. |
| Everything reads *Low Water Supply* | Modules not calibrated yet — `K` is still 0. Run `C1`/`C2`/`C3`. |
| Map never appears | No coordinates set on any line, or a weak connection was detected. |
| Alerts during a repair | Mark the line *under maintenance* first; that suppresses its alerts. |
