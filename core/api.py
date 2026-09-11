"""
JSON endpoints.

/api/ingest/  - the ESP32 posts here every sample interval (device-key auth)
/api/live/    - dashboards poll here for current values (session auth)
/api/history/ - chart data from the minute rollups (session auth)
"""

import json

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .models import (
    Device,
    Household,
    LatestReading,
    LeakEvent,
    Notification,
    ReadingRollup,
    Segment,
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


def _notify_households(segment, event):
    """
    Raise in-app notices for every active household on the affected supply
    line. SMS is not sent from here: the SIM800L on the device alerts the
    facilitator directly, so the server owns only the in-app channel.
    """
    if event.status == Segment.Status.POSSIBLE_LEAK:
        title = f"Possible leak on {segment.name}"
        body = (
            f"An automated alert was raised on your supply line at "
            f"{timezone.localtime(event.detected_at):%b %d, %Y %I:%M %p}. "
            f"The suspected leak is {event.location_label}. "
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
    Notification.objects.bulk_create(
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


def _apply_status_change(segment, new_status, pressure_bar, flow_lpm):
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
        location_from_m=segment.start_m,
        location_to_m=segment.end_m,
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

            LatestReading.objects.update_or_create(
                segment=segment,
                defaults={
                    "pressure_bar": pressure_bar,
                    "flow_lpm": flow_lpm,
                    "k_conductance": k_value,
                    "device_status": mapped_status,
                    "calibrating": bool(entry.get("calibrating")),
                    "simulated": bool(entry.get("simulated")),
                    "updated_at": now,
                },
            )

            rollup, _ = ReadingRollup.objects.get_or_create(segment=segment, minute=minute)
            rollup.fold(pressure_bar, flow_lpm, mapped_status == Segment.Status.POSSIBLE_LEAK)
            rollup.save()

            # A segment parked under maintenance by the facilitator keeps that
            # status until they clear it, so field repairs do not spam alerts.
            if segment.status != Segment.Status.MAINTENANCE and segment.status != mapped_status:
                event = _apply_status_change(segment, mapped_status, pressure_bar, flow_lpm)
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
        }
    )


def _segment_payload(segment):
    latest = getattr(segment, "latest", None)
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
        "start_m": segment.start_m,
        "end_m": segment.end_m,
        "location_label": segment.location_label,
        "maintenance_note": segment.maintenance_note,
        "pressure_bar": round(latest.pressure_bar, 2) if latest else None,
        "pressure_psi": latest.pressure_psi if latest else None,
        "flow_lpm": round(latest.flow_lpm, 2) if latest else None,
        "k": round(latest.k_conductance, 3) if latest else None,
        "calibrating": latest.calibrating if latest else False,
        "simulated": latest.simulated if latest else False,
        "updated_at": latest.updated_at.isoformat() if latest else None,
        # Annotated by live(); the fallback keeps this helper correct if it is
        # ever called with a plain Segment.
        "households": getattr(segment, "active_household_count", None)
        if getattr(segment, "active_household_count", None) is not None
        else segment.households.filter(
            account_status=Household.AccountStatus.ACTIVE
        ).count(),
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

    # The active-household count is annotated rather than prefetched: calling
    # .filter() on a prefetched relation ignores the prefetch and issues one
    # COUNT per segment, so the old prefetch was paid for and then wasted.
    segments = (
        Segment.objects.filter(is_active=True)
        .select_related("latest", "device")
        .annotate(
            active_household_count=Count(
                "households",
                filter=Q(households__account_status=Household.AccountStatus.ACTIVE),
            )
        )
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
