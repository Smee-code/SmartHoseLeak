"""
Fetch the road route from the water source to each household with coordinates.

    python manage.py fetch_routes          households with no route yet, or whose last attempt failed
    python manage.py fetch_routes --all    every household with coordinates (e.g. after the road data improved)

Routes are normally fetched when a household's coordinates are saved. This is
for the other cases: the key was added after households were surveyed, the
service was down at the time, or the whole barangay's routes should be redone.
"""

from django.core.management.base import BaseCommand

from core import routing
from core.models import Household


class Command(BaseCommand):
    help = "Fetch road routes from the water source to each household with coordinates."

    def add_arguments(self, parser):
        parser.add_argument(
            "--all", action="store_true",
            help="Refetch even households that already have a route.",
        )

    def handle(self, *args, **options):
        ready, note = routing.describe_routing()
        if not ready:
            self.stdout.write(self.style.WARNING(note))
            return

        households = Household.objects.filter(
            latitude__isnull=False, longitude__isnull=False
        ).select_related("barangay").order_by("household_id")
        if not options["all"]:
            households = households.filter(visit_route__isnull=True)

        fetched = missed = 0
        for household in households:
            if routing.update_household_route(household):
                fetched += 1
                self.stdout.write(f"  {household.household_id}: {household.visit_route_label}")
            else:
                missed += 1
                self.stdout.write(
                    f"  {household.household_id}: {household.visit_route_error or 'nothing to route between'}"
                )

        self.stdout.write(self.style.SUCCESS(f"Routes fetched: {fetched}. Not fetched: {missed}."))
