from django.apps import AppConfig
from django.conf import settings
from django.db.backends.signals import connection_created


def tune_sqlite(sender, connection, **kwargs):
    """
    Apply SQLite's concurrency settings to every new connection.

    SQLite allows one writer at a time. The ESP32 posts readings every two
    seconds while every open dashboard polls just as often, and the database
    file sits on a USB drive where writes are slow - so those collide, and
    the loser raises "database is locked" and loses that batch of readings.

    These are set here rather than through DATABASES["OPTIONS"] because the
    "init_command" and "transaction_mode" keys were only added in Django 5.1.
    The project's virtualenv has 5.2, but it also gets run against the system
    Python's 4.2, where those keys raise TypeError on connect. A signal works
    on both.
    """
    if connection.vendor != "sqlite":
        return

    mode = getattr(settings, "SQLITE_JOURNAL_MODE", "WAL")
    if mode not in {"WAL", "DELETE", "TRUNCATE", "PERSIST"}:
        mode = "WAL"
    with connection.cursor() as cursor:
        # Under WAL readers carry on while a write is in progress, which
        # removes nearly all of the contention rather than just making it
        # rarer. On a network filesystem (PythonAnywhere) WAL is unsafe and
        # the setting picks the plain rollback journal instead - see
        # SQLITE_JOURNAL_MODE in config/settings.py.
        cursor.execute(f"PRAGMA journal_mode={mode};")
        # Durability to match: under WAL, NORMAL is still crash-safe without
        # an fsync per transaction (the expensive part on removable media);
        # a rollback journal needs FULL to be crash-safe.
        cursor.execute("PRAGMA synchronous=%s;" % ("NORMAL" if mode == "WAL" else "FULL"))
        # Keep scratch files out of the system temp folder, which has been
        # full enough on this machine to fail a query outright.
        cursor.execute("PRAGMA temp_store=MEMORY;")
        # Belt and braces alongside OPTIONS["timeout"]: wait for a busy
        # database instead of giving up immediately.
        cursor.execute("PRAGMA busy_timeout=30000;")


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        connection_created.connect(tune_sqlite, dispatch_uid="core.tune_sqlite")
