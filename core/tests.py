"""
Tests for the parts that fail silently.

Most of this system announces its own breakage: a page that does not render
is obvious, and a device that stops reporting turns a row red. These are the
places where a fault would look exactly like success - a text that arrives
cut in half, a message dropped because nobody had a number, a queue that
quietly empties itself while the modem is unplugged.
"""

import json
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core import push as push_module
from core import sms
from core.api import _leak_sms_text
from core.models import (
    Announcement,
    AuditLog,
    Barangay,
    Complaint,
    Device,
    ErrorLog,
    ReadingRollup,
    SystemSetting,
    DeviceSeverity,
    Household,
    LoginCode,
    LeakEvent,
    Notification,
    PushSubscription,
    Segment,
    SmsMessage,
    User,
    WorkOrder,
    normalise_for_gsm,
)


class LeakSmsTextTests(TestCase):
    """The automatic alert has to fit one message, by design not by luck."""

    def setUp(self):
        self.device = Device.objects.create(device_id="test-01")
        self.segment = Segment.objects.create(device=self.device, module_index=1, name="test line")
        self.household = Household.objects.create(
            household_id="HH-001", owner_name="Maria Santos",
            address="Purok 1", contact_number="+639171234501", segment=self.segment,
        )
        self.event = LeakEvent.objects.create(
            segment=self.segment,
            status=Segment.Status.POSSIBLE_LEAK,
            detected_at=timezone.now(),
        )

    def test_leak_sms_fits_one_message(self):
        """
        Every severity, and a long household id, still fit in 160 characters.

        Two texts cost twice as much, can arrive out of order, and the half
        that gets separated is the instruction telling the owner what to do.
        """
        self.household.household_id = "HH-0000"  # wider than the current format
        for severity in DeviceSeverity.values:
            with self.subTest(severity=severity):
                self.event.device_severity = severity
                text = _leak_sms_text(self.household, self.event)
                self.assertLessEqual(
                    len(text), SmsMessage.SINGLE_LIMIT,
                    f"alert is {len(text)} characters and would split: {text}",
                )

    def test_leak_sms_names_the_household_and_the_action(self):
        """A text arrives with no app around it, so it must stand alone."""
        text = _leak_sms_text(self.household, self.event)
        self.assertIn("HH-001", text)
        self.assertIn("faucets", text)   # check your own taps before calling anyone


class SmsQueueTests(TestCase):
    def setUp(self):
        self.household = Household.objects.create(
            household_id="HH-002", owner_name="Jose Dela Cruz",
            address="Purok 1", contact_number="+639171234502",
        )

    def test_body_over_the_limit_is_split_and_numbered(self):
        parts = SmsMessage.split_body("A" * 400)
        self.assertGreater(len(parts), 1)
        self.assertTrue(parts[0].startswith("(1/"))
        for part in parts:
            self.assertLessEqual(len(part), SmsMessage.SINGLE_LIMIT)

    def test_short_body_is_not_split_or_marked(self):
        parts = SmsMessage.split_body("Water off until 3pm.")
        self.assertEqual(parts, ["Water off until 3pm."])

    def test_household_without_a_number_is_skipped_not_raised(self):
        """
        A household can be registered before anyone collects a number.

        Raising here would abort a broadcast partway through, so the ones
        after it in the list would silently never be written.
        """
        no_number = Household.objects.create(
            household_id="HH-003", owner_name="Ana Reyes", address="Purok 2", contact_number="",
        )
        self.assertEqual(SmsMessage.queue(no_number, "test"), [])
        self.assertEqual(SmsMessage.objects.count(), 0)

    def test_queue_snapshots_the_number(self):
        """The log records where a message went, not where it would go now."""
        SmsMessage.queue(self.household, "test")
        message = SmsMessage.objects.get()
        self.household.contact_number = "+639998887777"
        self.household.save()
        message.refresh_from_db()
        self.assertEqual(message.to_number, "+639171234502")

    def test_typographic_characters_are_folded(self):
        """
        One smart quote switches a message to UCS-2 and cuts the limit to 70.

        The result is a notice that looked fine in the browser arriving cut
        in half, which is why this is folded rather than left to the modem.
        """
        folded = normalise_for_gsm("it’s 9–12 ₱500")
        self.assertEqual(folded, "it's 9-12 PHP 500")
        self.assertTrue(folded.isascii())


class NumberNormalisationTests(TestCase):
    """
    Records get written by hand, so the same number arrives three ways.

    Rejecting two of the three would look like the number was wrong rather
    than punctuated differently - and a rejected number is indistinguishable
    from a household that never gets its alerts.
    """

    def test_accepts_the_three_forms_people_actually_write(self):
        for written in ("09171234501", "+639171234501", "639171234501",
                        "0917 123 4501", "+63 917-123-4501"):
            with self.subTest(written=written):
                self.assertEqual(sms.normalise_number(written), "639171234501")

    def test_rejects_something_that_is_not_a_mobile_number(self):
        self.assertNotEqual(len(sms.normalise_number("12345")), 12)


class SmsWorkerTests(TestCase):
    def setUp(self):
        self.household = Household.objects.create(
            household_id="HH-004", owner_name="Pedro Lim",
            address="Purok 2", contact_number="+639171234504",
        )

    @override_settings(SMS_ENABLED=True, SMS_MAX_ATTEMPTS=3)
    def test_missing_api_key_holds_the_message_without_spending_an_attempt(self):
        """
        No key is a configuration problem, not a message problem.

        Charging an attempt for it would burn the retry budget of every
        queued message before anybody had signed up for the gateway, and
        they would all be marked failed before one could ever be sent.
        """
        SmsMessage.queue(self.household, "test")
        with mock.patch.object(sms, "send_now", side_effect=sms.SmsUnavailable("no API token set")):
            sent, failed, skipped = sms.drain_once()

        self.assertEqual((sent, failed, skipped), (0, 0, 1))
        message = SmsMessage.objects.get()
        self.assertEqual(message.status, SmsMessage.Status.QUEUED)
        self.assertEqual(message.attempts, 0)

    @override_settings(SMS_ENABLED=True, SMS_MAX_ATTEMPTS=2)
    def test_network_refusal_retries_then_gives_up(self):
        """One bad number must not block the queue behind it forever."""
        SmsMessage.queue(self.household, "test")
        with mock.patch.object(sms, "send_now", side_effect=sms.SmsUnavailable("gateway refused it (402): insufficient credit")):
            sms.drain_once()
            message = SmsMessage.objects.get()
            self.assertEqual(message.status, SmsMessage.Status.QUEUED)
            self.assertEqual(message.attempts, 1)

            sms.drain_once()
            message.refresh_from_db()
            self.assertEqual(message.status, SmsMessage.Status.FAILED)

    @override_settings(SMS_ENABLED=True)
    def test_success_records_the_gateway_id_for_the_delivery_check(self):
        """Without the id there is no way to ask what happened to it."""
        SmsMessage.queue(self.household, "test")
        with mock.patch.object(sms, "send_now", return_value=("12345", "Pending")):
            sent, failed, skipped = sms.drain_once()

        self.assertEqual((sent, failed, skipped), (1, 0, 0))
        message = SmsMessage.objects.get()
        self.assertEqual(message.status, SmsMessage.Status.SENT)
        self.assertEqual(message.provider_message_id, "12345")
        self.assertIsNotNone(message.sent_at)

    @override_settings(SMS_ENABLED=False)
    def test_disabled_sends_nothing(self):
        SmsMessage.queue(self.household, "test")
        self.assertEqual(sms.drain_once(), (0, 0, 0))
        self.assertEqual(SmsMessage.objects.get().status, SmsMessage.Status.QUEUED)


class DeliveryTrackingTests(TestCase):
    """
    The gateway accepting a message is not a phone receiving it.

    An alert log that blurs those two cannot answer the only question ever
    put to it: somebody says they never got a text - did one reach them?
    """

    def setUp(self):
        self.household = Household.objects.create(
            household_id="HH-005", owner_name="Rosa Mendoza",
            address="Purok 3", contact_number="+639171234505",
        )
        SmsMessage.queue(self.household, "test")
        self.message = SmsMessage.objects.get()
        self.message.status = SmsMessage.Status.SENT
        self.message.provider_message_id = "12345"
        self.message.sent_at = timezone.now()
        self.message.save()

    def _poll(self, provider_status):
        with mock.patch.object(sms, "_get", return_value={"message_status": provider_status}):
            return sms.poll_delivery()

    @override_settings(SMS_ENABLED=True, SMS_TRACK_DELIVERY=True, IPROGSMS_API_TOKEN="k")
    def test_gateway_says_sent_means_delivered(self):
        self.assertEqual(self._poll("Sent"), 1)
        self.message.refresh_from_db()
        self.assertEqual(self.message.status, SmsMessage.Status.DELIVERED)
        self.assertIsNotNone(self.message.delivered_at)

    @override_settings(SMS_ENABLED=True, SMS_TRACK_DELIVERY=True, IPROGSMS_API_TOKEN="k")
    def test_gateway_says_failed_means_not_delivered(self):
        self.assertEqual(self._poll("Failed"), 1)
        self.message.refresh_from_db()
        self.assertEqual(self.message.status, SmsMessage.Status.UNDELIVERED)
        self.assertIn("Failed", self.message.error)

    @override_settings(SMS_ENABLED=True, SMS_TRACK_DELIVERY=True, IPROGSMS_API_TOKEN="k")
    def test_still_pending_is_left_alone(self):
        """A verdict we do not have yet must not be invented as either one."""
        self.assertEqual(self._poll("Pending"), 0)
        self.message.refresh_from_db()
        self.assertEqual(self.message.status, SmsMessage.Status.SENT)

    @override_settings(SMS_ENABLED=True, SMS_TRACK_DELIVERY=True, IPROGSMS_API_TOKEN="k")
    def test_a_word_the_gateway_invents_is_kept_but_not_acted_on(self):
        """
        Their vocabulary can change. Recording it verbatim makes that visible
        instead of silently mapping something new onto a wrong verdict.
        """
        self.assertEqual(self._poll("Throttled"), 0)
        self.message.refresh_from_db()
        self.assertEqual(self.message.status, SmsMessage.Status.SENT)
        self.assertEqual(self.message.provider_status, "Throttled")

    @override_settings(SMS_ENABLED=True, SMS_TRACK_DELIVERY=True, IPROGSMS_API_TOKEN="k")
    def test_a_failed_lookup_records_no_verdict(self):
        """The message is fine; the lookup failed. Ask again rather than guess."""
        with mock.patch.object(sms, "_get", side_effect=sms.SmsUnavailable("timeout")):
            self.assertEqual(sms.poll_delivery(), 0)
        self.message.refresh_from_db()
        self.assertEqual(self.message.status, SmsMessage.Status.SENT)


@override_settings(SMS_ENABLED=True, IPROGSMS_API_TOKEN="test-key")
class GatewayStateTests(TestCase):
    """
    Whether a text can go out, said in one sentence above the compose form.

    The page paints this amber when it is False and leaves it quiet when it
    is True, so the boolean is not a detail - it is the difference between a
    warning the facilitator acts on and a line they read past.
    """

    def setUp(self):
        sms._gateway_cache["value"] = None
        sms._gateway_cache["at"] = 0.0

    def _describe(self, account):
        with mock.patch.object(sms, "_get", return_value=account):
            return sms.describe_gateway(force=True)

    def test_no_credit_is_not_ready(self):
        """
        Zero credit means every send is refused with "402: insufficient
        credit". It used to report ready, so the one state where nothing
        could possibly be sent was dressed as a healthy gateway.
        """
        ready, note = self._describe({"status": "success", "data": {"load_balance": 0}})
        self.assertFalse(ready)
        self.assertIn("no SMS credit", note)

    def test_low_credit_still_sends(self):
        """Low is a warning, not a stop: it works, and the fix is to top up."""
        ready, note = self._describe({"status": "success", "data": {"load_balance": 12}})
        self.assertTrue(ready)
        self.assertIn("low", note)
        self.assertIn("12", note)

    def test_healthy_credit_reads_as_working(self):
        ready, note = self._describe({"status": "success", "data": {"load_balance": 450}})
        self.assertTrue(ready)
        self.assertIn("450", note)

    def test_the_billing_account_name_is_not_shown(self):
        """
        The gateway account name must not appear in the status sentence - it
        means nothing to a barangay facilitator.
        """
        _, note = self._describe({"status": "success", "data": {"load_balance": 450}})
        self.assertNotIn("Zynergyx", note)

    def test_a_rate_limited_credit_check_is_not_a_failure(self):
        """Being throttled on the balance lookup says nothing about sending."""
        with mock.patch.object(sms, "_get", side_effect=sms.SmsUnavailable("429 Too Many Attempts")):
            ready, note = sms.describe_gateway(force=True)
        self.assertTrue(ready)
        self.assertIn("rate-limited", note)

    def test_an_unreadable_balance_does_not_block_sending(self):
        """A value we cannot parse is not evidence that the account is empty."""
        ready, _ = self._describe({"status": "success", "data": {"load_balance": "unlimited"}})
        self.assertTrue(ready)


class PublicPageRoutingTests(TestCase):
    """
    The public page is for the public.

    Signed in, "/" used to render the marketing page over a working session -
    and the logo in the sidebar pointed at it, so clicking your own brand
    dropped you out of the dashboard onto a page asking you to sign in.
    """

    def setUp(self):
        # A facilitator belongs to a barangay, and sees only that barangay;
        # this fixture predates barangays, so it is given one here.
        brgy = Barangay.objects.create(name="Test Barangay")
        self.facilitator = User.objects.create_user(
            username="fac", password="pw", role=User.Role.FACILITATOR, barangay=brgy,
        )
        self.owner_user = User.objects.create_user(
            username="own", password="pw", role=User.Role.OWNER
        )
        self.segment = Segment.objects.create(name="HH-900 - Test", color="#0b6ea8", barangay=brgy)
        self.household = Household.objects.create(
            household_id="HH-900",
            owner_name="Test Owner",
            address="Purok 1",
            contact_number="09171234500",
            user=self.owner_user,
            segment=self.segment,
            barangay=brgy,
        )

    def test_signed_out_still_sees_the_public_page(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "home.html")

    def test_the_facilitator_is_sent_to_their_dashboard(self):
        self.client.force_login(self.facilitator)
        self.assertRedirects(self.client.get("/"), reverse("dashboard"))

    def test_an_owner_is_sent_to_their_own_portal(self):
        self.client.force_login(self.owner_user)
        self.assertRedirects(self.client.get("/"), reverse("owner_home"))

    def test_an_account_with_no_household_does_not_bounce_forever(self):
        """
        An owner account whose household is gone has nowhere of its own. If
        home() redirected it anyway, home() and post_login() would redirect
        to each other until the browser gave up - so it gets the public page.
        """
        self.household.delete()
        stray = User.objects.create_user(
            username="stray", password="pw", role=User.Role.OWNER
        )
        self.client.force_login(stray)

        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "home.html")

        self.assertRedirects(self.client.get(reverse("post_login")), "/")

    def test_the_brand_in_the_header_keeps_a_signed_in_user_inside(self):
        """The logo is the commonest way anybody reached the public page."""
        self.client.force_login(self.facilitator)
        body = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn(f'href="{reverse("post_login")}"', body)
        self.assertNotIn('class="brand" href="/"', body)


