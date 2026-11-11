"""
JSON endpoints.

/api/ingest/   - the ESP32 posts here every upload interval (device-key auth)
/api/commands/ - the ESP32 asks here for a pending valve command, every couple
                 of seconds while somebody is at a valve screen (device-key auth)
/api/live/     - dashboards poll here for current values (session auth)
/api/history/ - chart data from the minute rollups (session auth)
"""

import json
import logging
import math

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Q
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from django.utils import translation
from django.utils.translation import gettext as _

from .models import (
    ClaimCode,
    Device,
    DeviceSeverity,
    Household,
    LatestReading,
    LeakEvent,
    Notification,
    PushSubscription,
    ReadingRollup,
    Segment,
    SmsMessage,
    SystemSetting,
    Trigger,
    ValveReason,
    ValveState,
    describe_valve,
    offline_after_seconds,
    psi,
)
from . import mail, push

logger = logging.getLogger(__name__)

# The device reports these three; MAINTENANCE and OFFLINE are set server-side.
DEVICE_STATUSES = {
    "NORMAL": Segment.Status.NORMAL,
    "LOW_SUPPLY": Segment.Status.LOW_SUPPLY,
    "LOW WATER SUPPLY": Segment.Status.LOW_SUPPLY,
    "WATER_RUNNING": Segment.Status.WATER_RUNNING,
    "WATER RUNNING": Segment.Status.WATER_RUNNING,
    "POSSIBLE_LEAK": Segment.Status.POSSIBLE_LEAK,
    "POSSIBLE LEAK": Segment.Status.POSSIBLE_LEAK,
}

SEVERITY_BY_STATUS = {
    Segment.Status.POSSIBLE_LEAK: LeakEvent.Severity.CRITICAL,
    Segment.Status.WATER_RUNNING: LeakEvent.Severity.WARNING,
    Segment.Status.LOW_SUPPLY: LeakEvent.Severity.WARNING,
    Segment.Status.OFFLINE: LeakEvent.Severity.WARNING,
    Segment.Status.MAINTENANCE: LeakEvent.Severity.INFO,
    Segment.Status.NORMAL: LeakEvent.Severity.INFO,
}


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _authenticate_device(request):
    """Device auth is a shared key in a header. Body key is a fallback."""
    key = request.headers.get("X-Device-Key", "").strip()
    if not key:
        return None
    return Device.objects.filter(api_key=key).first()


def _choice_or_default(raw, choices, default):
    """
    Accept a device's string only if it is one this server understands.

    Firmware and server are flashed and deployed separately, so a board can
    legitimately be ahead of or behind the server. Storing an unrecognised
    value would put a string in the database that no template can render;
    falling back keeps the reading usable and loses only the extra detail.
    """
    if not raw:
        return default
    value = str(raw).strip().lower()
    return value if value in choices.values else default


def _push_body(event, in_app_body):
    """
    The same news, cut to what fits on a lock screen.

    A notification is read in a glance with the phone face-down on a table.
    The in-app body carries the reading and the full instruction because it is
    read inside the app with the pressure chart underneath it; here the
    instruction alone is the useful half, same reasoning as the SMS.
    """
    if event.status == Segment.Status.POSSIBLE_LEAK:
        return (
            _("Check that your faucets are fully closed. If they are and water "
            "is still running, tell the facilitator.")
        )
    if event.status == Segment.Status.WATER_RUNNING:
        return (
            _("Water has been running non-stop on your line. Turn off any tap or "
            "hose; if all are closed, tell the facilitator.")
        )
    if event.status == Segment.Status.LOW_SUPPLY:
        return _("Water may be slow at your tap. The source is probably low.")
    if event.status == Segment.Status.NORMAL:
        return _("The alert on your line has been cleared.")
    # Nothing else raises a notice, but a sentence is better than an empty
    # notification if that ever changes.
    return in_app_body.split("\n\n")[0]


