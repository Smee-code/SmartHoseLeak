"""
The household owner changing their own number or email from Settings.

Nothing changes until a code sent to the NEW number (or new email) comes
back; then the household record and the login change together, the
household gets a notice, the barangay's facilitator a text and an email, and
the audit log an entry. Refused on the spot: a bad number, a number or email
already on another household, and "nothing changed".
"""

from unittest import mock

from django.test import TestCase
from django.urls import reverse

from core.models import AuditLog, Barangay, Household, LoginCode, Notification, Segment, SmsMessage, User


class OwnerContactChangeTests(TestCase):
    def setUp(self):
        self.brgy = Barangay.objects.create(name="Lower Dakong Patag")
        self.faci = User.objects.create_user(
            username="faci", password="pw", role=User.Role.FACILITATOR, barangay=self.brgy,
            contact_number="09170000001", email="faci@example.com",
        )
        self.segment = Segment.objects.create(name="HH-020 line", barangay=self.brgy, module_index=1)
        self.owner = User.objects.create_user(
            username="lita", password="pw", role=User.Role.OWNER,
            contact_number="09171234520", email="lita@example.com",
        )
        self.household = Household.objects.create(
            household_id="HH-020", owner_name="Lita Ramos", address="Purok 2",
            contact_number="09171234520", email="lita@example.com", user=self.owner,
            segment=self.segment, barangay=self.brgy, language="en",
        )
        Household.objects.create(
            household_id="HH-021", owner_name="Jose Cruz", address="Purok 3",
            contact_number="09171234521", email="jose@example.com", barangay=self.brgy,
        )
        self.client.force_login(self.owner)

    def _start(self, number="09175550123", email="lita@example.com"):
        return self.client.post(reverse("owner_contact_start"), {"contact_number": number, "email": email})

    def _live_code(self):
        return LoginCode.objects.filter(user=self.owner, used_at__isnull=True).latest("created_at")

    def _messages(self, response):
        return [str(m) for m in response.context["messages"]]

    def test_the_settings_page_offers_it(self):
        resp = self.client.get(reverse("owner_settings"))
        self.assertContains(resp, "My contact details")
        self.assertContains(resp, 'name="contact_number"')
        self.assertContains(resp, "09171234520")

    def test_a_new_number_gets_a_code_texted_to_the_new_number_and_nothing_changes_yet(self):
        resp = self._start()
        self.assertRedirects(resp, reverse("owner_contact_confirm"))
        text = SmsMessage.objects.get()
        self.assertTrue(text.to_number.endswith("9175550123"), text.to_number)
        code = self._live_code()
        self.assertIn(code.code, text.body)
        self.assertEqual(code.sent_to, "09175550123")
        self.household.refresh_from_db()
        self.assertEqual(self.household.contact_number, "09171234520")   # untouched until confirmed
        confirm = self.client.get(reverse("owner_contact_confirm"))
        self.assertContains(confirm, "09175550123")
        self.assertContains(confirm, "sent by text")

    def test_the_right_code_changes_both_records_and_tells_everyone(self):
        self._start()
        SmsMessage.objects.all().delete()
        # A wrong guess first.
        resp = self.client.post(reverse("owner_contact_confirm"), {"code": "000000"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "not right")
        self.household.refresh_from_db()
        self.assertEqual(self.household.contact_number, "09171234520")

        with mock.patch("core.views.mail.send_email", return_value=True) as send_email:
            resp = self.client.post(reverse("owner_contact_confirm"), {"code": self._live_code().code})
        self.assertRedirects(resp, reverse("owner_settings"))

        self.household.refresh_from_db()
        self.owner.refresh_from_db()
        self.assertEqual(self.household.contact_number, "09175550123")
        self.assertEqual(self.owner.contact_number, "09175550123")        # the login too
        self.assertEqual(self.owner.email, "lita@example.com")

        # The household's own feed.
        note = Notification.objects.get(recipient=self.household)
        self.assertEqual(note.title, "Contact details changed")
        self.assertIn("09175550123", note.body)
        # The facilitator: a text and an email, old -> new. (The household's
        # notice is texted to the household as any targeted notice is, so the
        # facilitator's is picked out by number.)
        text = SmsMessage.objects.get(to_number__endswith="9170000001")
        self.assertEqual(text.to_number, "09170000001")
        self.assertIn("HH-020", text.body)
        self.assertIn("09171234520", text.body)
        self.assertIn("09175550123", text.body)
        send_email.assert_called_once()
        self.assertEqual(send_email.call_args[0][0], "faci@example.com")
        # The audit trail.
        self.assertTrue(AuditLog.objects.filter(action="household.contact_change", target="HH-020").exists())
        # Nothing is left waiting.
        self.assertIsNone(self.client.session.get("contact_change"))

    def test_an_email_only_change_is_confirmed_by_email(self):
        with mock.patch("core.views.mail.send_email", return_value=True) as send_email:
            resp = self._start(number="09171234520", email="new@example.com")
        self.assertRedirects(resp, reverse("owner_contact_confirm"))
        self.assertEqual(SmsMessage.objects.count(), 0)
        send_email.assert_called_once()
        self.assertEqual(send_email.call_args[0][0], "new@example.com")
        code = self._live_code()
        self.assertEqual(code.channel, LoginCode.Channel.EMAIL)
        self.assertIn(code.code, send_email.call_args[0][2])

        with mock.patch("core.views.mail.send_email", return_value=True):
            self.client.post(reverse("owner_contact_confirm"), {"code": code.code})
        self.household.refresh_from_db()
        self.owner.refresh_from_db()
        self.assertEqual(self.household.email, "new@example.com")
        self.assertEqual(self.owner.email, "new@example.com")

    def test_an_email_that_cannot_be_sent_stops_the_change(self):
        with mock.patch("core.views.mail.send_email", return_value=False):
            resp = self._start(number="09171234520", email="new@example.com", )
        self.assertRedirects(resp, reverse("owner_settings"))
        self.assertIsNone(self.client.session.get("contact_change"))
        self.assertFalse(LoginCode.objects.filter(user=self.owner, used_at__isnull=True).exists())

    def test_refused_on_the_spot(self):
        cases = {
            "a bad number": {"contact_number": "12345", "email": ""},
            "another household's number": {"contact_number": "09171234521", "email": ""},
            "another household's email": {"contact_number": "09171234520", "email": "jose@example.com"},
            "nothing changed": {"contact_number": "09171234520", "email": "lita@example.com"},
        }
        for label, data in cases.items():
            resp = self.client.post(reverse("owner_contact_start"), data, follow=True)
            self.assertEqual(resp.redirect_chain[-1][0], reverse("owner_settings"), label)
            self.assertTrue(self._messages(resp), label)
            self.assertEqual(SmsMessage.objects.count(), 0, label)
        self.household.refresh_from_db()
        self.assertEqual(self.household.contact_number, "09171234520")

    def test_cancel_drops_the_change_and_burns_the_code(self):
        self._start()
        resp = self.client.post(reverse("owner_contact_cancel"))
        self.assertRedirects(resp, reverse("owner_settings"))
        self.assertIsNone(self.client.session.get("contact_change"))
        self.assertFalse(LoginCode.objects.filter(user=self.owner, used_at__isnull=True).exists())
        resp = self.client.get(reverse("owner_contact_confirm"))
        self.assertRedirects(resp, reverse("owner_settings"))

    def test_resend_is_throttled_to_one_a_minute(self):
        self._start()
        resp = self.client.post(reverse("owner_contact_resend"), follow=True)
        self.assertIn("less than a minute ago", " ".join(self._messages(resp)))
        self.assertEqual(SmsMessage.objects.count(), 1)

    def test_a_waiting_sign_in_code_does_not_confirm_a_contact_change(self):
        self._start()
        # Somebody requests a sign-in code meanwhile; it replaces the live one
        # but was sent elsewhere, so it must not confirm the new number.
        LoginCode.issue(self.owner, LoginCode.Channel.SMS, sent_to="09171234520")
        resp = self.client.post(reverse("owner_contact_confirm"), {"code": self._live_code().code})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "No code is waiting")
        self.household.refresh_from_db()
        self.assertEqual(self.household.contact_number, "09171234520")

    def test_another_role_cannot_use_it(self):
        self.client.force_login(self.faci)
        resp = self.client.post(reverse("owner_contact_start"), {"contact_number": "09175550123", "email": ""})
        self.assertEqual(resp.status_code, 403)


