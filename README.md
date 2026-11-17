# Smart Hose Leak Detection System

A web-based and mobile-integrated water supply monitoring system for the community
water system of **Brgy. Lower Dakong Patag, Sison, Surigao del Norte**.

Capstone project — College of Information Technology Education, Department of Computer
Studies, North Eastern Mindanao State University, Cantilan Campus.

- **Backend:** Django 5.2 (Python), SQLite by default, PostgreSQL-ready
- **Frontend:** plain HTML, CSS and JavaScript — no build step, no framework; installable
  as an app (PWA) on any phone
- **Languages:** English and Filipino, switchable from the top bar on every page
- **Device:** one ESP32 per household hose line, with a pressure sensor, a flow sensor and a
  motorised ball valve, on the household's own WiFi
- **Hosting:** a laptop on the local network for development and demos, PythonAnywhere for
  the public site

> **GUIDE.txt is the operator runbook** — setup, logins, every command, every known
> failure and its fix. This README is the overview; when the two disagree, trust the guide.

---

## Quick start

```bash
# 1. Activate the virtual environment (always - the system Python has a different Django)
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS / Linux

# 2. Apply the database schema
python manage.py migrate

# 3. Create the admin and facilitator logins, the barangay, and sample households
python manage.py setup_demo

# 4. Run the server so the boards and phones on the network can reach it
python manage.py runserver 0.0.0.0:8000
```

`runserver` prints two addresses. Open the **On the network** one. Plain `runserver`
without `0.0.0.0:8000` listens on this computer only, and every board stays Offline.

`setup_demo` prints the admin password once. **Do not run it casually**: it regenerates
the device key, so a board already flashed stops being accepted.

To show what a household owner sees, `python manage.py setup_demo_owner` builds one fully
furnished household (board, valve, a day of history, notices, a complaint). It refuses to
run with `DEBUG=False`.

### See it working without the hardware

With the server running, in a second terminal:

```bash
python tools/simulator.py --key <device api key>
python tools/simulator.py --key <key> --device-id esp32-demo --modules 1 --valve open
```

The simulator posts the same payload the firmware sends, so the dashboards, alerts, notices
and charts all behave as they would with a real board.

---

## Three roles

| Role | Lands on | What they do |
|---|---|---|
| **Admin** | `/console/` | Registers households and facilitators, manages every login, announcements, analytics, backups, the audit log, detection settings and maintenance mode |
| **Facilitator** | `/dashboard/` | Monitors the barangay: live pressure, the leak map, alerts, complaints, notices and SMS, visit routes; activates new households |
| **Household owner** | Owner portal | Their own line's pressure and valve, notices, complaints, and setting up their own board |

Admin and facilitator are separate roles; neither can open the other's pages. Owner
accounts are created when a facilitator activates a household; there is no public sign-up.

**Signing in takes two steps for every role:** password, then a 6-digit code by text or
email. In development (`DEBUG=True`) the code is also shown on the sign-in page, so a demo
never depends on SMS credit or a mailbox.

---

## The board

The sketch is in
[`firmware/water_leak_single_wifi/`](firmware/water_leak_single_wifi/). The same sketch
goes on every board; a short setup code is what ties a board to a household. Its header
comment has the wiring, the pin numbers and the valve details.

### Setting a board up

The **owner** does it, from their phone, without a laptop:

1. The admin registers the household, which registers a board with it.
2. The facilitator activates the household. The owner receives their login and the
   board's setup code by text and email.
3. The owner powers the board, joins its hotspot `LeakDetect-Setup`, and opens
   `http://192.168.4.1/`. The portal's **Set up my board** button builds a link that opens
   that page already filled in, so the owner only presses *Save & connect*.

The owner's Settings page turns **Connected** once the first reading arrives. Later WiFi
changes can be sent from the portal to a board that is online; the board tries the new
network and falls back to the old one if it fails.

**When the server is a laptop on the same WiFi**, leave the server box on the board empty.
The board finds the laptop by a UDP broadcast and finds it again whenever the laptop's
address changes. Type an address only for a server elsewhere, such as the PythonAnywhere
site.

### What the board decides

The board decides the line's status itself — `NORMAL`, `WATER_RUNNING` or
`POSSIBLE_LEAK` — so it keeps protecting the line with the WiFi down or the server off.
The server turns a reported status into notices, texts and emails, and keeps the history.

1. **Continuous flow.** Water running non-stop past the warning time is `WATER_RUNNING`
   and the owner is warned. Past the leak time it is `POSSIBLE_LEAK`, and if auto-close is
   switched on the valve closes.
2. **Nightly sealed-line test.** At the quiet hour the board closes the valve and watches
   the pressure. A sealed line holds it; a hole bleeds it away. A drop past the threshold is
   `POSSIBLE_LEAK` and the valve stays closed. No drop, and the valve reopens when the test
   ends.

A valve held shut for a leak stays shut, across a power cut too, until somebody presses
Open — from the owner portal, the dashboard, the admin console, the board's own page, or
`OPEN` on the serial monitor.

