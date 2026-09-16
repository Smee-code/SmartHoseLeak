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

# One hose line is built per household below, so households no longer carry a
# module number: each house has its own run from the main source.
HOUSEHOLDS = [
    ("HH-001", "Maria Santos", "Purok 1, Lower Dakong Patag", "+639171234501"),
    ("HH-002", "Jose Dela Cruz", "Purok 1, Lower Dakong Patag", "+639171234502"),
    ("HH-003", "Ana Reyes", "Purok 2, Lower Dakong Patag", "+639171234503"),
    ("HH-004", "Pedro Lim", "Purok 2, Lower Dakong Patag", "+639171234504"),
    ("HH-005", "Rosa Mendoza", "Purok 3, Lower Dakong Patag", "+639171234505"),
    ("HH-006", "Danilo Cruz", "Purok 3, Lower Dakong Patag", "+639171234506"),
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
        # The first household's line gets this board; the rest are created
        # without hardware, which is the real state of the rollout and what
        # the dashboard is built to show.
        device, created = Device.objects.get_or_create(
            device_id=options["device_id"],
            defaults={
                "label": "ESP32 - household unit",
                "firmware_note": "1 flow + 1 pressure sensor, motorised valve, SIM800L fallback",
            },
        )
        self.stdout.write(("Created device " if created else "Using device ") + device.device_id)

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
            colors = Segment.DEFAULT_COLOR_CYCLE
            for position, (hid, owner, address, contact) in enumerate(HOUSEHOLDS):
                household, made = Household.objects.get_or_create(
                    household_id=hid,
                    defaults={"owner_name": owner, "address": address, "contact_number": contact},
                )
                if household.segment is None:
                    household.segment = Segment.objects.create(
                        device=device if position == 0 else None,
                        module_index=1,
                        name=f"{hid} - {owner}",
                        color=colors[position % len(colors)],
                    )
                    household.save(update_fields=["segment"])
                    fitted = "with the board" if position == 0 else "no board yet"
                    self.stdout.write(f"  line for {hid} ({fitted})")
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
