"""
Give every household owner the same known password.

Passwords are stored as hashes and cannot be read back - not by this command,
not by the facilitator, not by anyone. That is the point of them. So the only
way to make a forgotten demo login usable again is to set a new one.

Refuses to run unless DEBUG is on. On a real deployment this would hand every
household's account to whoever ran it.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.models import Household, User

DEFAULT_PASSWORD = "demo12345"


class Command(BaseCommand):
    help = "Reset every household owner's password to a known demo value (DEBUG only)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--password", default=DEFAULT_PASSWORD,
            help=f"What to set. Default: {DEFAULT_PASSWORD}",
        )
        parser.add_argument(
            "--keep-must-change", action="store_true",
            help=(
                "Leave the 'must change password' flag on, so owners are still "
                "prompted at first login. Off by default, because being asked to "
                "change it is the thing that makes a demo login stop working."
            ),
        )

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError(
                "Refusing to run with DEBUG off. This sets a known password on "
                "every household account, which on a live system would hand all "
                "of them to anybody who knows the default."
            )

        password = options["password"]
        if len(password) < 8:
            raise CommandError("Use at least 8 characters - Django will reject shorter ones.")

        owners = User.objects.filter(role=User.Role.OWNER).order_by("username")
        if not owners:
            self.stdout.write("No household owner accounts exist.")
            return

        for owner in owners:
            owner.set_password(password)
            if not options["keep_must_change"]:
                owner.must_change_password = False
            owner.save(update_fields=["password", "must_change_password"])

            household = Household.objects.filter(user=owner).first()
            label = household.household_id if household else "(no household)"
            self.stdout.write(f"  {label:8}  {owner.username:20}  {password}")

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(f"Reset {owners.count()} owner account(s) to: {password}")
        )
        if options["keep_must_change"]:
            self.stdout.write(
                "They will be asked to change it at first login, so this value "
                "stops working as soon as they do."
            )
        self.stdout.write("Write this into CREDENTIALS.txt if you are keeping it.")
