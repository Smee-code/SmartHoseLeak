"""
One household, fully furnished, for looking at the owner portal.

The demo households from setup_demo are the honest state of the rollout:
most have no board, none has a valve, and their feeds are empty until
something happens. That is right for the dashboard and wrong for showing a
panel what an OWNER sees, because an owner with nothing on their line sees
an empty screen.

This creates a household whose line has a board and a valve, with a day of
pressure history, a leak that was found and cleared, notices of every kind,
a complaint thread with a reply, and a visit on the calendar. The login has
no contact number and no email ON PURPOSE: the sign-in code has nowhere to
go, so the account signs in on the password alone, with the warning the
real system shows in that case. No SMS credit, no mailbox, no code to type.

Refuses to run with DEBUG off. A real deployment has no business holding an
account that skips the second step.

    python manage.py setup_demo_owner
    python manage.py setup_demo_owner --reset     # wipe and rebuild it

Keep the line alive with the simulator afterwards (the command prints the
exact line); without readings the portal reads "Not being checked" within
ten seconds, which is also true.
"""

import math
import random

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import (
    AuditLog,
    Barangay,
    Complaint,
    ComplaintMessage,
    Device,
    Household,
    LatestReading,
    LeakEvent,
    Notification,
    ReadingRollup,
    Segment,
    Trigger,
    User,
    ValveReason,
    ValveState,
    WorkOrder,
)

HOUSEHOLD_ID = "HH-DEMO"
OWNER_NAME = "Demo Owner"
DEVICE_ID = "esp32-demo"


