"""
Web push notifications for the Owner Portal.

Why this and not Firebase
-------------------------
Push on the web is a W3C standard. The browser holds a subscription to its
own vendor's push service, the server signs a message with a VAPID key pair,
and the service worker wakes up and draws the notification. Firebase Cloud
Messaging is one implementation of that, not a requirement for it, and
choosing it would have meant a Google project, a service-account JSON, and
the Firebase SDK fetched from gstatic on every load.

The last of those is the one that decided it. The portal is a PWA built to
open on a weak signal in Lower Dakong Patag, with its shell in a cache and an
offline page behind it. An SDK that has to be fetched from a CDN before
notifications can be registered is exactly the dependency this app spent
effort not having. pywebpush talks the protocol directly, so the only thing
that leaves the machine is the message itself.

Why it can be switched off
--------------------------
Without VAPID keys in .env every function here reports "not configured" and
nothing else happens. That is deliberate: the portal has to run on a laptop
during development and at a defence, where nobody has set up keys, and it
must not fail or nag because of it. Same shape as core.sms.

Sent, not delivered
-------------------
A push service accepting a message means it has taken it, not that a phone
showed it. A handset that has been off for a week gets the message when it
wakes, or does not get it at all once the message expires. Nothing here
claims otherwise, and the leak alert still goes out by SMS regardless - push
is an extra way to reach somebody, never the only one.
"""

import json
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

# How long a push service should hold a message for a phone that is off.
# A leak alert is worth waking up to in the morning; it is not worth
# delivering four days later, by which time the water has either been fixed
# or has been running into the ground long enough that the news is useless.
TTL_SECONDS = 6 * 60 * 60


class PushUnavailable(Exception):
    """Raised when a message cannot be handed to the push service."""


def is_configured():
    return bool(
        settings.PUSH_ENABLED
        and settings.VAPID_PUBLIC_KEY
        and settings.VAPID_PRIVATE_KEY
    )


def describe_push():
    """
    One honest sentence about whether a notification can be sent right now.

    Shown on the owner's settings page and in the facilitator's guide, so that
    "I never got a notification" has an answer that does not require reading
    the logs.
    """
    if not settings.PUSH_ENABLED:
        return False, "Notifications are switched off in settings (PUSH_ENABLED)."

    if not (settings.VAPID_PUBLIC_KEY and settings.VAPID_PRIVATE_KEY):
        return False, (
            "No notification keys set yet. Run "
            "\"python manage.py make_push_keys\" and put the two values it "
            "prints into your .env file."
        )

    try:
        import pywebpush  # noqa: F401
    except ImportError:
        return False, (
            "The pywebpush package is not installed. Run "
            "\"pip install -r requirements.txt\"."
        )

    return True, "Notifications are switched on."


def _claims():
    """
    Who is sending, for the push service's benefit.

    The subject must be a mailto: or https: URL identifying the sender, so a
    push service with a misbehaving application has somebody to contact. It is
    not shown to the person receiving the notification.
    """
    return {"sub": settings.VAPID_SUBJECT}


def send_to_subscription(subscription, payload):
    """
    Push one message to one browser.

    Returns True if the push service took it. Raises PushUnavailable if it
    could not be sent, and returns False if the subscription is dead - which
    the caller treats as "delete this row", not as an error worth reporting.
    """
    from pywebpush import WebPushException, webpush

    try:
        webpush(
            subscription_info=subscription.as_dict,
            data=json.dumps(payload),
            vapid_private_key=settings.VAPID_PRIVATE_KEY,
            vapid_claims=dict(_claims()),
            ttl=TTL_SECONDS,
        )
        return True
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)

        # 404 and 410 are the push service saying this subscription no longer
        # exists - the app was uninstalled, the browser data was cleared, or
        # the handset was reset. It is the one unambiguous signal we get, and
        # the only one worth deleting a row over.
        if status in (404, 410):
            return False

        raise PushUnavailable(f"{status or 'no status'}: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        # pywebpush only wraps what the push service says. Everything before
        # that - DNS, the TCP connection, a timeout, no internet at all -
        # comes out of requests untouched, so catching WebPushException alone
        # let a server with no connectivity raise straight through a function
        # whose callers were told it never raises.
        raise PushUnavailable(f"could not reach the push service: {exc}") from exc


def send_to_household(household, title, body, url="/portal/"):
    """
    Notify every handset that household has registered.

    Returns (sent, removed). Never raises: a leak alert must not fail because
    a push service is having a bad afternoon, and the SMS has gone out
    regardless. Problems are logged, counted and moved past.
    """
    if not is_configured():
        return 0, 0

    payload = {"title": title, "body": body, "url": url}
    sent = 0
    removed = 0

    from django.utils import timezone

    for subscription in household.push_subscriptions.all():
        try:
            ok = send_to_subscription(subscription, payload)
        except PushUnavailable as exc:
            logger.warning(
                "Push to %s failed: %s", household.household_id, exc
            )
            subscription.failures += 1
            subscription.save(update_fields=["failures"])
            continue

        if ok:
            sent += 1
            subscription.last_used_at = timezone.now()
            subscription.failures = 0
            subscription.save(update_fields=["last_used_at", "failures"])
        else:
            # Gone for good. Keeping it would mean trying a dead handset on
            # every alert forever.
            subscription.delete()
            removed += 1

    return sent, removed