def _household_notice(event, when):
    """
    The in-app notice for a status change, in the active language: (title,
    body), or None when this status raises nothing.

    Written to the person who lives there, not about them. Lines are named
    after their household now, so "Possible leak on HH-001 - Maria Santos"
    told Maria her own name twice and located the problem at the only
    address she already knew. To her it is simply her line.
    """
    if event.status == Segment.Status.POSSIBLE_LEAK:
        return (
            _("Possible leak on your water line"),
            _("A possible leak was detected on your line on {when}. Please check "
              "that all your faucets are fully closed. If they are and water is "
              "still running, tell the water system facilitator.\n\nReading at the "
              "time: {pressure_bar:.2f} bar, {flow_lpm:.2f} L/min.").format(
                when=when, pressure_bar=event.pressure_bar, flow_lpm=event.flow_lpm),
        )
    if event.status == Segment.Status.WATER_RUNNING:
        return (
            _("Water has been running on your line"),
            _("Water has been flowing without stopping on your line since before "
              "{when}. If you left a tap or hose on, please turn it off. If "
              "everything is closed and water is still running, you may have a leak "
              "- tell the water system facilitator.\n\nReading at the time: "
              "{pressure_bar:.2f} bar, {flow_lpm:.2f} L/min.").format(
                when=when, pressure_bar=event.pressure_bar, flow_lpm=event.flow_lpm),
        )
    if event.status == Segment.Status.LOW_SUPPLY:
        return (
            _("Your water is weaker than usual"),
            _("Low pressure was detected on your line on {when}. Water may be slow "
              "at your tap. This usually means the source is low rather than "
              "anything wrong at your house.\n\nReading at the time: "
              "{pressure_bar:.2f} bar.").format(when=when, pressure_bar=event.pressure_bar),
        )
    if event.status == Segment.Status.NORMAL:
        # Only announce a recovery if the household was told about the
        # problem in the first place. A device booting from OFFLINE into
        # NORMAL is not an all-clear anybody was waiting for.
        if event.previous_status not in {
            Segment.Status.POSSIBLE_LEAK,
            Segment.Status.WATER_RUNNING,
            Segment.Status.LOW_SUPPLY,
        }:
            return None
        return (
            _("Your water is back to normal"),
            _("The alert on your line was cleared on {when}. Readings are normal "
              "again.").format(when=when),
        )
    return None


def _notify_households(segment, event):
    """
    Raise the in-app notice for the household whose hose this is.

    One hose per house means one recipient, but the lookup stays a queryset:
    a line can legitimately have no household yet - a board on the bench
    during installation - and that must post no notice rather than fail.

    Everything written to a household is built in THAT household's language
    (translation.override): a board reports in no language, and a text at
    two in the morning has to be readable by whoever picks up the phone.
    """
    when = timezone.localtime(event.detected_at).strftime("%b %d, %Y at %I:%M %p")
    if _household_notice(event, when) is None:
        return

    households = list(Household.objects.filter(
        segment=segment, account_status=Household.AccountStatus.ACTIVE
    ))

    # The alert statuses that go out by text and email, not just in-app: a
    # leak, and water that has been running non-stop. Low supply and the
    # all-clear stay in-app and push only.
    alert_statuses = {Segment.Status.POSSIBLE_LEAK, Segment.Status.WATER_RUNNING}
    texting = settings.SMS_AUTO_ON_LEAK and event.status in alert_statuses

    for household in households:
        with translation.override(household.language or settings.LANGUAGE_CODE):
            title, body = _household_notice(event, when)
            notice = Notification.objects.create(
                kind=Notification.Kind.AUTOMATIC,
                title=title,
                body=body,
                recipient=household,
                segment=segment,
                event=event,
            )

            # Push it to any handset that asked for notifications.
            #
            # Sent for every status that raised a notice, including the
            # all-clear - unlike a text, a push costs nothing, so there is no
            # reason to ration it to the worst news. It is an addition to the
            # SMS below and never a substitute: push reaches only phones that
            # installed the portal and granted permission, and says nothing
            # when one is switched off.
            if settings.PUSH_AUTO_ON_LEAK:
                try:
                    push.send_to_household(household, title, _push_body(event, body))
                except Exception:  # noqa: BLE001
                    # A leak alert must not fail because a push service is
                    # having a bad afternoon. send_to_household already
                    # swallows the ordinary failures; this is the belt to
                    # that braces.
                    logger.exception("Push failed for %s", household.household_id)

            # Email the same notice to anyone with an address on file. In
            # addition to the text, not instead - email reaches a desktop and
            # leaves a record.
            if settings.EMAIL_AUTO_ON_LEAK and event.status in alert_statuses and household.email:
                try:
                    mail.send_email(household.email, title, body)
                except Exception:  # noqa: BLE001
                    logger.exception("Alert email failed for %s", household.household_id)

            # Text them as well, unless that has been switched off.
            #
            # An in-app notice is only seen by somebody who opens the app, and
            # a leak at two in the morning cannot wait for that. The message is
            # written shorter than the in-app one on purpose: a text is read on
            # a lock screen and the useful half is the instruction, not the
            # reading.
            if texting:
                SmsMessage.queue(
                    household,
                    _alert_sms_text(household, event),
                    kind=SmsMessage.Kind.AUTOMATIC,
                    notification=notice,
                    event=event,
                )

    if not texting:
        return

    # Only a confirmed leak is escalated to the facilitator. A water-running
    # warning is the owner's to act on first - they may simply have left a tap
    # on - so it does not pull a crew. A test alert never escalates: it is a
    # rehearsal of the household's path, not a real leak for a crew to chase.
    if event.status == Segment.Status.POSSIBLE_LEAK and not event.is_test:
        _notify_facilitators(segment, event)


