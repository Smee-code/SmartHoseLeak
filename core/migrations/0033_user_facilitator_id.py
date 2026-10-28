from django.db import migrations, models


def number_existing_facilitators(apps, schema_editor):
    """Give every facilitator already on the books a number, oldest first."""
    User = apps.get_model("core", "User")
    facilitators = User.objects.filter(role="facilitator").order_by("date_joined", "pk")
    for index, user in enumerate(facilitators, start=1):
        user.facilitator_id = f"FAC-{index:03d}"
        user.save(update_fields=["facilitator_id"])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0032_announcement_email"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="facilitator_id",
            field=models.CharField(
                blank=True, help_text="Assigned automatically to facilitator accounts.",
                max_length=12, null=True, unique=True,
            ),
        ),
        migrations.RunPython(number_existing_facilitators, migrations.RunPython.noop),
    ]
