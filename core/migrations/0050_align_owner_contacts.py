"""
Bring every owner login into step with its household record.

Until 20 Nov 2026 a facilitator or administrator editing a household changed
the household's number and email only; the owner's login kept the old ones,
and sign-in codes went there. From this migration on, Household.save()
carries them across; this aligns the rows that had already drifted. The
household record wins. Where the household has no email but the login does,
the login's is copied the other way, so nobody loses a way to be reached.
"""

from django.db import migrations


def align(apps, schema_editor):
    Household = apps.get_model("core", "Household")
    User = apps.get_model("core", "User")
    for household in Household.objects.exclude(user__isnull=True).select_related("user"):
        user = household.user
        number = (household.contact_number or "").strip()
        email = (household.email or "").strip()
        user_fields = []
        if number and (user.contact_number or "").strip() != number:
            user.contact_number = number
            user_fields.append("contact_number")
        if email and (user.email or "").strip() != email:
            user.email = email
            user_fields.append("email")
        elif not email and (user.email or "").strip():
            Household.objects.filter(pk=household.pk).update(email=user.email.strip())
        if user_fields:
            User.objects.filter(pk=user.pk).update(**{f: getattr(user, f) for f in user_fields})


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0049_flow_leak_reopen"),
    ]

    operations = [
        migrations.RunPython(align, migrations.RunPython.noop),
    ]