def send_test_alert(segment, status, trigger=Trigger.NONE,
                    device_severity=DeviceSeverity.NONE):
    """
    Fire a realistic alert for a line without a board or a real condition.

    An administrator uses this to prove the household is actually reached -
    in-app, by text and by email - for each of the two conditions the system
    watches: water left running, and a pressure drop after the night valve
    test. It builds a genuine event with plausible readings and runs it
    through the same notification path a real detection would, so what the
    household receives is identical to the real thing.

    The event is flagged is_test and recorded already cleared, so it never
    shows as an open alert, never pulls a crew, and is kept out of the leak
    statistics. Nothing on the segment's live status is touched.
    """
    now = timezone.now()

    # Readings chosen to match the condition, because the notice quotes them.
    # Water running: flow present, pressure normal. A pressure-decay leak:
    # the valve is shut so flow is nil and the pressure has fallen.
    if status == Segment.Status.WATER_RUNNING:
        pressure_bar, flow_lpm = 2.0, 9.0
    elif trigger == Trigger.DECAY:
        pressure_bar, flow_lpm = 1.2, 0.0
    else:
        pressure_bar, flow_lpm = 1.8, 9.0

    event = LeakEvent.objects.create(
        segment=segment,
        status=status,
        previous_status=Segment.Status.NORMAL,
        severity=SEVERITY_BY_STATUS.get(status, LeakEvent.Severity.INFO),
        pressure_bar=pressure_bar,
        flow_lpm=flow_lpm,
        device_severity=device_severity,
        trigger=trigger,
        detected_at=now,
        cleared_at=now,
        is_test=True,
    )
    _notify_households(segment, event)
    return event


def _notify_facilitators(segment, event):
    """
    Copy the facilitator on a leak alert.

    They are the one who has to act on it, and hearing about a leak first
    from the household that got the text is a poor way to find out. Their
    message is worded differently on purpose: the owner is asked to check
    their own taps, while the facilitator needs to know which house and how
    bad, so they can decide whether to go.
    """
    if not settings.SMS_FACILITATOR_ON_LEAK:
        return

    User = get_user_model()
    facilitators = User.objects.filter(
        role=User.Role.FACILITATOR
    ).exclude(contact_number="")
    # The facilitator of THIS line's barangay, not every facilitator in the
    # system: a leak in one barangay is not another's to walk to. A line
    # with no barangay yet falls back to everyone rather than to nobody.
    if segment.barangay_id:
        facilitators = facilitators.filter(barangay_id=segment.barangay_id)
    if not facilitators:
        return

    household = getattr(segment, "household", None)
    who = household.household_id if household else segment.name
    owner = f" ({household.owner_name})" if household else ""
    severity = ""
    if event.device_severity and event.device_severity != DeviceSeverity.NONE:
        severity = f", {event.get_device_severity_display().lower()}"

    text = (
        _("LEAK: {who}{owner}{severity}. {pressure_bar:.2f} bar, {flow_lpm:.2f} "
          "L/min. Detected by {get_trigger_display}. The owner has been asked to "
          "check their faucets.").format(who=who, owner=owner, severity=severity, pressure_bar=event.pressure_bar, flow_lpm=event.flow_lpm, get_trigger_display=event.get_trigger_display().lower())
    )

    for facilitator in facilitators:
        SmsMessage.queue_to_number(
            facilitator.contact_number,
            text,
            name=facilitator.get_full_name() or facilitator.username,
            kind=SmsMessage.Kind.AUTOMATIC,
            event=event,
        )


def _alert_sms_text(household, event):
    """One text, chosen by what was detected: a leak, or water left running."""
    if event.status == Segment.Status.WATER_RUNNING:
        when = timezone.localtime(event.detected_at).strftime("%b %d %I:%M%p").replace(" 0", " ")
        return (
            _("WATER RUNNING {household_id}: water has run non-stop since {when}. "
              "Turn off any tap or hose. If all are closed and it still runs, tell "
              "the facilitator.").format(household_id=household.household_id, when=when)
        )
    return _leak_sms_text(household, event)


def _leak_sms_text(household, event):
    """
    The words that arrive on somebody's phone when their line is flagged.

    It asks them to check their own taps first. A small leak and a tap left
    slightly open are identical to the sensors, and the far more common of
    the two is the tap - so sending a crew before asking would waste most
    trips and teach people the alerts are wrong.
    """
    when = timezone.localtime(event.detected_at).strftime("%b %d %I:%M%p").replace(" 0", " ")
    severity = ""
    if event.device_severity and event.device_severity != DeviceSeverity.NONE:
        severity = f", {event.get_device_severity_display().lower()}"

    # Written to fit one message. Two texts cost twice as much, can arrive out
    # of order, and the half that gets separated is the instruction - so the
    # wording is kept tight rather than trusting it to come out short.
    # test_leak_sms_fits_one_message guards this.
    return (
        _("LEAK ALERT {household_id}: possible leak {when}{severity}. Please "
          "check all your faucets are closed. If they are and water still runs, "
          "tell the facilitator.").format(household_id=household.household_id, when=when, severity=severity)
    )


