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


class SmsWorkerTests(TestCase):
    def setUp(self):
        self.household = Household.objects.create(
            household_id="HH-004", owner_name="Pedro Lim",
            address="Purok 2", contact_number="+639171234504",
        )

    @override_settings(SMS_ENABLED=True, SMS_MAX_ATTEMPTS=3)
    def test_missing_modem_holds_the_message_without_spending_an_attempt(self):
        """
        No hardware is a site problem, not a message problem.

        Charging an attempt for it would burn the retry budget of every
        queued message while the modem sat in somebody's bag, and they would
        all be marked failed before it was ever plugged in.
        """
        SmsMessage.queue(self.household, "test")
        with mock.patch.object(sms, "send_now", side_effect=sms.SmsUnavailable("no GSM module found")):
            sent, failed, skipped = sms.drain_once()

        self.assertEqual((sent, failed, skipped), (0, 0, 1))
        message = SmsMessage.objects.get()
        self.assertEqual(message.status, SmsMessage.Status.QUEUED)
        self.assertEqual(message.attempts, 0)

    @override_settings(SMS_ENABLED=True, SMS_MAX_ATTEMPTS=2)
    def test_network_refusal_retries_then_gives_up(self):
        """One bad number must not block the queue behind it forever."""
        SmsMessage.queue(self.household, "test")
        with mock.patch.object(sms, "send_now", side_effect=sms.SmsUnavailable("the network rejected the message")):
            sms.drain_once()
            message = SmsMessage.objects.get()
            self.assertEqual(message.status, SmsMessage.Status.QUEUED)
            self.assertEqual(message.attempts, 1)

            sms.drain_once()
            message.refresh_from_db()
            self.assertEqual(message.status, SmsMessage.Status.FAILED)

    @override_settings(SMS_ENABLED=True)
    def test_success_records_when_it_went(self):
        SmsMessage.queue(self.household, "test")
        with mock.patch.object(sms, "send_now", return_value=None):
            sent, failed, skipped = sms.drain_once()

        self.assertEqual((sent, failed, skipped), (1, 0, 0))
        message = SmsMessage.objects.get()
        self.assertEqual(message.status, SmsMessage.Status.SENT)
        self.assertIsNotNone(message.sent_at)

    @override_settings(SMS_ENABLED=False)
    def test_disabled_sends_nothing(self):
        SmsMessage.queue(self.household, "test")
        self.assertEqual(sms.drain_once(), (0, 0, 0))
        self.assertEqual(SmsMessage.objects.get().status, SmsMessage.Status.QUEUED)
