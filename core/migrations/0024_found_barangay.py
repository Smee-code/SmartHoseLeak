"""
Found the first barangay and move every existing record into it.

The system ran single-barangay for its whole life so far: households, hose
lines and the facilitator all implicitly belonged to Lower Dakong Patag, named
only in a settings string. Multiple barangays make that implicit belonging a
real foreign key, and this migration fills it in for the records that predate
it - so nothing is left unassigned and the upgrade is invisible in the field.
"""

from django.db import migrations

FOUNDING_BARANGAY = "Lower Dakong Patag"
MUNICIPALITY = "Sison"
PROVINCE = "Surigao del Norte"


def found_barangay(apps, schema_editor):
    Barangay = apps.get_model("core", "Barangay")
    Household = apps.get_model("core", "Household")
    Segment = apps.get_model("core", "Segment")
    User = apps.get_model("core", "User")

    # If there is nothing to move, do not invent a barangay. A fresh install
    # should start empty and let the administrator add the first one.
    if not (Household.objects.exists() or Segment.objects.exists()):
        return

    brgy, _ = Barangay.objects.get_or_create(
        name=FOUNDING_BARANGAY,
        defaults={"municipality": MUNICIPALITY, "province": PROVINCE},
    )

    Household.objects.filter(barangay__isnull=True).update(barangay=brgy)
    Segment.objects.filter(barangay__isnull=True).update(barangay=brgy)
    # Existing facilitators were all running this one barangay.
    User.objects.filter(role="facilitator", barangay__isnull=True).update(barangay=brgy)


def unfound_barangay(apps, schema_editor):
    # Reverse only clears the links; the barangay row is left in place because
    # the forward step may have found one that already existed.
    Household = apps.get_model("core", "Household")
    Segment = apps.get_model("core", "Segment")
    User = apps.get_model("core", "User")
    Barangay = apps.get_model("core", "Barangay")

    brgy = Barangay.objects.filter(name=FOUNDING_BARANGAY).first()
    if brgy is None:
        return
    Household.objects.filter(barangay=brgy).update(barangay=None)
    Segment.objects.filter(barangay=brgy).update(barangay=None)
    User.objects.filter(barangay=brgy).update(barangay=None)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0023_barangay_alter_user_role_household_barangay_and_more"),
    ]

    operations = [
        migrations.RunPython(found_barangay, unfound_barangay),
    ]