def _apply_status_change(segment, new_status, pressure_bar, flow_lpm,
                         device_severity=DeviceSeverity.NONE, trigger=Trigger.NONE):
    """
    Record a transition as a LeakEvent and close the previous open one.

    Called only when the incoming status actually differs from what is
    already stored, so the event log holds one row per real transition.
    """
    now = timezone.now()
    previous = segment.status

    LeakEvent.objects.filter(segment=segment, cleared_at__isnull=True).update(cleared_at=now)

    event = LeakEvent.objects.create(
        segment=segment,
        status=new_status,
        previous_status=previous,
        severity=SEVERITY_BY_STATUS.get(new_status, LeakEvent.Severity.INFO),
        pressure_bar=pressure_bar,
        flow_lpm=flow_lpm,
        device_severity=device_severity,
        trigger=trigger,
        detected_at=now,
    )
    if new_status == Segment.Status.NORMAL:
        event.cleared_at = now
        event.save(update_fields=["cleared_at"])

    segment.status = new_status
    segment.status_changed_at = now
    segment.save(update_fields=["status", "status_changed_at"])

    _notify_households(segment, event)
    return event


@csrf_exempt
@require_POST
def claim(request):
    """
    Trade a claim code for a device identity.

    The only endpoint that answers without a key, because a blank board has
    no key to answer with - that is the whole point of it. It is kept narrow
    in exchange: a code works once, expires on its own, and yields nothing
    but the identity of the one board it was issued for.

    CSRF is exempt for the same reason as ingest: the caller is a
    microcontroller, not a browser with a session.
    """
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "body is not valid JSON"}, status=400)

    # Accept what a person actually typed into the board's setup page -
    # lower case, missing dashes, stray spaces - rather than making a
    # formatting difference look like a wrong code.
    code = ClaimCode.normalise(payload.get("code"))
    if not code:
        return JsonResponse(
            {"ok": False, "error": "that is not a valid claim code"}, status=400
        )

    # select_for_update so two boards sent the same code cannot both pass the
    # "already used" check before either marks it used.
    with transaction.atomic():
        ticket = (
            ClaimCode.objects.select_for_update()
            .select_related("device")
            .filter(code=code)
            .first()
        )
        if ticket is None:
            return JsonResponse({"ok": False, "error": "unknown claim code"}, status=404)
        if ticket.is_used:
            return JsonResponse(
                {"ok": False, "error": "this code has already been used"}, status=409
            )
        if ticket.is_expired:
            return JsonResponse(
                {"ok": False, "error": "this code has expired - ask for a new one"}, status=410
            )

        ticket.used_at = timezone.now()
        ticket.used_from_ip = _client_ip(request)
        ticket.save(update_fields=["used_at", "used_from_ip"])

    device = ticket.device
    return JsonResponse(
        {
            "ok": True,
            "device_id": device.device_id,
            "api_key": device.api_key,
            "server_time": timezone.now().isoformat(),
        }
    )