class OneNumberEverywhereTests(TestCase):
    """A number changed by staff reaches the login too, so codes follow the alerts."""

    def setUp(self):
        self.brgy = Barangay.objects.create(name="Lower Dakong Patag")
        self.admin = User.objects.create_user(username="adm", password="pw", role=User.Role.ADMIN)
        self.owner = User.objects.create_user(
            username="lita", password="pw", role=User.Role.OWNER,
            contact_number="09171234520", email="lita@example.com",
        )
        self.household = Household.objects.create(
            household_id="HH-020", owner_name="Lita Ramos", address="Purok 2",
            contact_number="09171234520", email="lita@example.com", user=self.owner,
            barangay=self.brgy, language="en",
        )

    def test_a_staff_edit_of_the_household_moves_the_login_too(self):
        from core import otp
        household = Household.objects.select_related("user").get(pk=self.household.pk)
        household.contact_number = "09175550123"
        household.email = "new@example.com"
        household.save()
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.contact_number, "09175550123")
        self.assertEqual(self.owner.email, "new@example.com")
        self.assertEqual(otp.channels_for(self.owner), {"sms": "09175550123", "email": "new@example.com"})
        # The user loaded beside the household cannot write the old number back.
        household.user.save()
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.contact_number, "09175550123")

    def test_an_email_is_cleared_only_when_it_was_removed(self):
        # Removed on the household: the login loses it too.
        self.household.email = ""
        self.household.save()
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.email, "")
        # A household that never had one leaves the login's address alone.
        User.objects.filter(pk=self.owner.pk).update(email="admin.set@example.com")
        self.household.contact_number = "09175550123"
        self.household.save()
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.email, "admin.set@example.com")
        self.assertEqual(self.owner.contact_number, "09175550123")

    def test_a_save_of_other_fields_leaves_the_login_alone(self):
        User.objects.filter(pk=self.owner.pk).update(contact_number="09170000000")
        self.household.notes = "moved the meter"
        self.household.save(update_fields=["notes"])
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.contact_number, "09170000000")

    def test_the_sign_in_code_goes_where_the_household_says_even_if_the_login_drifted(self):
        from core import otp
        User.objects.filter(pk=self.owner.pk).update(contact_number="09170000000", email="old@example.com")
        self.owner.refresh_from_db()
        self.assertEqual(otp.channels_for(self.owner), {"sms": "09171234520", "email": "lita@example.com"})

    def test_the_admins_account_edit_reaches_the_household(self):
        self.client.force_login(self.admin)
        r = self.client.post(reverse("account_edit", args=[self.owner.pk]), {
            "first_name": "Lita", "last_name": "Ramos",
            "contact_number": "09175550999", "email": "lita.new@example.com",
        })
        self.assertEqual(r.status_code, 302)
        self.household.refresh_from_db()
        self.assertEqual(self.household.contact_number, "09175550999")
        self.assertEqual(self.household.email, "lita.new@example.com")
        parts = SmsMessage.queue(self.household, "test alert")
        self.assertEqual(parts[0].to_number, "09175550999")

    def test_the_migration_aligns_rows_that_had_drifted(self):
        import importlib
        from django.apps import apps as live_apps
        User.objects.filter(pk=self.owner.pk).update(contact_number="09170000000", email="old@example.com")
        importlib.import_module("core.migrations.0050_align_owner_contacts").align(live_apps, None)
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.contact_number, "09171234520")
        self.assertEqual(self.owner.email, "lita@example.com")
