"""
Sending SMS through iprogSMS (iprogsms.com), a Philippine SMS gateway.

Why a gateway and not a modem
------------------------------
This started as a SIM800L plugged into the machine running Django. Two things
killed it: a hosted server has no USB port, and pyserial can only open a port on
the machine executing the code. A gateway works from a laptop and from a data
centre alike, so one build serves the prototype and the deployment.

What that gives up is SMS during an internet outage. The answer is the SIM800L
on the BOARD, which texts the owner directly when it cannot reach the server.

Why queued and not sent inline
--------------------------------
A gateway call is a network round trip that can hang. Doing it inside a request
would make a facilitator watch a spinner while an API is slow, and lose the
message if they reloaded. Messages are written to the database first and drained
by a worker.

Sent is not delivered
---------------------
The gateway accepting a message means it has taken responsibility for it, not
that a phone received it. The status endpoint knows which happened and is polled
on a slower clock so the log can tell the two apart.
"""

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

_worker = None

HTTP_TIMEOUT_S = 20

# iprogSMS delivery statuses, mapped onto ours. Their vocabulary is not
# exhaustively documented; keeping the raw string in provider_status means any
# new word they introduce is visible rather than silently swallowed.
PROVIDER_STATUS = {
    "pending": None,          # still in transit - do not record a verdict yet
    "queued": None,
    "sent": "delivered",      # reached the handset
    "delivered": "delivered",
    "failed": "undelivered",
    "failed_delivery": "undelivered",
    "expired": "undelivered",
}


class SmsUnavailable(Exception):
    """The message could not be handed over. Usually recoverable."""


_HEADERS = {
    "User-Agent": "SmartHoseLeak/1.0",
    "Accept": "application/json",
}


