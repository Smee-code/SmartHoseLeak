"""
Django settings for the Smart Hose Leak Detection System.

SQLite is used by default so the project runs on any machine with no setup.
Set DB_ENGINE=postgres plus the DB_* variables in .env to move to PostgreSQL
without touching any model code.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env(key, default=None):
    return os.environ.get(key, default)


def env_bool(key, default=False):
    return str(env(key, str(default))).strip().lower() in {"1", "true", "yes", "on"}


def env_list(key, default=""):
    return [item.strip() for item in env(key, default).split(",") if item.strip()]


# Load a .env file if present, without adding a dependency.
_env_file = BASE_DIR / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

SECRET_KEY = env("SECRET_KEY", "dev-only-key-change-before-deployment")
DEBUG = env_bool("DEBUG", True)

# The ESP32 posts to this server by IP on the local network, so the LAN
# address must be allowed. In DEBUG we accept any host to keep field testing
# painless; production deployments must set ALLOWED_HOSTS explicitly.
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "*") if not DEBUG else ["*"]
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")

# Showing the system on a phone without deploying it.
#
# A tunnel (VS Code "Forward a Port", ngrok, Cloudflare) publishes this
# development server over HTTPS on a hostname that changes every session.
# ALLOWED_HOSTS above already accepts it, but CSRF does not: for an HTTPS
# request Django compares the Origin header against this list, and an
# unlisted tunnel host makes every login and every form fail - with a CSRF
# error that gives no hint that the tunnel is the cause.
#
# Trusting the tunnel providers' wildcards in DEBUG only keeps a demo
# working through a URL nobody can predict in advance. Outside DEBUG this
# stays exactly as configured, so a real deployment trusts nothing extra.
if DEBUG:
    CSRF_TRUSTED_ORIGINS += [
        "https://*.devtunnels.ms",       # VS Code Forward a Port
        "https://*.ngrok-free.dev",      # what ngrok hands out now
        "https://*.ngrok-free.app",      # and what it used to
        "https://*.ngrok.io",            # and before that
        "https://*.trycloudflare.com",
    ]

INSTALLED_APPS = [
    # core is listed first so its management commands win over the built-in
    # ones of the same name. It overrides runserver purely to print the LAN
    # address the ESP32 needs; the command still behaves exactly as Django's,
    # static file serving included (see core/management/commands/runserver.py).
    "core",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.site_context",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

if env("DB_ENGINE", "sqlite") == "postgres":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env("DB_NAME", "smarthoseleak"),
            "USER": env("DB_USER", "postgres"),
            "PASSWORD": env("DB_PASSWORD", ""),
            "HOST": env("DB_HOST", "127.0.0.1"),
            "PORT": env("DB_PORT", "5432"),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
            "OPTIONS": {
                # Wait for a busy database rather than failing at once. The
                # default is 5 seconds; a slow USB write under load can take
                # longer than that. Supported by every Django version.
                #
                # The rest of the tuning - WAL and friends - is applied in
                # core/apps.py on each new connection instead of through
                # OPTIONS, because the "init_command" and "transaction_mode"
                # keys only exist from Django 5.1 and this project gets run
                # against 4.2 as well.
                "timeout": 30,
            },
        }
    }

AUTH_USER_MODEL = "core.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "post_login"
LOGOUT_REDIRECT_URL = "home"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Manila"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"

# ---------------------------------------------------------------------------
# Smart Hose Leak settings
# ---------------------------------------------------------------------------

SITE_NAME = "Smart Hose Leak Detection System"
SITE_LOCALE = "Brgy. Lower Dakong Patag, Sison, Surigao del Norte"

# Bounding box of the municipality, from the official NAMRIA / PSA outline in
# static/data/sison.geojson. Household coordinates are checked against it so a
# mistyped value cannot silently drop a household in the sea. The margin allows
# for a tap just outside the surveyed edge without being so wide that a wrong
# digit slips through.
# Django defaults this to "same-origin", which strips the Referer from every
# cross-origin request. OpenStreetMap's tile servers use the Referer to tell
# which app is asking, and answer an unidentified request with an "Access
# blocked" image - served as HTTP 200, so the map fills with blocked tiles
# and nothing reports an error.
#
# strict-origin-when-cross-origin sends only the origin (no path, no query) to
# other sites, and nothing at all when downgrading HTTPS to HTTP. That is
# enough for the tile server to identify us without leaking which page of the
# dashboard the facilitator happens to be on.
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"

MUNICIPALITY_NAME = "Sison, Surigao del Norte"
# ---------------------------------------------------------------- SMS alerts
#
# Sent through Semaphore, a Philippine SMS gateway, over HTTPS.
#
# The earlier design used a SIM800L plugged into this machine. It was chosen
# for costing nothing per message on an unli-text promo, and for working with
# no internet at all - but a hosted server has no USB port to plug a modem
# into, and pyserial can only open a port on the machine running the code. A
# gateway works from a laptop and from a data centre alike, so the same build
# serves the prototype and the deployment with nothing to migrate.
#
# What is given up: SMS during an internet outage. That is exactly when it
# matters most, and the answer to it is the SIM800L on the BOARD - which
# texts the owner directly when it cannot reach the server at all.
#
# Sending stays queued rather than done inside the request. A gateway call is
# a network round trip that can hang, and a facilitator must never watch a
# spinner because an API is slow - nor lose the message by reloading.
SMS_ENABLED = env_bool("SMS_ENABLED", True)

# From semaphore.co. Without it nothing is sent and messages wait in the
# queue, which is the same way the system behaves when any other part of the
# delivery path is missing.
SEMAPHORE_API_KEY = env("SEMAPHORE_API_KEY", "")

# What the message appears to come from. Semaphore's default works with no
# registration; a custom name has to be registered with them first, and an
# unregistered one is rejected at send time.
SEMAPHORE_SENDER_NAME = env("SEMAPHORE_SENDER_NAME", "")

SEMAPHORE_BASE_URL = env("SEMAPHORE_BASE_URL", "https://api.semaphore.co/api/v4")

# Text the household automatically when their line is flagged. Off would mean
# a leak at 2 AM waits for somebody to open a browser.
SMS_AUTO_ON_LEAK = env_bool("SMS_AUTO_ON_LEAK", True)

# Copy the facilitator on every leak alert, at the contact number on their
# account. They are the one who has to act on it, and hearing about a leak
# first from the household that received the text is a poor way to find out.
SMS_FACILITATOR_ON_LEAK = env_bool("SMS_FACILITATOR_ON_LEAK", True)

# How often the worker looks for queued messages, and how many times it will
# retry one before giving up. A failed send is usually a network hiccup or an
# empty balance, both of which clear, so retrying is worth it - but not
# forever, or one bad number blocks the queue behind it.
SMS_POLL_SECONDS = int(env("SMS_POLL_SECONDS", "10"))
SMS_MAX_ATTEMPTS = int(env("SMS_MAX_ATTEMPTS", "3"))

# Semaphore knows whether a message reached the handset, but only tells us if
# we ask. Without this the log could say "sent" for a message that never
# arrived, which is the one thing an alert log must not do.
SMS_TRACK_DELIVERY = env_bool("SMS_TRACK_DELIVERY", True)
SMS_DELIVERY_POLL_SECONDS = int(env("SMS_DELIVERY_POLL_SECONDS", "60"))

# Stop asking after this long. A message the gateway has not resolved in a
# few hours is not going to resolve, and polling it forever would grow into
# a request per old message per minute.
SMS_DELIVERY_MAX_AGE_HOURS = int(env("SMS_DELIVERY_MAX_AGE_HOURS", "6"))

# ---------------------------------------------------------------------------
# Push notifications for the Owner Portal
#
# Web Push is a browser standard, not a product: the keys below are a VAPID
# key pair this server generates for itself with "manage.py make_push_keys",
# and nothing is registered with anybody. Without them every push function
# reports "not configured" and does nothing - the portal has to run on a
# laptop during development and at a defence, where no keys exist, and it
# must not fail or nag because of it.
#
# Push is never the only way an alert reaches a household. The SMS goes out
# regardless; this is a second, faster route to whichever handsets have asked
# for it.
# ---------------------------------------------------------------------------

PUSH_ENABLED = env_bool("PUSH_ENABLED", True)

VAPID_PUBLIC_KEY = env("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = env("VAPID_PRIVATE_KEY", "")

# Who to contact about this application, for the push service's records. It
# is never shown to the person receiving a notification. A mailto: or https:
# URL; the spec requires one, and some push services reject messages without.
VAPID_SUBJECT = env("VAPID_SUBJECT", "mailto:facilitator@example.com")

# Whether a leak alert also pushes. Separate from SMS_AUTO_ON_LEAK so either
# channel can be silenced on its own while testing.
PUSH_AUTO_ON_LEAK = env_bool("PUSH_AUTO_ON_LEAK", True)

MUNICIPALITY_BOUNDS = (9.61325, 125.43605, 9.69744, 125.54105)  # S, W, N, E
MUNICIPALITY_BOUNDS_MARGIN_DEG = 0.05  # about 5.5 km

# A segment whose last reading is older than this is shown as offline.
#
# The firmware posts every UPLOAD_INTERVAL_MS (2 s), so 10 s is five missed
# uploads - enough margin to ride out a dropped packet or a WiFi hiccup without
# flapping, while still noticing a genuinely dead board in about ten seconds
# rather than a minute. Raise it if the link is poor enough to cause false
# offline reports; it is the single value that decides detection speed.
DEVICE_OFFLINE_AFTER_SECONDS = int(env("DEVICE_OFFLINE_AFTER_SECONDS", "10"))

# Minute rollups older than this are pruned by `manage.py prune_rollups`.
ROLLUP_RETENTION_DAYS = int(env("ROLLUP_RETENTION_DAYS", "90"))

# How often the dashboards poll /api/live/ (milliseconds). Matched to the
# firmware's upload interval: polling faster than the device posts only costs
# requests without showing anything new.
LIVE_POLL_INTERVAL_MS = int(env("LIVE_POLL_INTERVAL_MS", "2000"))
