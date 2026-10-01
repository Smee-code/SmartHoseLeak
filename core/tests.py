"""
Tests for the parts that fail silently.

Most of this system announces its own breakage: a page that does not render
is obvious, and a device that stops reporting turns a row red. These are the
places where a fault would look exactly like success - a text that arrives
cut in half, a message dropped because nobody had a number, a queue that
quietly empties itself while the modem is unplugged.
"""

from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from core import sms
from core.api import _leak_sms_text
from core.models import (
    Device,
    DeviceSeverity,
    Household,
    LeakEvent,
    Segment,
    SmsMessage,
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
        with mock.patch.object(sms, "send_now", side_effect=sms.SmsUnavailable("no API key set")):
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
        with mock.patch.object(sms, "_get", return_value={"status": provider_status}):
            return sms.poll_delivery()

    @override_settings(SMS_ENABLED=True, SMS_TRACK_DELIVERY=True, SEMAPHORE_API_KEY="k")
    def test_gateway_says_sent_means_delivered(self):
        self.assertEqual(self._poll("Sent"), 1)
        self.message.refresh_from_db()
        self.assertEqual(self.message.status, SmsMessage.Status.DELIVERED)
        self.assertIsNotNone(self.message.delivered_at)

    @override_settings(SMS_ENABLED=True, SMS_TRACK_DELIVERY=True, SEMAPHORE_API_KEY="k")
    def test_gateway_says_failed_means_not_delivered(self):
        self.assertEqual(self._poll("Failed"), 1)
        self.message.refresh_from_db()
        self.assertEqual(self.message.status, SmsMessage.Status.UNDELIVERED)
        self.assertIn("Failed", self.message.error)

    @override_settings(SMS_ENABLED=True, SMS_TRACK_DELIVERY=True, SEMAPHORE_API_KEY="k")
    def test_still_pending_is_left_alone(self):
        """A verdict we do not have yet must not be invented as either one."""
        self.assertEqual(self._poll("Pending"), 0)
        self.message.refresh_from_db()
        self.assertEqual(self.message.status, SmsMessage.Status.SENT)

    @override_settings(SMS_ENABLED=True, SMS_TRACK_DELIVERY=True, SEMAPHORE_API_KEY="k")
    def test_a_word_the_gateway_invents_is_kept_but_not_acted_on(self):
        """
        Their vocabulary can change. Recording it verbatim makes that visible
        instead of silently mapping something new onto a wrong verdict.
        """
        self.assertEqual(self._poll("Throttled"), 0)
        self.message.refresh_from_db()
        self.assertEqual(self.message.status, SmsMessage.Status.SENT)
        self.assertEqual(self.message.provider_status, "Throttled")

    @override_settings(SMS_ENABLED=True, SMS_TRACK_DELIVERY=True, SEMAPHORE_API_KEY="k")
    def test_a_failed_lookup_records_no_verdict(self):
        """The message is fine; the lookup failed. Ask again rather than guess."""
        with mock.patch.object(sms, "_get", side_effect=sms.SmsUnavailable("timeout")):
            self.assertEqual(sms.poll_delivery(), 0)
        self.message.refresh_from_db()
        self.assertEqual(self.message.status, SmsMessage.Status.SENT)


@override_settings(SMS_ENABLED=True, SEMAPHORE_API_KEY="test-key")
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
        ready, note = self._describe({"credit_balance": 0})
        self.assertFalse(ready)
        self.assertIn("no SMS credit", note)

    def test_low_credit_still_sends(self):
        """Low is a warning, not a stop: it works, and the fix is to top up."""
        ready, note = self._describe({"credit_balance": 12})
        self.assertTrue(ready)
        self.assertIn("low", note)
        self.assertIn("12", note)

    def test_healthy_credit_reads_as_working(self):
        ready, note = self._describe({"credit_balance": 450})
        self.assertTrue(ready)
        self.assertIn("450", note)

    def test_the_billing_account_name_is_not_shown(self):
        """
        It printed as "Zynergyx Technologies" - the name on the Semaphore
        account, which means nothing to a barangay facilitator and read as
        though a third party were handling their messages.
        """
        _, note = self._describe({"credit_balance": 450, "account_name": "Zynergyx Technologies"})
        self.assertNotIn("Zynergyx", note)

    def test_a_rate_limited_credit_check_is_not_a_failure(self):
        """Being throttled on the balance lookup says nothing about sending."""
        with mock.patch.object(sms, "_get", side_effect=sms.SmsUnavailable("429 Too Many Attempts")):
            ready, note = sms.describe_gateway(force=True)
        self.assertTrue(ready)
        self.assertIn("rate-limited", note)

    def test_an_unreadable_balance_does_not_block_sending(self):
        """A value we cannot parse is not evidence that the account is empty."""
        ready, _ = self._describe({"credit_balance": "unlimited"})
        self.assertTrue(ready)
