"""
Views for both portals.

Role separation is enforced by the facilitator_required / owner_required
decorators rather than by hiding links, so an owner cannot reach a
facilitator page by typing its URL.
"""

from functools import wraps

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Avg, Count, Max, Q
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import (
    ComplaintForm,
    ComplaintResponseForm,
    HouseholdForm,
    MaintenanceForm,
    NotificationForm,
    SegmentCreateForm,
    SegmentForm,
    WaterSourceForm,
)
from .models import (
    Complaint,
    Device,
    Household,
    LeakEvent,
    Notification,
    ReadingRollup,
    Segment,
    WaterSource,
    psi,
)

User = get_user_model()


def facilitator_required(view):
    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        if not request.user.is_facilitator:
            return HttpResponseForbidden("This page is limited to the water system facilitator.")
        return view(request, *args, **kwargs)

    return wrapper


def owner_required(view):
    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        household = getattr(request.user, "household", None)
        if household is None:
            return HttpResponseForbidden(
                "This account is not linked to a registered household. "
                "Ask the water system facilitator to link it."
            )
        return view(request, household, *args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------


def home(request):
    """Public-facing homepage, shown to both user types before login."""
    segments = Segment.objects.filter(is_active=True).select_related("latest")
    stats = {
        "households": Household.objects.filter(account_status=Household.AccountStatus.ACTIVE).count(),
        "segments": segments.count(),
        "monitored_metres": sum(s.length_m for s in segments),
        "open_alerts": LeakEvent.objects.filter(
            cleared_at__isnull=True, severity=LeakEvent.Severity.CRITICAL
        ).count(),
        "events_30d": LeakEvent.objects.filter(
            detected_at__gte=timezone.now() - timezone.timedelta(days=30),
            status=Segment.Status.POSSIBLE_LEAK,
        ).count(),
    }
    return render(request, "home.html", {"segments": segments, "stats": stats})


@login_required
def post_login(request):
    """Send each role to its own landing page after authentication."""
    if request.user.is_facilitator:
        return redirect("dashboard")
    if getattr(request.user, "household", None) is not None:
        return redirect("owner_home")
    return redirect("home")


# ---------------------------------------------------------------------------
# Facilitator
# ---------------------------------------------------------------------------


@facilitator_required
def dashboard(request):
    segments = Segment.objects.filter(is_active=True).select_related("latest", "device")
    open_events = (
        LeakEvent.objects.filter(cleared_at__isnull=True)
        .exclude(status=Segment.Status.NORMAL)
        .select_related("segment")
    )
    # The address the ESP32 should POST to. Whatever host was used to reach
    # this page is by definition reachable on this network, so echo it back
    # rather than guessing at the machine's interfaces - a laptop with a
    # dormant hotspot adapter has more than one, and picking the wrong one is
    # the usual reason a device looks connected but never delivers.
    host = request.get_host()
    loopback = any(h in host for h in ("127.0.0.1", "localhost", "[::1]"))
    source = WaterSource.get()

    context = {
        "segments": segments,
        "open_events": open_events,
        "recent_events": LeakEvent.objects.select_related("segment")[:8],
        "devices": Device.objects.all(),
        "ingest_url": f"{request.scheme}://{host}{reverse('api_ingest')}",
        "ingest_url_is_loopback": loopback,
        "pending_complaints": Complaint.objects.filter(status=Complaint.Status.PENDING).count(),
        "household_count": Household.objects.filter(
            account_status=Household.AccountStatus.ACTIVE
        ).count(),
        "map_segments": [
            {
                "id": s.id,
                "module": s.module_index,
                "name": s.name,
                "start_m": s.start_m,
                "end_m": s.end_m,
                "status": s.status,
                "coords": {
                    "start": [s.start_lat, s.start_lng]
                    if s.start_lat is not None and s.start_lng is not None
                    else None,
                    "end": [s.end_lat, s.end_lng]
                    if s.end_lat is not None and s.end_lng is not None
                    else None,
                },
            }
            for s in segments
        ],
        # Only households that have been surveyed; the rest simply have no pin.
        "map_households": [
            {
                "id": h.id,
                "household_id": h.household_id,
                "owner_name": h.owner_name,
                "address": h.address,
                "segment": h.segment.name if h.segment else None,
                "module": h.segment.module_index if h.segment else None,
                "color": h.segment.color if h.segment else None,
                "status": h.segment.status if h.segment else "OFFLINE",
                "coords": [h.latitude, h.longitude],
            }
            for h in Household.objects.select_related("segment")
            .filter(
                account_status=Household.AccountStatus.ACTIVE,
                latitude__isnull=False,
                longitude__isnull=False,
            )
        ],
        "map_source": (
            {
                "name": source.name,
                "notes": source.notes,
                "coords": [source.latitude, source.longitude],
            }
            if source.has_location
            else None
        ),
    }
    return render(request, "facilitator/dashboard.html", context)


@facilitator_required
def pressure_monitor(request):
    """Centralised pressure table across every household and supply line."""
    households = (
        Household.objects.select_related("segment", "segment__latest")
        .filter(account_status=Household.AccountStatus.ACTIVE)
        .order_by("segment__module_index", "household_id")
    )
    return render(
        request,
        "facilitator/pressure.html",
        {
            "households": households,
            "segments": Segment.objects.filter(is_active=True).select_related("latest"),
        },
    )


@facilitator_required
def household_list(request, create_form=None, edit_form=None, edit_household=None):
    """
    The household table, with the add / edit / delete dialogs alongside it.

    create_form and edit_form are passed in by household_form() when a
    submission fails validation, so the dialog can reopen with the typed
    values and the errors still on screen rather than sending the facilitator
    to a separate page.
    """
    query = request.GET.get("q", "").strip()
    segment_id = request.GET.get("segment", "")
    households = Household.objects.select_related("segment", "user")

    if query:
        households = households.filter(
            Q(household_id__icontains=query)
            | Q(owner_name__icontains=query)
            | Q(address__icontains=query)
            | Q(contact_number__icontains=query)
        )
    if segment_id:
        households = households.filter(segment_id=segment_id)

    # Counted here so the delete dialog can say exactly what goes with the
    # household, rather than warning vaguely about "related records".
    # order_by is explicit because aggregating adds a GROUP BY, and Django
    # warns that paginating an unordered queryset can repeat or skip rows.
    households = households.annotate(
        complaint_count=Count("complaints", distinct=True),
        notification_count=Count("notifications", distinct=True),
    ).order_by("household_id")

    page = Paginator(households, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "facilitator/households.html",
        {
            "page": page,
            "query": query,
            "segment_id": segment_id,
            "segments": Segment.objects.filter(is_active=True),
            "create_form": create_form or HouseholdForm(),
            "create_form_errors": bool(create_form and create_form.errors),
            # auto_id keeps the edit dialog's field ids distinct from the add
            # dialog's; both render the same form class on the same page, and
            # duplicate ids would break every label and every JS lookup.
            "edit_form": edit_form or HouseholdForm(auto_id="edit_%s"),
            "edit_form_errors": bool(edit_form and edit_form.errors),
            "edit_household": edit_household,
            # Shown in the dialog where the id input used to be. Advisory only:
            # the real one is assigned by Household.save() at write time.
            "next_household_id": Household.next_household_id(),
        },
    )


@facilitator_required
def household_form(request, pk=None):
    household = get_object_or_404(Household, pk=pk) if pk else None
    # Editing renders into the shared edit dialog, which namespaces its ids.
    form = HouseholdForm(
        request.POST or None, instance=household,
        auto_id="edit_%s" if household else "id_%s",
    )

    if request.method == "POST" and form.is_valid():
        record = form.save()
        if form.cleaned_data.get("create_account") and record.user_id is None:
            username, password = form.generate_credentials()
            user = User.objects.create_user(
                username=username,
                password=password,
                role=User.Role.OWNER,
                first_name=record.owner_name.split(" ")[0],
                last_name=" ".join(record.owner_name.split(" ")[1:]),
                contact_number=record.contact_number,
                must_change_password=True,
            )
            record.user = user
            record.save(update_fields=["user"])
            messages.success(
                request,
                f"Household saved. Owner Portal login created - username: {username} "
                f"/ temporary password: {password}. Write this down now; it is not shown again.",
            )
        else:
            messages.success(request, "Household record saved.")
        return redirect("household_list")

    # A failed submission came from a dialog on the list page, so send the
    # bound form back there instead of to the standalone page - otherwise the
    # dialog appears to have thrown the typed values away.
    if request.method == "POST":
        if household is None:
            return household_list(request, create_form=form)
        return household_list(request, edit_form=form, edit_household=household)

    return render(
        request,
        "facilitator/household_form.html",
        {
            "form": form,
            "household": household,
            "next_household_id": Household.next_household_id(),
        },
    )


@facilitator_required
def household_detail(request, pk):
    household = get_object_or_404(
        Household.objects.select_related("segment", "segment__latest", "user"), pk=pk
    )
    return render(
        request,
        "facilitator/household_detail.html",
        {
            "household": household,
            "complaints": household.complaints.all()[:10],
            "notifications": household.notifications.all()[:10],
        },
    )


@facilitator_required
@require_POST
def household_delete(request, pk):
    """
    Remove a household and everything that only exists because of it.

    Complaints and notifications cascade at the database level. The owner's
    login does not - the relation is SET_NULL - so it is removed explicitly:
    leaving it behind would be an account that can still sign in but has no
    household to show, which fails with a bare "not linked" error.

    POST only, so the confirmation dialog is the single way in and no link or
    crawler can trigger it.
    """
    household = get_object_or_404(Household.objects.select_related("user"), pk=pk)

    label = f"{household.household_id} ({household.owner_name})"
    complaints = household.complaints.count()
    notifications = household.notifications.count()
    username = household.user.username if household.user else None

    with transaction.atomic():
        if household.user is not None:
            household.user.delete()
        household.delete()

    removed = [f"{complaints} complaint(s)", f"{notifications} notice(s)"]
    if username:
        removed.append(f"the login '{username}'")
    messages.success(request, f"Deleted {label}, along with {', '.join(removed)}.")
    return redirect("household_list")


@facilitator_required
@require_POST
def household_reset_password(request, pk):
    """Issue a new temporary password when an owner is locked out."""
    household = get_object_or_404(Household, pk=pk)
    if household.user is None:
        messages.error(request, "This household has no Owner Portal account yet.")
        return redirect("household_detail", pk=pk)

    import secrets

    password = secrets.token_urlsafe(6)
    household.user.set_password(password)
    household.user.must_change_password = True
    household.user.save()
    messages.success(
        request,
        f"New temporary password for {household.user.username}: {password} - "
        f"give this to the owner; it is not shown again.",
    )
    return redirect("household_detail", pk=pk)


@facilitator_required
def segment_list(request, create_form=None, edit_form=None, edit_segment=None):
    """
    The supply lines, plus the water source they are all measured from.

    The source form lives here rather than on its own page because it is the
    same job: describing the physical layout of the hose run.

    create_form is handed back by segment_create() when a new line fails
    validation, so the dialog reopens with what was typed still in it.
    """
    source = WaterSource.get()

    # Two forms post to this page's neighbourhood, and both have a "name"
    # field. Without this marker a failed add-line submission would bind to
    # the water source form and could save the line's name over the source.
    is_source_post = request.method == "POST" and request.POST.get("form_kind") == "source"
    source_form = WaterSourceForm(request.POST if is_source_post else None, instance=source)

    if is_source_post and source_form.is_valid():
        source_form.save()
        messages.success(request, "Water source location saved.")
        return redirect("segment_list")

    return render(
        request,
        "facilitator/segments.html",
        {
            # Counted so the delete dialog can say exactly how much history
            # goes with the line rather than warning about it vaguely.
            "segments": Segment.objects.select_related("device", "latest").annotate(
                household_count=Count("households", distinct=True),
                rollup_count=Count("rollups", distinct=True),
                event_count=Count("events", distinct=True),
            ),
            "devices": Device.objects.all(),
            "source": source,
            "source_form": source_form,
            # Three forms with a "name" field share this page - the water
            # source, the add dialog and the edit dialog - so each needs its
            # own id prefix or their labels focus the wrong input.
            "create_form": create_form or SegmentCreateForm(
                device=Device.objects.first(), auto_id="new_%s"
            ),
            "create_form_errors": bool(create_form and create_form.errors),
            "edit_form": edit_form or SegmentForm(auto_id="edit_%s"),
            "edit_form_errors": bool(edit_form and edit_form.errors),
            "edit_segment": edit_segment,
            "next_module_index": (
                Segment.next_module_index(Device.objects.first())
                if Device.objects.exists() else 1
            ),
        },
    )


@facilitator_required
def segment_create(request):
    """
    Add a hose line. Posted from the dialog on the Lines page.

    Every line belongs to a device, and there is one device in this
    deployment, so it is chosen rather than asked for. The module number is
    assigned in the form, matching the position the firmware will report it
    under.
    """
    device = Device.objects.first()
    if device is None:
        messages.error(request, "Register a device before adding hose lines.")
        return redirect("segment_list")

    form = SegmentCreateForm(request.POST or None, device=device, auto_id="new_%s")
    if request.method == "POST" and form.is_valid():
        segment = form.save()
        messages.success(
            request,
            f"{segment.name} added as module M{segment.module_index}. "
            f"It will read OFFLINE until the ESP32 reports a module "
            f"{segment.module_index}.",
        )
        return redirect("segment_list")

    if request.method == "POST":
        return segment_list(request, create_form=form)
    return redirect("segment_list")


@facilitator_required
def segment_edit(request, pk):
    segment = get_object_or_404(Segment, pk=pk)
    # auto_id namespaces the fields so the edit dialog cannot collide with the
    # add dialog, which renders the same fields on the same page.
    form = SegmentForm(request.POST or None, instance=segment, auto_id="edit_%s")
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"{segment.name} updated.")
        return redirect("segment_list")

    # A failed save came from the dialog on the list page, so re-render that
    # rather than the standalone page - otherwise the edits appear to vanish.
    if request.method == "POST":
        return segment_list(request, edit_form=form, edit_segment=segment)
    return render(request, "facilitator/segment_form.html", {"form": form, "segment": segment})


