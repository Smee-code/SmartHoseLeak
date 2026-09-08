"""
One-shot bootstrap: creates the device, the three segments with the agreed
distance markers, a facilitator account and a few sample households.

    python manage.py setup_demo
"""

import secrets

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from core.models import Device, Household, Segment

User = get_user_model()

SEGMENTS = [
    (1, "Line 1 - Main Valve to Upper Purok", 0, 15),
    (2, "Line 2 - Upper Purok to Chapel Junction", 15, 30),
    (3, "Line 3 - Chapel Junction to Lower Patag", 30, 50),
]

HOUSEHOLDS = [
    ("HH-001", "Maria Santos", "Purok 1, Lower Dakong Patag", "+639171234501", 1),
    ("HH-002", "Jose Dela Cruz", "Purok 1, Lower Dakong Patag", "+639171234502", 1),
    ("HH-003", "Ana Reyes", "Purok 2, Lower Dakong Patag", "+639171234503", 2),
    ("HH-004", "Pedro Lim", "Purok 2, Lower Dakong Patag", "+639171234504", 2),
    ("HH-005", "Rosa Mendoza", "Purok 3, Lower Dakong Patag", "+639171234505", 3),
    ("HH-006", "Danilo Cruz", "Purok 3, Lower Dakong Patag", "+639171234506", 3),
]


class Command(BaseCommand):
    help = "Create the device, segments, facilitator and sample households."

    def add_arguments(self, parser):
        parser.add_argument("--device-id", default="esp32-01")
        parser.add_argument("--facilitator", default="facilitator")
        parser.add_argument("--password", default="", help="Facilitator password (generated if omitted).")
        parser.add_argument(
            "--no-households", action="store_true", help="Skip creating the sample households."
        )

    def handle(self, *args, **options):
        device, created = Device.objects.get_or_create(
            device_id=options["device_id"],
            defaults={
                "label": "ESP32 - 3 module rig",
                "firmware_note": "3 flow + 3 pressure sensors, SIM800L SMS to facilitator",
            },
        )
        self.stdout.write(("Created device " if created else "Using device ") + device.device_id)

        for index, name, start_m, end_m in SEGMENTS:
            segment, made = Segment.objects.get_or_create(
                device=device,
                module_index=index,
                defaults={"name": name, "start_m": start_m, "end_m": end_m},
            )
            self.stdout.write(f"  {'created' if made else 'exists '} M{index}: {segment.name} ({start_m}-{end_m} m)")

        password = options["password"] or secrets.token_urlsafe(8)
        facilitator, made = User.objects.get_or_create(
            username=options["facilitator"],
            defaults={
                "role": User.Role.FACILITATOR,
                "first_name": "Water System",
                "last_name": "Facilitator",
                "is_staff": True,
                "is_superuser": True,
            },
        )
        if made:
            facilitator.set_password(password)
            facilitator.save()
            self.stdout.write(self.style.SUCCESS(f"Facilitator login: {facilitator.username} / {password}"))
        else:
            self.stdout.write(f"Facilitator {facilitator.username} already exists (password unchanged).")

        if not options["no_households"]:
            segments = {s.module_index: s for s in device.segments.all()}
            for hid, owner, address, contact, module in HOUSEHOLDS:
                household, made = Household.objects.get_or_create(
                    household_id=hid,
                    defaults={
                        "owner_name": owner,
                        "address": address,
                        "contact_number": contact,
                        "segment": segments.get(module),
                    },
                )
                if made and household.user is None:
                    owner_password = secrets.token_urlsafe(6)
                    username = hid.lower().replace("-", "")
                    user = User.objects.create_user(
                        username=username,
                        password=owner_password,
                        role=User.Role.OWNER,
                        first_name=owner.split(" ")[0],
                        last_name=" ".join(owner.split(" ")[1:]),
                        contact_number=contact,
                        must_change_password=True,
                    )
                    household.user = user
                    household.save(update_fields=["user"])
                    self.stdout.write(f"  {hid}: owner login {username} / {owner_password}")

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Setup complete."))
        self.stdout.write(f"Device API key for the ESP32 sketch:\n  {device.api_key}")
