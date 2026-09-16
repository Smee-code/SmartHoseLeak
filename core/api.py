"""
JSON endpoints.

/api/ingest/  - the ESP32 posts here every sample interval (device-key auth)
/api/live/    - dashboards poll here for current values (session auth)
/api/history/ - chart data from the minute rollups (session auth)
"""

import json

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Q
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .models import (
    ClaimCode,
    Device,
    DeviceSeverity,
    Household,
    LatestReading,
    LeakEvent,
    Notification,
    ReadingRollup,
    Segment,
    SmsMessage,
    Trigger,
    ValveState,
    psi,
)

# The device reports these three; MAINTENANCE and OFFLINE are set server-side.
DEVICE_STATUSES = {
    "NORMAL": Segment.Status.NORMAL,
    "LOW_SUPPLY": Segment.Status.LOW_SUPPLY,
    "LOW WATER SUPPLY": Segment.Status.LOW_SUPPLY,
    "POSSIBLE_LEAK": Segment.Status.POSSIBLE_LEAK,
    "POSSIBLE LEAK": Segment.Status.POSSIBLE_LEAK,
}

SEVERITY_BY_STATUS = {
    Segment.Status.POSSIBLE_LEAK: LeakEvent.Severity.CRITICAL,
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


def _notify_households(segment, event):
    """
    Raise the in-app notice for the household whose hose this is.

    One hose per house means one recipient, but the lookup stays a queryset:
    a line can legitimately have no household yet - a board on the bench
    during installation - and that must post no notice rather than fail.
    """
    if event.status == Segment.Status.POSSIBLE_LEAK:
        title = f"Possible leak on {segment.name}"
        body = (
            f"An automated alert was raised on your supply line at "
            f"{timezone.localtime(event.detected_at):%b %d, %Y %I:%M %p}. "
            f"The alert was raised {event.location_label}. "
            f"Pressure was {event.pressure_bar:.2f} bar ({psi(event.pressure_bar)} psi) "
            f"with a flow of {event.flow_lpm:.2f} L/min. "
            f"Your water supply may be interrupted while the line is inspected."
        )
    elif event.status == Segment.Status.LOW_SUPPLY:
        title = f"Low water supply on {segment.name}"
        body = (
            f"Low supply pressure was detected on your line at "
            f"{timezone.localtime(event.detected_at):%b %d, %Y %I:%M %p} "
            f"({event.pressure_bar:.2f} bar / {psi(event.pressure_bar)} psi). "
            f"You may experience reduced water flow."
        )
    elif event.status == Segment.Status.NORMAL:
        # Only announce a recovery if the household was told about the
        # problem in the first place. A device booting from OFFLINE into
        # NORMAL is not an all-clear anybody was waiting for.
        if event.previous_status not in {Segment.Status.POSSIBLE_LEAK, Segment.Status.LOW_SUPPLY}:
            return
        title = f"{segment.name} is back to normal"
        body = (
            f"The alert on your supply line was cleared at "
            f"{timezone.localtime(event.detected_at):%b %d, %Y %I:%M %p}. "
            f"Water supply has returned to normal readings."
        )
    else:
        return

    households = Household.objects.filter(
        segment=segment, account_status=Household.AccountStatus.ACTIVE
    )
    created = Notification.objects.bulk_create(
        [
            Notification(
                kind=Notification.Kind.AUTOMATIC,
                title=title,
                body=body,
                recipient=household,
                segment=segment,
                event=event,
            )
            for household in households
        ]
    )

    # Text them as well, unless that has been switched off.
    #
    # An in-app notice is only seen by somebody who opens the app, and a leak
    # at two in the morning cannot wait for that. The message is written
    # shorter than the in-app one on purpose: a text is read on a lock screen
    # and the useful half is the instruction, not the reading.
    if not (settings.SMS_AUTO_ON_LEAK and event.status == Segment.Status.POSSIBLE_LEAK):
        return

    notices = {n.recipient_id: n for n in created}
    for household in households:
        SmsMessage.queue(
            household,
            _leak_sms_text(household, event),
            kind=SmsMessage.Kind.AUTOMATIC,
            notification=notices.get(household.id),
            event=event,
        )

    _notify_facilitators(segment, event)


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
    if not facilitators:
        return

    household = getattr(segment, "household", None)
    who = household.household_id if household else segment.name
    owner = f" ({household.owner_name})" if household else ""
    severity = ""
    if event.device_severity and event.device_severity != DeviceSeverity.NONE:
        severity = f", {event.get_device_severity_display().lower()}"

    text = (
        f"LEAK: {who}{owner}{severity}. "
        f"{event.pressure_bar:.2f} bar, {event.flow_lpm:.2f} L/min. "
        f"Detected by {event.get_trigger_display().lower()}. "
        f"The owner has been asked to check their faucets."
    )

    for facilitator in facilitators:
        SmsMessage.queue_to_number(
            facilitator.contact_number,
            text,
            name=facilitator.get_full_name() or facilitator.username,
            kind=SmsMessage.Kind.AUTOMATIC,
            event=event,
        )


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
        f"LEAK ALERT {household.household_id}: possible leak {when}{severity}. "
        f"Please check all your faucets are closed. If they are and water "
        f"still runs, tell the facilitator."
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

            rollup, _ = ReadingRollup.objects.get_or_create(segment=segment, minute=minute)
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

        device.last_seen = now
        device.last_ip = _client_ip(request)
        device.save(update_fields=["last_seen", "last_ip"])

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
    return {
        str(index): {
            "low_pressure_bar": round(segment.low_pressure_bar, 3),
            "run_valve_test": segment.valve_test_requested,
        }
        for index, segment in segments.items()
        if segment.is_active
    }


def _segment_payload(segment):
    latest = getattr(segment, "latest", None)
    household = getattr(segment, "household", None)
    stale = latest is None or (
        timezone.now() - latest.updated_at
    ).total_seconds() > settings.DEVICE_OFFLINE_AFTER_SECONDS

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

    # Household owners see only their own supply line.
    household = getattr(request.user, "household", None)
    if not request.user.is_facilitator:
        if household is None or household.segment_id is None:
            return JsonResponse({"ok": True, "segments": [], "open_events": [], "unread": 0})
        segments = segments.filter(id=household.segment_id)

    payload = [_segment_payload(segment) for segment in segments]

    open_events = []
    if request.user.is_facilitator:
        for event in LeakEvent.objects.filter(
            cleared_at__isnull=True, severity__in=[LeakEvent.Severity.CRITICAL, LeakEvent.Severity.WARNING]
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
    else:
        unread = Notification.objects.filter(recipient=household, read_at__isnull=True).count()

    # Device reachability, so the dashboards can announce it changing rather
    # than only showing it on a reload. Reported for owners too: their line
    # reads OFFLINE for the same reason, and they deserve the explanation.
    devices = list(Device.objects.all())
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
        rollups = rollups.filter(segment_id=segment_id)

    if not request.user.is_facilitator:
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