@facilitator_required
@require_POST
def segment_delete(request, pk):
    """
    Remove a hose line and the readings recorded against it.

    This is the most destructive action in the dashboard. Rollups, leak
    events and the live reading all cascade, so a line's entire pressure
    history goes with it. Households do not: their link is SET_NULL, so they
    survive as Unassigned and can be pointed at another line.

    POST only, so the confirmation dialog is the only way to reach it.
    """
    segment = get_object_or_404(Segment, pk=pk)

    label = f"M{segment.module_index} - {segment.name}"
    rollups = segment.rollups.count()
    events = segment.events.count()
    households = segment.households.count()

    with transaction.atomic():
        segment.delete()

    detail = f"{rollups} minute(s) of history and {events} event(s) were removed"
    if households:
        detail += f"; {households} household(s) are now unassigned"
    messages.success(request, f"Deleted {label}. {detail}.")
    return redirect("segment_list")


@facilitator_required
@require_POST
def segment_maintenance(request, pk):
    """
    Toggle a segment in and out of maintenance.

    While a segment is under maintenance the ingest endpoint stops raising
    events for it, so a repair crew opening the line does not generate a
    stream of false leak alerts.
    """
    segment = get_object_or_404(Segment, pk=pk)
    form = MaintenanceForm(request.POST)
    note = form.cleaned_data.get("note", "") if form.is_valid() else ""

    if segment.status == Segment.Status.MAINTENANCE:
        segment.status = Segment.Status.NORMAL
        segment.maintenance_note = ""
        messages.success(request, f"{segment.name} returned to live monitoring.")
    else:
        segment.status = Segment.Status.MAINTENANCE
        segment.maintenance_note = note
        messages.success(request, f"{segment.name} marked as under maintenance.")

    segment.status_changed_at = timezone.now()
    segment.save(update_fields=["status", "maintenance_note", "status_changed_at"])
    return redirect(request.POST.get("next") or "dashboard")


