"""
Sending SMS through Semaphore, a Philippine SMS gateway.

Why a gateway and not a modem
-----------------------------
This started as a SIM800L plugged into the machine running Django, chosen
because an unli-text promo costs nothing per message and a modem works with
no internet at all.

Two things killed it. A hosted server has no USB port to plug a modem into,
and pyserial can only open a port on the machine executing the code - so the
modem and the code have to be on the same machine, which rules out hosting
entirely. A gateway works from a laptop and from a data centre alike, so one
build serves the prototype and the deployment with nothing to migrate.

What that gives up is SMS during an internet outage, which is exactly when it
matters most. The answer to that is the SIM800L on the BOARD, which texts the
owner directly when it cannot reach the server - a different module doing a
different job.

Why queued and not sent inline
------------------------------
A gateway call is a network round trip that can hang. Doing it inside a
request would make a facilitator watch a spinner while an API is slow, and
lose the message entirely if they got bored and hit reload. Messages are
written to the database first and drained by a worker.

Sent is not delivered
---------------------
The gateway accepting a message means it has taken responsibility for it, not
that a phone received it. Semaphore knows which happened but only says if
asked, so a second pass asks. An alert log that cannot tell those apart
cannot answer the one question anyone puts to it.
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

# Semaphore's own vocabulary, mapped onto ours. Their "Sent" means the message
# reached the handset; "Pending" and "Queued" mean they have it and are
# working on it. Keeping their word verbatim in provider_status as well, so a
# vocabulary change on their side is visible rather than silently swallowed.
PROVIDER_STATUS = {
    "pending": "sent",
    "queued": "sent",
    "sent": "delivered",
    "delivered": "delivered",
    "failed": "undelivered",
    "refunded": "undelivered",
}


class SmsUnavailable(Exception):
    """The message could not be handed over. Usually recoverable."""


def _post(path, fields):
    """
    One form-encoded POST to the gateway.

    Errors are turned into SmsUnavailable with the gateway's own words where
    it gave any: "insufficient credit" and "sender name not registered" are
    both things only the gateway can tell us, and both are fixed by a person
    rather than by retrying.
    """
    url = f"{settings.SEMAPHORE_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    data = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(url, data=data, method="POST")

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
    url = f"{settings.SEMAPHORE_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    url = f"{url}?{urllib.parse.urlencode(params)}"

    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_S) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        raise SmsUnavailable(f"gateway refused it ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise SmsUnavailable(f"cannot reach the gateway: {exc.reason}") from exc
    except (ValueError, TimeoutError) as exc:
        raise SmsUnavailable(f"gateway gave an unreadable answer: {exc}") from exc


# The account lookup is cached because this runs on every render of the
# Notices page, and Semaphore rate-limits it hard - hard enough that two
# lookups a minute apart were refused during testing.
#
# A balance does not move between page loads, and being told "too many
# attempts" instead of a number is worse than a number five minutes old. The
# cost of the staleness is that a top-up takes up to this long to show.
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

    if not settings.SEMAPHORE_API_KEY:
        return False, (
            "No Semaphore API key set. Add SEMAPHORE_API_KEY to your .env file - "
            "sign up at semaphore.co to get one."
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
        account = _get("account", {"apikey": settings.SEMAPHORE_API_KEY})
    except SmsUnavailable as exc:
        detail = str(exc)
        # Being rate-limited on the BALANCE lookup says nothing about whether
        # a message can be sent. Reporting it as a failure would tell a
        # facilitator that SMS is down when it is working perfectly.
        if "429" in detail or "Too Many Attempts" in detail:
            return True, "Texts are going out. The credit check is rate-limited just now."
        return False, f"The SMS gateway is set up but not answering: {detail}"

    if isinstance(account, list):
        account = account[0] if account else {}

    balance = account.get("credit_balance", account.get("account_balance"))
    if balance is None:
        return True, "Texts are going out."

    # The account name was in this sentence and has been taken out. It is the
    # name on the Semaphore billing account - here it printed as "Zynergyx
    # Technologies", which tells a barangay facilitator nothing and read as
    # though some third party were involved.
    try:
        credit = float(balance)
    except (TypeError, ValueError):
        return True, "Texts are going out."

    # No credit is not a warning, it is a stop: every send is refused with
    # "402: insufficient credit". This used to report itself as a healthy
    # gateway - a green bar reading "LOW CREDIT: 0 left - top up soon" - so
    # the one state where nothing can possibly be sent was the state that
    # looked most reassuring. Messages still queue and still go out after a
    # top-up, which is what the banner says next.
    if credit <= 0:
        return False, "There is no SMS credit left, so no text can be sent."
    if credit < 20:
        # A real warning: it still works, and the fix is to top up.
        return True, f"Texts are going out, but credit is low - {credit:g} left."
    return True, f"Texts are going out. Credit: {credit:g}."


def normalise_number(raw):
    """
    Put a Philippine number into the form the gateway expects.

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
    if not settings.SEMAPHORE_API_KEY:
        raise SmsUnavailable("no API key set")

    number = normalise_number(message.to_number)
    if len(number) != 12:
        # Not recoverable by retrying, and worth saying precisely: a wrong
        # number in a household record otherwise looks exactly like a
        # successful send.
        raise SmsUnavailable(f"'{message.to_number}' is not a valid PH mobile number")

    fields = {
        "apikey": settings.SEMAPHORE_API_KEY,
        "number": number,
        "message": message.body,
    }
    if settings.SEMAPHORE_SENDER_NAME:
        fields["sendername"] = settings.SEMAPHORE_SENDER_NAME

    reply = _post("messages", fields)

    # The gateway answers with a list, one entry per message it created.
    if isinstance(reply, dict):
        # An error body rather than a result. Its own words are the useful
        # part - "insufficient credit", "sender name not registered".
        if "message_id" not in reply:
            raise SmsUnavailable(str(reply)[:200])
        reply = [reply]
    if not reply:
        raise SmsUnavailable("gateway accepted nothing and said nothing")

    first = reply[0]
    return str(first.get("message_id", "")), str(first.get("status", ""))