@csrf_exempt
@require_POST
def ingest(request):
    """
    Accept one batch of readings from an ESP32.

    Expected body:
        {"device_id": "esp32-01",
         "readings": [{"module": 1, "pressure_bar": 2.14, "flow_lpm": 4.5,
                       "k": 0.32, "status": "NORMAL",
                       "calibrating": false, "simulated": false}, ...]}

    CSRF is exempt because the caller is a microcontroller holding a device
    key, not a browser session.
    """
    device = _authenticate_device(request)
    if device is None:
        return JsonResponse({"ok": False, "error": "invalid or missing device key"}, status=401)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "body is not valid JSON"}, status=400)

    readings = payload.get("readings")
    if not isinstance(readings, list) or not readings:
        return JsonResponse({"ok": False, "error": "readings must be a non-empty list"}, status=400)

    now = timezone.now()
    minute = now.replace(second=0, microsecond=0)
    accepted, events, errors = 0, [], []

    with transaction.atomic():
        segments = {s.module_index: s for s in device.segments.select_for_update()}

        for entry in readings:
            if not isinstance(entry, dict):
                errors.append("reading entry is not an object")
                continue

            module = entry.get("module")
            segment = segments.get(module)
            if segment is None:
                errors.append(f"no segment configured for module {module}")
                continue
            if not segment.is_active:
                continue

            try:
                pressure_bar = float(entry.get("pressure_bar", 0) or 0)
                flow_lpm = float(entry.get("flow_lpm", 0) or 0)
                k_value = float(entry.get("k", 0) or 0)
            except (TypeError, ValueError):
                errors.append(f"module {module}: non-numeric reading")
                continue
            # JSON lets "NaN" and "Infinity" through float(); SQLite stores a
            # NaN as NULL and the row then fails its NOT NULL constraint with
            # a 500. A sensor cannot read either, so they are refused like
            # any other non-number, as is anything far outside what a hose
            # can carry (a disconnected ADC pin reads as garbage, not as a
            # million bar).
            if not all(math.isfinite(v) for v in (pressure_bar, flow_lpm, k_value)) \
                    or abs(pressure_bar) > 100 or abs(flow_lpm) > 10000:
                errors.append(f"module {module}: reading out of range")
                continue

            raw_status = str(entry.get("status", "NORMAL")).upper().strip()
            mapped_status = DEVICE_STATUSES.get(raw_status)
            if mapped_status is None:
                errors.append(f"module {module}: unknown status {raw_status!r}")
                mapped_status = Segment.Status.NORMAL

            # Fields the per-household firmware adds. Unknown or absent values
            # fall back rather than being rejected, so a board still running
            # the three-module sketch keeps reporting while it waits its turn
            # to be reflashed.
            severity = _choice_or_default(
                entry.get("severity"), DeviceSeverity, DeviceSeverity.NONE
            )
            trigger = _choice_or_default(entry.get("trigger"), Trigger, Trigger.NONE)
            valve_state = _choice_or_default(
                entry.get("valve_state"), ValveState, ValveState.NOT_FITTED
            )
            valve_reason = _choice_or_default(
                entry.get("valve_reason"), ValveReason, ValveReason.NONE
            )

            LatestReading.objects.update_or_create(
                segment=segment,
                defaults={
                    "pressure_bar": pressure_bar,
                    "flow_lpm": flow_lpm,
                    "k_conductance": k_value,
                    "device_status": mapped_status,
                    "severity": severity,
                    "trigger": trigger,
                    "valve_state": valve_state,
                    "valve_reason": valve_reason,
                    "calibrating": bool(entry.get("calibrating")),
                    "simulated": bool(entry.get("simulated")),
                    "updated_at": now,
                },
            )

            # The board acknowledges a valve-test request by reporting that
            # the valve has left the open position. Clearing it here rather
            # than when the request was sent means a reply the board never
            # received does not quietly drop the test.
            if segment.valve_test_requested and valve_state in (
                ValveState.CLOSED, ValveState.MOVING
            ):
                segment.valve_test_requested = False
                segment.save(update_fields=["valve_test_requested"])

            # A manual open/close clears once the board reports it reached the
            # commanded position. Until then the command keeps riding out on
            # every config reply, so a close survives a dropped reply or a
            # board reboot and the line ends up where the admin put it.
            if segment.valve_command == Segment.ValveCommand.CLOSE and valve_state == ValveState.CLOSED:
                segment.valve_command = Segment.ValveCommand.NONE
                segment.save(update_fields=["valve_command"])
            elif segment.valve_command == Segment.ValveCommand.OPEN and valve_state == ValveState.OPEN:
                segment.valve_command = Segment.ValveCommand.NONE
                segment.save(update_fields=["valve_command"])

            rollup, _created = ReadingRollup.objects.get_or_create(segment=segment, minute=minute)
            rollup.fold(pressure_bar, flow_lpm, mapped_status == Segment.Status.POSSIBLE_LEAK)
            rollup.save()

            # A segment parked under maintenance by the facilitator keeps that
            # status until they clear it, so field repairs do not spam alerts.
            if segment.status != Segment.Status.MAINTENANCE and segment.status != mapped_status:
                event = _apply_status_change(
                    segment, mapped_status, pressure_bar, flow_lpm, severity, trigger
                )
                events.append({"module": module, "status": event.status, "id": event.id})

            accepted += 1

        first_contact = device.last_seen is None
        device.last_seen = now
        device.last_ip = _client_ip(request)
        device.save(update_fields=["last_seen", "last_ip"])

    if first_contact and accepted:
        # The owner set the board up themselves; the facilitator should not
        # have to notice a tile turn green to know it worked.
        from . import setup
        setup.announce_first_connection(device)

    return JsonResponse(
        {
            "ok": True,
            "accepted": accepted,
            "events": events,
            "errors": errors,
            "server_time": now.isoformat(),
            "config": _device_config(segments),
        }
    )


def _device_config(segments):
    """
    The tunable settings, sent back on every batch of readings.

    The device decides LOW/NORMAL/LEAK itself - it has to, because it must
    keep working when the WiFi drops - but the figures it decides against
    belong to the site, not to the firmware. Returning them on the reply the
    board is already waiting for means a facilitator can retune a line from
    the dashboard and see it take effect within seconds, with no laptop, no
    cable and no re-flashing.

    Keyed by module index as a string because that is what JSON gives us
    back. Built from the segments already loaded above, so this costs no
    extra queries.
    """
    # Site-wide detection settings, the same for every line, tuned from the
    # admin console (SystemSetting) and carried here so the board adopts them
    # without a re-flash and keeps using the last set it saw while offline.
    s = SystemSetting.load()
    detection = {
        "flow_warn_minutes": s.flow_warn_minutes,
        "flow_leak_minutes": s.flow_leak_minutes,
        "valve_test_hour": s.valve_test_hour,
        "valve_test_grace_minutes": s.valve_test_grace_minutes,
        "valve_test_drop_bar": round(s.valve_test_drop_bar, 3),
        "auto_close_on_flow_leak": s.auto_close_on_flow_leak,
        "upload_interval_seconds": s.upload_interval_seconds,
    }
    return {
        str(index): {
            "low_pressure_bar": round(segment.low_pressure_bar, 3),
            **_command_payload(segment),
            **detection,
        }
        for index, segment in segments.items()
        if segment.is_active
    }