class Command(BaseCommand):
    help = "Create a furnished demo household (login: demo / demo12345, no sign-in code)."

    def add_arguments(self, parser):
        parser.add_argument("--username", default="demo")
        parser.add_argument("--password", default="demo12345")
        parser.add_argument(
            "--reset", action="store_true",
            help="Delete the demo household, its line, board and login first, then rebuild.",
        )

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError(
                "Refusing: DEBUG is off. This account skips the sign-in code and "
                "belongs on a demo machine only."
            )

        if options["reset"]:
            self._wipe()

        barangay = Barangay.objects.filter(name=settings.DEFAULT_BARANGAY_NAME).first() \
            or Barangay.objects.order_by("id").first()
        if barangay is None:
            raise CommandError("No barangay yet. Run setup_demo first.")
        facilitator = (
            User.objects.filter(role=User.Role.FACILITATOR, barangay=barangay, is_active=True)
            .exclude(first_name="").order_by("id").first()
            or User.objects.filter(role=User.Role.FACILITATOR, barangay=barangay).first()
        )

        # ---- the board, the line and the household -----------------------
        device, _ = Device.objects.get_or_create(
            device_id=DEVICE_ID,
            defaults={
                "label": "ESP32 - demo household unit",
                "firmware_note": "water_leak_single_wifi, motorised valve fitted",
            },
        )
        household = Household.objects.filter(household_id=HOUSEHOLD_ID).first()
        if household is None:
            segment = Segment.objects.create(
                device=device, module_index=1,
                name=f"{HOUSEHOLD_ID} - {OWNER_NAME}",
                color="#7e22ce", barangay=barangay,
                status=Segment.Status.NORMAL,
            )
            household = Household.objects.create(
                household_id=HOUSEHOLD_ID,
                owner_name=OWNER_NAME,
                address="Purok 3, Lower Dakung Patag",
                # Blank on purpose: no number and no email means no sign-in
                # code can be sent, so the password alone signs this in.
                contact_number="",
                email="",
                barangay=barangay,
                segment=segment,
                latitude=9.6372, longitude=125.5186,
                account_status=Household.AccountStatus.ACTIVE,
                activated_at=timezone.now(),
                activated_by=facilitator,
                notes="Demo household. Safe to delete: python manage.py setup_demo_owner --reset",
            )
            self.stdout.write(f"Created {HOUSEHOLD_ID} with line and board {device.device_id}")
        else:
            segment = household.segment
            self.stdout.write(f"Using existing {HOUSEHOLD_ID}")

        # ---- the login ---------------------------------------------------
        user, made = User.objects.get_or_create(
            username=options["username"],
            defaults={
                "role": User.Role.OWNER,
                "first_name": "Demo",
                "last_name": "Owner",
                "contact_number": "",
                "email": "",
                "must_change_password": False,
            },
        )
        user.set_password(options["password"])
        user.must_change_password = False
        user.save()
        if household.user_id != user.id:
            household.user = user
            household.save(update_fields=["user"])
        self.stdout.write(("Created" if made else "Reset") + f" login {user.username}")

        # ---- a day of readings, so the pressure page has a chart ----------
        now = timezone.now()
        minute = now.replace(second=0, microsecond=0)
        if not ReadingRollup.objects.filter(segment=segment).exists():
            rows = []
            for back in range(24 * 60, 0, -1):
                t = minute - timezone.timedelta(minutes=back)
                # Gentle diurnal swing: lower in the evening when the barangay
                # draws water, plus noise. A dip two days ago is the leak below.
                hour = timezone.localtime(t).hour
                base = 2.3 - 0.35 * math.sin((hour - 6) / 24 * 2 * math.pi)
                p = max(0.4, base + random.gauss(0, 0.03))
                f = 0.0 if 0 <= hour < 5 else max(0.0, random.gauss(2.0, 1.4))
                rows.append(ReadingRollup(
                    segment=segment, minute=t,
                    avg_pressure_bar=p, min_pressure_bar=p - 0.02, max_pressure_bar=p + 0.02,
                    avg_flow_lpm=f, min_flow_lpm=max(0.0, f - 0.3), max_flow_lpm=f + 0.3,
                    sample_count=30, leak_samples=0,
                ))
            ReadingRollup.objects.bulk_create(rows, batch_size=500)
            self.stdout.write(f"  {len(rows)} minutes of pressure history")

        # The live value: valve fitted and open. The simulator keeps it fresh.
        LatestReading.objects.update_or_create(
            segment=segment,
            defaults={
                "pressure_bar": 2.2, "flow_lpm": 0.0, "k_conductance": 3.0,
                "device_status": "NORMAL",
                "valve_state": ValveState.OPEN, "valve_reason": ValveReason.NONE,
                "simulated": True, "updated_at": now,
            },
        )
        device.last_seen = now
        device.last_ip = "127.0.0.1"
        device.save(update_fields=["last_seen", "last_ip"])
        segment.status = Segment.Status.NORMAL
        segment.save(update_fields=["status"])

        # ---- history: a leak found by the night test and cleared ----------
        if not LeakEvent.objects.filter(segment=segment).exists():
            found = now - timezone.timedelta(days=2, hours=3)
            leak = LeakEvent.objects.create(
                segment=segment, status=Segment.Status.POSSIBLE_LEAK,
                previous_status=Segment.Status.NORMAL,
                severity=LeakEvent.Severity.CRITICAL,
                pressure_bar=1.4, flow_lpm=0.9, trigger=Trigger.NIGHT,
                detected_at=found, cleared_at=found + timezone.timedelta(hours=9),
                acknowledged_at=found + timezone.timedelta(minutes=25),
                acknowledged_by=facilitator,
                resolution_note="Garden hose joint had split. Replaced the coupling.",
            )
            running = now - timezone.timedelta(hours=20)
            LeakEvent.objects.create(
                segment=segment, status=Segment.Status.WATER_RUNNING,
                previous_status=Segment.Status.NORMAL,
                severity=LeakEvent.Severity.WARNING,
                pressure_bar=2.0, flow_lpm=8.5, trigger=Trigger.SUSTAINED,
                detected_at=running, cleared_at=running + timezone.timedelta(minutes=40),
            )
            self.stdout.write("  2 past alerts (one leak, one water left running)")

            # ---- notices of every kind ----------------------------------
            Notification.objects.create(
                kind=Notification.Kind.AUTOMATIC, recipient=household, segment=segment,
                event=leak, title="Possible leak on your line",
                body=("A possible leak was detected on your line at 1.40 bar with water "
                      "still flowing while every tap should be closed. Please check your "
                      "faucets and hose. The facilitator has been told."),
                created_at=found,
            )
            Notification.objects.create(
                kind=Notification.Kind.TARGETED, recipient=household, segment=segment,
                sent_by=facilitator, title="Repair done",
                body=("Hi! We replaced the split coupling on your garden hose this morning. "
                      "Pressure is back to normal. Thank you for reporting it quickly."),
                created_at=found + timezone.timedelta(hours=9, minutes=10),
            )
            Notification.objects.create(
                kind=Notification.Kind.BROADCAST, recipient=household,
                sent_by=facilitator, title="Scheduled interruption",
                body=("Water will be off this Saturday from 8 AM to 11 AM while the main "
                      "line is flushed. Please store water the night before."),
                created_at=now - timezone.timedelta(days=1, hours=2),
            )
            Notification.objects.create(
                kind=Notification.Kind.AUTOMATIC, recipient=household, segment=segment,
                title="Water has been running",
                body=("Water has run non-stop on your line for 30 minutes. If nobody is "
                      "using it, please check for an open tap or hose."),
                created_at=running + timezone.timedelta(minutes=30),
            )
            self.stdout.write("  4 notices in the feed")

            # ---- a complaint thread with a reply ------------------------
            complaint = Complaint.objects.create(
                household=household, category=Complaint.Category.LOW_PRESSURE,
                subject="Weak flow in the evenings",
                body="From about 6 PM the shower barely runs. Mornings are fine.",
                status=Complaint.Status.IN_PROGRESS,
                created_at=now - timezone.timedelta(days=3),
                last_message_at=now - timezone.timedelta(days=2, hours=20),
            )
            ComplaintMessage.objects.create(
                complaint=complaint, author=user, from_owner=True,
                body="From about 6 PM the shower barely runs. Mornings are fine.",
                created_at=complaint.created_at, read_by_facilitator=True, read_by_owner=True,
            )
            ComplaintMessage.objects.create(
                complaint=complaint, author=facilitator, from_owner=False,
                body=("Thanks. Evening pressure drops when the whole purok cooks at once. "
                      "We have scheduled a visit to check your branch valve."),
                created_at=complaint.last_message_at, read_by_facilitator=True, read_by_owner=True,
            )
            self.stdout.write("  1 complaint with a reply")

            # ---- a visit on the calendar --------------------------------
            WorkOrder.objects.create(
                kind=WorkOrder.Kind.VISIT, status=WorkOrder.Status.SCHEDULED,
                barangay=barangay, household=household, segment=segment,
                title="Check branch valve and evening pressure",
                details="Follow-up to the low-pressure complaint.",
                assigned_to=facilitator, created_by=facilitator,
                scheduled_for=timezone.localdate() + timezone.timedelta(days=2),
                source_complaint=complaint,
            )
            self.stdout.write("  1 visit scheduled")

        AuditLog.record(None, "demo.owner", f"Demo household {HOUSEHOLD_ID} set up", target=HOUSEHOLD_ID)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Demo owner ready."))
        self.stdout.write(f"  Sign in as   {user.username} / {options['password']}   (no code is asked for)")
        self.stdout.write("  Portal       /portal/")
        self.stdout.write("")
        self.stdout.write("Keep the line live and the valve reporting (another terminal):")
        self.stdout.write(
            f"  python tools/simulator.py --device-id {device.device_id} --modules 1 "
            f"--valve open --key {device.api_key}"
        )

    def _wipe(self):
        household = Household.objects.filter(household_id=HOUSEHOLD_ID).first()
        if household:
            if household.user:
                household.user.delete()
            segment = household.segment
            household.delete()
            if segment:
                segment.delete()
        Device.objects.filter(device_id=DEVICE_ID).delete()
        self.stdout.write("Removed the previous demo household.")
