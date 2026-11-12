"""
Clear pointers to rows that no longer exist.

A household points at its hose line and a line at its board. Django clears
those pointers when the target is deleted through the app, but a restore
from a backup that has the household and not its line (or a hand edit of
the database) leaves the pointer dangling - and the first page to follow it
crashes with "Segment matching query does not exist". This puts such a
pointer back to "none", which the screens know how to show ("no line yet",
"no board fitted"). Run after a restore; the restore button runs it too.
"""

from django.core.management.base import BaseCommand

from core.models import Device, Household, Segment


def repair_links():
    """Returns (households_fixed, segments_fixed)."""
    segment_ids = set(Segment.objects.values_list("id", flat=True))
    device_ids = set(Device.objects.values_list("id", flat=True))

    bad_households = [
        h.pk for h in Household.objects.exclude(segment_id=None).only("id", "segment_id")
        if h.segment_id not in segment_ids
    ]
    if bad_households:
        Household.objects.filter(pk__in=bad_households).update(segment=None)

    bad_segments = [
        s.pk for s in Segment.objects.exclude(device_id=None).only("id", "device_id")
        if s.device_id not in device_ids
    ]
    if bad_segments:
        Segment.objects.filter(pk__in=bad_segments).update(device=None)

    return len(bad_households), len(bad_segments)


class Command(BaseCommand):
    help = "Clear household -> line and line -> board pointers whose target no longer exists."

    def handle(self, *args, **options):
        households, segments = repair_links()
        self.stdout.write(
            f"Cleared {households} household(s) pointing at a missing line and "
            f"{segments} line(s) pointing at a missing board."
        )
