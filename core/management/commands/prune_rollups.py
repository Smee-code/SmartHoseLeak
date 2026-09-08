"""Delete minute rollups older than ROLLUP_RETENTION_DAYS."""

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import ReadingRollup


class Command(BaseCommand):
    help = "Prune reading rollups beyond the retention window."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=settings.ROLLUP_RETENTION_DAYS)

    def handle(self, *args, **options):
        cutoff = timezone.now() - timezone.timedelta(days=options["days"])
        deleted, _ = ReadingRollup.objects.filter(minute__lt=cutoff).delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} rollup rows older than {options['days']} days."))