def _command_payload(segment):
    """
    What the board must act on now, and how soon to ask again.

    command_poll_seconds is the fast cadence while somebody is at a valve
    screen for this line, and 0 when nobody is - the board then waits for its
    next upload, which carries the same keys.
    """
    return {
        "run_valve_test": segment.valve_test_requested,
        "valve_command": segment.valve_command,
        "command_poll_seconds": settings.COMMAND_POLL_SECONDS if segment.valve_watched else 0,
    }


@require_GET
def commands(request):
    """
    The pending valve command for each of a board's lines - nothing else.

    Deliberately the cheapest request the server answers: one lookup, no
    writes, no reading accepted, so a board can afford to ask every two
    seconds while a person stands at a valve button, and the hosted server
    can afford to be asked. Who is at a button is what the dashboards' live
    poll records (Segment.valve_watch_until); when nobody is, the reply says
    so (command_poll_seconds 0) and the board stops asking until its next
    upload says otherwise.
    """
    device = _authenticate_device(request)
    if device is None:
        return JsonResponse({"ok": False, "error": "invalid or missing device key"}, status=401)
    config = {
        str(segment.module_index): _command_payload(segment)
        for segment in Segment.objects.filter(device=device, is_active=True)
    }
    return JsonResponse({"ok": True, "config": config, "server_time": timezone.now().isoformat()})


def _note_valve_watch(segments):
    """
    Somebody active at a valve screen is looking at these lines.

    Written at most once every ~20 s per line however often the page polls,
    and only ever forwards, so a 2-second poll does not become a 2-second
    write.
    """
    now = timezone.now()
    until = now + timezone.timedelta(seconds=settings.VALVE_WATCH_SECONDS)
    refresh_below = until - timezone.timedelta(seconds=20)
    stale = [
        s.id for s in segments
        if s.valve_watch_until is None or s.valve_watch_until < refresh_below
    ]
    if stale:
        Segment.objects.filter(id__in=stale).update(valve_watch_until=until)


def _segment_payload(segment):
    latest = getattr(segment, "latest", None)
    household = getattr(segment, "household", None)
    stale = latest is None or (
        timezone.now() - latest.updated_at
    ).total_seconds() > offline_after_seconds()

    # A stale feed is reported as offline without overwriting the stored
    # status, so the dashboard never shows a stale "NORMAL" as if it were live.
    effective = segment.status
    if stale and segment.status != Segment.Status.MAINTENANCE:
        effective = Segment.Status.OFFLINE

    return {
        "id": segment.id,
        "module": segment.module_index,
        "name": segment.name,
        "status": effective,
        "status_label": dict(Segment.Status.choices).get(effective, effective),
        "stored_status": segment.status,
        "stale": stale,
        "low_pressure_bar": segment.low_pressure_bar,
        "location_label": segment.location_label,
        "has_device": segment.has_device,
        "device_id": segment.device.device_id if segment.device_id else None,
        "device_online": segment.device.is_online if segment.device_id else False,
        "device_ip": segment.device.last_ip if segment.device_id else None,
        "maintenance_note": segment.maintenance_note,
        "pressure_bar": round(latest.pressure_bar, 2) if latest else None,
        "pressure_psi": latest.pressure_psi if latest else None,
        "flow_lpm": round(latest.flow_lpm, 2) if latest else None,
        "severity": latest.severity if latest else DeviceSeverity.NONE,
        "severity_label": latest.get_severity_display() if latest else "",
        "trigger": latest.trigger if latest else Trigger.NONE,
        "trigger_label": latest.get_trigger_display() if latest else "",
        "valve_state": latest.valve_state if latest else ValveState.NOT_FITTED,
        "valve_label": latest.get_valve_state_display() if latest else "Not fitted",
        "valve_reason": latest.valve_reason if latest else ValveReason.NONE,
        # The sentence every screen shows under the state, and the lock the
        # household's and facilitator's buttons must respect.
        "valve_detail": describe_valve(segment, latest),
        "valve_locked": segment.valve_locked,
        "valve_closed_by": segment.valve_closed_by,
        "valve_command": segment.valve_command,
        "calibrating": latest.calibrating if latest else False,
        "simulated": latest.simulated if latest else False,
        "updated_at": latest.updated_at.isoformat() if latest else None,
        # The household this hose serves. A count would be meaningless now
        # that it is always one, and the table shows who it is instead.
        "household": {
            "id": household.household_id,
            "owner": household.owner_name,
            "active": household.account_status == Household.AccountStatus.ACTIVE,
        }
        if household is not None
        else None,
        "coords": {
            "start": [segment.start_lat, segment.start_lng]
            if segment.start_lat is not None and segment.start_lng is not None
            else None,
            "end": [segment.end_lat, segment.end_lng]
            if segment.end_lat is not None and segment.end_lng is not None
            else None,
        },
    }


