"""
Sending email: sign-in codes, and notices for people who prefer email.

Kept to one function on purpose, the way sms.py wraps the gateway: every
place that emails goes through here, so "is email set up" is answered in one
place and a mail server that is down never takes a page down with it.

Email is sent inline rather than queued. A sign-in code has to arrive while
the person is standing at the login page, and Django's SMTP send is a few
hundred milliseconds against Gmail; a notice to a handful of households is
the same. If volume ever grows past that, this is the function to queue.
"""

import logging

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


def describe_email():
    """One honest sentence about whether an email can be sent right now."""
    if settings.EMAIL_BACKEND.endswith("console.EmailBackend"):
        if settings.EMAIL_HOST_USER and not settings.EMAIL_HOST_PASSWORD:
            return False, (
                f"Email is not set up yet: {settings.EMAIL_HOST_USER} is the sending address, "
                "but EMAIL_HOST_PASSWORD (its Gmail app password) is blank in .env. "
                "Until then messages are printed to the server terminal instead."
            )
        return False, (
            "Email is not set up: messages are printed to the server terminal instead. "
            "Add EMAIL_HOST_USER and EMAIL_HOST_PASSWORD (a Gmail app password) to .env."
        )
    return True, f"Emails are sent from {settings.DEFAULT_FROM_EMAIL}."


def send_email(to, subject, body):
    """
    One plain-text email. Returns True if it was handed to the mail server.

    Never raises: a refused login, a timeout or an unreachable server are
    logged and reported as False so the caller can say "could not be sent"
    rather than show an error page. With the console backend this "sends" to
    the terminal and returns True, which is the right answer in development.
    """
    to = (to or "").strip()
    if not to:
        return False
    try:
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [to], fail_silently=False)
    except Exception as exc:  # smtplib and socket raise a family of these
        logger.warning("Email to %s failed: %s", to, exc)
        return False
    return True
