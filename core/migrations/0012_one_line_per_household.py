"""
Rebuild the lines: one hose per household, instead of three shared runs.

The rig was three sensor pairs on one board strung along a single run, so a
household was assigned to whichever stretch it happened to sit on and a leak
could only be narrowed to that stretch. Each house now has its own hose from
the main source and its own board, which makes the line and the household the
same thing - and makes an alert name a door rather than a length of pipe.

Readings, rollups and leak events are discarded rather than carried over.
They were recorded against the old three-line geometry and there is no honest
way to reassign them: a reading from "15-30 m" belonged to two households at
once. Households, complaints, notices and logins are untouched.
"""

from django.db import migrations

# The same palette Segment.COLOR_CHOICES offers, so rebuilt lines look like
# ones added through the dashboard rather than a special case.
COLOR_CYCLE = [
    "#0891b2", "#4338ca", "#be185d", "#b45309",
    "#15803d", "#7c3aed", "#0f766e",
]


def rebuild(apps, schema_editor):
    Segment = apps.get_model("core", "Segment")
    Household = apps.get_model("core", "Household")
    LatestReading = apps.get_model("core", "LatestReading")
    ReadingRollup = apps.get_model("core", "ReadingRollup")
    LeakEvent = apps.get_model("core", "LeakEvent")
    Notification = apps.get_model("core", "Notification")

    # Automatic leak notices point at events that are about to go. Clear them
    # first so the cascade cannot take an owner's manual notices with it.
    Notification.objects.filter(kind="automatic").delete()
    LatestReading.objects.all().delete()
    ReadingRollup.objects.all().delete()
    LeakEvent.objects.all().delete()

    # Take the board off the old lines and clear them out before building the
    # new ones. "One line per board" is a database constraint, so the old
    # line holding this device has to be gone before its replacement exists.
    existing_device_id = (
        Segment.objects.exclude(device__isnull=True)
        .order_by("module_index")
        .values_list("device_id", flat=True)
        .first()
    )
    Segment.objects.all().delete()

    for position, household in enumerate(Household.objects.order_by("household_id")):
        line = Segment.objects.create(
            device_id=existing_device_id if position == 0 else None,
            module_index=1,
            name=f"{household.household_id} - {household.owner_name}",
            color=COLOR_CYCLE[position % len(COLOR_CYCLE)],
            low_pressure_bar=0.3,
            status="OFFLINE",
            is_active=True,
        )
        household.segment = line
        household.save(update_fields=["segment"])


def unrebuild(apps, schema_editor):
    """
    Deliberately not reversible.

    Going back would mean inventing three shared runs and guessing which
    household sat on which - fabricating the very mapping this migration
    exists to remove.
    """
    raise migrations.exceptions.IrreversibleError(
        "Per-household lines cannot be turned back into shared runs."
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0011_per_household_lines")]
    operations = [migrations.RunPython(rebuild, unrebuild)]