@require_GET
def live(request):
    """Current state of every segment, plus open alerts. Polled by dashboards."""
    if not request.user.is_authenticated:
        return JsonResponse({"ok": False, "error": "authentication required"}, status=403)

    # One household per line, so it joins rather than being counted. Pulling
    # it in the same query keeps the live poll at a fixed cost instead of one
    # extra lookup per household, which matters at a 2-second interval.
    segments = (
        Segment.objects.filter(is_active=True)
        .select_related("latest", "device", "household")
        .order_by("name")
    )

    # The administrator oversees every barangay and sees all of them; a
    # facilitator only their own; a household owner only their own line.
    household = getattr(request.user, "household", None)
    if request.user.is_admin:
        pass                                            # every active segment
    elif request.user.is_facilitator:
        barangay_id = getattr(request.user, "barangay_id", None)
        segments = segments.filter(barangay_id=barangay_id) if barangay_id else segments.none()
    else:
        if household is None or household.segment_id is None:
            return JsonResponse({"ok": True, "segments": [], "open_events": [], "unread": 0})
        segments = segments.filter(id=household.segment_id)

    segments = list(segments)
    payload = [_segment_payload(segment) for segment in segments]

    # The page says whether a person is active at its valve buttons; that is
    # what makes the board start asking for commands every two seconds.
    if request.headers.get("X-Valve-Watch") == "1" and segments:
        _note_valve_watch(segments)

    open_events = []
    if request.user.is_admin or request.user.is_facilitator:
        for event in LeakEvent.objects.filter(
            cleared_at__isnull=True,
            severity__in=[LeakEvent.Severity.CRITICAL, LeakEvent.Severity.WARNING],
            segment__in=segments,   # the same barangay the lines above came from
        ).select_related("segment")[:20]:
            open_events.append(
                {
                    "id": event.id,
                    "segment": event.segment.name,
                    "module": event.segment.module_index,
                    "status": event.status,
                    "status_label": event.get_status_display(),
                    "severity": event.severity,
                    "location": event.location_label,
                    "pressure_bar": round(event.pressure_bar, 2),
                    "pressure_psi": psi(event.pressure_bar),
                    "flow_lpm": round(event.flow_lpm, 2),
                    "detected_at": event.detected_at.isoformat(),
                    "acknowledged": event.acknowledged_at is not None,
                }
            )
        unread = 0
    elif household is not None:
        unread = Notification.objects.filter(recipient=household, read_at__isnull=True).count()
    else:
        unread = 0

    # Device reachability, so the dashboards can announce it changing rather
    # than only showing it on a reload. Reported for owners too: their line
    # reads OFFLINE for the same reason, and they deserve the explanation.
    devices = list(Device.objects.filter(segments__in=segments).distinct())
    last_seen = max((d.last_seen for d in devices if d.last_seen), default=None)

    # The address the board last posted from. Worth surfacing: it is how the
    # facilitator reaches the device's own settings page, and it changes
    # whenever the network does.
    reporting = max(
        (d for d in devices if d.last_seen), key=lambda d: d.last_seen, default=None
    )

    return JsonResponse(
        {
            "ok": True,
            "segments": payload,
            "open_events": open_events,
            "unread": unread,
            "device": {
                "online": any(d.is_online for d in devices),
                "last_seen": last_seen.isoformat() if last_seen else None,
                "ever_seen": last_seen is not None,
                "ip": reporting.last_ip if reporting else None,
            },
            "server_time": timezone.now().isoformat(),
            "poll_ms": settings.LIVE_POLL_INTERVAL_MS,
        }
    )