def _post(path, fields):
    """
    One form-encoded POST to the gateway.

    Errors are turned into SmsUnavailable with the gateway's own words where
    it gave any: "Invalid Token" and similar are things only the gateway can
    tell us, and both are fixed by a person rather than by retrying.
    """
    url = f"{settings.IPROGSMS_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    data = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(url, data=data, method="POST", headers=_HEADERS)

    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        raise SmsUnavailable(f"gateway refused it ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise SmsUnavailable(f"cannot reach the gateway: {exc.reason}") from exc
    except (ValueError, TimeoutError) as exc:
        raise SmsUnavailable(f"gateway gave an unreadable answer: {exc}") from exc


def _get(path, params):
    url = f"{settings.IPROGSMS_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers=_HEADERS)

    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        raise SmsUnavailable(f"gateway refused it ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise SmsUnavailable(f"cannot reach the gateway: {exc.reason}") from exc
    except (ValueError, TimeoutError) as exc:
        raise SmsUnavailable(f"gateway gave an unreadable answer: {exc}") from exc


_gateway_cache = {"at": 0.0, "value": None}
GATEWAY_CACHE_SECONDS = 300


def describe_gateway(force=False):
    """
    One honest sentence about whether a text can be sent right now.

    Shown above the compose form so a facilitator knows before writing
    anything rather than after. The balance is included because running out
    of credit is the failure that looks most like the system being broken.
    """
    if not settings.SMS_ENABLED:
        return False, "Texts are switched off in settings (SMS_ENABLED)."

    if not settings.IPROGSMS_API_TOKEN:
        return False, (
            "No iprogSMS API token set. Add IPROGSMS_API_TOKEN to your .env file - "
            "sign up at iprogsms.com to get one."
        )

    now = time.monotonic()
    if not force and _gateway_cache["value"] is not None:
        if now - _gateway_cache["at"] < GATEWAY_CACHE_SECONDS:
            return _gateway_cache["value"]

    result = _describe_gateway_uncached()
    _gateway_cache["at"] = now
    _gateway_cache["value"] = result
    return result


def _describe_gateway_uncached():
    try:
        account = _get("account/sms_credits", {"api_token": settings.IPROGSMS_API_TOKEN})
    except SmsUnavailable as exc:
        detail = str(exc)
        if "429" in detail or "Too Many Attempts" in detail:
            return True, "Texts are going out. The credit check is rate-limited just now."
        return False, f"The SMS gateway is set up but not answering: {detail}"

    if account.get("status") != "success":
        msg = account.get("message", "unknown error")
        return False, f"The SMS gateway reported a problem: {msg}"

    data = account.get("data", {})
    balance = data.get("load_balance")

    if balance is None:
        return True, "Texts are going out."

    try:
        credit = float(balance)
    except (TypeError, ValueError):
        return True, "Texts are going out."

    if credit <= 0:
        return False, "There is no SMS credit left, so no text can be sent."
    if credit < 20:
        return True, f"Texts are going out, but credit is low - {credit:g} left."
    return True, f"Texts are going out. Credit: {credit:g}."


def normalise_number(raw):
    """
    Put a Philippine number into the form the gateway expects (639XXXXXXXXX).

    Records get written by hand, so the same number turns up as 09171234567,
    +639171234567 and 639171234567. Rejecting two of those three would look
    like the number was wrong rather than punctuated differently.
    """
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if digits.startswith("0") and len(digits) == 11:
        return "63" + digits[1:]
    if digits.startswith("63") and len(digits) == 12:
        return digits
    if len(digits) == 10 and digits.startswith("9"):
        return "63" + digits
    return digits


def send_now(message):
    """
    Hand one message to the gateway. Blocking; called from the worker.

    Returns (provider_message_id, provider_status). Raises SmsUnavailable for
    anything recoverable so the caller can leave the message queued rather
    than discard it.
    """
    if not settings.IPROGSMS_API_TOKEN:
        raise SmsUnavailable("no API token set")

    number = normalise_number(message.to_number)
    if len(number) != 12:
        raise SmsUnavailable(f"'{message.to_number}' is not a valid PH mobile number")

    fields = {
        "api_token": settings.IPROGSMS_API_TOKEN,
        "phone_number": number,
        "message": message.body,
    }

    reply = _post("sms_messages", fields)

    # Success: {"status": 200, "message": "...", "message_id": "iSms-XHYBk"}
    # Error:   {"status": 500, "message": "Invalid Token"}
    if not isinstance(reply, dict):
        raise SmsUnavailable(f"gateway gave an unexpected response type: {type(reply).__name__}")

    status_code = reply.get("status")
    if str(status_code) != "200":
        raise SmsUnavailable(reply.get("message", str(reply))[:200])

    message_id = reply.get("message_id", "")
    return str(message_id), "pending"


def drain_once(limit=20):
    """
    Send whatever is waiting. Returns (sent, failed, skipped).

    Skipped is not failed. With no API token there is nothing wrong with the
    message, so it stays queued at its current attempt count and goes out
    when one is configured.
    """
    from .models import SmsMessage

    if not settings.SMS_ENABLED:
        return 0, 0, 0

    pending = list(
        SmsMessage.objects.filter(
            status=SmsMessage.Status.QUEUED,
            attempts__lt=settings.SMS_MAX_ATTEMPTS,
        ).order_by("created_at")[:limit]
    )
    if not pending:
        return 0, 0, 0

    sent = failed = skipped = 0
    for message in pending:
        try:
            provider_id, provider_status = send_now(message)
        except SmsUnavailable as exc:
            reason = str(exc)
            if "no API token" in reason:
                skipped += 1
                logger.debug("SMS held: %s", reason)
                continue

            message.attempts += 1
            message.error = reason[:200]
            if message.attempts >= settings.SMS_MAX_ATTEMPTS:
                message.status = SmsMessage.Status.FAILED
                failed += 1
            message.save(update_fields=["attempts", "error", "status"])
            logger.warning("SMS to %s failed: %s", message.to_number, reason)
            continue

        message.status = SmsMessage.Status.SENT
        message.provider_message_id = provider_id
        message.provider_status = provider_status
        message.sent_at = timezone.now()
        message.attempts += 1
        message.error = ""
        message.save(
            update_fields=["status", "provider_message_id", "provider_status",
                           "sent_at", "attempts", "error"]
        )
        sent += 1
        logger.info("SMS handed to gateway for %s (id %s)", message.to_number, provider_id)

    return sent, failed, skipped


def poll_delivery(limit=40):
    """
    Ask the gateway what actually happened to messages it accepted.

    Only messages still at SENT, and only for a few hours. A message the
    gateway has not resolved by then is not going to, and asking forever
    would grow into a request per old message per minute.
    """
    from .models import SmsMessage

    if not (settings.SMS_ENABLED and settings.SMS_TRACK_DELIVERY):
        return 0
    if not settings.IPROGSMS_API_TOKEN:
        return 0

    cutoff = timezone.now() - timezone.timedelta(hours=settings.SMS_DELIVERY_MAX_AGE_HOURS)
    waiting = list(
        SmsMessage.objects.filter(
            status=SmsMessage.Status.SENT,
            sent_at__gte=cutoff,
        ).exclude(provider_message_id="").order_by("sent_at")[:limit]
    )
    if not waiting:
        return 0

    updated = 0
    for message in waiting:
        try:
            reply = _get(
                "sms_messages/status",
                {"api_token": settings.IPROGSMS_API_TOKEN,
                 "message_id": message.provider_message_id},
            )
        except SmsUnavailable as exc:
            logger.debug("Delivery lookup failed for %s: %s", message.provider_message_id, exc)
            continue

        raw = str(reply.get("message_status", "")).strip()
        mapped = PROVIDER_STATUS.get(raw.lower())

        message.provider_status = raw
        if mapped == "delivered":
            message.status = SmsMessage.Status.DELIVERED
            message.delivered_at = timezone.now()
            updated += 1
        elif mapped == "undelivered":
            message.status = SmsMessage.Status.UNDELIVERED
            message.error = f"gateway reported: {raw}"[:200]
            updated += 1
        message.save(
            update_fields=["status", "provider_status", "delivered_at", "error"]
        )

    return updated


def start_worker():
    """
    Begin draining the queue in the background. Safe to call more than once.

    Started from runserver rather than from app startup, so management
    commands, migrations and tests never open network connections as a side
    effect of importing the app.
    """
    global _worker

    if _worker is not None and _worker.is_alive():
        return False
    if not settings.SMS_ENABLED:
        return False

    def loop():
        last_poll = 0.0
        while True:
            try:
                drain_once()

                now = time.monotonic()
                if now - last_poll >= settings.SMS_DELIVERY_POLL_SECONDS:
                    last_poll = now
                    poll_delivery()
            except Exception:
                logger.exception("SMS worker hit an unexpected error; continuing")
            time.sleep(settings.SMS_POLL_SECONDS)

    _worker = threading.Thread(target=loop, name="sms-worker", daemon=True)
    _worker.start()
    return True