@facilitator_required
def event_list(request):
    events = LeakEvent.objects.select_related("segment", "acknowledged_by")
    status = request.GET.get("status", "")
    if status == "open":
        events = events.filter(cleared_at__isnull=True)
    elif status:
        events = events.filter(status=status)
    page = Paginator(events, 30).get_page(request.GET.get("page"))
    return render(
        request,
        "facilitator/events.html",
        {"page": page, "status": status, "statuses": Segment.Status.choices},
    )


@facilitator_required
@require_POST
def event_acknowledge(request, pk):
    event = get_object_or_404(LeakEvent, pk=pk)
    event.acknowledged_at = timezone.now()
    event.acknowledged_by = request.user
    note = request.POST.get("resolution_note", "").strip()
    if note:
        event.resolution_note = note
    event.save(update_fields=["acknowledged_at", "acknowledged_by", "resolution_note"])
    messages.success(request, f"Alert on {event.segment.name} acknowledged.")
    return redirect(request.POST.get("next") or "event_list")


@facilitator_required
def complaint_list(request):
    complaints = Complaint.objects.select_related("household", "household__segment")
    status = request.GET.get("status", "")
    if status:
        complaints = complaints.filter(status=status)
    page = Paginator(complaints, 20).get_page(request.GET.get("page"))
    return render(
        request,
        "facilitator/complaints.html",
        {
            "page": page,
            "status": status,
            "statuses": Complaint.Status.choices,
            "counts": {
                key: Complaint.objects.filter(status=key).count() for key, _ in Complaint.Status.choices
            },
        },
    )


