"""
Put every line's colour back inside the palette the form offers.

0012 rebuilt the lines with a hardcoded list of hex values instead of reading
Segment.DEFAULT_COLOR_CYCLE, and three of them were not in it. The colour
field is a choice field, so those three lines could not be saved at all: the
edit dialog showed no colour selected, and pressing Save returned "Select a
valid choice" about a value the facilitator never chose and could not see.

Each stray colour is moved to the nearest unused palette entry so the lines
stay visually distinct on the leak map, which is the only thing their colour
is for.
"""

from django.db import migrations

PALETTE = [
    "#0891b2", "#4338ca", "#be185d", "#0f766e",
    "#c2410c", "#4d7c0f", "#7e22ce", "#1e293b",
]


def snap(apps, schema_editor):
    Segment = apps.get_model("core", "Segment")

    taken = {
        colour
        for colour in Segment.objects.values_list("color", flat=True)
        if colour in PALETTE
    }

    for segment in Segment.objects.exclude(color__in=PALETTE).order_by("id"):
        free = next((c for c in PALETTE if c not in taken), PALETTE[0])
        segment.color = free
        segment.save(update_fields=["color"])
        taken.add(free)


def unsnap(apps, schema_editor):
    """
    Nothing to undo. The colours this replaced were never valid choices, and
    restoring them would put the form back into the state where three lines
    could not be saved.
    """


class Migration(migrations.Migration):
    dependencies = [("core", "0020_sms_delivery_status")]
    operations = [migrations.RunPython(snap, unsnap)]
