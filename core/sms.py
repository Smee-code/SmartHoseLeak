"""
Sending SMS through a SIM800L plugged into this machine.

Why the server and not the ESP32
--------------------------------
The board has its own GSM module, but it only knows module numbers and bar
readings. The household database lives here, so this is the only place that
can say "HH-003, Ana Reyes, 0917..." and write a sentence a person can act
on. The board's module stays as a last resort for when it cannot reach the
server at all.

Why queued and not inline
-------------------------
A SIM800L takes several seconds per message: it has to register on the
network, accept the number, accept the body, and wait for the network to
acknowledge. Doing that inside a request would make the facilitator watch a
spinner while a modem negotiates, and lose the message entirely if they got
bored and hit reload. Messages are written to the database first and drained
by a worker, so nothing is lost and nothing blocks.

Everything here is written to survive the module simply not being there. It
is a loose USB device on a shared desk, and the realistic states are "not
plugged in", "plugged into a different port than last time" and "no signal".
None of those may raise into a web request.
"""

import logging
import threading
import time

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# The modem is a single piece of hardware. Two threads issuing AT commands
# into the same port interleave their replies and both get nonsense back, so
# every conversation with it happens under this lock.
_port_lock = threading.Lock()

_worker = None


class SmsUnavailable(Exception):
    """The modem could not be reached. Expected, and always recoverable."""


def _serial_module():
    """
    pyserial, or None.

    Imported here rather than at module scope so that a checkout without it
    installed still runs the site - every other page works fine without a
    modem, and refusing to start over an optional dependency would be a poor
    trade.
    """
    try:
        import serial  # noqa: PLC0415 - optional dependency, see docstring
        return serial
    except ImportError:
        return None


def find_port():
    """
    Which serial port the modem is on.

    A configured port wins. Otherwise look for one: a USB-serial adapter
    lands on whatever COM number Windows has free that day, so the port that
    worked last week is not necessarily the port that works now. Bluetooth
    virtual ports are skipped because every Windows laptop has several and
    none of them is a modem.
    """
    configured = (settings.SMS_SERIAL_PORT or "").strip()
    if configured:
        return configured

    serial = _serial_module()
    if serial is None:
        return None

    from serial.tools import list_ports

    for port in list_ports.comports():
        description = (port.description or "").lower()
        if "bluetooth" in description:
            continue
        return port.device
    return None


def describe_gateway():
    """
    One honest sentence about whether a text can be sent right now.

    Shown on the Notices page so a facilitator knows before they write
    anything, rather than after. The states are deliberately distinct:
    switched off is a decision, no library or no port is a setup problem.
    """
    if not settings.SMS_ENABLED:
        return False, "SMS is switched off in settings (SMS_ENABLED)."

    if _serial_module() is None:
        return False, (
            "The pyserial library is not installed, so the GSM module cannot be "
            "reached. Run: pip install pyserial"
        )

    port = find_port()
    if not port:
        return False, (
            "No GSM module found. Plug the SIM800L into this computer, or set "
            "SMS_SERIAL_PORT in .env to its COM port."
        )

    return True, f"GSM module on {port}."


def _read_until(connection, terminators, timeout_s):
    """
    Read replies until the modem says something conclusive.

    A fixed sleep would be wrong in both directions: AT answers instantly,
    while the network can take twenty seconds to acknowledge a message. This
    waits for an actual answer and gives up only when there is none.
    """
    deadline = time.monotonic() + timeout_s
    buffer = ""
    while time.monotonic() < deadline:
        waiting = connection.in_waiting
        if waiting:
            buffer += connection.read(waiting).decode("utf-8", errors="replace")
            if any(term in buffer for term in terminators):
                return buffer
        else:
            time.sleep(0.05)
    return buffer