class TemplateSyntaxLeakTests(TestCase):
    """
    Nothing on a page should be template source.

    Django's {# #} comment is single-line only. Written across two lines it is
    not a comment at all - it is text, and it renders. One of those in the
    sidebar header printed four lines of commentary beside the logo and
    pushed it out of view, on every page, for both roles, while the markup
    itself looked perfectly correct.
    """

    # Only the markers that cannot occur by accident. Braces are left out:
    # "}}" closes nested object literals in the inline scripts on half these
    # pages, and a check that cries wolf on every page gets deleted.
    MARKERS = ["{#", "#}", "{%", "%}"]

    def setUp(self):
        # Given a barangay so the facilitator pages render real rows rather
        # than the empty state an unassigned facilitator sees.
        brgy = Barangay.objects.create(name="Test Barangay")
        self.facilitator = User.objects.create_user(
            username="fac2", password="pw", role=User.Role.FACILITATOR, barangay=brgy,
        )
        self.owner_user = User.objects.create_user(
            username="own2", password="pw", role=User.Role.OWNER
        )
        segment = Segment.objects.create(name="HH-901 - Test", color="#0b6ea8", barangay=brgy)
        Household.objects.create(
            household_id="HH-901",
            owner_name="Test Owner",
            address="Purok 1",
            contact_number="09171234501",
            user=self.owner_user,
            segment=segment,
            barangay=brgy,
        )

    def _assert_clean(self, url):
        body = self.client.get(url).content.decode()
        for marker in self.MARKERS:
            self.assertNotIn(
                marker, body, f"unrendered template syntax {marker!r} in {url}"
            )

    def test_the_public_page_renders_no_template_source(self):
        self._assert_clean("/")

    def test_facilitator_pages_render_no_template_source(self):
        self.client.force_login(self.facilitator)
        for name in [
            "dashboard", "pressure_monitor", "event_list", "household_list",
            "segment_list", "complaint_list", "notification_center", "reports",
        ]:
            with self.subTest(page=name):
                self._assert_clean(reverse(name))

    def test_owner_pages_render_no_template_source(self):
        self.client.force_login(self.owner_user)
        for name in ["owner_home", "owner_pressure", "owner_complaints", "owner_notifications"]:
            with self.subTest(page=name):
                self._assert_clean(reverse(name))

    def test_the_brand_logo_is_on_every_signed_in_page(self):
        self.client.force_login(self.facilitator)
        body = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn("img/icon.svg", body)