@facilitator_required
def complaint_detail(request, pk):
    complaint = get_object_or_404(
        Complaint.objects.select_related("household", "household__segment"), pk=pk
    )
    form = ComplaintResponseForm(request.POST or None, instance=complaint)

    if request.method == "POST" and form.is_valid():
        # Only a newly typed reply counts. A blank box keeps the existing
        # response (see the form), so without this a status-only change would
        # re-stamp the timestamp and send the owner a duplicate notice.
        new_reply = getattr(form, "new_reply", False)

        updated = form.save(commit=False)
        if new_reply:
            updated.responded_at = timezone.now()
            updated.responded_by = request.user
        updated.save()

        if new_reply:
            # Mirror the reply into the owner notification inbox so they see
            # it in the same place as every other message from the facilitator.
            Notification.objects.create(
                kind=Notification.Kind.TARGETED,
                title=f"Reply to your complaint {updated.reference}",
                body=updated.response,
                sent_by=request.user,
                recipient=updated.household,
                segment=updated.household.segment,
            )
            messages.success(request, "Reply sent. The owner has been notified.")
        else:
            messages.success(
                request, f"Complaint marked as {updated.get_status_display()}."
            )
        return redirect("complaint_detail", pk=pk)

    return render(request, "facilitator/complaint_detail.html", {"complaint": complaint, "form": form})


