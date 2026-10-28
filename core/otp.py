"""
The second step of signing in: a one-time code by text or email.

Where things go
---------------
channels_for() answers "where can this person be reached": the number and
email on their own login, falling back to their household's for an owner.
send_code() issues a LoginCode and delivers it on the channel the person
chose, or the other one if theirs is not on file, and says which it used.
Trust is a signed cookie: "remember this device" means that phone skips the
code for OTP_TRUSTED_DEVICE_DAYS, and signing out does not clear it - the
device is trusted, not the session.

Why the code is shown in DEBUG
------------------------------
A sign-in that depends on SMS credit or a configured mailbox is a sign-in
that fails at a demonstration. With DEBUG on, the code is put in a message
on the page as well as sent, so the flow can be walked through anywhere.
That is never done when DEBUG is off.
"""

import logging

from django.conf import settings
from django.core import signing
from django.utils import timezone

from . import mail
from .models import LoginCode, SmsMessage


class DeliveryError(Exception):
    """The code was issued but could not be handed to the mail server."""

logger = logging.getLogger(__name__)

TRUSTED_COOKIE = "shl_trusted_device"


def channels_for(user):
    """{'sms': number or None, 'email': address or None} for this person."""
    household = getattr(user, "household", None)
    number = (user.contact_number or "").strip() or (
        (household.contact_number or "").strip() if household is not None else ""
    )
    email = (user.email or "").strip() or (
        (household.email or "").strip() if household is not None else ""
    )
    return {"sms": number or None, "email": email or None}


def send_code(user, preferred):
    """
    Issue and deliver a code. Returns the LoginCode, or None if the account
    has nowhere to send one.

    The preferred channel is used when it is on file; otherwise the other
    one. The caller tells the person which was actually used.
    """
    available = channels_for(user)
    channel = preferred if available.get(preferred) else next(
        (name for name, value in available.items() if value), None
    )
    if channel is None:
        return None

    destination = available[channel]
    code = LoginCode.issue(user, channel, sent_to=destination)
    minutes = settings.OTP_VALIDITY_MINUTES

    if channel == LoginCode.Channel.SMS:
        SmsMessage.queue_to_number(
            destination,
            f"Smart Hose Leak sign-in code: {code.code}. It expires in {minutes} minutes. "
            f"If you did not try to sign in, ignore this.",
            name=user.get_full_name() or user.username,
            kind=SmsMessage.Kind.AUTOMATIC,
        )
    else:
        delivered = mail.send_email(
            destination,
            "Your Smart Hose Leak sign-in code",
            f"Your sign-in code is {code.code}. It expires in {minutes} minutes.\n\n"
            f"If you did not try to sign in, you can ignore this email.",
        )
        if not delivered:
            # Telling someone "check your inbox" for a mail that never left
            # is the worst outcome; burn the code and say what happened.
            code.delete()
            logger.warning("Sign-in code for %s could not be emailed", user.username)
            raise DeliveryError(
                "The sign-in code could not be emailed right now. Try having it texted "
                "instead, or ask the administrator to check the email setup."
            )
    logger.info("Sign-in code sent to %s by %s", user.username, channel)
    return code


def verify(user, raw):
    """
    Check a typed code against the user's live one.

    Returns (ok, reason). A wrong guess counts against the code; the fifth
    wrong guess burns it, so a new one has to be requested.
    """
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    code = user.login_codes.filter(used_at__isnull=True).order_by("-created_at").first()
    if code is None:
        return False, "No code is waiting. Request a new one."
    if code.is_expired:
        return False, "That code has expired. Request a new one."
    if code.attempts >= LoginCode.MAX_ATTEMPTS:
        return False, "Too many wrong guesses. Request a new code."
    if digits != code.code:
        code.attempts += 1
        code.save(update_fields=["attempts"])
        left = LoginCode.MAX_ATTEMPTS - code.attempts
        if left <= 0:
            return False, "Too many wrong guesses. Request a new code."
        return False, f"That code is not right. {left} tr{'y' if left == 1 else 'ies'} left."
    code.used_at = timezone.now()
    code.save(update_fields=["used_at"])
    return True, ""


def trusted_cookie_value(user):
    """A signed token naming the user; the signature carries the timestamp."""
    return signing.dumps({"u": user.pk}, salt=TRUSTED_COOKIE)


def is_trusted(request, user):
    """Whether this browser was marked 'remember this device' for this user."""
    raw = request.COOKIES.get(TRUSTED_COOKIE)
    if not raw:
        return False
    try:
        data = signing.loads(
            raw, salt=TRUSTED_COOKIE, max_age=settings.OTP_TRUSTED_DEVICE_DAYS * 86400
        )
    except signing.BadSignature:
        return False
    return data.get("u") == user.pk


def can_resend(user):
    """Not more than one code a minute: a tap-tap-tap must not send three texts."""
    latest = user.login_codes.order_by("-created_at").first()
    if latest is None:
        return True
    age = (timezone.now() - latest.created_at).total_seconds()
    return age >= settings.OTP_RESEND_SECONDS
