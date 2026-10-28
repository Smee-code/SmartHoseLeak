import datetime

from django.conf import settings
from django.db import migrations, models
from django.utils import timezone


def mark_existing_logins_as_activated(apps, schema_editor):
    """A household that already has an owner login was activated when it was registered."""
    Household = apps.get_model("core", "Household")
    for household in Household.objects.filter(user__isnull=False, activated_at__isnull=True):
        when = datetime.datetime.combine(household.date_registered, datetime.time.min)
        household.activated_at = timezone.make_aware(when)
        household.save(update_fields=["activated_at"])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0033_user_facilitator_id"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="household",
            name="activated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="household",
            name="activated_by",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=models.deletion.SET_NULL,
                related_name="activated_households", to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(mark_existing_logins_as_activated, migrations.RunPython.noop),
    ]