@facilitator_required
def notification_center(request):
    form = NotificationForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        recipients, kind = form.recipients()
        if not recipients:
            messages.error(request, "No active households match that audience.")
        else:
            Notification.objects.bulk_create(
                [
                    Notification(
                        kind=kind,
                        title=form.cleaned_data["title"],
                        body=form.cleaned_data["body"],
                        sent_by=request.user,
                        recipient=household,
                        segment=household.segment,
                    )
                    for household in recipients
                ]
            )
            messages.success(request, f"Notice sent to {len(recipients)} household(s).")
            return redirect("notification_center")

    sent = (
        Notification.objects.filter(kind__in=[Notification.Kind.TARGETED, Notification.Kind.BROADCAST])
        .select_related("recipient")
        .order_by("-created_at")[:40]
    )
    return render(request, "facilitator/notifications.html", {"form": form, "sent": sent})


@facilitator_required
def reports(request):
    """Summary figures plus CSV export for the pressure and event logs."""
    since = timezone.now() - timezone.timedelta(days=30)
    per_segment = []
    for segment in Segment.objects.filter(is_active=True):
        agg = ReadingRollup.objects.filter(segment=segment, minute__gte=since).aggregate(
            avg_p=Avg("avg_pressure_bar"),
            avg_f=Avg("avg_flow_lpm"),
            max_p=Max("max_pressure_bar"),
            samples=Count("id"),
        )
        per_segment.append(
            {
                "segment": segment,
                "avg_pressure_bar": round(agg["avg_p"] or 0, 2),
                "avg_pressure_psi": psi(agg["avg_p"] or 0),
                "avg_flow_lpm": round(agg["avg_f"] or 0, 2),
                "max_pressure_bar": round(agg["max_p"] or 0, 2),
                "minutes_logged": agg["samples"] or 0,
                "leak_events": LeakEvent.objects.filter(
                    segment=segment, status=Segment.Status.POSSIBLE_LEAK, detected_at__gte=since
                ).count(),
            }
        )
    return render(
        request,
        "facilitator/reports.html",
        {
            "per_segment": per_segment,
            "complaint_counts": Complaint.objects.values("status").annotate(n=Count("id")),
            "since": since,
        },
    )


