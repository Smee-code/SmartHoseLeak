"""
Flag segments whose device has stopped reporting.

The dashboards already show a stale feed as offline on the fly; this command
writes that state into the database and logs an event, so an outage that
happens overnight still appears in the event history. Run it from Task
Scheduler or cron every minute if you want that audit trail.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import mark_stale_offline


class Command(BaseCommand):
    help = "Mark segments offline when their last reading is stale (the site does this itself; kept for cron)."

    def handle(self, *args, **options):
        count = mark_stale_offline(force=True)
        self.stdout.write(self.style.SUCCESS(f"Marked {count} segment(s) offline."))
