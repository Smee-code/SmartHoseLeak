"""
Send one test email, to prove the mailbox in .env works end to end.

    python manage.py send_email you@example.com

Says first whether email is set up at all, then whether the message was
handed to the mail server. With no mailbox configured the "email" is printed
to this terminal, which is the development state and not a failure.
"""

from django.core.management.base import BaseCommand

from core import mail


class Command(BaseCommand):
    help = "Send a test email through the configured mailbox."

    def add_arguments(self, parser):
        parser.add_argument("to", help="The address to send the test to.")

    def handle(self, *args, **options):
        ready, note = mail.describe_email()
        self.stdout.write(("" if ready else "WARNING: ") + note)
        sent = mail.send_email(
            options["to"],
            "Smart Hose Leak - test email",
            "This is a test from the Smart Hose Leak Detection System. "
            "If you can read this, email notifications and sign-in codes by email will work.",
        )
        if sent and ready:
            self.stdout.write(self.style.SUCCESS(f"Sent to {options['to']}. Check the inbox (and spam)."))
        elif sent:
            self.stdout.write("Printed above instead of sent, because no mailbox is set up.")
        else:
            self.stderr.write(
                "NOT sent. Gmail refused the login or could not be reached - check EMAIL_HOST_USER, "
                "the app password, and the server log."
            )