@override_settings(
    PUSH_ENABLED=True,
    VAPID_PUBLIC_KEY="BTestPublicKey",
    VAPID_PRIVATE_KEY="test-private-key",
    VAPID_SUBJECT="mailto:test@example.com",
)
class PushSubscriptionTests(TestCase):
    """
    A household registering a phone for notifications, and losing one.

    The endpoint is the handset's identity, so the interesting cases are all
    about it: the same phone re-subscribing, a phone that has been wiped, and
    one household reaching for another household's.
    """

    SUB = {
        "endpoint": "https://push.example.com/abc123",
        "keys": {"p256dh": "a-public-key", "auth": "an-auth-secret"},
    }

    def setUp(self):
        self.user = User.objects.create_user(
            username="pushowner", password="pw", role=User.Role.OWNER
        )
        segment = Segment.objects.create(name="HH-910 - Push", color="#0b6ea8")
        self.household = Household.objects.create(
            household_id="HH-910",
            owner_name="Push Tester",
            address="Purok 1",
            contact_number="09171234510",
            user=self.user,
            segment=segment,
        )
        self.client.force_login(self.user)

    def _subscribe(self, payload=None):
        return self.client.post(
            reverse("api_push_subscribe"),
            data=json.dumps(payload or self.SUB),
            content_type="application/json",
        )

    def test_a_phone_can_register(self):
        self.assertEqual(self._subscribe().status_code, 200)
        self.assertEqual(self.household.push_subscriptions.count(), 1)

    def test_the_same_phone_registering_twice_is_still_one_phone(self):
        """
        Re-granting permission, or reinstalling, sends the same endpoint with
        fresh keys. Stored as a second row it would be pushed to twice.
        """
        self._subscribe()
        again = dict(self.SUB)
        again["keys"] = {"p256dh": "rotated-key", "auth": "rotated-auth"}
        self._subscribe(again)

        self.assertEqual(self.household.push_subscriptions.count(), 1)
        self.assertEqual(self.household.push_subscriptions.first().p256dh, "rotated-key")

    def test_a_payload_without_keys_is_refused(self):
        response = self._subscribe({"endpoint": "https://push.example.com/x"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(PushSubscription.objects.count(), 0)

    def test_unsubscribing_removes_it(self):
        self._subscribe()
        response = self.client.post(
            reverse("api_push_unsubscribe"),
            data=json.dumps({"endpoint": self.SUB["endpoint"]}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(PushSubscription.objects.count(), 0)

    def test_one_household_cannot_unsubscribe_another(self):
        """
        Without the household filter, knowing somebody's endpoint would be
        enough to switch their alerts off.
        """
        self._subscribe()

        other_user = User.objects.create_user(
            username="other", password="pw", role=User.Role.OWNER
        )
        Household.objects.create(
            household_id="HH-911",
            owner_name="Someone Else",
            address="Purok 2",
            contact_number="09171234511",
            user=other_user,
        )
        self.client.force_login(other_user)
        self.client.post(
            reverse("api_push_unsubscribe"),
            data=json.dumps({"endpoint": self.SUB["endpoint"]}),
            content_type="application/json",
        )

        self.assertEqual(PushSubscription.objects.count(), 1)

    def test_a_facilitator_has_no_household_to_subscribe(self):
        facilitator = User.objects.create_user(
            username="pushfac", password="pw", role=User.Role.FACILITATOR
        )
        self.client.force_login(facilitator)
        self.assertEqual(self._subscribe().status_code, 403)

    def test_a_dead_subscription_is_deleted_not_retried(self):
        """
        404 and 410 are the push service saying the handset is gone for good.
        Kept, the row would be pushed to on every alert forever.
        """
        self._subscribe()

        # send_to_subscription returns False for exactly this: the push
        # service reporting the subscription gone, rather than failing.
        with mock.patch("core.push.send_to_subscription", return_value=False):
            sent, removed = push_module.send_to_household(self.household, "t", "b")

        self.assertEqual((sent, removed), (0, 1))
        self.assertEqual(PushSubscription.objects.count(), 0)

    def test_a_failing_push_service_does_not_delete_anything(self):
        """A bad afternoon at the push service is not evidence a phone is gone."""
        self._subscribe()
        with mock.patch(
            "core.push.send_to_subscription",
            side_effect=push_module.PushUnavailable("500: server error"),
        ):
            sent, removed = push_module.send_to_household(self.household, "t", "b")

        self.assertEqual((sent, removed), (0, 0))
        self.assertEqual(PushSubscription.objects.count(), 1)
        self.assertEqual(PushSubscription.objects.first().failures, 1)


class PushConfigTests(TestCase):
    """Switched off is a state the portal has to render, not an error."""

    @override_settings(PUSH_ENABLED=False)
    def test_disabled_says_so(self):
        ready, note = push_module.describe_push()
        self.assertFalse(ready)
        self.assertIn("switched off", note)

    @override_settings(PUSH_ENABLED=True, VAPID_PUBLIC_KEY="", VAPID_PRIVATE_KEY="")
    def test_no_keys_names_the_command_that_makes_them(self):
        ready, note = push_module.describe_push()
        self.assertFalse(ready)
        self.assertIn("make_push_keys", note)

    @override_settings(PUSH_ENABLED=True, VAPID_PUBLIC_KEY="", VAPID_PRIVATE_KEY="")
    def test_nothing_is_sent_when_unconfigured(self):
        user = User.objects.create_user(username="u", password="pw", role=User.Role.OWNER)
        household = Household.objects.create(
            household_id="HH-912", owner_name="X", address="P", user=user
        )
        PushSubscription.objects.create(
            household=household, endpoint="https://e/1", p256dh="k", auth="a"
        )
        self.assertEqual(push_module.send_to_household(household, "t", "b"), (0, 0))

    @override_settings(PUSH_ENABLED=True, VAPID_PUBLIC_KEY="pub", VAPID_PRIVATE_KEY="priv")
    def test_the_settings_page_renders_for_an_owner(self):
        user = User.objects.create_user(username="u2", password="pw", role=User.Role.OWNER)
        Household.objects.create(
            household_id="HH-913", owner_name="Y", address="P", user=user
        )
        self.client.force_login(user)
        response = self.client.get(reverse("owner_settings"))
        self.assertEqual(response.status_code, 200)
        # The install steps must be in the HTML, not drawn by JavaScript -
        # a phone that cannot run the script still has to be installable.
        self.assertContains(response, "Add to Home Screen")
        self.assertContains(response, "Install app")


@override_settings(
    PUSH_ENABLED=True,
    VAPID_PUBLIC_KEY="BTestPublicKey",
    VAPID_PRIVATE_KEY="ReU-fZuxyinHY80UauMIg0mYdpXF6xeEbG95_PQwvIY",
    VAPID_SUBJECT="mailto:test@example.com",
)
class PushTransportTests(TestCase):
    """
    What happens when the push service cannot be reached at all.

    pywebpush raises WebPushException only for things the push service said.
    Everything before that - DNS, the connection, a timeout, no internet -
    comes out of requests untouched, so this is the failure a server in a
    barangay with an intermittent line will actually hit.
    """

    def setUp(self):
        user = User.objects.create_user(username="t", password="pw", role=User.Role.OWNER)
        self.household = Household.objects.create(
            household_id="HH-914", owner_name="T", address="P", user=user
        )
        self.subscription = PushSubscription.objects.create(
            household=self.household,
            endpoint="https://push.invalid.example/x",
            p256dh="k",
            auth="a",
        )

    def test_a_connection_error_becomes_PushUnavailable(self):
        import requests

        with mock.patch(
            "pywebpush.webpush", side_effect=requests.ConnectionError("no DNS")
        ):
            with self.assertRaises(push_module.PushUnavailable):
                push_module.send_to_subscription(self.subscription, {"title": "t"})

    def test_an_unreachable_service_does_not_break_an_alert(self):
        """
        send_to_household promises its callers it never raises. A leak alert
        must go out by SMS even when nothing can be pushed.
        """
        import requests

        with mock.patch(
            "pywebpush.webpush", side_effect=requests.ConnectionError("no DNS")
        ):
            sent, removed = push_module.send_to_household(self.household, "t", "b")

        self.assertEqual((sent, removed), (0, 0))
        # The handset is not gone - the internet is. Nothing is deleted.
        self.assertEqual(PushSubscription.objects.count(), 1)


@override_settings(DEBUG=False, ALLOWED_HOSTS=["testserver"])
class ErrorPageTests(TestCase):
    """
    The pages nobody plans for.

    Until now a wrong address produced Django's development traceback page,
    which besides looking like a fault also prints every URL pattern in the
    project - the claim endpoint, the device key endpoints, all of it - to
    whoever typed the address.
    """

    def setUp(self):
        self.facilitator = User.objects.create_user(
            username="errfac", password="pw", role=User.Role.FACILITATOR
        )
        self.owner_user = User.objects.create_user(
            username="errown", password="pw", role=User.Role.OWNER
        )
        Household.objects.create(
            household_id="HH-920",
            owner_name="Err Owner",
            address="Purok 1",
            user=self.owner_user,
        )

    def test_a_wrong_address_renders_the_custom_page(self):
        response = self.client.get("/classes/37/")
        self.assertEqual(response.status_code, 404)
        self.assertTemplateUsed(response, "404.html")
        self.assertContains(response, "doesn", status_code=404)

    def test_it_shows_the_address_that_was_asked_for(self):
        """The one genuinely useful thing when the cause is a typo."""
        response = self.client.get("/classes/37/")
        self.assertContains(response, "/classes/37/", status_code=404)

    def test_it_does_not_leak_the_url_map(self):
        """
        The development page lists every route in the project. This one must
        name only the handful a visitor is being offered.
        """
        body = self.client.get("/classes/37/").content.decode()
        for secret in ["api/claim", "new-key", "claim-code", "api/ingest"]:
            self.assertNotIn(secret, body)

    def test_a_signed_out_visitor_is_pointed_at_the_front_page(self):
        body = self.client.get("/classes/37/").content.decode()
        self.assertIn(reverse("login"), body)
        self.assertNotIn(reverse("dashboard"), body)

    def test_a_facilitator_is_pointed_at_their_dashboard(self):
        self.client.force_login(self.facilitator)
        body = self.client.get("/classes/37/").content.decode()
        self.assertIn(reverse("dashboard"), body)
        self.assertNotIn(reverse("owner_home"), body)

    def test_an_owner_is_pointed_at_their_own_portal(self):
        """
        A household must never be offered a facilitator link. It would 403,
        which is a second dead end reached from the page meant to end the
        first one.
        """
        self.client.force_login(self.owner_user)
        body = self.client.get("/classes/37/").content.decode()
        self.assertIn(reverse("owner_home"), body)
        self.assertNotIn(reverse("dashboard"), body)

    def test_the_500_page_renders_with_no_context_at_all(self):
        """
        Django renders handler500 with an empty context - no request, no user,
        no context processors. A 500 page that assumes any of them is a page
        that fails exactly when it is needed.
        """
        from django.template.loader import render_to_string

        html = render_to_string("500.html")
        self.assertIn("Error 500", html)
        self.assertIn("still being watched", html)

    def test_the_500_page_needs_no_stylesheet(self):
        """
        It carries its own styles. If the error being reported is the database
        or the static files, a page that fetches app.css to look right is a
        page that arrives unreadable.
        """
        from django.template.loader import render_to_string

        html = render_to_string("500.html")
        # No request for anything. A stylesheet link is the one that matters;
        # script and image requests would fail the same way.
        self.assertNotIn("<link", html)
        self.assertNotIn("<script", html)
        self.assertNotIn("/static/", html)
        self.assertIn("<style>", html)

    def test_the_500_page_explains_nothing_about_the_system(self):
        """
        The reasoning in that file is a Django comment, not an HTML one. An
        error page is the last place to be describing the system's innards to
        whoever just triggered the error.
        """
        from django.template.loader import render_to_string

        self.assertNotIn("<!--", render_to_string("500.html"))

    def test_the_403_page_renders(self):
        from django.template.loader import render_to_string

        html = render_to_string("403.html", {"user": self.owner_user})
        self.assertIn("Error 403", html)

    def test_the_400_page_renders(self):
        from django.template.loader import render_to_string

        html = render_to_string("400.html")
        self.assertIn("Error 400", html)


@override_settings(DEBUG=False, ALLOWED_HOSTS=["testserver"])
class ServerErrorHandlerTests(TestCase):
    """
    That Django's own handler500 reaches templates/500.html.

    Rendering the template in isolation proves it is valid; it does not prove
    it is the page a visitor gets. Those are different claims, and only the
    second one matters at two in the morning.
    """

    def test_a_crashing_view_returns_the_custom_page(self):
        from django.test import Client

        # raise_request_exception belongs to the Client, not to get(): passed
        # to get() it is treated as an extra header and the exception still
        # propagates, so the test fails without ever reaching handler500.
        client = Client(raise_request_exception=False)

        # Not core.views.home either: the URLconf binds the function object at
        # import time, so replacing the module attribute afterwards changes
        # nothing and the view runs normally. Patch what the view calls.
        with mock.patch("core.views.render", side_effect=RuntimeError("boom")):
            response = client.get("/")

        self.assertEqual(response.status_code, 500)
        self.assertTemplateUsed(response, "500.html")
        self.assertIn(b"still being watched", response.content)
        # And nothing of the traceback.
        self.assertNotIn(b"RuntimeError", response.content)
        self.assertNotIn(b"boom", response.content)


class AdminConsoleTests(TestCase):
    """
    The administrator portal: a third role, above the facilitators.

    These cover the parts that would fail silently or dangerously - a
    facilitator slipping into the console, a suspension that does not actually
    block sign-in, or the delete that must never let an admin remove
    themselves or another admin and lock everyone out.
    """

    def setUp(self):
        self.brgy = Barangay.objects.create(name="Lower Dakong Patag")
        self.other = Barangay.objects.create(name="Upper Patag")

        self.admin = User.objects.create_user(
            username="admin", password="pw", role=User.Role.ADMIN,
            is_staff=True, is_superuser=True,
        )
        self.facilitator = User.objects.create_user(
            username="faci", password="pw", role=User.Role.FACILITATOR, barangay=self.brgy,
        )
        self.owner = User.objects.create_user(
            username="owner", password="pw", role=User.Role.OWNER,
        )
        self.household = Household.objects.create(
            household_id="HH-001", owner_name="Maria Santos",
            address="Purok 1", contact_number="+639171234501",
            barangay=self.brgy, user=self.owner,
        )

    # -- access control -----------------------------------------------------

    def test_admin_lands_on_console(self):
        self.client.login(username="admin", password="pw")
        response = self.client.get(reverse("post_login"))
        self.assertRedirects(response, reverse("console_dashboard"), fetch_redirect_response=False)

    def test_facilitator_cannot_reach_console(self):
        """A facilitator typing the console URL is forbidden, not redirected."""
        self.client.login(username="faci", password="pw")
        self.assertEqual(self.client.get(reverse("console_dashboard")).status_code, 403)
        self.assertEqual(self.client.get(reverse("account_list")).status_code, 403)

    def test_owner_cannot_reach_console(self):
        self.client.login(username="owner", password="pw")
        self.assertEqual(self.client.get(reverse("console_dashboard")).status_code, 403)

    def test_admin_and_facilitator_are_separate(self):
        """
        Separate roles: an admin cannot open the facilitator dashboard, and a
        facilitator cannot open the admin console. Each sees only its side.
        """
        self.client.login(username="admin", password="pw")
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 403)
        self.assertEqual(self.client.get(reverse("console_dashboard")).status_code, 200)

        self.client.login(username="faci", password="pw")
        self.assertEqual(self.client.get(reverse("console_dashboard")).status_code, 403)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

    # -- barangays ----------------------------------------------------------

    def test_create_barangay(self):
        self.client.login(username="admin", password="pw")
        response = self.client.post(reverse("barangay_create"), {
            "name": "New Barangay", "municipality": "Sison",
            "province": "Surigao del Norte", "is_active": "on",
        })
        self.assertRedirects(response, reverse("barangay_list"))
        self.assertTrue(Barangay.objects.filter(name="New Barangay").exists())

    def test_toggle_barangay_active(self):
        self.client.login(username="admin", password="pw")
        self.client.post(reverse("barangay_toggle", args=[self.brgy.pk]))
        self.brgy.refresh_from_db()
        self.assertFalse(self.brgy.is_active)

    # -- facilitators -------------------------------------------------------

    def test_create_facilitator_assigns_barangay_and_temp_password(self):
        self.client.login(username="admin", password="pw")
        response = self.client.post(reverse("facilitator_create"), {
            "first_name": "Juan", "last_name": "Dela Cruz",
            "contact_number": "+639170000000", "email": "juan@example.com",
        })
        self.assertRedirects(response, reverse("facilitator_list"))
        created = User.objects.exclude(pk=self.facilitator.pk).get(role=User.Role.FACILITATOR)
        self.assertEqual(created.barangay, Barangay.default())
        self.assertEqual(created.email, "juan@example.com")
        self.assertTrue(created.must_change_password)
        self.assertTrue(created.is_active)
        self.assertEqual(created.role, User.Role.FACILITATOR)

    # -- household registration --------------------------------------------

    def test_register_household_records_it_pending_without_a_login(self):
        self.client.login(username="admin", password="pw")
        response = self.client.post(reverse("admin_household_create"), {
            "owner_name": "Pedro Lim", "address": "Purok 2",
            "contact_number": "+639170000002",
            "account_status": "active",
            # The board is registered here now, and the admin can label it.
            "device_choice": "new", "device_label": "Pedro's line board",
            # Neither of these is the administrator's to set any more.
            "create_account": "on", "latitude": "9.7", "longitude": "125.5",
        })
        self.assertRedirects(response, reverse("admin_household_list"))
        household = Household.objects.get(owner_name="Pedro Lim")
        self.assertEqual(household.barangay, Barangay.default())
        self.assertIsNotNone(household.segment)          # the hose line is created
        self.assertIsNotNone(household.segment.device)   # and the board that was asked for
        self.assertEqual(household.segment.device.label, "Pedro's line board")  # with the label
        self.assertEqual(household.device_setup["key"], "awaiting_activation")
        self.assertIsNone(household.user)                # the login is the facilitator's job
        self.assertIsNone(household.latitude)            # so is the location
        self.assertTrue(household.is_pending)
        html = self.client.get(reverse("admin_household_list")).content.decode()
        self.assertIn("Pending activation", html)
        self.assertNotIn('name="create_account"', html)
        self.assertNotIn('name="latitude"', html)
        self.assertIn('name="device_choice"', html)      # the board can now be chosen and labelled
        self.assertIn("Purok or street", html)
        self.assertIn('value="Surigao del Norte" readonly', html)
        self.assertIn('value="Sison" readonly', html)
        self.assertIn('value="Lower Dakong Patag" readonly', html)

    def test_admin_registration_without_a_board_leaves_the_line_bare(self):
        self.client.login(username="admin", password="pw")
        self.client.post(reverse("admin_household_create"), {
            "owner_name": "Ana Reyes", "address": "Purok 3",
            "contact_number": "+639170000003",
            "account_status": "active", "device_choice": "none",
        })
        household = Household.objects.get(owner_name="Ana Reyes")
        self.assertIsNotNone(household.segment)           # the hose line is always made
        self.assertIsNone(household.segment.device)       # but no board when none is chosen

    def test_admin_new_board_without_a_label_gets_a_recognisable_one(self):
        self.client.login(username="admin", password="pw")
        self.client.post(reverse("admin_household_create"), {
            "owner_name": "Rosa Mendoza", "address": "Purok 4",
            "contact_number": "+639170000004",
            "account_status": "active", "device_choice": "new", "device_label": "",
        })
        household = Household.objects.get(owner_name="Rosa Mendoza")
        self.assertIsNotNone(household.segment.device)
        self.assertIn(household.household_id, household.segment.device.label)
        self.assertIn("Rosa Mendoza", household.segment.device.label)

    # -- suspend & delete ---------------------------------------------------

    def test_suspend_blocks_sign_in(self):
        self.client.login(username="admin", password="pw")
        self.client.post(reverse("account_suspend", args=[self.facilitator.pk]))
        self.facilitator.refresh_from_db()
        self.assertFalse(self.facilitator.is_active)
        # is_active=False is what actually stops authentication.
        self.assertFalse(self.client.login(username="faci", password="pw"))

    def test_suspend_is_reversible(self):
        self.client.login(username="admin", password="pw")
        self.client.post(reverse("account_suspend", args=[self.facilitator.pk]))
        self.client.post(reverse("account_suspend", args=[self.facilitator.pk]))
        self.facilitator.refresh_from_db()
        self.assertTrue(self.facilitator.is_active)

    def test_delete_owner_keeps_household(self):
        self.client.login(username="admin", password="pw")
        self.client.post(reverse("account_delete", args=[self.owner.pk]))
        self.assertFalse(User.objects.filter(pk=self.owner.pk).exists())
        # The household record survives; only its login is gone.
        self.household.refresh_from_db()
        self.assertIsNone(self.household.user)

    def test_cannot_delete_self(self):
        self.client.login(username="admin", password="pw")
        response = self.client.post(reverse("account_delete", args=[self.admin.pk]))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(User.objects.filter(pk=self.admin.pk).exists())

    def test_cannot_delete_another_admin(self):
        second = User.objects.create_user(
            username="admin2", password="pw", role=User.Role.ADMIN, is_superuser=True,
        )
        self.client.login(username="admin", password="pw")
        response = self.client.post(reverse("account_delete", args=[second.pk]))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(User.objects.filter(pk=second.pk).exists())

    def test_cannot_suspend_admin(self):
        second = User.objects.create_user(
            username="admin2", password="pw", role=User.Role.ADMIN, is_superuser=True,
        )
        self.client.login(username="admin", password="pw")
        response = self.client.post(reverse("account_suspend", args=[second.pk]))
        self.assertEqual(response.status_code, 403)


class WorkOrderTests(TestCase):
    """
    Field operations: the work-order workflow that unifies visits, repairs
    and maintenance, and the reported-anomaly triage that feeds it.

    The parts that would fail silently: an approval that does not text anyone,
    a facilitator able to close a job that is not theirs, or a report filed
    into no barangay at all.
    """

    def setUp(self):
        self.brgy = Barangay.objects.create(name="Lower Dakong Patag")
        self.admin = User.objects.create_user(
            username="admin", password="pw", role=User.Role.ADMIN,
            is_staff=True, is_superuser=True,
        )
        self.facilitator = User.objects.create_user(
            username="faci", password="pw", role=User.Role.FACILITATOR,
            barangay=self.brgy, contact_number="+639170000000",
        )
        self.other_faci = User.objects.create_user(
            username="faci2", password="pw", role=User.Role.FACILITATOR, barangay=self.brgy,
        )
        self.segment = Segment.objects.create(module_index=1, name="HH-001 line", barangay=self.brgy)
        self.household = Household.objects.create(
            household_id="HH-001", owner_name="Maria Santos", address="Purok 1",
            contact_number="+639171234501", barangay=self.brgy, segment=self.segment,
        )

    # -- facilitator reporting ---------------------------------------------


    # -- approval & SMS -----------------------------------------------------

    def test_approve_assigns_schedules_and_texts_both(self):
        order = WorkOrder.objects.create(
            kind=WorkOrder.Kind.VISIT, title="Inspect HH-001", barangay=self.brgy,
            household=self.household, status=WorkOrder.Status.REQUESTED,
        )
        self.client.login(username="admin", password="pw")
        response = self.client.post(reverse("work_order_approve", args=[order.pk]), {
            "assigned_to": self.facilitator.pk, "scheduled_for": "2026-11-05",
        })
        self.assertRedirects(response, reverse("work_order_detail", args=[order.pk]))
        order.refresh_from_db()
        self.assertEqual(order.status, WorkOrder.Status.SCHEDULED)
        self.assertEqual(order.assigned_to, self.facilitator)
        self.assertEqual(str(order.scheduled_for), "2026-11-05")
        self.assertEqual(order.reviewed_by, self.admin)
        # Both the household and the assigned facilitator are texted.
        self.assertTrue(SmsMessage.objects.filter(household=self.household).exists())
        self.assertTrue(SmsMessage.objects.filter(to_number=self.facilitator.contact_number).exists())

    def test_approve_requires_an_assignee(self):
        order = WorkOrder.objects.create(
            kind=WorkOrder.Kind.VISIT, title="No assignee", barangay=self.brgy,
            status=WorkOrder.Status.REQUESTED,
        )
        self.client.login(username="admin", password="pw")
        self.client.post(reverse("work_order_approve", args=[order.pk]), {"assigned_to": ""})
        order.refresh_from_db()
        self.assertEqual(order.status, WorkOrder.Status.REQUESTED)

    def test_reject_closes_with_a_note(self):
        order = WorkOrder.objects.create(
            kind=WorkOrder.Kind.REPAIR, title="Dupe", barangay=self.brgy,
            status=WorkOrder.Status.REQUESTED,
        )
        self.client.login(username="admin", password="pw")
        self.client.post(reverse("work_order_reject", args=[order.pk]), {"review_note": "Duplicate"})
        order.refresh_from_db()
        self.assertEqual(order.status, WorkOrder.Status.REJECTED)
        self.assertEqual(order.review_note, "Duplicate")
        self.assertFalse(order.is_open)

    def test_admin_create_with_assignee_schedules_and_texts(self):
        self.client.login(username="admin", password="pw")
        response = self.client.post(reverse("work_order_create"), {
            "title": "Planned visit", "details": "", "household": self.household.pk,
            "assigned_to": self.facilitator.pk, "scheduled_for": "2026-11-10",
        })
        order = WorkOrder.objects.get(title="Planned visit")
        self.assertRedirects(response, reverse("work_order_detail", args=[order.pk]))
        self.assertEqual(order.status, WorkOrder.Status.SCHEDULED)
        self.assertTrue(SmsMessage.objects.filter(household=self.household).exists())

    def test_admin_create_without_assignee_is_requested(self):
        self.client.login(username="admin", password="pw")
        self.client.post(reverse("work_order_create"), {
            "title": "Later job", "details": "", "household": self.household.pk,
            "assigned_to": "", "scheduled_for": "",
        })
        order = WorkOrder.objects.get(title="Later job")
        self.assertEqual(order.status, WorkOrder.Status.REQUESTED)

    # -- facilitator working their own assignments -------------------------

    def test_assignee_can_mark_done(self):
        order = WorkOrder.objects.create(
            kind=WorkOrder.Kind.VISIT, title="Visit", barangay=self.brgy,
            assigned_to=self.facilitator, status=WorkOrder.Status.SCHEDULED,
        )
        self.client.login(username="faci", password="pw")
        self.client.post(reverse("assignment_update", args=[order.pk]), {
            "status": "done", "resolution_note": "All good",
        })
        order.refresh_from_db()
        self.assertEqual(order.status, WorkOrder.Status.DONE)
        self.assertIsNotNone(order.completed_at)
        self.assertEqual(order.resolution_note, "All good")

    def test_facilitator_cannot_touch_someone_elses_assignment(self):
        order = WorkOrder.objects.create(
            kind=WorkOrder.Kind.VISIT, title="Not mine", barangay=self.brgy,
            assigned_to=self.other_faci, status=WorkOrder.Status.SCHEDULED,
        )
        self.client.login(username="faci", password="pw")
        response = self.client.post(reverse("assignment_update", args=[order.pk]), {"status": "done"})
        self.assertEqual(response.status_code, 403)
        order.refresh_from_db()
        self.assertEqual(order.status, WorkOrder.Status.SCHEDULED)

    def test_my_assignments_lists_only_mine(self):
        WorkOrder.objects.create(kind=WorkOrder.Kind.VISIT, title="Mine", barangay=self.brgy,
                                 assigned_to=self.facilitator, status=WorkOrder.Status.SCHEDULED)
        WorkOrder.objects.create(kind=WorkOrder.Kind.VISIT, title="Theirs", barangay=self.brgy,
                                 assigned_to=self.other_faci, status=WorkOrder.Status.SCHEDULED)
        self.client.login(username="faci", password="pw")
        response = self.client.get(reverse("my_assignments"))
        self.assertContains(response, "Mine")
        self.assertNotContains(response, "Theirs")

    # -- access control -----------------------------------------------------

    def test_facilitator_cannot_reach_admin_field_views(self):
        self.client.login(username="faci", password="pw")
        self.assertEqual(self.client.get(reverse("work_order_list")).status_code, 403)
        self.assertEqual(self.client.get(reverse("work_order_create")).status_code, 403)

    # -- repairs view -------------------------------------------------------


class WorkOrderRenderTests(TestCase):
    """Every field-operations page renders (templates compile, URLs resolve)."""

    def setUp(self):
        self.brgy = Barangay.objects.create(name="Lower Dakong Patag")
        self.admin = User.objects.create_user(
            username="admin", password="pw", role=User.Role.ADMIN,
            is_staff=True, is_superuser=True,
        )
        self.faci = User.objects.create_user(
            username="faci", password="pw", role=User.Role.FACILITATOR, barangay=self.brgy,
        )
        self.order = WorkOrder.objects.create(
            kind=WorkOrder.Kind.VISIT, title="Sample", barangay=self.brgy,
            status=WorkOrder.Status.REQUESTED,
        )

    def test_admin_pages_render(self):
        self.client.login(username="admin", password="pw")
        for url in [
            reverse("work_order_list"),
            reverse("work_order_create"),
            reverse("work_order_detail", args=[self.order.pk]),
            reverse("work_order_edit", args=[self.order.pk]),
        ]:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_facilitator_pages_render(self):
        self.client.login(username="faci", password="pw")
        self.assertEqual(self.client.get(reverse("my_assignments")).status_code, 200)


class AnnouncementTests(TestCase):
    """
    Admin announcements: reaching households across one barangay or all of
    them, and reusing the owner's existing Notices feed for delivery.
    """

    def setUp(self):
        self.a = Barangay.objects.create(name="Lower Dakong Patag")
        self.b = Barangay.objects.create(name="Upper Patag")
        self.admin = User.objects.create_user(
            username="admin", password="pw", role=User.Role.ADMIN,
            is_staff=True, is_superuser=True,
        )
        self.faci = User.objects.create_user(
            username="faci", password="pw", role=User.Role.FACILITATOR, barangay=self.a,
        )
        self.owner = User.objects.create_user(username="owner", password="pw", role=User.Role.OWNER)
        self.hh_a = Household.objects.create(
            household_id="HH-001", owner_name="Maria", address="P1",
            contact_number="+639171234501", barangay=self.a, user=self.owner,
        )
        self.hh_b = Household.objects.create(
            household_id="HH-002", owner_name="Jose", address="P2",
            contact_number="+639171234502", barangay=self.b,
        )

    def test_announce_to_all_reaches_every_active_household(self):
        self.client.login(username="admin", password="pw")
        response = self.client.post(reverse("announcement_create"), {
            "audience": "all", "title": "System notice", "body": "Water off 1-3pm.",
            "in_app": "on",
        })
        self.assertRedirects(response, reverse("announcement_list"))
        ann = Announcement.objects.get(title="System notice")
        self.assertEqual(ann.recipients_count, 2)
        # A Notification row per household, so owners see it in their feed.
        self.assertEqual(Notification.objects.filter(title="System notice").count(), 2)
        self.assertTrue(self.hh_a.notifications.filter(title="System notice").exists())


    def test_announce_selected_households(self):
        self.client.login(username="admin", password="pw")
        self.client.post(reverse("announcement_create"), {
            "audience": "selected", "households": [self.hh_a.pk],
            "title": "Just you", "body": "Hi Maria.", "in_app": "on",
        })
        self.assertTrue(self.hh_a.notifications.filter(title="Just you").exists())
        self.assertFalse(self.hh_b.notifications.filter(title="Just you").exists())

    def test_sms_channel_queues_texts(self):
        self.client.login(username="admin", password="pw")
        self.client.post(reverse("announcement_create"), {
            "audience": "all", "title": "By text", "body": "Texted notice.",
            "in_app": "on", "by_sms": "on",
        })
        ann = Announcement.objects.get(title="By text")
        self.assertGreaterEqual(ann.sms_count, 2)
        self.assertTrue(SmsMessage.objects.filter(household=self.hh_a).exists())

    def test_no_channel_is_rejected(self):
        self.client.login(username="admin", password="pw")
        self.client.post(reverse("announcement_create"), {
            "audience": "all", "title": "Nowhere", "body": "No channel.",
        })
        self.assertFalse(Announcement.objects.filter(title="Nowhere").exists())
        self.assertFalse(Notification.objects.filter(title="Nowhere").exists())


    def test_facilitator_cannot_announce(self):
        self.client.login(username="faci", password="pw")
        self.assertEqual(self.client.get(reverse("announcement_list")).status_code, 403)
        self.assertEqual(self.client.get(reverse("announcement_create")).status_code, 403)


class AnalyticsTests(TestCase):
    """The analytics page renders for an admin and is closed to others."""

    def setUp(self):
        self.brgy = Barangay.objects.create(name="Lower Dakong Patag")
        self.admin = User.objects.create_user(
            username="admin", password="pw", role=User.Role.ADMIN,
            is_staff=True, is_superuser=True,
        )
        self.faci = User.objects.create_user(
            username="faci", password="pw", role=User.Role.FACILITATOR, barangay=self.brgy,
        )
        Household.objects.create(
            household_id="HH-001", owner_name="Maria", address="P1",
            contact_number="+639171234501", barangay=self.brgy,
        )

    def test_analytics_renders_for_admin(self):
        self.client.login(username="admin", password="pw")
        response = self.client.get(reverse("analytics"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Work orders by status")

    def test_analytics_closed_to_facilitator(self):
        self.client.login(username="faci", password="pw")
        self.assertEqual(self.client.get(reverse("analytics")).status_code, 403)


class DataOpsTests(TestCase):
    """Import/export, backup/restore, the audit log, maintenance and errors."""

    def setUp(self):
        self.a = Barangay.objects.create(name="Lower Dakong Patag")
        self.b = Barangay.objects.create(name="Upper Patag")
        self.admin = User.objects.create_user(
            username="admin", password="pw", role=User.Role.ADMIN,
            is_staff=True, is_superuser=True,
        )
        self.faci = User.objects.create_user(
            username="faci", password="pw", role=User.Role.FACILITATOR, barangay=self.a,
        )
        self.owner = User.objects.create_user(username="owner", password="pw", role=User.Role.OWNER)
        self.hh = Household.objects.create(
            household_id="HH-001", owner_name="Maria", address="P1",
            contact_number="+639171234501", barangay=self.a, user=self.owner,
        )

    # -- access & render ----------------------------------------------------

    def test_pages_render_for_admin_and_are_closed_to_facilitator(self):
        self.client.login(username="admin", password="pw")
        for name in ["data_tools", "audit_log", "system_status"]:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        self.client.login(username="faci", password="pw")
        for name in ["data_tools", "audit_log", "system_status"]:
            self.assertEqual(self.client.get(reverse(name)).status_code, 403, name)

    # -- export -------------------------------------------------------------

    def test_export_households_csv(self):
        self.client.login(username="admin", password="pw")
        response = self.client.get(reverse("export_console", args=["households"]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn(b"HH-001", response.content)
        self.assertIn(b"Maria", response.content)

    def test_unknown_export_404s(self):
        self.client.login(username="admin", password="pw")
        self.assertEqual(self.client.get(reverse("export_console", args=["nonsense"])).status_code, 404)

    # -- import -------------------------------------------------------------

    def test_household_import_creates_and_matches_barangay(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        csv = (
            "owner_name,address,contact_number,barangay\n"
            "Jose Cruz,Purok 2,+639170000001,Upper Patag\n"
            "Ana Reyes,Purok 3,+639170000002,\n"
            ",no owner,+639170000003,\n"
        )
        upload = SimpleUploadedFile("hh.csv", csv.encode("utf-8"), content_type="text/csv")
        self.client.login(username="admin", password="pw")
        response = self.client.post(reverse("household_import"), {
            "default_barangay": self.a.pk, "csv_file": upload,
        })
        self.assertRedirects(response, reverse("data_tools"))
        jose = Household.objects.get(owner_name="Jose Cruz")
        ana = Household.objects.get(owner_name="Ana Reyes")
        self.assertEqual(jose.barangay, self.b)   # named barangay honoured
        self.assertEqual(ana.barangay, self.a)     # blank -> default
        # The row with no owner_name is skipped.
        self.assertFalse(Household.objects.filter(address="no owner").exists())

    # -- backup & restore ---------------------------------------------------

    def test_backup_then_restore_roundtrip(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.client.login(username="admin", password="pw")
        backup = self.client.get(reverse("backup_download"))
        self.assertEqual(backup.status_code, 200)
        self.assertIn("attachment", backup["Content-Disposition"])

        # Delete a household, then restore from the backup and see it return.
        Household.objects.filter(pk=self.hh.pk).delete()
        self.assertFalse(Household.objects.filter(pk=self.hh.pk).exists())

        upload = SimpleUploadedFile("backup.json", backup.content, content_type="application/json")
        response = self.client.post(reverse("restore_upload"), {
            "backup_file": upload, "confirm": "RESTORE",
        })
        self.assertRedirects(response, reverse("data_tools"))
        self.assertTrue(Household.objects.filter(pk=self.hh.pk).exists())

    def test_restore_requires_typed_confirmation(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.client.login(username="admin", password="pw")
        upload = SimpleUploadedFile("backup.json", b"[]", content_type="application/json")
        response = self.client.post(reverse("restore_upload"), {
            "backup_file": upload, "confirm": "nope",
        })
        # Re-renders the page rather than restoring; no redirect.
        self.assertEqual(response.status_code, 200)

    # -- audit log ----------------------------------------------------------

    def test_admin_action_writes_audit_entry(self):
        self.client.login(username="admin", password="pw")
        self.client.post(reverse("account_suspend", args=[self.faci.pk]))
        self.assertTrue(AuditLog.objects.filter(action="account.suspend").exists())
        entry = AuditLog.objects.get(action="account.suspend")
        self.assertEqual(entry.actor, self.admin)
        self.assertIn("faci", entry.summary)

    # -- maintenance mode ---------------------------------------------------

    def test_maintenance_toggle_and_gate(self):
        self.client.login(username="admin", password="pw")
        self.client.post(reverse("maintenance_toggle"))
        self.assertTrue(SystemSetting.load().maintenance_mode)
        self.assertTrue(AuditLog.objects.filter(action="system.maintenance").exists())

        # Admin still gets through the console; the device ingest is untouched.
        self.assertEqual(self.client.get(reverse("console_dashboard")).status_code, 200)

        # A household is held out with a 503 notice.
        self.client.login(username="owner", password="pw")
        held = self.client.get(reverse("owner_home"))
        self.assertEqual(held.status_code, 503)
        self.assertTemplateUsed(held, "maintenance.html")

    def test_maintenance_off_lets_households_in(self):
        # With maintenance off (default), an owner reaches their portal.
        self.client.login(username="owner", password="pw")
        self.assertEqual(self.client.get(reverse("owner_home")).status_code, 200)

    # -- error logging ------------------------------------------------------

    def test_error_middleware_records_a_row(self):
        from django.contrib.auth.models import AnonymousUser
        from django.test import RequestFactory
        from core.middleware import ErrorLogMiddleware

        mw = ErrorLogMiddleware(lambda r: None)
        request = RequestFactory().get("/boom/")
        request.user = AnonymousUser()
        result = mw.process_exception(request, ValueError("kaboom"))
        self.assertIsNone(result)  # lets Django's own 500 handling proceed
        self.assertTrue(ErrorLog.objects.filter(exception_type="ValueError").exists())


@override_settings(OTP_ENABLED=False)
class LoginDialogTests(TestCase):
    """
    Sign in as a dialog over the landing page, with the password eye.

    The parts that would fail quietly: a wrong password dumping the visitor
    on a bare page, the typed username or the "next" destination being lost
    on the way back, or the standalone /login/ fallback coming up empty.
    """

    def setUp(self):
        Barangay.objects.create(name="Lower Dakong Patag")
        self.owner = User.objects.create_user(username="owner", password="pw", role=User.Role.OWNER)
        Household.objects.create(
            household_id="HH-001", owner_name="Maria", address="P1",
            contact_number="+639171234501", user=self.owner,
        )

    def test_landing_carries_the_hidden_dialog_and_openers(self):
        response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('id="login-modal"', html)
        self.assertIn('data-modal-open="login-modal"', html)
        self.assertIn("data-password-toggle", html)
        # Hidden until a Sign in button opens it.
        self.assertRegex(html, r'id="login-modal"[^>]*\bhidden\b')
        self.assertNotIn("data-modal-autoopen", html)

    def test_wrong_password_reopens_the_dialog_on_the_landing(self):
        response = self.client.post(reverse("login"), {"username": "owner", "password": "wrong"})
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "home.html")
        html = response.content.decode()
        self.assertIn("data-modal-autoopen", html)
        self.assertIn("incorrect", html)
        self.assertIn('value="owner"', html)  # the typed username is kept

    def test_wrong_password_keeps_next(self):
        response = self.client.post(
            reverse("login"), {"username": "owner", "password": "wrong", "next": "/portal/"}
        )
        self.assertIn('name="next" value="/portal/"', response.content.decode())

    def test_right_password_still_routes_by_role(self):
        response = self.client.post(reverse("login"), {"username": "owner", "password": "pw"})
        self.assertRedirects(response, reverse("post_login"), fetch_redirect_response=False)

    def test_standalone_login_page_is_the_dialog_shown(self):
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('id="login-modal"', html)
        self.assertNotRegex(html, r'id="login-modal"[^>]*\bhidden\b')
        self.assertIn("data-password-toggle", html)

    def test_dialog_carries_the_design_details(self):
        html = self.client.get(reverse("home")).content.decode()
        self.assertIn('class="login-brand"', html)       # the mark and the name
        self.assertIn('href="#i-user"', html)             # leading field icons
        self.assertIn('href="#i-lock"', html)
        self.assertIn("data-caps-hint", html)             # Caps Lock hint
        self.assertIn("data-login-submit", html)          # submit feedback hook

    def test_failed_sign_in_marks_fields_and_announces_the_error(self):
        """Per the form-error guidance: an announced alert, and each field
        connected to it - not a red border or a top-level message alone."""
        response = self.client.post(reverse("login"), {"username": "owner", "password": "wrong"})
        html = response.content.decode()
        self.assertIn('role="alert"', html)
        self.assertIn('id="login-error"', html)
        self.assertEqual(html.count('aria-invalid="true"'), 2)
        self.assertIn('aria-describedby="login-error"', html)

    def test_untouched_dialog_has_no_error_state(self):
        html = self.client.get(reverse("home")).content.decode()
        self.assertNotIn('aria-invalid="true"', html)
        self.assertNotIn('id="login-error"', html)


class FacilitatorScopeTests(TestCase):
    """
    A facilitator sees exactly one barangay.

    The failures that would be silent: a household from the next barangay
    showing up in a list, a direct URL opening another barangay's record, the
    live poll feeding a dashboard lines it should not have, a leak alert
    texting every facilitator in the system, or a facilitator with no
    barangay seeing everyone's data as a fallback.
    """

    def setUp(self):
        self.a = Barangay.objects.create(name="Lower Dakong Patag")
        self.b = Barangay.objects.create(name="Upper Patag")
        self.faci_a = User.objects.create_user(
            username="faci_a", password="pw", role=User.Role.FACILITATOR,
            barangay=self.a, contact_number="+639170000001",
        )
        self.faci_b = User.objects.create_user(
            username="faci_b", password="pw", role=User.Role.FACILITATOR,
            barangay=self.b, contact_number="+639170000002",
        )
        self.faci_none = User.objects.create_user(
            username="faci_none", password="pw", role=User.Role.FACILITATOR,
        )
        self.seg_a = Segment.objects.create(module_index=1, name="Line A", barangay=self.a)
        self.seg_b = Segment.objects.create(module_index=1, name="Line B", barangay=self.b)
        self.hh_a = Household.objects.create(
            household_id="HH-A01", owner_name="Maria A", address="Purok 1",
            contact_number="+639171234501", barangay=self.a, segment=self.seg_a,
        )
        self.hh_b = Household.objects.create(
            household_id="HH-B01", owner_name="Jose B", address="Purok 2",
            contact_number="+639171234502", barangay=self.b, segment=self.seg_b,
        )
        category = Complaint.Category.choices[0][0]
        self.complaint_a = Complaint.objects.create(
            household=self.hh_a, category=category, subject="Leak at A", body="Water under the sink.",
        )
        self.complaint_b = Complaint.objects.create(
            household=self.hh_b, category=category, subject="Leak at B", body="Pipe burst.",
        )
        self.event_a = LeakEvent.objects.create(
            segment=self.seg_a, status=Segment.Status.POSSIBLE_LEAK,
            detected_at=timezone.now(), severity=LeakEvent.Severity.CRITICAL,
        )
        self.event_b = LeakEvent.objects.create(
            segment=self.seg_b, status=Segment.Status.POSSIBLE_LEAK,
            detected_at=timezone.now(), severity=LeakEvent.Severity.CRITICAL,
        )

    def test_dashboard_and_lists_show_only_own_barangay(self):
        self.client.login(username="faci_a", password="pw")
        for name in ["dashboard", "household_list", "segment_list", "pressure_monitor",
                     "event_list", "complaint_list", "reports", "notification_center"]:
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 200, name)
            html = response.content.decode()
            self.assertNotIn("HH-B01", html, name)
            self.assertNotIn("Jose B", html, name)
            self.assertNotIn("Line B", html, name)
        self.assertContains(self.client.get(reverse("household_list")), "HH-A01")
        self.assertContains(self.client.get(reverse("dashboard")), "Lower Dakong Patag")

    def test_direct_urls_into_another_barangay_are_not_found(self):
        self.client.login(username="faci_a", password="pw")
        self.assertEqual(self.client.get(reverse("household_detail", args=[self.hh_b.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("segment_edit", args=[self.seg_b.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("complaint_thread", args=[self.complaint_b.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("event_acknowledge", args=[self.event_b.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("segment_delete", args=[self.seg_b.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("household_delete", args=[self.hh_b.pk])).status_code, 404)
        # Their own records still open.
        self.assertEqual(self.client.get(reverse("household_detail", args=[self.hh_a.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("complaint_thread", args=[self.complaint_a.pk])).status_code, 200)

    def test_live_and_history_feeds_are_scoped(self):
        self.client.login(username="faci_a", password="pw")
        live = self.client.get(reverse("api_live")).json()
        names = {s["name"] for s in live["segments"]}
        self.assertIn("Line A", names)
        self.assertNotIn("Line B", names)
        self.assertTrue(all(e["segment"] != "Line B" for e in live["open_events"]))
        ReadingRollup.objects.create(segment=self.seg_a, minute=timezone.now(), sample_count=1)
        ReadingRollup.objects.create(segment=self.seg_b, minute=timezone.now(), sample_count=1)
        history = self.client.get(reverse("api_history")).json()
        self.assertEqual({s["segment"] for s in history["series"]}, {"Line A"})

    def test_unassigned_facilitator_sees_nothing_and_is_told_why(self):
        self.client.login(username="faci_none", password="pw")
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No barangay assigned")
        self.assertNotContains(response, "HH-A01")
        self.assertNotContains(response, "HH-B01")
        self.assertEqual(self.client.get(reverse("api_live")).json()["segments"], [])
        self.assertNotContains(self.client.get(reverse("household_list")), "HH-A01")
        self.assertEqual(self.client.get(reverse("segment_list")).status_code, 200)

    @override_settings(SMS_FACILITATOR_ON_LEAK=True)
    def test_leak_alert_texts_only_that_barangays_facilitator(self):
        from core.api import _notify_facilitators
        _notify_facilitators(self.seg_a, self.event_a)
        self.assertTrue(SmsMessage.objects.filter(to_number=self.faci_a.contact_number).exists())
        self.assertFalse(SmsMessage.objects.filter(to_number=self.faci_b.contact_number).exists())

    def test_each_barangay_has_its_own_water_source(self):
        from core.models import WaterSource
        self.client.login(username="faci_a", password="pw")
        self.client.get(reverse("segment_list"))
        self.client.login(username="faci_b", password="pw")
        self.client.get(reverse("segment_list"))
        self.assertEqual(WaterSource.objects.filter(barangay=self.a).count(), 1)
        self.assertEqual(WaterSource.objects.filter(barangay=self.b).count(), 1)
        self.assertNotEqual(WaterSource.for_barangay(self.a).pk, WaterSource.for_barangay(self.b).pk)

    def test_pending_complaint_badge_is_scoped(self):
        self.client.login(username="faci_a", password="pw")
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.context["pending_complaint_count"], 1)

    def test_notice_audience_is_scoped(self):
        """'All active households' means all of THIS barangay's."""
        self.client.login(username="faci_a", password="pw")
        self.client.post(reverse("notification_center"), {
            "to_all": "on", "title": "Water off", "body": "1 to 3pm.", "in_app": "on",
        })
        self.assertTrue(self.hh_a.notifications.filter(title="Water off").exists())
        self.assertFalse(self.hh_b.notifications.filter(title="Water off").exists())


class FacilitatorFeatureTests(TestCase):
    """Escalation, texting credentials, the repairs view, and the schedule."""

    def setUp(self):
        self.a = Barangay.objects.create(name="Lower Dakong Patag")
        self.b = Barangay.objects.create(name="Upper Patag")
        self.admin = User.objects.create_user(
            username="admin", password="pw", role=User.Role.ADMIN, is_staff=True, is_superuser=True,
        )
        self.faci = User.objects.create_user(
            username="faci", password="pw", role=User.Role.FACILITATOR,
            barangay=self.a, contact_number="+639170000001",
        )
        self.seg = Segment.objects.create(module_index=1, name="Line A", barangay=self.a)
        self.quiet = Segment.objects.create(module_index=1, name="Quiet line", barangay=self.a)
        self.owner = User.objects.create_user(username="owner", password="pw", role=User.Role.OWNER)
        self.hh = Household.objects.create(
            household_id="HH-A01", owner_name="Maria A", address="Purok 1",
            contact_number="+639171234501", barangay=self.a, segment=self.seg, user=self.owner,
        )
        self.complaint = Complaint.objects.create(
            household=self.hh, category=Complaint.Category.choices[0][0],
            subject="Leak at A", body="Water under the sink.",
        )
        self.client.login(username="faci", password="pw")


    def test_activating_a_household_creates_and_texts_the_login(self):
        from django.core import mail as django_mail
        household = Household.objects.create(
            household_id="HH-090", owner_name="Pedro New", address="Purok 3",
            contact_number="+639170000009", email="pedro@example.com", barangay=self.a,
        )
        self.assertTrue(household.is_pending)
        html = self.client.get(reverse("household_list")).content.decode()
        self.assertIn("Pending activation", html)
        self.assertIn('id="household-activate-modal"', html)
        self.assertNotIn('id="household-modal"', html)     # no "add household" any more
        # No board yet: the row offers to register one, not to activate.
        self.assertNotIn("data-activate-household", html)
        self.assertIn("Register board", html)
        response = self.client.post(reverse("household_register_board", args=[household.pk]))
        self.assertRedirects(response, reverse("household_list") + f"?activate={household.pk}")
        household.refresh_from_db()
        self.assertIsNotNone(household.segment.device)
        html = self.client.get(reverse("household_list") + f"?activate={household.pk}").content.decode()
        self.assertIn("data-modal-autoopen", html)          # the dialog reopens on return
        self.assertIn("data-activate-household", html)
        self.assertIn(household.segment.device.device_id, html)

        # Outside the municipality: refused, dialog reopened on the same household.
        response = self.client.post(reverse("household_activate", args=[household.pk]),
                                    {"latitude": "14.6", "longitude": "121.0"})
        self.assertTemplateUsed(response, "facilitator/households.html")
        self.assertIn("data-modal-autoopen", response.content.decode())
        household.refresh_from_db()
        self.assertIsNone(household.user)

        response = self.client.post(reverse("household_activate", args=[household.pk]),
                                    {"latitude": "9.7167", "longitude": "125.5333"})
        self.assertRedirects(response, reverse("household_list"))
        household.refresh_from_db()
        self.assertFalse(household.is_pending)
        self.assertIsNotNone(household.user)
        self.assertEqual(household.user.role, User.Role.OWNER)
        self.assertTrue(household.user.must_change_password)
        self.assertEqual(household.latitude, 9.7167)
        self.assertEqual(household.activated_by.username, "faci")
        self.assertIsNotNone(household.activated_at)
        texts = [t.body for t in SmsMessage.objects.filter(household=household)]
        code = household.segment.device.claim_code.code
        # Two messages: the login (which may run to two parts) and the setup code (one part).
        self.assertTrue(any(household.user.username in t and "temporary password" in t for t in texts))
        setup_parts = [t for t in texts if code in t]
        self.assertEqual(len(setup_parts), 1)
        self.assertIn("LeakDetect-Setup", setup_parts[0])
        self.assertNotIn("(1/", setup_parts[0])
        self.assertEqual([m.to for m in django_mail.outbox], [["pedro@example.com"]] * 2)
        bodies = " ".join(m.body for m in django_mail.outbox)
        self.assertIn(household.user.username, bodies)
        self.assertIn(code, bodies)
        # Once activated, the row offers no Activate button and the POST is a no-op.
        self.assertNotIn("data-activate-household", self.client.get(reverse("household_list")).content.decode())
        self.client.post(reverse("household_activate", args=[household.pk]),
                         {"latitude": "9.7", "longitude": "125.5"})
        self.assertEqual(User.objects.filter(role=User.Role.OWNER, household=household).count(), 1)

    def test_send_credentials_resets_and_texts(self):
        before = self.owner.password
        response = self.client.post(reverse("household_send_credentials", args=[self.hh.pk]))
        self.assertRedirects(response, reverse("household_detail", args=[self.hh.pk]))
        self.owner.refresh_from_db()
        self.assertNotEqual(self.owner.password, before)
        self.assertTrue(self.owner.must_change_password)
        self.assertTrue(SmsMessage.objects.filter(household=self.hh, body__icontains="username").exists())


    def test_schedule_groups_by_day_and_dashboard_summarises(self):
        today = timezone.localdate()
        WorkOrder.objects.create(
            kind=WorkOrder.Kind.VISIT, title="Visit Maria today", barangay=self.a,
            household=self.hh, assigned_to=self.faci, status=WorkOrder.Status.SCHEDULED,
            scheduled_for=today,
        )
        WorkOrder.objects.create(
            kind=WorkOrder.Kind.REPAIR, title="Undated repair", barangay=self.a,
            segment=self.seg, assigned_to=self.faci, status=WorkOrder.Status.SCHEDULED,
        )
        schedule = self.client.get(reverse("my_assignments"))
        self.assertEqual(schedule.status_code, 200)
        self.assertContains(schedule, "Today")
        self.assertContains(schedule, "Visit Maria today")
        self.assertContains(schedule, "Not yet scheduled")
        self.assertContains(schedule, "Undated repair")
        dashboard = self.client.get(reverse("dashboard"))
        self.assertContains(dashboard, "Your schedule")
        self.assertContains(dashboard, "Visit Maria today")


class VisitRouteTests(TestCase):
    """
    Road routes to households: fetched once, stored, drawn, and navigable.

    Every test that touches the routing service patches it out - no test may
    reach the internet - and the ones about "not configured" and "no
    coordinates" assert that nothing was even attempted.
    """

    GEOJSON = {
        "features": [{
            "geometry": {"coordinates": [[125.529, 9.662], [125.530, 9.663], [125.5333, 9.7167]]},
            "properties": {"summary": {"distance": 1234.5, "duration": 240.0}},
        }]
    }

    def setUp(self):
        from core.models import WaterSource
        self.brgy = Barangay.objects.create(name="Lower Dakong Patag")
        self.source = WaterSource.objects.create(barangay=self.brgy, latitude=9.662, longitude=125.529)
        self.faci = User.objects.create_user(
            username="faci", password="pw", role=User.Role.FACILITATOR, barangay=self.brgy,
        )
        self.hh = Household.objects.create(
            household_id="HH-001", owner_name="Maria", address="P1",
            contact_number="+639171234501", barangay=self.brgy,
            latitude=9.7167, longitude=125.5333,
        )

    def _fake_service(self, payload=None):
        import io
        body = json.dumps(payload or self.GEOJSON).encode()
        return mock.patch("core.routing.urllib.request.urlopen", return_value=io.BytesIO(body))

    def test_parse_turns_geojson_into_latlng_points(self):
        from core import routing
        route = routing.parse_route(self.GEOJSON)
        self.assertEqual(route["points"][0], [9.662, 125.529])   # [lng, lat] -> [lat, lng]
        self.assertEqual(len(route["points"]), 3)
        self.assertEqual(route["distance_m"], 1234.5)
        self.assertEqual(route["duration_s"], 240.0)

    @override_settings(ORS_API_KEY="test-key", ROUTING_ENABLED=True)
    def test_route_is_fetched_and_stored(self):
        from core import routing
        with self._fake_service():
            self.assertTrue(routing.update_household_route(self.hh))
        self.hh.refresh_from_db()
        self.assertEqual(len(self.hh.visit_route), 3)
        self.assertEqual(self.hh.visit_route_distance_m, 1234.5)
        self.assertEqual(self.hh.visit_route_error, "")
        self.assertIsNotNone(self.hh.visit_route_fetched_at)
        self.assertEqual(self.hh.visit_route_label, "1.2 km, about 4 min")

    @override_settings(ORS_API_KEY="", ROUTING_ENABLED=True)
    def test_without_a_key_nothing_is_attempted_and_it_says_so(self):
        from core import routing
        with mock.patch("core.routing.urllib.request.urlopen") as service:
            self.assertFalse(routing.update_household_route(self.hh))
            service.assert_not_called()
        self.hh.refresh_from_db()
        self.assertIsNone(self.hh.visit_route)
        self.assertIn("not configured", self.hh.visit_route_error)

    @override_settings(ORS_API_KEY="test-key")
    def test_no_coordinates_clears_the_route_without_calling_out(self):
        from core import routing
        self.hh.visit_route = [[1, 2], [3, 4]]
        self.hh.save()
        self.hh.latitude = None
        self.hh.longitude = None
        with mock.patch("core.routing.urllib.request.urlopen") as service:
            routing.update_household_route(self.hh)
            service.assert_not_called()
        self.hh.refresh_from_db()
        self.assertIsNone(self.hh.visit_route)
        self.assertEqual(self.hh.visit_route_error, "")

    @override_settings(ORS_API_KEY="test-key")
    def test_service_failure_keeps_the_old_route_and_records_why(self):
        import urllib.error
        from core import routing
        self.hh.visit_route = [[9.66, 125.52], [9.71, 125.53]]
        self.hh.save()
        with mock.patch("core.routing.urllib.request.urlopen", side_effect=urllib.error.URLError("no DNS")):
            self.assertFalse(routing.update_household_route(self.hh))
        self.hh.refresh_from_db()
        self.assertEqual(len(self.hh.visit_route), 2)      # the old route survives
        self.assertIn("cannot reach", self.hh.visit_route_error)

    def test_navigate_url_goes_from_source_to_house(self):
        url = self.hh.navigate_url
        self.assertIn("google.com/maps/dir/", url)
        self.assertIn("origin=9.662,125.529", url)
        self.assertIn("destination=9.7167,125.5333", url)
        self.assertIn("travelmode=driving", url)
        self.hh.latitude = None
        self.assertIsNone(self.hh.navigate_url)

    @override_settings(ORS_API_KEY="test-key")
    def test_saving_coordinates_from_the_form_fetches_the_route(self):
        self.client.login(username="faci", password="pw")
        with self._fake_service():
            response = self.client.post(reverse("household_edit", args=[self.hh.pk]), {
                "owner_name": "Maria", "address": "P1", "contact_number": "+639171234501",
                "account_status": "active", "latitude": "9.7167", "longitude": "125.5333", "notes": "",
            })
        self.assertEqual(response.status_code, 302)
        self.hh.refresh_from_db()
        self.assertIsNotNone(self.hh.visit_route)

    @override_settings(ORS_API_KEY="test-key")
    def test_moving_the_source_refetches_routes(self):
        from core import routing
        with self._fake_service():
            routing.update_household_route(self.hh)
        self.client.login(username="faci", password="pw")
        with mock.patch("core.routing.urllib.request.urlopen") as service:
            service.return_value = __import__("io").BytesIO(json.dumps(self.GEOJSON).encode())
            self.client.post(reverse("segment_list"), {
                "form_kind": "source", "name": "Spring box",
                "latitude": "9.664", "longitude": "125.531", "notes": "",
            })
            self.assertTrue(service.called)

    @override_settings(ORS_API_KEY="test-key")
    def test_dashboard_carries_routes_and_navigate_links(self):
        from core import routing
        with self._fake_service():
            routing.update_household_route(self.hh)
        self.client.login(username="faci", password="pw")
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, 'id="route-toggle"')
        payload = response.context["map_households"]
        self.assertEqual(len(payload[0]["route"]), 3)
        self.assertIn("google.com/maps/dir/", payload[0]["navigate_url"])

    @override_settings(ORS_API_KEY="test-key")
    def test_refresh_route_view_fetches_and_reports(self):
        self.client.login(username="faci", password="pw")
        with self._fake_service():
            response = self.client.post(reverse("household_refresh_route", args=[self.hh.pk]))
        self.assertRedirects(response, reverse("household_detail", args=[self.hh.pk]))
        self.hh.refresh_from_db()
        self.assertEqual(len(self.hh.visit_route), 3)

    def test_household_page_shows_getting_there(self):
        self.client.login(username="faci", password="pw")
        response = self.client.get(reverse("household_detail", args=[self.hh.pk]))
        self.assertContains(response, "Getting there")
        self.assertContains(response, "Navigate (Google Maps)")
        # No key in tests: the page says so rather than offering a button
        # that would do nothing.
        self.assertContains(response, "No OpenRouteService key")

    @override_settings(ORS_API_KEY="test-key")
    def test_management_command_fetches_missing_routes(self):
        import io
        from django.core.management import call_command
        out = io.StringIO()
        with self._fake_service():
            call_command("fetch_routes", stdout=out)
        self.hh.refresh_from_db()
        self.assertIsNotNone(self.hh.visit_route)
        self.assertIn("HH-001", out.getvalue())
        self.assertIn("Routes fetched: 1", out.getvalue())


@override_settings(OTP_ENABLED=True, DEBUG=False)
class SignInCodeTests(TestCase):
    """
    The second step of signing in: a 10-minute, 6-digit code by text or email.

    The quiet failures: a password alone signing someone in, a wrong code
    being accepted, an expired code still working, a code surviving its
    fifth wrong guess, a "remembered" device still being asked, or an
    account with nowhere to send a code being locked out.
    """

    def setUp(self):
        Barangay.objects.create(name="Lower Dakong Patag")
        self.owner = User.objects.create_user(
            username="owner", password="pw", role=User.Role.OWNER,
            contact_number="+639171234501", email="maria@example.com",
        )
        Household.objects.create(
            household_id="HH-001", owner_name="Maria", address="P1",
            contact_number="+639171234501", user=self.owner,
        )

    def _password_step(self, channel="sms", remember=False):
        data = {"username": "owner", "password": "pw", "otp_channel": channel}
        if remember:
            data["remember_device"] = "1"
        return self.client.post(reverse("login"), data)

    def _live_code(self):
        return self.owner.login_codes.filter(used_at__isnull=True).latest("created_at")

    def test_password_alone_does_not_sign_in(self):
        response = self._password_step()
        self.assertRedirects(response, reverse("login_code"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)
        # A code was issued and texted.
        code = self._live_code()
        self.assertEqual(len(code.code), 6)
        self.assertEqual(code.channel, "sms")
        self.assertTrue(SmsMessage.objects.filter(to_number="+639171234501", body__contains=code.code).exists())

    def test_email_channel_sends_the_code_by_email(self):
        from django.core import mail as django_mail
        self._password_step(channel="email")
        code = self._live_code()
        self.assertEqual(code.channel, "email")
        self.assertEqual(len(django_mail.outbox), 1)
        self.assertIn(code.code, django_mail.outbox[0].body)
        self.assertEqual(django_mail.outbox[0].to, ["maria@example.com"])

    def test_right_code_signs_in_and_routes_by_role(self):
        self._password_step()
        response = self.client.post(reverse("login_code"), {"code": self._live_code().code})
        self.assertRedirects(response, reverse("post_login"), fetch_redirect_response=False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.owner.pk)
        self.assertRedirects(self.client.get(reverse("post_login")), reverse("owner_home"), fetch_redirect_response=False)

    def test_wrong_code_is_refused_and_counted(self):
        self._password_step()
        response = self.client.post(reverse("login_code"), {"code": "000000"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "not right")
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(self._live_code().attempts, 1)

    def test_fifth_wrong_guess_burns_the_code(self):
        self._password_step()
        real = self._live_code().code
        for _ in range(5):
            self.client.post(reverse("login_code"), {"code": "000000"})
        response = self.client.post(reverse("login_code"), {"code": real})
        self.assertContains(response, "Too many wrong guesses")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_expired_code_is_refused(self):
        self._password_step()
        code = self._live_code()
        code.expires_at = timezone.now() - timezone.timedelta(minutes=1)
        code.save(update_fields=["expires_at"])
        response = self.client.post(reverse("login_code"), {"code": code.code})
        self.assertContains(response, "expired")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_code_is_valid_for_ten_minutes(self):
        self._password_step()
        code = self._live_code()
        self.assertAlmostEqual(
            (code.expires_at - code.created_at).total_seconds(), 600, delta=5,
        )

    def test_remembered_device_skips_the_code_next_time(self):
        self._password_step(remember=True)
        self.client.post(reverse("login_code"), {"code": self._live_code().code})
        self.assertIn("shl_trusted_device", self.client.cookies)
        # Sign out through the app, as a person would. (Client.logout() would
        # empty the whole cookie jar, including the device cookie - which is
        # not what signing out does: the device stays trusted, the session ends.)
        self.client.post(reverse("logout"))
        self.assertNotIn("_auth_user_id", self.client.session)
        # Same browser, same cookie jar: password alone is enough now.
        response = self.client.post(reverse("login"), {"username": "owner", "password": "pw", "otp_channel": "sms"})
        self.assertRedirects(response, reverse("post_login"), fetch_redirect_response=False)
        self.assertIn("_auth_user_id", self.client.session)

    def test_resend_is_throttled_to_once_a_minute(self):
        self._password_step()
        first = self._live_code().code
        self.client.post(reverse("login_code_resend"), {"otp_channel": "sms"})
        self.assertEqual(self._live_code().code, first)   # too soon: nothing new

    def test_account_with_nowhere_to_send_signs_in_with_a_warning(self):
        bare = User.objects.create_user(username="bare", password="pw", role=User.Role.ADMIN, is_superuser=True)
        response = self.client.post(reverse("login"), {"username": "bare", "password": "pw", "otp_channel": "sms"})
        self.assertRedirects(response, reverse("post_login"), fetch_redirect_response=False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), bare.pk)

    def test_code_page_without_a_pending_sign_in_goes_back_to_login(self):
        self.assertRedirects(self.client.get(reverse("login_code")), reverse("login"), fetch_redirect_response=False)

    def test_sign_in_dialog_offers_the_channel_choice(self):
        html = self.client.get(reverse("home")).content.decode()
        self.assertIn('name="otp_channel"', html)
        self.assertIn('name="remember_device"', html)


class EmailChannelTests(TestCase):
    """Notices and announcements can go out by email, to whoever has one."""

    def setUp(self):
        from django.core import mail as django_mail
        django_mail.outbox.clear()
        self.brgy = Barangay.objects.create(name="Lower Dakong Patag")
        self.admin = User.objects.create_user(username="admin", password="pw", role=User.Role.ADMIN, is_staff=True, is_superuser=True)
        self.faci = User.objects.create_user(username="faci", password="pw", role=User.Role.FACILITATOR, barangay=self.brgy)
        self.with_email = Household.objects.create(
            household_id="HH-001", owner_name="Maria", address="P1",
            contact_number="+639171234501", barangay=self.brgy, email="maria@example.com",
        )
        self.without = Household.objects.create(
            household_id="HH-002", owner_name="Jose", address="P2",
            contact_number="+639171234502", barangay=self.brgy,
        )

    def test_email_status_names_the_missing_app_password(self):
        from django.test import override_settings
        from core import mail as shl_mail
        with override_settings(EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend",
                               EMAIL_HOST_USER="smarthoseleak@gmail.com", EMAIL_HOST_PASSWORD=""):
            ready, note = shl_mail.describe_email()
        self.assertFalse(ready)
        self.assertIn("smarthoseleak@gmail.com", note)
        self.assertIn("EMAIL_HOST_PASSWORD", note)
        with override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                               DEFAULT_FROM_EMAIL="Smart Hose Leak <smarthoseleak@gmail.com>"):
            ready, note = shl_mail.describe_email()
        self.assertTrue(ready)
        self.assertIn("smarthoseleak@gmail.com", note)

    def test_send_email_command_sends_one_message(self):
        from io import StringIO
        from django.core import mail as django_mail
        from django.core.management import call_command
        out = StringIO()
        call_command("send_email", "someone@example.com", stdout=out)
        self.assertEqual([m.to for m in django_mail.outbox], [["someone@example.com"]])
        self.assertIn("test", django_mail.outbox[0].subject.lower())

    def test_services_card_on_system_status(self):
        self.client.login(username="admin", password="pw")
        html = self.client.get(reverse("system_status")).content.decode()
        self.assertIn("Services", html)
        self.assertIn("Email", html)
        self.assertIn("Text messages (SMS)", html)
        self.assertIn("send_email", html)

    def test_sign_in_code_that_cannot_be_emailed_is_not_claimed_as_sent(self):
        from unittest import mock
        from core import otp as shl_otp
        self.faci.email = "faci@example.com"
        self.faci.save()
        with mock.patch("core.otp.mail.send_email", return_value=False):
            with self.assertRaises(shl_otp.DeliveryError):
                shl_otp.send_code(self.faci, LoginCode.Channel.EMAIL)
        self.assertFalse(LoginCode.objects.filter(user=self.faci).exists())   # burned, not left live
        # At the sign-in dialog the person is told, and is not signed in.
        with self.settings(OTP_ENABLED=True), mock.patch("core.otp.mail.send_email", return_value=False):
            response = self.client.post(reverse("login"), {
                "username": "faci", "password": "pw", "otp_channel": LoginCode.Channel.EMAIL,
            })
        self.assertEqual(response.status_code, 200)
        self.assertIn("could not be emailed", response.content.decode())
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_announcement_by_email_reaches_only_households_with_an_address(self):
        from django.core import mail as django_mail
        self.client.login(username="admin", password="pw")
        self.client.post(reverse("announcement_create"), {
            "audience": "all", "title": "Water off", "body": "1 to 3pm.", "in_app": "on", "by_email": "on",
        })
        self.assertEqual([m.to for m in django_mail.outbox], [["maria@example.com"]])
        ann = Announcement.objects.get(title="Water off")
        self.assertTrue(ann.by_email)
        self.assertEqual(ann.email_count, 1)
        self.assertEqual(ann.recipients_count, 2)   # in-app still reached both

    def test_facilitator_notice_by_email(self):
        from django.core import mail as django_mail
        self.client.login(username="faci", password="pw")
        self.client.post(reverse("notification_center"), {
            "to_all": "on", "title": "Repair tomorrow", "body": "8am.", "in_app": "on", "by_email": "on",
        })
        self.assertEqual(len(django_mail.outbox), 1)
        self.assertEqual(django_mail.outbox[0].to, ["maria@example.com"])
        self.assertIn("Repair tomorrow", django_mail.outbox[0].subject)

    def test_household_email_is_copied_to_the_login_at_activation(self):
        self.client.login(username="faci", password="pw")
        self.without.email = "jose@example.com"
        self.without.save()
        self.client.post(reverse("household_register_board", args=[self.without.pk]))
        self.client.post(reverse("household_activate", args=[self.without.pk]),
                         {"latitude": "9.7167", "longitude": "125.5333"})
        self.without.refresh_from_db()
        self.assertEqual(self.without.user.email, "jose@example.com")


class PwaForEveryRoleTests(TestCase):
    """The same installable app serves all three roles."""

    def setUp(self):
        self.brgy = Barangay.objects.create(name="Lower Dakong Patag")
        self.admin = User.objects.create_user(username="admin", password="pw", role=User.Role.ADMIN, is_staff=True, is_superuser=True)
        self.faci = User.objects.create_user(username="faci", password="pw", role=User.Role.FACILITATOR, barangay=self.brgy)

    def test_manifest_is_linked_for_every_role(self):
        for username, page in [("admin", "console_dashboard"), ("faci", "dashboard")]:
            self.client.login(username=username, password="pw")
            html = self.client.get(reverse(page)).content.decode()
            self.assertIn('rel="manifest"', html, username)
            self.assertIn("data-install-trigger", html, username)

    def test_manifest_starts_where_the_role_lands(self):
        manifest = self.client.get(reverse("manifest")).json()
        self.assertEqual(manifest["start_url"], "/post-login/")
        self.assertNotIn("shortcuts", manifest)


class NavigationIntegrityTests(TestCase):
    """
    Every page a role is offered in its sidebar renders, and the flows that
    cross roles actually connect: a visit the admin schedules shows on the
    facilitator's schedule and dashboard; a household the facilitator adds
    shows in the admin's lists.
    """

    def setUp(self):
        self.brgy = Barangay.objects.create(name="Lower Dakong Patag")
        self.admin = User.objects.create_user(username="admin", password="pw", role=User.Role.ADMIN, is_staff=True, is_superuser=True)
        self.faci = User.objects.create_user(
            username="faci", password="pw", role=User.Role.FACILITATOR, barangay=self.brgy, contact_number="+639170000001",
        )
        self.owner = User.objects.create_user(username="owner", password="pw", role=User.Role.OWNER)
        self.hh = Household.objects.create(
            household_id="HH-001", owner_name="Maria", address="P1",
            contact_number="+639171234501", barangay=self.brgy, user=self.owner,
        )

    def test_every_admin_sidebar_page_renders(self):
        self.client.login(username="admin", password="pw")
        for name in ["console_dashboard", "admin_pressure", "analytics", "facilitator_list", "admin_household_list",
                     "admin_household_create", "account_list",
                     "work_order_list", "work_order_create", "announcement_list", "announcement_create",
                     "data_tools", "audit_log", "system_status"]:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)

    def test_every_facilitator_sidebar_page_renders(self):
        self.client.login(username="faci", password="pw")
        for name in ["dashboard", "pressure_monitor", "event_list", "my_assignments", "household_list",
                     "segment_list", "complaint_list", "notification_center", "reports"]:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)

    def test_every_owner_sidebar_page_renders(self):
        self.client.login(username="owner", password="pw")
        for name in ["owner_home", "owner_pressure", "owner_notifications", "owner_complaints", "owner_settings"]:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)

    def test_scheduled_visit_reaches_the_facilitator_and_texts_the_household(self):
        self.client.login(username="admin", password="pw")
        self.client.post(reverse("work_order_create"), {
            "household": self.hh.pk, "title": "Application visit", "details": "",
            "assigned_to": self.faci.pk, "scheduled_for": "2026-11-12",
        })
        order = WorkOrder.objects.get(title="Application visit")
        self.assertEqual(order.kind, WorkOrder.Kind.VISIT)
        self.assertEqual(order.status, WorkOrder.Status.SCHEDULED)
        self.assertEqual(order.barangay, self.brgy)
        # Texted to the household (no account needed) and to the facilitator.
        self.assertTrue(SmsMessage.objects.filter(household=self.hh).exists())
        self.assertTrue(SmsMessage.objects.filter(to_number=self.faci.contact_number).exists())
        # And it is on the facilitator's schedule and dashboard card.
        self.client.login(username="faci", password="pw")
        self.assertContains(self.client.get(reverse("my_assignments")), "Application visit")
        self.assertContains(self.client.get(reverse("dashboard")), "Application visit")

    def test_household_activated_by_facilitator_appears_for_the_admin(self):
        faci = User.objects.get(username="faci")
        household = Household.objects.create(
            household_id="HH-091", owner_name="Pedro New", address="P3",
            contact_number="+639170000009", barangay=faci.barangay,
        )
        self.client.login(username="faci", password="pw")
        self.client.post(reverse("household_register_board", args=[household.pk]))
        self.client.post(reverse("household_activate", args=[household.pk]),
                         {"latitude": "9.7167", "longitude": "125.5333"})
        self.client.login(username="admin", password="pw")
        self.assertContains(self.client.get(reverse("account_list")), "pedro")
        self.assertContains(self.client.get(reverse("export_console", args=["households"])), "Pedro New")


class AccountContactTests(TestCase):
    """The administrator can give any login a number and an email - including their own."""

    def setUp(self):
        Barangay.objects.create(name="Lower Dakong Patag")
        self.admin = User.objects.create_user(username="admin", password="pw", role=User.Role.ADMIN, is_staff=True, is_superuser=True)
        self.other_admin = User.objects.create_user(username="admin2", password="pw", role=User.Role.ADMIN, is_superuser=True)
        self.faci = User.objects.create_user(username="faci", password="pw", role=User.Role.FACILITATOR)
        self.client.login(username="admin", password="pw")

    def test_edit_a_facilitators_contact(self):
        response = self.client.post(reverse("account_edit", args=[self.faci.pk]), {
            "first_name": "Juan", "last_name": "Cruz", "contact_number": "+639170000001", "email": "juan@example.com",
        })
        self.assertRedirects(response, reverse("account_list"))
        self.faci.refresh_from_db()
        self.assertEqual(self.faci.contact_number, "+639170000001")
        self.assertEqual(self.faci.email, "juan@example.com")
        self.assertTrue(AuditLog.objects.filter(action="account.edit", target="faci").exists())

    def test_admin_can_edit_their_own_contact(self):
        self.client.post(reverse("account_edit", args=[self.admin.pk]), {
            "first_name": "System", "last_name": "Administrator", "contact_number": "+639170000009", "email": "",
        })
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.contact_number, "+639170000009")

    def test_cannot_edit_another_admin(self):
        self.assertEqual(self.client.get(reverse("account_edit", args=[self.other_admin.pk])).status_code, 403)

    def test_accounts_page_offers_edit(self):
        self.assertContains(self.client.get(reverse("account_list")), reverse("account_edit", args=[self.faci.pk]))


class DashboardGrowthTests(TestCase):
    """
    The three dashboards, built for numbers that grow: capped lists that say
    what they hide, deltas that say which way things moved, problems first,
    and the owner seeing a visit before the knock on the door.
    """

    def setUp(self):
        self.brgy = Barangay.objects.create(name="Lower Dakong Patag")
        self.admin = User.objects.create_user(username="admin", password="pw", role=User.Role.ADMIN, is_staff=True, is_superuser=True)
        self.faci = User.objects.create_user(
            username="faci", password="pw", role=User.Role.FACILITATOR, barangay=self.brgy,
            first_name="Juan", last_name="Cruz", contact_number="+639170000001",
        )
        self.owner = User.objects.create_user(username="owner", password="pw", role=User.Role.OWNER)
        self.ok_line = Segment.objects.create(module_index=1, name="Quiet line", barangay=self.brgy, status=Segment.Status.NORMAL)
        self.leak_line = Segment.objects.create(module_index=1, name="Leaky line", barangay=self.brgy, status=Segment.Status.POSSIBLE_LEAK)
        self.hh_ok = Household.objects.create(
            household_id="HH-001", owner_name="Alma Quiet", address="P1",
            contact_number="+639171234501", barangay=self.brgy, segment=self.ok_line, user=self.owner,
        )
        self.hh_leak = Household.objects.create(
            household_id="HH-002", owner_name="Ben Leak", address="P2",
            contact_number="+639171234502", barangay=self.brgy, segment=self.leak_line,
        )

    # -- admin ----------------------------------------------------------------

    def test_admin_overview_shows_movement_and_attention(self):
        LeakEvent.objects.create(segment=self.leak_line, status=Segment.Status.POSSIBLE_LEAK,
                                 detected_at=timezone.now(), severity=LeakEvent.Severity.CRITICAL)
        WorkOrder.objects.create(kind=WorkOrder.Kind.VISIT, title="Unassigned visit", barangay=self.brgy,
                                 household=self.hh_ok, status=WorkOrder.Status.REQUESTED)
        AuditLog.record(self.admin, "test.action", "Did a thing", target="x")
        self.client.login(username="admin", password="pw")
        response = self.client.get(reverse("console_dashboard"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("+2 this week", html)                      # two households registered today
        self.assertIn('class="stat is-alert"', html)             # the leak tile turned red
        self.assertIn("waiting for a facilitator", html)         # attention strip
        self.assertIn("possible leak alert", html)
        self.assertIn("Did a thing", html)                       # recent activity feed
        self.assertIn('count-chip', html)

    def test_admin_upcoming_visits_are_capped_and_dated_relatively(self):
        today = timezone.localdate()
        for i in range(7):
            WorkOrder.objects.create(kind=WorkOrder.Kind.VISIT, title=f"Visit {i}", barangay=self.brgy,
                                     household=self.hh_ok, assigned_to=self.faci, status=WorkOrder.Status.SCHEDULED,
                                     scheduled_for=today + timezone.timedelta(days=i - 1))
        self.client.login(username="admin", password="pw")
        response = self.client.get(reverse("console_dashboard"))
        self.assertEqual(len(response.context["upcoming"]), 5)
        self.assertContains(response, "Showing the next 5 of 7")
        self.assertContains(response, "Overdue by 1 day")
        self.assertContains(response, "Today")
        self.assertContains(response, "Tomorrow")

    # -- facilitator --------------------------------------------------------

    def test_facilitator_table_lists_problems_first_and_caps_alerts(self):
        for i in range(8):
            LeakEvent.objects.create(segment=self.leak_line, status=Segment.Status.POSSIBLE_LEAK,
                                     detected_at=timezone.now(), severity=LeakEvent.Severity.CRITICAL)
        self.client.login(username="faci", password="pw")
        response = self.client.get(reverse("dashboard") + "?q=ben&problems=1")
        rows = response.context["segment_rows"]
        self.assertEqual(rows[0].name, "Leaky line")
        self.assertTrue(rows[0].is_problem)
        self.assertFalse(rows[-1].is_problem)
        self.assertEqual(response.context["open_events_total"], 8)
        self.assertEqual(len(response.context["open_events"]), 6)
        html = response.content.decode()
        self.assertIn("8 lines need attention", html)
        self.assertIn('value="ben"', html)                       # filter state from the URL
        self.assertIn("data-problems-toggle checked", html)
        self.assertIn('data-search="hh-002 ben leak leaky line"', html)
        self.assertIn("View all (8)", html)

    # -- owner ---------------------------------------------------------------

    def test_owner_sees_the_next_visit_and_the_past_week(self):
        visit_day = timezone.localdate() + timezone.timedelta(days=2)
        WorkOrder.objects.create(kind=WorkOrder.Kind.VISIT, title="Check the tap", barangay=self.brgy,
                                 household=self.hh_ok, assigned_to=self.faci, status=WorkOrder.Status.SCHEDULED,
                                 scheduled_for=visit_day)
        LeakEvent.objects.create(segment=self.ok_line, status=Segment.Status.POSSIBLE_LEAK,
                                 detected_at=timezone.now(), severity=LeakEvent.Severity.CRITICAL)
        Notification.objects.create(kind=Notification.Kind.TARGETED, title="Hi", body="Hello", recipient=self.hh_ok)
        self.client.login(username="owner", password="pw")
        response = self.client.get(reverse("owner_home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "A facilitator is coming on")
        self.assertContains(response, "Check the tap")
        self.assertContains(response, "Juan Cruz")
        self.assertEqual(response.context["week_alerts"], 1)
        self.assertEqual(response.context["week_notices"], 1)
        self.assertContains(response, "All notices (1, 1 unread)")

    def test_owner_without_a_visit_sees_no_visit_card(self):
        self.client.login(username="owner", password="pw")
        self.assertNotContains(self.client.get(reverse("owner_home")), "A facilitator is coming")


class AdminDialogTests(TestCase):
    """
    Adding a facilitator and registering a household happen in dialogs on
    the page, and a failed save reopens the dialog with the errors in place
    rather than dropping the administrator onto a separate page.
    """

    def setUp(self):
        Barangay.objects.create(name="Lower Dakong Patag")
        self.admin = User.objects.create_user(username="admin", password="pw", role=User.Role.ADMIN, is_staff=True, is_superuser=True)
        self.client.login(username="admin", password="pw")

    def test_facilitators_page_carries_the_dialog_and_its_opener(self):
        html = self.client.get(reverse("facilitator_list")).content.decode()
        self.assertIn('id="facilitator-modal"', html)
        self.assertIn('data-modal-open="facilitator-modal"', html)
        self.assertNotIn("data-modal-autoopen", html)
        # Arriving with ?open=facilitator opens it at once.
        self.assertIn("data-modal-autoopen", self.client.get(reverse("facilitator_list") + "?open=facilitator").content.decode())

    def test_facilitators_get_a_number_and_edit_and_delete_dialogs(self):
        fac = User.objects.create_user(username="juan.cruz", password="pw", role=User.Role.FACILITATOR,
                                       first_name="Juan", last_name="Cruz")
        self.assertEqual(fac.facilitator_id, "FAC-001")
        html = self.client.get(reverse("facilitator_list")).content.decode()
        self.assertIn("FAC-001", html)
        self.assertIn("FAC-002", html)                    # the number the next one gets
        self.assertIn('id="facilitator-edit-modal"', html)
        self.assertIn('id="facilitator-delete-modal"', html)
        self.assertIn("data-edit-facilitator", html)
        self.assertIn("data-delete-facilitator", html)

        response = self.client.post(reverse("facilitator_edit", args=[fac.pk]), {
            "first_name": "Juan", "last_name": "dela Cruz", "contact_number": "+639170000001", "email": "",
        })
        self.assertRedirects(response, reverse("facilitator_list"))
        fac.refresh_from_db()
        self.assertEqual(fac.last_name, "dela Cruz")
        self.assertEqual(fac.username, "juan.cruz")       # the login is untouched
        self.assertEqual(fac.facilitator_id, "FAC-001")   # and so is the number
        self.assertTrue(fac.check_password("pw"))
        self.assertTrue(AuditLog.objects.filter(action="facilitator.edit").exists())

        # The number is never typed in: posting one changes nothing.
        self.client.post(reverse("facilitator_edit", args=[fac.pk]), {
            "first_name": "Juan", "last_name": "dela Cruz", "facilitator_id": "FAC-999",
        })
        fac.refresh_from_db()
        self.assertEqual(fac.facilitator_id, "FAC-001")

        # A failed edit reopens the edit dialog on the list page.
        response = self.client.post(reverse("facilitator_edit", args=[fac.pk]), {"first_name": "", "last_name": "X"})
        self.assertTemplateUsed(response, "admin/facilitators.html")
        self.assertIn("data-modal-autoopen", response.content.decode())

        response = self.client.post(reverse("facilitator_delete", args=[fac.pk]))
        self.assertRedirects(response, reverse("facilitator_list"))
        self.assertFalse(User.objects.filter(pk=fac.pk).exists())
        self.assertTrue(AuditLog.objects.filter(action="facilitator.delete").exists())
        # Deleting the newest hands its number out again - the same rule as households.
        self.assertEqual(User.next_facilitator_id(), "FAC-001")

    def test_only_facilitators_can_be_deleted_from_the_facilitators_page(self):
        other_admin = User.objects.create_user(username="admin2", password="pw", role=User.Role.ADMIN)
        self.assertEqual(self.client.post(reverse("facilitator_delete", args=[other_admin.pk])).status_code, 404)
        self.assertIsNone(other_admin.facilitator_id)      # admins carry no number

    def test_visits_announcements_and_accounts_open_as_dialogs(self):
        # Schedule a visit: the dialog is on the list, and a failed save reopens it there.
        html = self.client.get(reverse("work_order_list")).content.decode()
        self.assertIn('id="visit-modal"', html)
        self.assertIn('data-modal-open="visit-modal"', html)
        self.assertNotIn("data-modal-autoopen", html)
        self.assertIn("data-modal-autoopen", self.client.get(reverse("work_order_list") + "?open=visit").content.decode())
        response = self.client.post(reverse("work_order_create"), {"household": "", "title": ""})
        self.assertTemplateUsed(response, "admin/workorders.html")
        self.assertIn("data-modal-autoopen", response.content.decode())

        # Edit a visit: the dialog sits on the detail page, bound to that visit.
        hh = Household.objects.create(household_id="HH-001", owner_name="Maria Santos", address="P1", contact_number="+639171234501")
        self.client.post(reverse("work_order_create"), {"household": hh.pk, "title": "Application visit"})
        order = WorkOrder.objects.get()
        html = self.client.get(reverse("work_order_detail", args=[order.pk])).content.decode()
        self.assertIn('id="visit-edit-modal"', html)
        self.assertIn('data-modal-open="visit-edit-modal"', html)
        self.assertIn('value="Application visit"', html)
        response = self.client.post(reverse("work_order_edit", args=[order.pk]), {"household": hh.pk, "title": ""})
        self.assertTemplateUsed(response, "admin/workorder_detail.html")
        self.assertIn("data-modal-autoopen", response.content.decode())
        response = self.client.post(reverse("work_order_edit", args=[order.pk]), {"household": hh.pk, "title": "Follow-up visit"})
        self.assertRedirects(response, reverse("work_order_detail", args=[order.pk]))
        order.refresh_from_db()
        self.assertEqual(order.title, "Follow-up visit")

        # Announcements: compose in a dialog on the list; a bad form reopens it.
        html = self.client.get(reverse("announcement_list")).content.decode()
        self.assertIn('id="announcement-modal"', html)
        self.assertIn("data-modal-autoopen", self.client.get(reverse("announcement_list") + "?open=announcement").content.decode())
        response = self.client.post(reverse("announcement_create"), {"audience": "all", "title": "", "body": "x", "in_app": "on"})
        self.assertTemplateUsed(response, "admin/announcements.html")
        self.assertIn("data-modal-autoopen", response.content.decode())

        # Accounts: edit is a row-driven dialog on the list.
        faci = User.objects.create_user(username="faci", password="pw", role=User.Role.FACILITATOR)
        html = self.client.get(reverse("account_list")).content.decode()
        self.assertIn('id="account-edit-modal"', html)
        self.assertIn("data-edit-account", html)
        response = self.client.post(reverse("account_edit", args=[faci.pk]), {"first_name": "", "last_name": "", "email": "not-an-email"})
        self.assertTemplateUsed(response, "admin/accounts.html")
        self.assertIn("data-modal-autoopen", response.content.decode())

    def test_admin_sees_pressure_and_flow_for_every_household(self):
        # A household with a line and a recent reading, in the served barangay.
        brgy = Barangay.default()
        seg = Segment.objects.create(module_index=1, name="HH-900 line", color="#123456", barangay=brgy)
        hh = Household.objects.create(household_id="HH-900", owner_name="Rita Flores", address="P1",
                                      contact_number="+639170000900", barangay=brgy, segment=seg,
                                      account_status=Household.AccountStatus.ACTIVE)
        # The console page lists the household and links to its CSV.
        html = self.client.get(reverse("admin_pressure")).content.decode()
        self.assertIn("Pressure and flow", html)
        self.assertIn("HH-900", html)
        self.assertIn("Rita Flores", html)
        self.assertIn(reverse("export_console", args=["pressure"]), html)
        # The nav carries it, and the page polls the shared live endpoint.
        self.assertIn('href="/console/pressure/"', html)
        self.assertIn("SHL.startPolling()", html)
        # The live endpoint gives an admin every line, not an empty set.
        live = self.client.get(reverse("api_live"), HTTP_X_REQUESTED_WITH="fetch").json()
        self.assertTrue(live["ok"])
        self.assertIn(seg.id, [s["id"] for s in live["segments"]])
        self.assertIn("open_events", live)
        # The pressure CSV exports rollups across the system.
        ReadingRollup.objects.create(segment=seg, minute=timezone.now(), avg_pressure_bar=1.1, avg_flow_lpm=0.2)
        csv_text = self.client.get(reverse("export_console", args=["pressure"])).content.decode()
        self.assertIn("avg_pressure_bar", csv_text)
        self.assertIn("HH-900", csv_text)

    def test_failed_facilitator_save_reopens_the_dialog_with_errors(self):
        response = self.client.post(reverse("facilitator_create"), {"first_name": "", "last_name": "X"})
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "admin/facilitators.html")
        html = response.content.decode()
        self.assertIn("data-modal-autoopen", html)
        self.assertIn('value="X"', html)                  # typed values survive
        self.assertIn("This field is required", html)     # the error is in the dialog

    def test_households_page_lists_and_carries_the_register_dialog(self):
        Household.objects.create(household_id="HH-001", owner_name="Maria Santos", address="P1", contact_number="+639171234501")
        html = self.client.get(reverse("admin_household_list")).content.decode()
        self.assertIn("Maria Santos", html)
        self.assertIn('id="household-modal"', html)
        self.assertIn('id="household-edit-modal"', html)
        self.assertIn('id="household-delete-modal"', html)
        self.assertIn('data-modal-open="household-modal"', html)
        self.assertIn("HH-002", html)                     # the id the next one will get
        self.assertNotIn("data-modal-autoopen", html)
        self.assertIn("data-modal-autoopen", self.client.get(reverse("admin_household_list") + "?open=household").content.decode())
        # The sidebar goes to the list, never straight into the form.
        self.assertIn('href="/console/households/"', html)
        self.assertNotIn('id="household-modal"', self.client.get(reverse("console_dashboard")).content.decode())

    def test_edit_and_delete_from_the_households_page(self):
        owner = User.objects.create_user(username="owner", password="pw", role=User.Role.OWNER)
        hh = Household.objects.create(household_id="HH-001", owner_name="Maria Santos", address="P1",
                                      contact_number="+639171234501", user=owner)
        response = self.client.post(reverse("admin_household_edit", args=[hh.pk]), {
            "owner_name": "Maria S. Cruz", "address": "P1", "contact_number": "+639171234501",
            "account_status": "active",
        })
        self.assertRedirects(response, reverse("admin_household_list"))
        hh.refresh_from_db()
        self.assertEqual(hh.owner_name, "Maria S. Cruz")
        self.assertTrue(AuditLog.objects.filter(action="household.edit").exists())
        response = self.client.post(reverse("admin_household_delete", args=[hh.pk]))
        self.assertRedirects(response, reverse("admin_household_list"))
        self.assertFalse(Household.objects.filter(pk=hh.pk).exists())
        self.assertFalse(User.objects.filter(pk=owner.pk).exists())   # the login goes with it

    def test_failed_household_save_reopens_the_dialog_on_the_households_page(self):
        response = self.client.post(reverse("admin_household_create"), {
            "owner_name": "", "address": "Purok 9", "contact_number": "+639170000009",
            "account_status": "active", "device_choice": "none",
        })
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "admin/households.html")
        html = response.content.decode()
        self.assertIn("data-modal-autoopen", html)
        self.assertIn('value="Purok 9"', html)
        self.assertFalse(Household.objects.filter(address="Purok 9").exists())

    def test_standalone_pages_still_work_without_javascript(self):
        self.assertEqual(self.client.get(reverse("facilitator_create")).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin_household_create")).status_code, 200)


class OwnerBoardSetupTests(TestCase):
    """
    The owner connects their own board: the code reaches them at activation,
    Settings > My device says where things stand, and the facilitator hears
    when the first reading lands.
    """

    def setUp(self):
        from django.core import mail as django_mail
        django_mail.outbox.clear()
        self.brgy = Barangay.objects.create(name="Lower Dakong Patag")
        self.admin = User.objects.create_user(username="admin", password="pw", role=User.Role.ADMIN, is_staff=True, is_superuser=True)
        self.faci = User.objects.create_user(username="faci", password="pw", role=User.Role.FACILITATOR,
                                             barangay=self.brgy, contact_number="+639170000001")
        self.hh = Household.objects.create(
            household_id="HH-050", owner_name="Rosa Dizon", address="Purok 5",
            contact_number="+639170000050", email="rosa@example.com", barangay=self.brgy,
        )
        self.client.login(username="faci", password="pw")

    def _activate(self):
        self.client.post(reverse("household_register_board", args=[self.hh.pk]))
        self.client.post(reverse("household_activate", args=[self.hh.pk]), {"latitude": "9.7167", "longitude": "125.5333"})
        self.hh.refresh_from_db()
        owner = self.hh.user
        owner.set_password("owner-pw")
        owner.must_change_password = False
        owner.save()
        self.client.login(username=owner.username, password="owner-pw")
        return self.hh.segment.device

    def _status(self):
        return self.client.get(reverse("owner_device_status")).json()

    def test_activation_is_refused_without_a_board(self):
        response = self.client.post(reverse("household_activate", args=[self.hh.pk]), {"latitude": "9.7167", "longitude": "125.5333"})
        self.assertEqual(response.status_code, 302)
        self.hh.refresh_from_db()
        self.assertIsNone(self.hh.user)
        self.assertTrue(self.hh.is_pending)
        self.assertEqual(self.hh.device_setup["key"], "no_board")

    def test_register_board_creates_line_and_board_once(self):
        self.client.post(reverse("household_register_board", args=[self.hh.pk]))
        self.hh.refresh_from_db()
        first = self.hh.segment.device
        self.assertEqual(self.hh.segment.module_index, 1)
        self.assertEqual(self.hh.device_setup["key"], "awaiting_activation")
        self.client.post(reverse("household_register_board", args=[self.hh.pk]))   # idempotent
        self.hh.refresh_from_db()
        self.assertEqual(self.hh.segment.device, first)
        self.assertEqual(Device.objects.count(), 1)

    def test_owner_walks_the_board_from_code_to_connected(self):
        from json import dumps
        from django.core import mail as django_mail
        device = self._activate()
        code = device.claim_code.code

        # Ready: the page and the poll both carry the code and the steps.
        html = self.client.get(reverse("owner_settings")).content.decode()
        self.assertIn("My device", html)
        self.assertIn("Ready to set up", html)
        self.assertIn(code, html)
        self.assertIn("LeakDetect-Setup", html)
        self.assertIn('data-device-poll="/portal/device/status/"', html)
        # The code and the steps are in a dialog behind one button, not on the page face.
        self.assertIn('id="device-modal"', html)
        self.assertIn('data-modal-open="device-modal"', html)
        self.assertIn("Set up my board", html)
        self.assertNotIn("data-modal-autoopen", html)
        state = self._status()
        self.assertEqual(state["key"], "ready")
        self.assertEqual(state["code"], code)
        self.assertTrue(state["can_resend"] and not state["can_reissue"])

        # Re-send goes by text and email.
        mails_before = len(django_mail.outbox)
        self.client.post(reverse("owner_device_resend"))
        self.assertEqual(SmsMessage.objects.filter(household=self.hh, body__contains=code).count(), 2)  # at activation, and now
        self.assertEqual(len(django_mail.outbox), mails_before + 1)
        self.assertIn(code, django_mail.outbox[-1].body)

        # Expired: a new code can be requested; the old one is gone.
        ticket = device.claim_code
        ticket.expires_at = timezone.now() - timezone.timedelta(days=1)
        ticket.save()
        state = self._status()
        self.assertEqual(state["key"], "expired")
        self.assertTrue(state["can_reissue"])
        self.client.post(reverse("owner_device_reissue"))
        device.refresh_from_db()
        new_code = device.claim_code.code
        self.assertNotEqual(new_code, code)
        self.assertEqual(self._status()["key"], "ready")
        self.assertTrue(AuditLog.objects.filter(action="board.code_reissued").exists())

        # The board claims the code: connecting, then "not sending" if silence drags on.
        response = self.client.post(reverse("api_claim"), data=dumps({"code": new_code.lower().replace("-", "")}),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._status()["key"], "connecting")
        ticket = device.claim_code
        ticket.used_at = timezone.now() - timezone.timedelta(minutes=10)
        ticket.save()
        state = self._status()
        self.assertEqual(state["key"], "claimed_silent")
        self.assertTrue(state["can_reissue"])

        # The first reading: connected, the facilitator is texted, and it is written down.
        response = self.client.post(
            reverse("api_ingest"),
            data=dumps({"readings": [{"module": 1, "pressure_bar": 1.2, "flow_lpm": 0.0, "status": "NORMAL"}]}),
            content_type="application/json", HTTP_X_DEVICE_KEY=device.api_key,
        )
        self.assertEqual(response.status_code, 200, response.content)
        state = self._status()
        self.assertEqual(state["key"], "connected")
        self.assertTrue(state["connected"])
        self.assertFalse(state["can_reissue"])
        self.assertTrue(AuditLog.objects.filter(action="board.connected").exists())
        self.assertTrue(SmsMessage.objects.filter(to_number=self.faci.contact_number, body__contains=device.device_id).exists())
        # Once connected, a new code is the facilitator's call, not the owner's.
        response = self.client.post(reverse("owner_device_reissue"))
        device.refresh_from_db()
        self.assertEqual(device.claim_code.code, new_code)

        # Silence after having worked: a problem, with what to do.
        device.last_seen = timezone.now() - timezone.timedelta(minutes=10)
        device.save(update_fields=["last_seen"])
        state = self._status()
        self.assertEqual(state["key"], "lost")
        self.assertIn("power", state["note"])
        html = self.client.get(reverse("owner_settings")).content.decode()
        self.assertIn("What to check", html)                # the button is reworded for the state
        # Connected again: nothing to set up, so no button at all.
        device.last_seen = timezone.now()
        device.save(update_fields=["last_seen"])
        html = self.client.get(reverse("owner_settings")).content.decode()
        self.assertIn('data-modal-open="device-modal"\n              hidden', html)

    def test_board_state_shows_for_facilitator_and_admin(self):
        self._activate()
        self.client.login(username="faci", password="pw")
        self.assertContains(self.client.get(reverse("household_detail", args=[self.hh.pk])), "Ready to set up")
        self.client.login(username="admin", password="pw")
        self.assertContains(self.client.get(reverse("admin_household_list")), "Ready to set up")

    def test_only_the_owner_reaches_their_own_board(self):
        self._activate()
        self.client.login(username="faci", password="pw")
        self.assertEqual(self.client.get(reverse("owner_device_status")).status_code, 403)


class WaterRunningAlertTests(TestCase):
    """
    The board's new 'water running' warning, and that the detection settings
    ride back to it on the ingest reply.
    """

    def setUp(self):
        from django.core import mail as django_mail
        django_mail.outbox.clear()
        self.brgy = Barangay.objects.create(name="Lower Dakong Patag")
        self.device = Device.objects.create(label="board-70")
        self.segment = Segment.objects.create(
            module_index=1, name="HH-070 line", device=self.device,
            barangay=self.brgy, status=Segment.Status.NORMAL,
        )
        self.hh = Household.objects.create(
            household_id="HH-070", owner_name="Nora Cruz", address="Purok 7",
            contact_number="+639170000070", email="nora@example.com",
            barangay=self.brgy, account_status=Household.AccountStatus.ACTIVE,
        )
        self.hh.segment = self.segment
        self.hh.save(update_fields=["segment"])

    def _ingest(self, status):
        return self.client.post(
            reverse("api_ingest"),
            data=json.dumps({
                "device_id": self.device.device_id,
                "readings": [{"module": 1, "pressure_bar": 0.8, "flow_lpm": 9.0, "status": status}],
            }),
            content_type="application/json",
            HTTP_X_DEVICE_KEY=self.device.api_key,
        )

    @override_settings(SMS_AUTO_ON_LEAK=True, EMAIL_AUTO_ON_LEAK=True, PUSH_AUTO_ON_LEAK=False)
    def test_water_running_alerts_by_notice_sms_and_email(self):
        from django.core import mail as django_mail
        resp = self._ingest("WATER_RUNNING")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.segment.refresh_from_db()
        self.assertEqual(self.segment.status, Segment.Status.WATER_RUNNING)
        self.assertTrue(
            Notification.objects.filter(recipient=self.hh, title__icontains="running").exists()
        )
        self.assertTrue(
            SmsMessage.objects.filter(household=self.hh, body__icontains="WATER RUNNING").exists()
        )
        self.assertEqual(len(django_mail.outbox), 1)
        self.assertIn("running", django_mail.outbox[0].subject.lower())

    @override_settings(SMS_FACILITATOR_ON_LEAK=True)
    def test_water_running_does_not_pull_the_facilitator(self):
        faci = User.objects.create_user(
            username="faci70", password="pw", role=User.Role.FACILITATOR,
            barangay=self.brgy, contact_number="+639170000001",
        )
        # Already connected, so the one-time "board connected" text to the
        # facilitator does not fire and muddy the check.
        self.device.last_seen = timezone.now()
        self.device.save(update_fields=["last_seen"])
        self._ingest("WATER_RUNNING")
        self.assertFalse(SmsMessage.objects.filter(to_number=faci.contact_number).exists())

    def test_config_push_carries_the_detection_settings(self):
        s = SystemSetting.load()
        s.flow_warn_minutes = 25
        s.valve_test_hour = 3
        s.save()
        cfg = self._ingest("NORMAL").json()["config"]["1"]
        self.assertEqual(cfg["flow_warn_minutes"], 25)
        self.assertEqual(cfg["valve_test_hour"], 3)
        self.assertIn("flow_leak_minutes", cfg)
        self.assertIn("valve_test_drop_bar", cfg)
        self.assertIn("auto_close_on_flow_leak", cfg)


class DetectionSettingsConsoleTests(TestCase):
    """Editing the leak-detection thresholds from the admin console."""

    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin", password="pw", role=User.Role.ADMIN,
            is_staff=True, is_superuser=True,
        )
        self.client.login(username="admin", password="pw")

    def test_admin_can_edit_detection_settings(self):
        resp = self.client.post(reverse("detection_settings"), {
            "flow_warn_minutes": 20, "flow_leak_minutes": 90,
            "valve_test_hour": 3, "valve_test_grace_minutes": 45,
            "valve_test_drop_bar": "0.2", "auto_close_on_flow_leak": "on",
        })
        self.assertRedirects(resp, reverse("system_status"))
        s = SystemSetting.load()
        self.assertEqual(s.flow_warn_minutes, 20)
        self.assertEqual(s.flow_leak_minutes, 90)
        self.assertEqual(s.valve_test_hour, 3)
        self.assertEqual(s.valve_test_grace_minutes, 45)
        self.assertTrue(s.auto_close_on_flow_leak)
        self.assertTrue(AuditLog.objects.filter(action="system.detection").exists())

    def test_warning_time_must_be_before_the_leak_time(self):
        resp = self.client.post(reverse("detection_settings"), {
            "flow_warn_minutes": 60, "flow_leak_minutes": 30,
            "valve_test_hour": 2, "valve_test_grace_minutes": 60,
            "valve_test_drop_bar": "0.15", "auto_close_on_flow_leak": "on",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "must be longer")
        self.assertEqual(SystemSetting.load().flow_warn_minutes, 30)   # unchanged