The thresholds live on the server (admin console → System) and ride back on every upload
reply, so they can be retuned without re-flashing. The board caches the last set it saw.

### How often it talks

A routine reading goes up every 15 seconds by default, set on the admin console. A change of
status or valve state goes up at once. While someone has a valve screen open, the board
checks for commands every two seconds, so a valve press reaches it within about three
seconds. This is what lets one hosted server carry many boards.

### The older 3-module board

[`firmware/water_leak_detection_3modules_gsm_wifi/`](firmware/water_leak_detection_3modules_gsm_wifi/)
is the earlier design: one ESP32 with three flow sensors, three pressure sensors and a
SIM800L GSM module watching three stretches of a shared line. It is kept for reference.
New installations use the single-WiFi board, one per household.

---

## API

| Endpoint | Caller | Purpose |
|---|---|---|
| `/api/claim/` | Board | Trades a setup code for the board's identity |
| `/api/ingest/` | Board | Posts readings and status; the reply carries settings and valve commands |
| `/api/commands/` | Board | Fast poll for a pending open or close while someone is at a valve screen |
| `/api/live/`, `/api/history/` | Dashboards | Live tiles and chart data |
| `/api/push/…` | Browsers | Web push subscribe, unsubscribe and test |

---

## Notifications

- **SMS** through iprogSMS. Without a token in `.env` messages are queued, not lost.
- **Email** from the project Gmail mailbox, using an app password. Without it, email is
  printed to the server terminal instead.
- **Web push** to the installed app, standard Web Push with no Firebase account.

The admin console's System page shows in one place whether each of these is set up.

---

## Filipino

Strings live in `locale/fil/LC_MESSAGES/`. After changing wording:

```bash
python manage.py makemessages -l fil --ignore=.venv --ignore=staticfiles   # only after adding new text
python manage.py compilemessages -l fil
```

Both need GNU gettext on PATH. Restart the server after compiling. Untranslated text shows in
English.

---

## Data retention

- **`LatestReading`** — the current value per line, overwritten each time.
- **`ReadingRollup`** — one averaged row per line per minute; the charts and reports read
  this. Trim with `python manage.py prune_rollups` (default: keep 90 days).
- **`LeakEvent`** — every confirmed status change, kept permanently.

Schedule `python manage.py mark_offline` every minute to log outages that happen while
nobody is watching.

---

## Deploying

Copy `.env.example` to `.env`. For a real deployment set at least:

```
DEBUG=False
SECRET_KEY=<a long random string>
ALLOWED_HOSTS=yourname.pythonanywhere.com
```

With `DEBUG=False` the server refuses to start until those are set. The SMS token, the
Gmail app password and the push keys also go in the hosted `.env`, or no notice ever leaves
the site.

**On PythonAnywhere, SQLite runs in DELETE journal mode, not WAL.** WAL corrupts the file on
a network filesystem once two processes write. This is detected automatically;
`SQLITE_JOURNAL_MODE` in `.env` overrides it. GUIDE.txt section 16 has the full
walkthrough, and section 12 the recovery steps if a database is already damaged
(`python manage.py repair_links`).

To use PostgreSQL instead, set `DB_ENGINE=postgres` and the `DB_*` values in `.env`,
`pip install psycopg[binary]`, and migrate. No model changes are needed.

---

## Project layout

```
config/              Django settings and root URLs
core/
  models.py          Devices, hose lines, readings, households, events, notices, complaints,
                     work orders, announcements, audit log, system settings
  api.py             Board and dashboard JSON endpoints
  views.py           Admin console, facilitator dashboard, owner portal
  forms.py           All forms
  otp.py             Sign-in codes
  sms.py, mail.py, push.py    The three notification channels
  discovery.py       UDP responder the boards use to find a laptop server
  management/commands/        setup_demo, setup_demo_owner, mark_offline, prune_rollups,
                              repair_links, send_sms, send_email, make_push_keys, ...
  tests*.py          The test suite: python manage.py test core
templates/           All HTML, plus the PWA manifest and service worker
static/              app.css, app.js, map.js, chart.js, fonts, icons
locale/fil/          Filipino translations
firmware/            The ESP32 sketches (single-WiFi board, and the older 3-module board)
tools/simulator.py   Stand-in for a board
GUIDE.txt            The operator runbook
```

## Troubleshooting

| Symptom | Cause |
|---|---|
| Everything says *Offline* | Server started without `0.0.0.0:8000`, the board is on a different WiFi, or the firewall is blocking port 8000. GUIDE.txt section 7. |
| Board was fine, then rejected | `setup_demo` was run and regenerated the device key. |
| Valve buttons missing for an online household | The board's firmware reports no valve. Re-flash `water_leak_single_wifi`. |
| No texts or emails from the hosted site | The SMS token or Gmail app password is missing from the hosted `.env`. |
| Sign-in code never arrives | The account has no number or email, or the channel is not set up. Check admin console → System. |

GUIDE.txt section 12 has the rest.
