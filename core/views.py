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
from django.db.models import Avg, Count, Max, Q
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import (
    ComplaintForm,
    ComplaintResponseForm,
    HouseholdForm,
    MaintenanceForm,
    NotificationForm,
    SegmentForm,
)
from .models import (
    Complaint,
    Device,
    Household,
    LeakEvent,
    Notification,
    ReadingRollup,
    Segment,
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
    context = {
        "segments": segments,
        "open_events": open_events,
        "recent_events": LeakEvent.objects.select_related("segment")[:8],
        "devices": Device.objects.all(),
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
def household_list(request):
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

    page = Paginator(households, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "facilitator/households.html",
        {
            "page": page,
            "query": query,
            "segment_id": segment_id,
            "segments": Segment.objects.filter(is_active=True),
        },
    )


@facilitator_required
def household_form(request, pk=None):
    household = get_object_or_404(Household, pk=pk) if pk else None
    form = HouseholdForm(request.POST or None, instance=household)

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

    return render(request, "facilitator/household_form.html", {"form": form, "household": household})


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
def segment_list(request):
    return render(
        request,
        "facilitator/segments.html",
        {
            "segments": Segment.objects.select_related("device", "latest").annotate(
                household_count=Count("households")
            ),
            "devices": Device.objects.all(),
        },
    )


@facilitator_required
def segment_edit(request, pk):
    segment = get_object_or_404(Segment, pk=pk)
    form = SegmentForm(request.POST or None, instance=segment)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"{segment.name} updated.")
        return redirect("segment_list")
    return render(request, "facilitator/segment_form.html", {"form": form, "segment": segment})


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
        updated = form.save(commit=False)
        if updated.response:
            updated.responded_at = timezone.now()
            updated.responded_by = request.user
        updated.save()

        # Mirror the reply into the owner notification inbox so they see it
        # in the same place as every other message from the facilitator.
        if updated.response:
            Notification.objects.create(
                kind=Notification.Kind.TARGETED,
                title=f"Reply to your complaint {updated.reference}",
                body=updated.response,
                sent_by=request.user,
                recipient=updated.household,
                segment=updated.household.segment,
            )
        messages.success(request, "Complaint updated and the owner has been notified.")
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
