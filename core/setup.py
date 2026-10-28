"""
Owner-led board setup: where a household's board is in its life, in words
the owner can act on, and the messages that carry the setup code to them.

The server only ever sees what a board tells it. It cannot see a board that
failed to join the WiFi, so everything between "code sent" and "readings
arriving" is inferred from silence over time - which is why the states here
carry a note saying what to do, not just a word.
"""

from django.conf import settings
from django.utils import timezone

from . import mail
from .models import AuditLog, SmsMessage


def _ago(when, now):
    seconds = int((now - when).total_seconds())
    if seconds < 90:
        return f"{max(seconds, 1)} seconds ago"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes} minutes ago"
    hours = minutes // 60
    if hours < 36:
        return f"{hours} hours ago"
    return f"{hours // 24} days ago"


def _state(key, label, tone, note, **extra):
    base = {
        "key": key, "label": label, "tone": tone, "note": note,
        "device_id": "", "code": "", "code_expires": "", "last_seen": "",
        "can_reissue": False, "can_resend": False, "show_steps": False, "connected": False,
    }
    base.update(extra)
    return base


def device_setup(household, now=None):
    """
    Where this household's board is, as one of a handful of named states.

    Returned as a plain dict so the same answer renders on the owner's
    Settings page, is polled as JSON, and sits in the facilitator's and the
    administrator's lists without three versions of the rules.
    """
    now = now or timezone.now()
    segment = household.segment
    device = segment.device if segment is not None else None
    if device is None:
        return _state(
            "no_board", "No board fitted yet", "muted",
            "The water system facilitator has not fitted a monitoring board to your line yet. "
            "There is nothing for you to do until they have.",
        )

    common = {"device_id": device.device_id}
    if device.last_seen:
        silent = (now - device.last_seen).total_seconds()
        common["last_seen"] = _ago(device.last_seen, now)
        if silent < settings.OWNER_DEVICE_GRACE_SECONDS:
            return _state(
                "connected", "Connected", "ok",
                f"Your board is sending readings. Last heard {common['last_seen']}.",
                connected=True, **common,
            )
        return _state(
            "lost", "Problem: connection lost", "danger",
            f"Last heard {common['last_seen']}. Check that the board has power and that your WiFi is "
            "working; after a power cut it reconnects on its own. If your WiFi name or password "
            "changed, the board needs to be told - contact the water system facilitator.",
            **common,
        )

    if household.is_pending:
        return _state(
            "awaiting_activation", "Board fitted, awaiting activation", "muted",
            "The facilitator activates the household; the setup code is sent then.", **common,
        )

    ticket = getattr(device, "claim_code", None)
    if ticket is not None and ticket.is_used:
        waited = (now - ticket.used_at).total_seconds()
        if waited < settings.OWNER_DEVICE_CONNECTING_SECONDS:
            return _state(
                "connecting", "Connecting…", "warn",
                f"The board accepted the code {_ago(ticket.used_at, now)}. It should start sending "
                "readings within a minute or two. This page updates by itself.",
                **common,
            )
        return _state(
            "claimed_silent", "Problem: not sending readings", "danger",
            f"The board took the code {_ago(ticket.used_at, now)} but no readings have arrived. "
            "Make sure your WiFi has internet and the board has power. You can get a new code and "
            "set it up again; if it stays like this, contact the water system facilitator.",
            can_reissue=True, **common,
        )

    if ticket is not None and not ticket.is_expired:
        return _state(
            "ready", "Ready to set up", "warn",
            "Follow the steps below with the board powered on. The code works once.",
            code=ticket.code, code_expires=timezone.localtime(ticket.expires_at).strftime("%b %d, %Y"),
            can_resend=True, show_steps=True, **common,
        )
    if ticket is not None:
        return _state(
            "expired", "Setup code expired", "danger",
            "Setup codes last 14 days. Get a new one below - it is sent to you by text and email "
            "and shown here.",
            can_reissue=True, **common,
        )
    return _state(
        "no_code", "No setup code yet", "warn",
        "Get a setup code below to connect your board. It is sent to you by text and email and "
        "shown here.",
        can_reissue=True, **common,
    )


def setup_text(household, ticket, site):
    """The short version: one SMS part (153 characters) with the default hotspot names."""
    return (
        f"Smart Hose Leak setup code: {ticket.code}. Plug in the board, join its WiFi "
        f"{settings.BOARD_SETUP_SSID} (pw {settings.BOARD_SETUP_PASSWORD}), enter your home WiFi and this code."
    )


def setup_email(household, ticket, site):
    return (
        f"Your household ({household.household_id}) has a leak-monitoring board waiting to be set up.\n\n"
        f"Setup code: {ticket.code}\n"
        f"Expires: {timezone.localtime(ticket.expires_at).strftime('%B %d, %Y')}\n\n"
        f"1. Plug in the board and wait about a minute.\n"
        f"2. On your phone, join the WiFi network '{settings.BOARD_SETUP_SSID}' "
        f"(password: {settings.BOARD_SETUP_PASSWORD}).\n"
        f"3. A setup page opens by itself. If it does not, open http://192.168.4.1/ in your browser.\n"
        f"4. Choose your home WiFi, type its password, and enter the setup code above. Leave the "
        f"server address as it is.\n"
        f"5. Press Save. The board restarts and joins your WiFi; the setup network disappears.\n\n"
        f"Sign in at {site}, open Settings > My device, and watch it change to Connected. "
        f"The code works once. If it expires or something goes wrong, you can get a new one there."
    )


def send_setup_code(request, household, ticket, sent_by=None):
    """
    Put the setup code in front of the owner by text and by email, whichever
    the household has. Returns the channels that took it, for the message.
    """
    site = request.build_absolute_uri("/").rstrip("/")
    sent = []
    if (household.contact_number or "").strip():
        parts = SmsMessage.queue(
            household, setup_text(household, ticket, site),
            kind=SmsMessage.Kind.MANUAL, sent_by=sent_by,
        )
        if parts:
            sent.append(f"text to {household.contact_number}")
    address = (household.email or "").strip()
    if address and mail.send_email(address, "Set up your Smart Hose Leak board", setup_email(household, ticket, site)):
        sent.append(f"email to {address}")
    return sent


def announce_first_connection(device):
    """
    A board has sent its first reading. Tell the facilitator, who otherwise
    only finds out by noticing a tile turn green, and write it down.
    """
    from django.contrib.auth import get_user_model

    segment = device.line
    household = getattr(segment, "household", None) if segment is not None else None
    who = household.household_id if household else (segment.name if segment else device.device_id)
    owner = f" ({household.owner_name})" if household else ""
    AuditLog.record(None, "board.connected", f"Board {device.device_id} sent its first reading for {who}{owner}", target=device.device_id)

    User = get_user_model()
    facilitators = User.objects.filter(role=User.Role.FACILITATOR, is_active=True).exclude(contact_number="")
    if segment is not None and segment.barangay_id:
        facilitators = facilitators.filter(barangay_id=segment.barangay_id)
    for faci in facilitators:
        SmsMessage.queue_to_number(
            faci.contact_number,
            f"Board {device.device_id} is now connected for {who}{owner}. Readings are arriving.",
            name=faci.get_full_name() or faci.username,
            kind=SmsMessage.Kind.AUTOMATIC,
        )
