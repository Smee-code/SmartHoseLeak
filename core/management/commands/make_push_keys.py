"""
Generate the VAPID key pair that signs push notifications.

Run once, ever. The pair identifies this server to the browsers' push
services: every subscription a phone creates is tied to the public key it saw
at the time, so regenerating the keys silently invalidates every subscription
already out there and every installed portal stops receiving notifications
until each household turns them on again.
"""

import base64

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Generate a VAPID key pair for push notifications, to paste into .env."

    def handle(self, *args, **options):
        try:
            from py_vapid import Vapid01
        except ImportError:
            raise SystemExit(
                "pywebpush is not installed. Run: pip install -r requirements.txt"
            )

        from cryptography.hazmat.primitives import serialization

        vapid = Vapid01()
        vapid.generate_keys()

        private_raw = vapid.private_key.private_numbers().private_value.to_bytes(32, "big")
        public_raw = vapid.public_key.public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )

        def b64(raw):
            # URL-safe base64 without padding: what both the Push API in the
            # browser and pywebpush expect.
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Put these three lines in your .env file:"))
        self.stdout.write("")
        self.stdout.write(f"VAPID_PUBLIC_KEY={b64(public_raw)}")
        self.stdout.write(f"VAPID_PRIVATE_KEY={b64(private_raw)}")
        self.stdout.write("VAPID_SUBJECT=mailto:your-email@example.com")
        self.stdout.write("")
        self.stdout.write(
            "Then restart the server. Keep the private key out of git - .env\n"
            "is already ignored. Generate this once and never again: every\n"
            "phone's subscription is tied to the public key it saw, so a new\n"
            "pair silently stops notifications on every installed portal."
        )
