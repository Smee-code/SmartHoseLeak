"""
Point existing "Reply to your complaint CMP-..." notices at their thread.

Notices created before complaints became conversations have no link to the
complaint they came from, so they render as a dead copy of a message the
owner cannot click through to. The reference is in the title, which is
enough to match them up.

Only rows whose title matches the exact pattern are touched; anything else
is left alone.
"""

import re

from django.db import migrations

REFERENCE = re.compile(r"^Reply to your complaint (CMP-[0-9A-Za-z-]+)$")


def link_notices(apps, schema_editor):
    Notification = apps.get_model("core", "Notification")
    Complaint = apps.get_model("core", "Complaint")

    by_reference = {c.reference: c.pk for c in Complaint.objects.all()}
    if not by_reference:
        return

    updates = []
    for notification in Notification.objects.filter(
        complaint__isnull=True, title__startswith="Reply to your complaint "
    ).iterator():
        match = REFERENCE.match(notification.title.strip())
        if not match:
            continue
        complaint_pk = by_reference.get(match.group(1))
        if complaint_pk is None:
            continue          # thread since deleted; leave the notice as it is
        notification.complaint_id = complaint_pk
        updates.append(notification)

    if updates:
        Notification.objects.bulk_update(updates, ["complaint_id"])


def unlink_notices(apps, schema_editor):
    apps.get_model("core", "Notification").objects.filter(
        title__startswith="Reply to your complaint "
    ).update(complaint_id=None)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0008_notification_complaint"),
    ]

    operations = [
        migrations.RunPython(link_notices, unlink_notices),
    ]
