"""
Attach the single, founding water source to the founding barangay.

Until now there was one main source for the whole system, because there was
one barangay. With a source per barangay, the row that already exists is the
founding barangay's; this gives it that owner so its location is not lost and
the Lines page keeps showing it where it always did.
"""

from django.db import migrations

FOUNDING_BARANGAY = "Lower Dakong Patag"


def attach_source(apps, schema_editor):
    WaterSource = apps.get_model("core", "WaterSource")
    Barangay = apps.get_model("core", "Barangay")

    orphan = WaterSource.objects.filter(barangay__isnull=True).order_by("pk").first()
    if orphan is None:
        return
    barangay = (
        Barangay.objects.filter(name=FOUNDING_BARANGAY).first()
        or Barangay.objects.order_by("pk").first()
    )
    if barangay is None or WaterSource.objects.filter(barangay=barangay).exists():
        return
    orphan.barangay = barangay
    orphan.save(update_fields=["barangay"])


def detach_source(apps, schema_editor):
    WaterSource = apps.get_model("core", "WaterSource")
    WaterSource.objects.update(barangay=None)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0028_watersource_per_barangay"),
    ]

    operations = [
        migrations.RunPython(attach_source, detach_source),
    ]