@facilitator_required
def export_csv(request, kind):
    """Plain CSV export for the LGU records the paper calls for."""
    import csv

    from django.http import HttpResponse

    response = HttpResponse(content_type="text/csv")
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
    response["Content-Disposition"] = f'attachment; filename="{kind}-{stamp}.csv"'
    writer = csv.writer(response)

    if kind == "households":
        writer.writerow(
            ["Household ID", "Owner", "Address", "Contact", "Supply Line", "Status", "Registered"]
        )
        for h in Household.objects.select_related("segment"):
            writer.writerow(
                [
                    h.household_id,
                    h.owner_name,
                    h.address,
                    h.contact_number,
                    h.segment.name if h.segment else "Unassigned",
                    h.get_account_status_display(),
                    h.date_registered,
                ]
            )
    elif kind == "events":
        writer.writerow(
            ["Detected", "Segment", "Module", "Status", "Location", "Pressure (bar)",
             "Pressure (psi)", "Flow (L/min)", "Cleared", "Acknowledged By"]
        )
        for e in LeakEvent.objects.select_related("segment", "acknowledged_by"):
            writer.writerow(
                [
                    timezone.localtime(e.detected_at).strftime("%Y-%m-%d %H:%M:%S"),
                    e.segment.name,
                    e.segment.module_index,
                    e.get_status_display(),
                    e.location_label,
                    round(e.pressure_bar, 2),
                    psi(e.pressure_bar),
                    round(e.flow_lpm, 2),
                    timezone.localtime(e.cleared_at).strftime("%Y-%m-%d %H:%M:%S") if e.cleared_at else "",
                    e.acknowledged_by.username if e.acknowledged_by else "",
                ]
            )
    elif kind == "pressure":
        writer.writerow(
            ["Minute", "Segment", "Module", "Avg Pressure (bar)", "Avg Pressure (psi)",
             "Min (bar)", "Max (bar)", "Avg Flow (L/min)", "Samples", "Leak Samples"]
        )
        since = timezone.now() - timezone.timedelta(days=30)
        for r in ReadingRollup.objects.filter(minute__gte=since).select_related("segment").order_by("minute"):
            writer.writerow(
                [
                    timezone.localtime(r.minute).strftime("%Y-%m-%d %H:%M"),
                    r.segment.name,
                    r.segment.module_index,
                    round(r.avg_pressure_bar, 3),
                    psi(r.avg_pressure_bar),
                    round(r.min_pressure_bar, 3),
                    round(r.max_pressure_bar, 3),
                    round(r.avg_flow_lpm, 3),
                    r.sample_count,
                    r.leak_samples,
                ]
            )
    else:
        writer.writerow(["Unknown export type"])

    return response


# ---------------------------------------------------------------------------
# Owner portal
# ---------------------------------------------------------------------------


@owner_required
def owner_home(request, household):
    segment = household.segment
    latest = getattr(segment, "latest", None) if segment else None
    return render(
        request,
        "owner/home.html",
        {
            "household": household,
            "segment": segment,
            "latest": latest,
            "unread": Notification.objects.filter(recipient=household, read_at__isnull=True).count(),
            "open_complaints": household.complaints.exclude(
                status__in=[Complaint.Status.RESOLVED, Complaint.Status.REJECTED]
            ).count(),
            "recent_notifications": household.notifications.all()[:5],
        },
    )


@owner_required
def owner_notifications(request, household):
    notifications = household.notifications.select_related("segment")
    page = Paginator(notifications, 20).get_page(request.GET.get("page"))
    # Opening the inbox marks what is on screen as read.
    unread_ids = [n.id for n in page if n.read_at is None]
    if unread_ids:
        Notification.objects.filter(id__in=unread_ids).update(read_at=timezone.now())
    return render(request, "owner/notifications.html", {"page": page, "household": household})


@owner_required
def owner_complaints(request, household):
    form = ComplaintForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        complaint = form.save(commit=False)
        complaint.household = household
        complaint.save()
        messages.success(
            request,
            f"Complaint {complaint.reference} submitted. You can track its status on this page.",
        )
        return redirect("owner_complaints")

    return render(
        request,
        "owner/complaints.html",
        {"form": form, "household": household, "complaints": household.complaints.all()},
    )


@owner_required
def owner_pressure(request, household):
    return render(
        request,
        "owner/pressure.html",
        {
            "household": household,
            "segment": household.segment,
            "latest": getattr(household.segment, "latest", None) if household.segment else None,
        },
    )
