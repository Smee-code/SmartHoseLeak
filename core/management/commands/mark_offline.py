"""
Flag segments whose device has stopped reporting.

The dashboards already show a stale feed as offline on the fly; this command
writes that state into the database and logs an event, so an outage that
happens overnight still appears in the event history. Run it from Task
Scheduler or cron every minute if you want that audit trail.
"""

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import LeakEvent, Segment


class Command(BaseCommand):
    help = "Mark segments offline when their last reading is stale."

    def handle(self, *args, **options):
        cutoff = timezone.now() - timezone.timedelta(seconds=settings.DEVICE_OFFLINE_AFTER_SECONDS)
        stale = Segment.objects.filter(is_active=True, latest__updated_at__lt=cutoff).exclude(
            status__in=[Segment.Status.OFFLINE, Segment.Status.MAINTENANCE]
        )
        count = 0
        for segment in stale:
            LeakEvent.objects.filter(segment=segment, cleared_at__isnull=True).update(
                cleared_at=timezone.now()
            )
            LeakEvent.objects.create(
                segment=segment,
                status=Segment.Status.OFFLINE,
                previous_status=segment.status,
                severity=LeakEvent.Severity.WARNING,
            )
            segment.status = Segment.Status.OFFLINE
            segment.status_changed_at = timezone.now()
            segment.save(update_fields=["status", "status_changed_at"])
            count += 1
        self.stdout.write(self.style.SUCCESS(f"Marked {count} segment(s) offline."))