@require_GET
def history(request):
    """Minute rollups for the pressure/flow charts."""
    if not request.user.is_authenticated:
        return JsonResponse({"ok": False, "error": "authentication required"}, status=403)

    try:
        hours = max(1, min(int(request.GET.get("hours", 6)), 168))
    except ValueError:
        hours = 6

    since = timezone.now() - timezone.timedelta(hours=hours)
    rollups = ReadingRollup.objects.filter(minute__gte=since).select_related("segment")

    segment_id = request.GET.get("segment")
    if segment_id:
        try:
            rollups = rollups.filter(segment_id=int(segment_id))
        except (TypeError, ValueError):
            return JsonResponse({"ok": False, "error": "segment must be a number"}, status=400)

    if request.user.is_admin:
        pass                                            # every barangay's rollups
    elif request.user.is_facilitator:
        barangay_id = getattr(request.user, "barangay_id", None)
        rollups = rollups.filter(segment__barangay_id=barangay_id) if barangay_id else rollups.none()
    else:
        household = getattr(request.user, "household", None)
        if household is None or household.segment_id is None:
            return JsonResponse({"ok": True, "series": []})
        rollups = rollups.filter(segment_id=household.segment_id)

    series = {}
    for row in rollups.order_by("minute"):
        bucket = series.setdefault(
            row.segment_id,
            {"segment": row.segment.name, "module": row.segment.module_index, "points": []},
        )
        bucket["points"].append(
            {
                "t": row.minute.isoformat(),
                "pressure_bar": round(row.avg_pressure_bar, 3),
                "pressure_psi": psi(row.avg_pressure_bar),
                "flow_lpm": round(row.avg_flow_lpm, 3),
                "leak_samples": row.leak_samples,
            }
        )

    return JsonResponse({"ok": True, "hours": hours, "series": list(series.values())})


# ---------------------------------------------------------------------------
# Push subscriptions
#
# These are the browser handing the server its own push credentials. They are
# session-authenticated and belong to the signed-in household - a device
# cannot register a subscription, and one household can never register a
# subscription against another.
# ---------------------------------------------------------------------------


def _subscription_payload(request):
    """Pull the three values a Push API subscription carries, or raise."""
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        raise ValueError("body is not JSON")

    endpoint = (data.get("endpoint") or "").strip()
    keys = data.get("keys") or {}
    p256dh = (keys.get("p256dh") or "").strip()
    auth = (keys.get("auth") or "").strip()

    if not endpoint:
        raise ValueError("no endpoint")
    if not (p256dh and auth):
        raise ValueError("no keys")

    return endpoint, p256dh, auth


# Exempt from CSRF, unlike its two neighbours, because the service worker
# calls this one as well as the page does - a push service can rotate a
# subscription while no tab is open, and a worker has no document to read a
# CSRF token from. The exemption is narrow and the endpoint still requires a
# signed-in household session.
#
# What a forged request could achieve is limited to registering a push
# endpoint that belongs to the victim's own browser, for this server's own
# VAPID key: they would receive the notifications they were going to receive
# anyway. It cannot read anything and it cannot send anything.
@csrf_exempt
@require_POST
def push_subscribe(request):
    household = getattr(request.user, "household", None)
    if household is None:
        return JsonResponse({"error": "not a household account"}, status=403)

    try:
        endpoint, p256dh, auth = _subscription_payload(request)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    # update_or_create on the endpoint, because the endpoint IS the handset's
    # identity. A phone that re-subscribes - permission re-granted, app
    # reinstalled, keys rotated by the browser - sends the same endpoint with
    # fresh keys, and storing a second row would push to it twice.
    PushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={
            "household": household,
            "p256dh": p256dh,
            "auth": auth,
            "user_agent": request.META.get("HTTP_USER_AGENT", "")[:300],
            "failures": 0,
        },
    )
    return JsonResponse({"ok": True})


@require_POST
def push_unsubscribe(request):
    household = getattr(request.user, "household", None)
    if household is None:
        return JsonResponse({"error": "not a household account"}, status=403)

    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"error": "body is not JSON"}, status=400)

    endpoint = (data.get("endpoint") or "").strip()
    if not endpoint:
        return JsonResponse({"error": "no endpoint"}, status=400)

    # Scoped to this household on purpose: without it, knowing somebody else's
    # endpoint would be enough to switch their notifications off.
    deleted, _by_model = PushSubscription.objects.filter(
        household=household, endpoint=endpoint
    ).delete()
    return JsonResponse({"ok": True, "removed": deleted})


@require_POST
def push_test(request):
    """
    Send one notification to the handset that asked for it.

    Worth its own endpoint: "did it work?" is the first thing anybody asks
    after granting permission, and the honest answer needs a real message to
    travel the whole path - server, push service, handset, service worker.
    """
    household = getattr(request.user, "household", None)
    if household is None:
        return JsonResponse({"error": "not a household account"}, status=403)

    ready, note = push.describe_push()
    if not ready:
        return JsonResponse({"ok": False, "error": note}, status=503)

    sent, removed = push.send_to_household(
        household,
        _("Notifications are working"),
        _("This is a test. Alerts about your water will look like this."),
    )
    if not sent:
        return JsonResponse(
            {
                "ok": False,
                "error": (
                    _("This phone is not registered for notifications. Turn them "
                      "off and on again.") if removed else
                    _("No phone is registered for notifications yet.")
                ),
            },
            status=409,
        )
    return JsonResponse({"ok": True, "sent": sent})
