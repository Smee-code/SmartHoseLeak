"""
Which language a person sees, and which they are written to in.

The EN/FIL toggle is Django's. What is ours, and what these tests pin down:
a household opens the portal in its own language before the owner has ever
pressed the toggle; pressing it writes the choice to the household so the
texts follow; and nobody else is affected by a household's preference.
"""

from django.conf import settings
from django.test import TestCase
from django.urls import reverse

from core.models import Barangay, Household, Segment, User


class HouseholdLanguageTests(TestCase):
    def setUp(self):
        self.brgy = Barangay.objects.create(name="Lower Dakong Patag")
        self.owner_user = User.objects.create_user(username="own", password="pw", role=User.Role.OWNER)
        self.segment = Segment.objects.create(name="HH-900 line", barangay=self.brgy)
        self.household = Household.objects.create(
            household_id="HH-900", owner_name="Test Owner", address="Purok 1",
            contact_number="09171234500", user=self.owner_user, segment=self.segment,
            barangay=self.brgy,
        )
        self.faci = User.objects.create_user(
            username="faci", password="pw", role=User.Role.FACILITATOR, barangay=self.brgy,
        )

    def test_a_household_is_filipino_first(self):
        self.assertEqual(self.household.language, "fil")
        self.client.force_login(self.owner_user)
        response = self.client.get(reverse("owner_home"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Content-Language"), "fil")
        self.assertContains(response, '<html lang="fil"')

    def test_the_cookie_wins_once_the_owner_has_chosen(self):
        self.client.force_login(self.owner_user)
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"
        response = self.client.get(reverse("owner_home"))
        self.assertEqual(response.headers.get("Content-Language"), "en")

    def test_a_household_set_to_english_opens_in_english(self):
        self.household.language = "en"
        self.household.save(update_fields=["language"])
        self.client.force_login(self.owner_user)
        response = self.client.get(reverse("owner_home"))
        self.assertEqual(response.headers.get("Content-Language"), "en")

    def test_the_toggle_writes_the_owners_choice_to_the_household(self):
        self.client.force_login(self.owner_user)
        response = self.client.post(reverse("set_language"), {"language": "en", "next": "/portal/"})
        self.assertEqual(response.status_code, 302)
        self.household.refresh_from_db()
        self.assertEqual(self.household.language, "en")
        self.assertEqual(response.cookies[settings.LANGUAGE_COOKIE_NAME].value, "en")

        self.client.post(reverse("set_language"), {"language": "fil", "next": "/portal/"})
        self.household.refresh_from_db()
        self.assertEqual(self.household.language, "fil")

    def test_a_facilitator_is_not_affected_by_any_household(self):
        self.client.force_login(self.faci)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.headers.get("Content-Language"), "en")

    def test_a_visitor_can_still_toggle(self):
        response = self.client.post(reverse("set_language"), {"language": "fil", "next": "/"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.cookies[settings.LANGUAGE_COOKIE_NAME].value, "fil")

    def test_the_javascript_catalog_is_served_in_the_active_language(self):
        response = self.client.get(reverse("javascript-catalog"), HTTP_ACCEPT_LANGUAGE="fil")
        self.assertEqual(response.status_code, 200)
        self.assertIn("gettext", response.content.decode())
