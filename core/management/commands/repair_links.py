"""
Clear pointers to rows that no longer exist.

A household points at its hose line, a line at its board, and notices and
work orders may point at a line too. Django clears those pointers when the
target is deleted through the app, but a restore from a backup that has the
household and not its line, a hand edit of the database, or a recovery from
a damaged file leaves the pointer dangling - and the first page to follow it
crashes with "Segment matching query does not exist", while "manage.py
migrate" refuses to run at all (SQLite checks every foreign key when it
rebuilds a table). This puts such pointers back to "none", which the screens
know how to show ("no line yet", "no board fitted"), and then asks SQLite
for any dangling pointer it still sees, so nothing is left to guess. Run
after a restore; the restore button runs it too.
"""

from django.core.management.base import BaseCommand
from django.db import connection

from core.models import Device, Household, Notification, Segment, WorkOrder


def _clear_dangling(queryset, field, live_ids):
    """Null `field` on every row of `queryset` whose `field` is not in live_ids."""
    column = f"{field}_id"
    bad = [
        row.pk for row in queryset.exclude(**{column: None}).only("id", column)
        if getattr(row, column) not in live_ids
    ]
    if bad:
        queryset.filter(pk__in=bad).update(**{field: None})
    return len(bad)


def repair_links():
    """Returns {"households": n, "segments": n, "notifications": n, "work_orders": n}."""
    segment_ids = set(Segment.objects.values_list("id", flat=True))
    device_ids = set(Device.objects.values_list("id", flat=True))
    return {
        "households": _clear_dangling(Household.objects.all(), "segment", segment_ids),
        "segments": _clear_dangling(Segment.objects.all(), "device", device_ids),
        "notifications": _clear_dangling(Notification.objects.all(), "segment", segment_ids),
        "work_orders": _clear_dangling(WorkOrder.objects.all(), "segment", segment_ids),
    }


def remaining_violations():
    """
    What SQLite itself still considers a broken reference, as
    (table, row pk, referenced table) - anything here is a pointer this
    command does not know how to clear (a non-nullable one, or a table that
    gained a link since) and will stop the next migrate.
    """
    if connection.vendor != "sqlite":
        return []
    with connection.cursor() as cur:
        rows = cur.execute("PRAGMA foreign_key_check").fetchall()
    return [(table, rowid, parent) for table, rowid, parent, _ in rows]


class Command(BaseCommand):
    help = "Clear household -> line, line -> board, notice -> line and work order -> line pointers whose target no longer exists."

    def handle(self, *args, **options):
        fixed = repair_links()
        self.stdout.write(
            f"Cleared {fixed['households']} household(s) pointing at a missing line, "
            f"{fixed['segments']} line(s) pointing at a missing board, "
            f"{fixed['notifications']} notice(s) and {fixed['work_orders']} work order(s) "
            f"pointing at a missing line."
        )
        left = remaining_violations()
        if left:
            self.stdout.write(self.style.WARNING(
                f"{len(left)} broken reference(s) remain that this command cannot clear "
                "(the pointer is required, not optional). Each is (table, row, points at):"
            ))
            for item in left[:50]:
                self.stdout.write(f"  {item}")
            if len(left) > 50:
                self.stdout.write(f"  ... and {len(left) - 50} more")
        else:
            self.stdout.write("No broken references remain.")