def drain_once(limit=20):
    """
    Send whatever is waiting. Returns (sent, failed, skipped).

    Skipped is not failed. With no API key there is nothing wrong with the
    message, so it stays queued at its current attempt count and goes out
    when one is configured - otherwise a missing key would burn through the
    retry budget of everything queued behind it.
    """
    from .models import SmsMessage  # local: avoids an import cycle at startup

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
            if "no API key" in reason:
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

    Only messages still at SENT, and only for a few hours. One the gateway
    has not resolved by then is not going to, and asking forever would grow
    into a request per old message per minute.
    """
    from .models import SmsMessage

    if not (settings.SMS_ENABLED and settings.SMS_TRACK_DELIVERY):
        return 0
    if not settings.SEMAPHORE_API_KEY:
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
                f"messages/{message.provider_message_id}",
                {"apikey": settings.SEMAPHORE_API_KEY},
            )
        except SmsUnavailable as exc:
            # The message is fine; the lookup failed. Leave it to be asked
            # about again rather than recording a verdict we do not have.
            logger.debug("Delivery lookup failed for %s: %s", message.provider_message_id, exc)
            continue

        if isinstance(reply, list):
            reply = reply[0] if reply else {}
        raw = str(reply.get("status", "")).strip()
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

                # Delivery is checked on its own, slower clock: it is a
                # request per outstanding message, and nothing is waiting on
                # the answer the way a person waits on a send.
                now = time.monotonic()
                if now - last_poll >= settings.SMS_DELIVERY_POLL_SECONDS:
                    last_poll = now
                    poll_delivery()
            except Exception:  # a worker thread that dies stops all SMS silently
                logger.exception("SMS worker hit an unexpected error; continuing")
            time.sleep(settings.SMS_POLL_SECONDS)

    _worker = threading.Thread(target=loop, name="sms-worker", daemon=True)
    _worker.start()
    return True
