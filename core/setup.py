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
from django.utils import translation
from django.utils.translation import gettext as _

from . import mail
from .models import AuditLog, SmsMessage


def _ago(when, now):
    seconds = int((now - when).total_seconds())
    if seconds < 90:
        return _("{seconds} seconds ago").format(seconds=max(seconds, 1))
    minutes = seconds // 60
    if minutes < 90:
        return _("{minutes} minutes ago").format(minutes=minutes)
    hours = minutes // 60
    if hours < 36:
        return _("{hours} hours ago").format(hours=hours)
    return _("{days} days ago").format(days=hours // 24)


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
            "no_board", _("No board fitted yet"), "muted",
            _("The water system facilitator has not fitted a monitoring board to your line yet. "
            "There is nothing for you to do until they have."),
        )

    common = {"device_id": device.device_id}
    if device.last_seen:
        silent = (now - device.last_seen).total_seconds()
        common["last_seen"] = _ago(device.last_seen, now)
        if silent < settings.OWNER_DEVICE_GRACE_SECONDS:
            return _state(
                "connected", _("Connected"), "ok",
                _("Your board is sending readings. Last heard {last_seen}.").format(last_seen=common['last_seen']),
                connected=True, **common,
            )
        return _state(
            "lost", _("Problem: connection lost"), "danger",
            _("Last heard {last_seen}. Check that the board has power and that your "
              "WiFi is working; after a power cut it reconnects on its own. If your "
              "WiFi name or password changed, the board needs to be told - contact "
              "the water system facilitator.").format(last_seen=common['last_seen']),
            **common,
        )

    if household.is_pending:
        return _state(
            "awaiting_activation", _("Board fitted, awaiting activation"), "muted",
            _("The facilitator activates the household; the setup code is sent then."), **common,
        )

    ticket = getattr(device, "claim_code", None)
    if ticket is not None and ticket.is_used:
        waited = (now - ticket.used_at).total_seconds()
        if waited < settings.OWNER_DEVICE_CONNECTING_SECONDS:
            return _state(
                "connecting", _("Connecting…"), "warn",
                _("The board accepted the code {ago}. It should start sending readings "
                  "within a minute or two. This page updates by itself.").format(ago=_ago(ticket.used_at, now)),
                **common,
            )
        return _state(
            "claimed_silent", _("Problem: not sending readings"), "danger",
            _("The board took the code {ago} but no readings have arrived. Make sure "
              "your WiFi has internet and the board has power. You can get a new "
              "code and set it up again; if it stays like this, contact the water "
              "system facilitator.").format(ago=_ago(ticket.used_at, now)),
            can_reissue=True, **common,
        )

    if ticket is not None and not ticket.is_expired:
        return _state(
            "ready", _("Ready to set up"), "warn",
            _("Follow the steps below with the board powered on. The code works once."),
            code=ticket.code, code_expires=timezone.localtime(ticket.expires_at).strftime("%b %d, %Y"),
            can_resend=True, show_steps=True, **common,
        )
    if ticket is not None:
        return _state(
            "expired", _("Setup code expired"), "danger",
            _("Setup codes last 14 days. Get a new one below - it is sent to you by text and email "
            "and shown here."),
            can_reissue=True, **common,
        )
    return _state(
        "no_code", _("No setup code yet"), "warn",
        _("Get a setup code below to connect your board. It is sent to you by text and email and "
        "shown here."),
        can_reissue=True, **common,
    )


def setup_text(household, ticket, site):
    """The short version: one SMS part (153 characters) with the default hotspot names."""
    return (
        _("Smart Hose Leak setup code: {code}. Plug in the board, join its WiFi "
          "{board_setup_ssid} (pw {board_setup_password}), enter your home WiFi "
          "and this code.").format(code=ticket.code, board_setup_ssid=settings.BOARD_SETUP_SSID, board_setup_password=settings.BOARD_SETUP_PASSWORD)
    )


def setup_email(household, ticket, site):
    return (
        _("Your household ({household_id}) has a leak-monitoring board waiting "
          "to be set up.\n\nSetup code: {code}\nExpires: {expires}\n\n1. Plug in the board "
          "and wait about a minute.\n2. On your phone, join the WiFi network "
          "'{board_setup_ssid}' (password: {board_setup_password}).\n3. A setup "
          "page opens by itself. If it does not, open http://192.168.4.1/ in "
          "your browser.\n4. Choose your home WiFi, type its password, and enter "
          "the setup code above. Leave the server address as it is.\n5. Press "
          "Save. The board restarts and joins your WiFi; the setup network "
          "disappears.\n\nSign in at {site}, open Settings > My device, and watch "
          "it change to Connected. The code works once. If it expires or "
          "something goes wrong, you can get a new one there.").format(household_id=household.household_id, code=ticket.code, expires=timezone.localtime(ticket.expires_at).strftime('%B %d, %Y'), board_setup_ssid=settings.BOARD_SETUP_SSID, board_setup_password=settings.BOARD_SETUP_PASSWORD, site=site)
    )


def send_setup_code(request, household, ticket, sent_by=None):
    """
    Put the setup code in front of the owner by text and by email, whichever
    the household has. Returns the channels that took it, for the message.
    """
    site = request.build_absolute_uri("/").rstrip("/")
    sent = []
    # The code goes out in the household's language; the summary of where
    # it went is for whoever pressed the button, in theirs.
    with translation.override(household.language or settings.LANGUAGE_CODE):
        text = setup_text(household, ticket, site)
        subject, email_body = _("Set up your Smart Hose Leak board"), setup_email(household, ticket, site)
    if (household.contact_number or "").strip():
        parts = SmsMessage.queue(household, text, kind=SmsMessage.Kind.MANUAL, sent_by=sent_by)
        if parts:
            sent.append(_("text to {contact_number}").format(contact_number=household.contact_number))
    address = (household.email or "").strip()
    if address and mail.send_email(address, subject, email_body):
        sent.append(_("email to {address}").format(address=address))
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
            _("Board {device_id} is now connected for {who}{owner}. Readings are "
              "arriving.").format(device_id=device.device_id, who=who, owner=owner),
            name=faci.get_full_name() or faci.username,
            kind=SmsMessage.Kind.AUTOMATIC,
        )
