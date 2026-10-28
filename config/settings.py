"""
Django settings for the Smart Hose Leak Detection System.

SQLite is used by default so the project runs on any machine with no setup.
Set DB_ENGINE=postgres plus the DB_* variables in .env to move to PostgreSQL
without touching any model code.
"""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

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
# painless; a real deployment names its hosts explicitly.
if DEBUG:
    ALLOWED_HOSTS = ["*"]
else:
    # A wildcard in production invites Host-header spoofing. Fail loudly if it
    # was forgotten rather than run wide open.
    ALLOWED_HOSTS = env_list("ALLOWED_HOSTS")
    if not ALLOWED_HOSTS:
        raise ImproperlyConfigured(
            "DEBUG is False but ALLOWED_HOSTS is empty. Set it in .env, e.g.\n"
            "    ALLOWED_HOSTS=yourname.pythonanywhere.com"
        )

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
else:
    # ----------------------------------------------------------------------
    # Production hardening. All of this runs only when DEBUG is False, so
    # local development, field testing and the test suite are untouched.
    # ----------------------------------------------------------------------

    # Refuse to boot with the throwaway development key - shipping it would let
    # anyone forge a session cookie.
    if SECRET_KEY == "dev-only-key-change-before-deployment":
        raise ImproperlyConfigured(
            "DEBUG is False but SECRET_KEY is still the development default. "
            "Put a real one in .env. Generate it with:\n"
            "    python -c \"from django.core.management.utils import "
            "get_random_secret_key as g; print(g())\""
        )

    # For an HTTPS POST Django checks the Origin against this list; without the
    # site's own origin, every login and form submission would 403. Derived
    # from the hosts named above unless set explicitly in .env.
    if not CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS = [f"https://{h}" for h in ALLOWED_HOSTS if h != "*"]

    # PythonAnywhere (and most hosts) terminate TLS at a proxy and forward the
    # request over plain HTTP, marking the original scheme in this header.
    # Without it Django treats every request as insecure: it will not set
    # secure cookies, and request.is_secure() is always False.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

    # Send the session and CSRF cookies back only over HTTPS.
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

    # Redirect plain HTTP to HTTPS. Off by default: the host usually does this
    # already, and the ESP32 posts readings over HTTP during bench testing.
    # Safe to turn on once the deployment is HTTPS-only, thanks to the proxy
    # header above.
    SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", False)

    # HSTS tells a browser to use HTTPS for this site for this many seconds.
    # Left at 0 so a misconfigured first deploy is not locked out of HTTP for
    # a year; set 2592000 (30 days) once the HTTPS setup is proven.
    SECURE_HSTS_SECONDS = int(env("SECURE_HSTS_SECONDS", "0"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", False)
    SECURE_HSTS_PRELOAD = env_bool("SECURE_HSTS_PRELOAD", False)

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
    # Picks the active language from the user's choice (a cookie set by the
    # EN/FIL toggle), falling back to the browser's Accept-Language. Must sit
    # after sessions and before common.
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # After auth, so maintenance mode can let admins through, and so the error
    # logger sees the signed-in user. Both are defined in core/middleware.py.
    "core.middleware.MaintenanceModeMiddleware",
    "core.middleware.ErrorLogMiddleware",
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

# Sign in with a username or an email address. The backend subclasses
# ModelBackend, so it carries the usual permission checks too.
AUTHENTICATION_BACKENDS = ["core.auth.EmailOrUsernameBackend"]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "post_login"
LOGOUT_REDIRECT_URL = "home"

LANGUAGE_CODE = "en"
TIME_ZONE = "Asia/Manila"
USE_I18N = True
USE_TZ = True

# The two languages the toggle offers. Filipino (Tagalog) is the national
# language; English stays the default. Translations live in locale/<code>/.
LANGUAGES = [
    ("en", "English"),
    ("fil", "Filipino"),
]
LOCALE_PATHS = [BASE_DIR / "locale"]

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

# From iprogsms.com. Without it nothing is sent and messages wait in the
# queue, which is the same way the system behaves when any other part of the
# delivery path is missing.
IPROGSMS_API_TOKEN = env("IPROGSMS_API_TOKEN", "")

IPROGSMS_BASE_URL = env("IPROGSMS_BASE_URL", "https://www.iprogsms.com/api/v1")

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

# iprogSMS reports delivery status via a status endpoint. Without this the
# log could say "sent" for a message that never arrived, which is the one
# thing an alert log must not do.
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

# ---------------------------------------------------------------- visit routes
#
# Road-following routes from the barangay's water source to each household,
# so a facilitator can see how to GET there, not just where it is. The hose
# on the map stays a straight line - the pipe goes cross-country and the
# line means "this house is on this hose"; the route is a separate layer.
#
# Fetched ONCE from OpenRouteService when a household's coordinates are
# saved (or the source moves) and stored on the record, so the map draws
# routes with no internet and no further calls. Without a key nothing is
# fetched and the map simply shows no routes. See core/routing.py.
ROUTING_ENABLED = env_bool("ROUTING_ENABLED", True)

# From openrouteservice.org - free, and the daily allowance is thousands of
# routes where this system needs a handful, ever.
ORS_API_KEY = env("ORS_API_KEY", "")
ORS_BASE_URL = env("ORS_BASE_URL", "https://api.openrouteservice.org")

# A motorcycle or tricycle follows the same roads as a car; "driving-car" is
# the profile with the best coverage. "foot-walking" allows footpaths.
ROUTING_PROFILE = env("ROUTING_PROFILE", "driving-car")

# One route per save, done inline; short, so a slow service cannot hold a
# household save for long. A failure keeps the old route and says why.
ROUTING_TIMEOUT_S = int(env("ROUTING_TIMEOUT_S", "8"))

# The one barangay this installation serves. Records are attached to it
# without asking; see Barangay.default().
DEFAULT_BARANGAY_NAME = env("DEFAULT_BARANGAY_NAME", "Lower Dakong Patag")

# ------------------------------------------------------------- sign-in codes
#
# After the password, a 6-digit code by text or email, for every role. Ten
# minutes to use it, five guesses, one use. "Remember this device" sets a
# signed cookie that skips the code on that phone for 30 days. An account
# with neither a number nor an email cannot be sent a code and signs in on
# the password alone, with a warning - a lockout would be worse than a
# missing second step. With DEBUG on, the code is also shown on the page so
# a demo can sign in with no SMS credit and no mailbox.
OTP_ENABLED = env_bool("OTP_ENABLED", True)
OTP_VALIDITY_MINUTES = int(env("OTP_VALIDITY_MINUTES", "10"))
OTP_TRUSTED_DEVICE_DAYS = int(env("OTP_TRUSTED_DEVICE_DAYS", "30"))
OTP_RESEND_SECONDS = int(env("OTP_RESEND_SECONDS", "60"))

# ------------------------------------------------------------------- email
#
# Used for sign-in codes and for notices sent "by email". A Gmail account
# with an app password is the expected setup (EMAIL_HOST_USER is the
# address, EMAIL_HOST_PASSWORD the app password). Until BOTH are set, mail
# is printed to the server terminal instead of sent - nothing breaks, and
# the pages say email is not set up. (An address with no app password would
# otherwise switch SMTP on and have every send refused by Gmail.)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
if EMAIL_HOST_USER and EMAIL_HOST_PASSWORD:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = env("EMAIL_HOST", "smtp.gmail.com")
    EMAIL_PORT = int(env("EMAIL_PORT", "587"))
    EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
    EMAIL_TIMEOUT = int(env("EMAIL_TIMEOUT", "10"))
    DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", f"Smart Hose Leak <{EMAIL_HOST_USER}>")
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
    DEFAULT_FROM_EMAIL = "Smart Hose Leak <no-reply@localhost>"

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

# ------------------------------------------------------- owner board setup
#
# The owner connects their own board: the setup code is sent to them at
# activation, and Settings > My device tells them how it is going. The
# hotspot name and password are what the firmware raises in setup mode;
# change both together with the sketch. The grace periods are longer than
# the dashboard's 10 s so the owner's status does not flicker on a weak
# signal.
BOARD_SETUP_SSID = env("BOARD_SETUP_SSID", "LeakDetect-Setup")
BOARD_SETUP_PASSWORD = env("BOARD_SETUP_PASSWORD", "leakdetect123")
OWNER_DEVICE_GRACE_SECONDS = int(env("OWNER_DEVICE_GRACE_SECONDS", "180"))
OWNER_DEVICE_CONNECTING_SECONDS = int(env("OWNER_DEVICE_CONNECTING_SECONDS", "180"))

# Minute rollups older than this are pruned by `manage.py prune_rollups`.
ROLLUP_RETENTION_DAYS = int(env("ROLLUP_RETENTION_DAYS", "90"))

# How often the dashboards poll /api/live/ (milliseconds). Matched to the
# firmware's upload interval: polling faster than the device posts only costs
# requests without showing anything new.
LIVE_POLL_INTERVAL_MS = int(env("LIVE_POLL_INTERVAL_MS", "2000"))
