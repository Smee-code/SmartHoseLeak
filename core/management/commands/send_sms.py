"""
Drain the SMS queue by hand, and ask what happened to what was sent.

The server drains it automatically while runserver is up. This exists for the
cases where that is not true - a production server behind gunicorn, a cron
job, or checking what the gateway actually says when messages are not going
out.
"""

import time

from django.core.management.base import BaseCommand

from core import sms
from core.models import SmsMessage


class Command(BaseCommand):
    help = "Send queued SMS through the gateway, and update delivery status."

    def add_arguments(self, parser):
        parser.add_argument(
            "--watch", action="store_true",
            help="Keep running and drain the queue continuously.",
        )
        parser.add_argument(
            "--status-only", action="store_true",
            help="Do not send anything; only ask about messages already sent.",
        )

    def handle(self, *args, **options):
        ready, note = sms.describe_gateway()
        self.stdout.write(("" if ready else "WARNING: ") + note)

        queued = SmsMessage.objects.filter(status=SmsMessage.Status.QUEUED).count()
        awaiting = SmsMessage.objects.filter(status=SmsMessage.Status.SENT).count()
        self.stdout.write(f"{queued} waiting to send, {awaiting} awaiting delivery.")

        if options["watch"]:
            self.stdout.write("Watching. Press Ctrl+C to stop.")
            sms.start_worker()
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                self.stdout.write("\nStopped.")
            return

        if not options["status_only"]:
            sent, failed, skipped = sms.drain_once(limit=100)
            self.stdout.write(f"sent {sent}, failed {failed}, held {skipped}")
            if skipped:
                self.stdout.write(
                    "Held messages are still queued - there is no API key, which is "
                    "not the message's fault. They go out once one is set."
                )

        updated = sms.poll_delivery(limit=100)
        self.stdout.write(f"delivery status updated for {updated} message(s)")