def _command(connection, text, terminators=("OK", "ERROR"), timeout_s=5):
    connection.write((text + "\r\n").encode())
    reply = _read_until(connection, terminators, timeout_s)
    if "ERROR" in reply:
        raise SmsUnavailable(f"{text} -> {reply.strip().splitlines()[-1][:80]}")
    return reply


def send_now(to_number, body):
    """
    Put one message on the air. Blocking; call it from the worker.

    Raises SmsUnavailable for anything recoverable - unplugged, no signal,
    not registered - so the caller can leave the message queued and try
    again rather than discarding it.
    """
    serial = _serial_module()
    if serial is None:
        raise SmsUnavailable("pyserial is not installed")

    port = find_port()
    if not port:
        raise SmsUnavailable("no GSM module found")

    with _port_lock:
        try:
            connection = serial.Serial(port, settings.SMS_BAUD, timeout=1)
        except Exception as exc:  # pyserial raises several unrelated types
            raise SmsUnavailable(f"cannot open {port}: {exc}") from exc

        try:
            # The module echoes whatever was left in its buffer from the last
            # conversation, which would otherwise be read as this one's reply.
            connection.reset_input_buffer()
            time.sleep(0.2)

            _command(connection, "AT")
            _command(connection, "AT+CMGF=1")  # text mode, not PDU

            # Registration, checked before sending rather than after. A module
            # with no SIM or no signal accepts the number and the body quite
            # happily and only fails at the very end, 20 seconds later.
            status = _command(connection, "AT+CREG?", terminators=("OK", "ERROR"))
            if ",1" not in status and ",5" not in status:  # 1 = home, 5 = roaming
                raise SmsUnavailable("not registered on the network - check SIM and signal")

            connection.write(f'AT+CMGS="{to_number}"\r'.encode())
            prompt = _read_until(connection, (">", "ERROR"), timeout_s=5)
            if ">" not in prompt:
                raise SmsUnavailable("modem did not accept the number")

            connection.write(body.encode("ascii", errors="replace"))
            connection.write(bytes([26]))  # Ctrl-Z ends the message

            # The network, not the modem, decides how long this takes.
            reply = _read_until(connection, ("+CMGS:", "OK", "ERROR"), timeout_s=30)
            if "ERROR" in reply or "+CMGS:" not in reply:
                raise SmsUnavailable("the network rejected the message")
        finally:
            connection.close()


def drain_once(limit=20):
    """
    Send whatever is waiting. Returns (sent, failed, skipped).

    Skipped is not failed: if the modem is unplugged there is nothing wrong
    with the message, so it stays queued at its current attempt count and
    goes out when the hardware comes back. Only the network actually
    refusing a message counts as an attempt.
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
            send_now(message.to_number, message.body)
        except SmsUnavailable as exc:
            reason = str(exc)
            # No hardware is a site problem, not a message problem. Charging
            # an attempt for it would burn through the retry budget of every
            # queued message while the modem sat in somebody's bag.
            if "no GSM module" in reason or "not installed" in reason or "cannot open" in reason:
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
        message.sent_at = timezone.now()
        message.attempts += 1
        message.error = ""
        message.save(update_fields=["status", "sent_at", "attempts", "error"])
        sent += 1
        logger.info("SMS sent to %s", message.to_number)

    return sent, failed, skipped


def start_worker():
    """
    Begin draining the queue in the background. Safe to call more than once.

    Started from runserver rather than from app startup, so management
    commands, migrations and tests never open a serial port as a side effect
    of importing the app.
    """
    global _worker

    if _worker is not None and _worker.is_alive():
        return False
    if not settings.SMS_ENABLED:
        return False

    def loop():
        while True:
            try:
                drain_once()
            except Exception:  # a worker thread that dies stops all SMS silently
                logger.exception("SMS worker hit an unexpected error; continuing")
            time.sleep(settings.SMS_POLL_SECONDS)

    _worker = threading.Thread(target=loop, name="sms-worker", daemon=True)
    _worker.start()
    return True
