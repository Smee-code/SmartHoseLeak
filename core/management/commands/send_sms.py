"""
Drain the SMS queue by hand.

The server drains it automatically while runserver is up. This exists for the
cases where that is not true: a production server behind gunicorn, a cron job,
or - most usefully - checking what the GSM module actually says when messages
are not going out.
"""

from django.core.management.base import BaseCommand

from core import sms
from core.models import SmsMessage


class Command(BaseCommand):
    help = "Send queued SMS messages through the GSM module attached to this machine."

    def add_arguments(self, parser):
        parser.add_argument(
            "--watch", action="store_true",
            help="Keep running and drain the queue continuously.",
        )

    def handle(self, *args, **options):
        ready, note = sms.describe_gateway()
        self.stdout.write(("" if ready else "WARNING: ") + note)

        queued = SmsMessage.objects.filter(status=SmsMessage.Status.QUEUED).count()
        self.stdout.write(f"{queued} message(s) waiting.")

        if options["watch"]:
            self.stdout.write("Watching. Press Ctrl+C to stop.")
            sms.start_worker()
            try:
                import time
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                self.stdout.write("\nStopped.")
            return

        sent, failed, skipped = sms.drain_once(limit=100)
        self.stdout.write(f"sent {sent}, failed {failed}, held {skipped}")
        if skipped:
            self.stdout.write(
                "Held messages are still queued - the module was not reachable, "
                "which is not the message's fault. They go out when it is."
            )
