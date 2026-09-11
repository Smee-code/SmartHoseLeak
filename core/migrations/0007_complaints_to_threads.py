"""
Turn each existing complaint into the first message of its own thread.

Before this, a complaint held the owner's text in `body` and at most one
facilitator reply in `response`. Threads need those as messages, so they are
copied across here rather than being read from two places at render time.

The original columns are deliberately left in place. Nothing reads them for
display any more, but dropping them in the same change as the rewrite would
remove the only copy of the data if this migration turned out to be wrong.
"""

from django.db import migrations


def build_threads(apps, schema_editor):
    Complaint = apps.get_model("core", "Complaint")
    ComplaintMessage = apps.get_model("core", "ComplaintMessage")

    for complaint in Complaint.objects.all().iterator():
        # Guard against a re-run: never duplicate an existing thread.
        if ComplaintMessage.objects.filter(complaint=complaint).exists():
            continue

        owner_user = complaint.household.user if complaint.household_id else None

        messages = [
            ComplaintMessage(
                complaint=complaint,
                author=owner_user,
                from_owner=True,
                body=complaint.body,
                created_at=complaint.created_at,
                # Historic complaints have already been seen and acted on.
                read_by_facilitator=True,
                read_by_owner=True,
            )
        ]

        if (complaint.response or "").strip():
            messages.append(
                ComplaintMessage(
                    complaint=complaint,
                    author=complaint.responded_by,
                    from_owner=False,
                    body=complaint.response,
                    created_at=complaint.responded_at or complaint.updated_at,
                    read_by_facilitator=True,
                    read_by_owner=True,
                )
            )

        ComplaintMessage.objects.bulk_create(messages)
        Complaint.objects.filter(pk=complaint.pk).update(
            last_message_at=messages[-1].created_at
        )


def drop_threads(apps, schema_editor):
    """Reverse: the originals are still in body/response, so just clear these."""
    apps.get_model("core", "ComplaintMessage").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0006_complaint_last_message_at_complaintmessage"),
    ]

    operations = [
        migrations.RunPython(build_threads, drop_threads),
    ]
