"""
Registering, archiving and deleting households from the console.

Archive hides and is reversible; Delete is for good and must be confirmed by
typing the household ID; and registering a contact number or email that is
already on a household - archived or not - stops at a warning with a link
(or a restore shortcut) until the admin says it really is a second house.
"""

from django.test import TestCase
from django.urls import reverse

from core.models import (
    AuditLog, Barangay, Complaint, Device, Household, LatestReading, Segment, User, ValveState,
)


class _Console(TestCase):
    def setUp(self):
        self.brgy = Barangay.objects.create(name="Lower Dakong Patag")
        self.admin = User.objects.create_user(username="adm", password="pw", role=User.Role.ADMIN)
        self.client.force_login(self.admin)
        self.device = Device.objects.create(device_id="esp32-hh", label="board")
        self.segment = Segment.objects.create(name="HH-010 line", barangay=self.brgy, device=self.device, module_index=1)
        self.owner = User.objects.create_user(username="maria", password="pw", role=User.Role.OWNER)
        self.household = Household.objects.create(
            household_id="HH-010", owner_name="Maria Santos", address="Purok 1",
            contact_number="09171234510", email="maria@example.com", user=self.owner,
            segment=self.segment, barangay=self.brgy, language="en",
        )
        LatestReading.objects.create(
            segment=self.segment, pressure_bar=1.0, flow_lpm=0.0,
            device_status=Segment.Status.NORMAL, valve_state=ValveState.OPEN,
        )
        Complaint.objects.create(
            household=self.household, category=Complaint.Category.NO_WATER,
            subject="No water", body="Since noon.",
        )

    def _register(self, **overrides):
        data = {
            "owner_name": "Someone Else", "address": "Purok 9", "contact_number": "09171234599",
            "email": "", "language": "fil", "account_status": Household.AccountStatus.ACTIVE,
            "notes": "", "device_choice": "none",
        }
        data.update(overrides)
        return self.client.post(reverse("admin_household_create"), data)


class PermanentDeleteTests(_Console):
    def test_delete_needs_the_household_id_typed_back(self):
        r = self.client.post(reverse("admin_household_destroy", args=[self.household.pk]), {"confirm": "HH-999"})
        self.assertRedirects(r, reverse("admin_household_list"))
        self.assertTrue(Household.objects.filter(pk=self.household.pk).exists())
        r = self.client.post(reverse("admin_household_destroy", args=[self.household.pk]), {})
        self.assertTrue(Household.objects.filter(pk=self.household.pk).exists())

    def test_delete_removes_everything_but_the_board(self):
        r = self.client.post(
            reverse("admin_household_destroy", args=[self.household.pk]),
            {"confirm": "hh-010", "from": "archive"},            # case does not matter
        )
        self.assertRedirects(r, reverse("archive_list"))
        self.assertFalse(Household.objects.filter(pk=self.household.pk).exists())
        self.assertFalse(User.objects.filter(username="maria").exists())
        self.assertFalse(Segment.objects.filter(pk=self.segment.pk).exists())
        self.assertFalse(LatestReading.objects.filter(segment_id=self.segment.pk).exists())
        self.assertFalse(Complaint.objects.filter(subject="No water").exists())
        self.device.refresh_from_db()
        self.assertTrue(Device.objects.filter(pk=self.device.pk).exists())   # a spare now
        self.assertFalse(self.device.segments.exists())
        self.assertTrue(AuditLog.objects.filter(action="household.delete").exists())

    def test_archive_still_only_hides(self):
        r = self.client.post(reverse("admin_household_delete", args=[self.household.pk]))
        self.assertRedirects(r, reverse("admin_household_list"))
        self.household.refresh_from_db()
        self.assertTrue(self.household.is_archived)
        self.assertTrue(Segment.objects.filter(pk=self.segment.pk).exists())
        # Both buttons are offered: Archive on the list, Delete on the archive page too.
        page = self.client.get(reverse("archive_list")).content.decode()
        self.assertIn(reverse("admin_household_destroy", args=[self.household.pk]), page)

    def test_the_list_offers_archive_and_delete(self):
        page = self.client.get(reverse("admin_household_list")).content.decode()
        self.assertIn(reverse("admin_household_delete", args=[self.household.pk]), page)
        self.assertIn(reverse("admin_household_destroy", args=[self.household.pk]), page)

    def test_a_facilitator_delete_also_removes_the_line(self):
        faci = User.objects.create_user(
            username="faci", password="pw", role=User.Role.FACILITATOR, barangay=self.brgy,
        )
        self.client.force_login(faci)
        r = self.client.post(reverse("household_delete", args=[self.household.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Household.objects.filter(pk=self.household.pk).exists())
        self.assertFalse(Segment.objects.filter(pk=self.segment.pk).exists())
        self.assertTrue(Device.objects.filter(pk=self.device.pk).exists())


class DuplicateRegistrationTests(_Console):
    def test_same_number_stops_with_a_link_to_the_existing_household(self):
        before = Household.objects.count()
        r = self._register(contact_number="+639171234510")          # Maria's, written differently
        self.assertEqual(r.status_code, 200)                         # the dialog re-opens
        self.assertEqual(Household.objects.count(), before)
        page = r.content.decode()
        self.assertIn("HH-010", page)
        self.assertIn("Maria Santos", page)
        self.assertIn("Register anyway", page)
        self.assertIn(reverse("admin_household_list") + "?q=HH-010", page)

    def test_same_email_counts_too(self):
        before = Household.objects.count()
        r = self._register(email="MARIA@example.com")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Household.objects.count(), before)

    def test_register_anyway_goes_through(self):
        before = Household.objects.count()
        r = self._register(contact_number="09171234510", confirm_duplicate="on")
        self.assertEqual(r.status_code, 302, r.content[:300])
        self.assertEqual(Household.objects.count(), before + 1)

    def test_an_archived_match_offers_restore_instead(self):
        self.client.post(reverse("admin_household_delete", args=[self.household.pk]))   # archive it
        r = self._register(contact_number="09171234510")
        self.assertEqual(r.status_code, 200)
        page = r.content.decode()
        self.assertIn("Restore it instead", page)
        self.assertIn(reverse("archive_restore_household", args=[self.household.pk]), page)

    def test_a_different_number_is_not_questioned(self):
        before = Household.objects.count()
        r = self._register(contact_number="09171234511")
        self.assertEqual(r.status_code, 302, r.content[:300])
        self.assertEqual(Household.objects.count(), before + 1)

    def test_editing_never_asks(self):
        other = Household.objects.create(
            household_id="HH-011", owner_name="Pedro", address="Purok 2",
            contact_number="09171234511", barangay=self.brgy,
        )
        r = self.client.post(reverse("admin_household_edit", args=[other.pk]), {
            "owner_name": "Pedro", "address": "Purok 2", "contact_number": "09171234510",
            "account_status": Household.AccountStatus.ACTIVE, "language": "en",
        })
        self.assertEqual(r.status_code, 302, r.content[:300])
