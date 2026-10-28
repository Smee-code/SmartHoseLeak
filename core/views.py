"""
Views for both portals.

Role separation is enforced by the facilitator_required / owner_required
decorators rather than by hiding links, so an owner cannot reach a
facilitator page by typing its URL.
"""

from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login as auth_login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Avg, Count, F, Max, Q
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import formats, timezone
from django.views.decorators.http import require_POST

from .forms import (
    AccountContactForm,
    AdminHouseholdForm,
    AnnouncementForm,
    BarangayForm,
    ComplaintForm,
    ComplaintMessageForm,
    ComplaintStatusForm,
    FacilitatorForm,
    HouseholdActivationForm,
    HouseholdForm,
    HouseholdImportForm,
    owner_credentials,
    DetectionSettingsForm,
    MaintenanceForm,
    NotificationForm,
    RestoreForm,
    SegmentCreateForm,
    SegmentForm,
    WaterSourceForm,
    WorkOrderForm,

)
from .models import (
    Announcement,
    AuditLog,
    Barangay,
    ClaimCode,
    Complaint,
    ComplaintMessage,
    Device,
    ErrorLog,
    Household,
    LeakEvent,
    LoginCode,
    Notification,
    ReadingRollup,
    Segment,
    SmsMessage,
    SystemSetting,
    WaterSource,
    WorkOrder,
    psi,
)
from . import mail, otp, push, routing, setup, sms

User = get_user_model()


def facilitator_required(view):
    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        if not request.user.is_facilitator:
            return HttpResponseForbidden("This page is limited to the water system facilitator.")
        return view(request, *args, **kwargs)

    return wrapper


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        if not request.user.is_admin:
            return HttpResponseForbidden("This page is limited to the system administrator.")
        return view(request, *args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Barangay scoping for the facilitator side
#
# A facilitator works in exactly one barangay and must never see another's
# households, lines, alerts or conversations. Every facilitator query goes
# through these, and every object lookup uses the scoped queryset, so a
# record in another barangay is simply not found - a 404, not a 403 that
# confirms it exists. A facilitator with no barangay yet sees nothing at all,
# and the dashboard says why.
# ---------------------------------------------------------------------------


def _barangay_of(user):
    return getattr(user, "barangay", None)


def _scoped_segments(user):
    barangay = _barangay_of(user)
    if barangay is None:
        return Segment.objects.none()
    return Segment.objects.filter(barangay=barangay)


def _scoped_households(user):
    barangay = _barangay_of(user)
    if barangay is None:
        return Household.objects.none()
    return Household.objects.filter(barangay=barangay)


def _scoped_complaints(user):
    barangay = _barangay_of(user)
    if barangay is None:
        return Complaint.objects.none()
    return Complaint.objects.filter(household__barangay=barangay)


def _scoped_events(user):
    barangay = _barangay_of(user)
    if barangay is None:
        return LeakEvent.objects.none()
    return LeakEvent.objects.filter(segment__barangay=barangay)


def _scoped_rollups(user):
    barangay = _barangay_of(user)
    if barangay is None:
        return ReadingRollup.objects.none()
    return ReadingRollup.objects.filter(segment__barangay=barangay)


def _scoped_devices(user):
    """
    Boards this facilitator may handle: those fitted to a line in their
    barangay, plus spares fitted to nothing yet. A spare has no barangay
    until it is fitted, and it has to be reachable to be fitted at all.
    """
    barangay = _barangay_of(user)
    if barangay is None:
        return Device.objects.none()
    return Device.objects.filter(
        Q(segments__barangay=barangay) | Q(segments__isnull=True)
    ).distinct()


def _scoped_sms(user):
    """Texts about this barangay's households, plus any this facilitator sent."""
    barangay = _barangay_of(user)
    if barangay is None:
        return SmsMessage.objects.none()
    return SmsMessage.objects.filter(
        Q(household__barangay=barangay) | Q(sent_by=user)
    ).distinct()


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


def _home_for(user):
    """
    Where a signed-in user belongs, or None if they have nowhere of their own.

    None is not a quirk to tidy away. A superuser made with createsuperuser is
    neither a facilitator nor a household, and if this returned a destination
    for them anyway, home() and post_login() would redirect to each other for
    as long as the browser allowed it. They get the public page instead.
    """
    if not user.is_authenticated:
        return None
    # Checked before is_facilitator: an admin is also a facilitator, so this
    # order is what sends them to the console rather than the facilitator
    # dashboard - otherwise the two would be indistinguishable here.
    if user.is_admin:
        return "console_dashboard"
    if user.is_facilitator:
        return "dashboard"
    if getattr(user, "household", None) is not None:
        return "owner_home"
    return None


def home(request):
    """
    The public page - and only for the public.

    Signed in, this used to render the marketing page over the top of a
    working session: the facilitator's own logo in the sidebar pointed here,
    so clicking it dropped them out of the dashboard and onto a page inviting
    them to sign in, which they already had. Anyone with a place of their own
    is sent to it.
    """
    destination = _home_for(request.user)
    if destination:
        return redirect(destination)

    return render(request, "home.html", _landing_context())


def _landing_context():
    """
    Everything the public page needs.

    Shared with the sign-in view, which re-renders the landing page - with
    the sign-in dialog reopened and the error inside it - when a login fails,
    so a typo never dumps the visitor onto a separate bare page.
    """
    segments = (
        Segment.objects.filter(is_active=True)
        .select_related("latest", "device", "household")
        .order_by("name")
    )
    stats = {
        "households": Household.objects.filter(account_status=Household.AccountStatus.ACTIVE).count(),
        "segments": segments.count(),
        "monitored_lines": segments.count(),
        # How many lines actually have a board on them. The public page used
        # to answer this by naming every household and the state of its line
        # in a table; counted, it makes the same point without publishing a
        # roster of who is connected to the water system.
        "fitted": sum(1 for segment in segments if segment.has_device),
        "open_alerts": LeakEvent.objects.filter(
            cleared_at__isnull=True, severity=LeakEvent.Severity.CRITICAL
        ).count(),
        "events_30d": LeakEvent.objects.filter(
            detected_at__gte=timezone.now() - timezone.timedelta(days=30),
            status=Segment.Status.POSSIBLE_LEAK,
        ).count(),
    }
    return {"segments": segments, "stats": stats}


class LandingLoginView(LoginView):
    """
    Sign in, as a dialog over the landing page.

    The standalone /login/ page still exists - it is where a protected URL
    sends a signed-out visitor, and it works with no JavaScript - but it
    renders the very same dialog. A failed attempt comes back to the landing
    page with the dialog open and the error inside it, keeping the "next"
    destination so a deep link still lands where it was going once the
    password is right. Where a successful sign-in goes is decided by role,
    in post_login, exactly as before.
    """

    template_name = "registration/login.html"

    def form_valid(self, form):
        """
        The password was right. Now the second step, unless this device is
        trusted or the account has nowhere to receive a code.

        The user is NOT signed in yet: their id is parked in the session and
        the code page finishes the sign-in. An account with no number and no
        email cannot be sent a code, so it signs in on the password alone
        with a warning - locking it out would be worse than a missing step.
        """
        user = form.get_user()
        if not settings.OTP_ENABLED or otp.is_trusted(self.request, user):
            return super().form_valid(form)

        preferred = self.request.POST.get("otp_channel") or LoginCode.Channel.SMS
        try:
            code = otp.send_code(user, preferred)
        except otp.DeliveryError as exc:
            # Not signed in, not locked out: back to the dialog with the reason.
            return self.form_invalid(form, problem=str(exc))
        if code is None:
            response = super().form_valid(form)
            messages.warning(
                self.request,
                "Signed in without a code: this account has no contact number or email "
                "on file. Ask the administrator to add one.",
            )
            return response

        self.request.session["otp_user"] = user.pk
        self.request.session["otp_next"] = self.get_success_url()
        self.request.session["otp_remember"] = bool(self.request.POST.get("remember_device"))
        if settings.DEBUG:
            # A demo must not depend on SMS credit or a mailbox. Never in production.
            messages.info(self.request, f"DEBUG only: your sign-in code is {code.code}.")
        return redirect("login_code")

    def form_invalid(self, form, problem=None):
        """
        Back to the dialog. `problem` replaces the wrong-password wording when
        the password was right but the sign-in code could not be sent.
        """
        context = _landing_context()
        context.update({
            "login_failed": True,
            "login_problem": problem,
            "login_modal_open": True,
            "login_username": (form.data.get("username") or "").strip(),
            "next": self.request.POST.get("next", "") or self.request.GET.get("next", ""),
        })
        return render(self.request, "home.html", context)


@login_required
def post_login(request):
    """
    Send each role to its own landing page after authentication.

    Also what the brand in the header points at once somebody is signed in,
    so "take me home" means their dashboard rather than the public page.
    """
    return redirect(_home_for(request.user) or "home")


# ---------------------------------------------------------------------------
# Facilitator
# ---------------------------------------------------------------------------


@facilitator_required
def dashboard(request):
    barangay = _barangay_of(request.user)
    segments = (
        _scoped_segments(request.user).filter(is_active=True).select_related("latest", "device")
    )
    open_events = (
        _scoped_events(request.user).filter(cleared_at__isnull=True)
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
    source = WaterSource.for_barangay(barangay)

    # The facilitator's own field work, for the summary card: what is next
    # and how much is open, so the day starts here rather than on the
    # schedule page. Undated jobs sort last - a date is what makes it next.
    assignments = WorkOrder.objects.filter(assigned_to=request.user).exclude(
        status__in=WorkOrder.CLOSED_STATUSES
    )
    upcoming = list(
        assignments.select_related("household", "segment")
        .order_by(F("scheduled_for").asc(nulls_last=True), "created_at")[:3]
    )

    # Problems first, so a table that grows with the barangay still shows
    # what matters without scrolling: leaks, then low supply, then a board
    # gone quiet, then maintenance, then normal, then lines still waiting
    # for a board. is_problem drives the "Problems only" quick filter.
    def _priority(segment):
        if segment.status == Segment.Status.POSSIBLE_LEAK:
            return 0
        if segment.status == Segment.Status.LOW_SUPPLY:
            return 1
        if segment.status == Segment.Status.OFFLINE and segment.has_device:
            return 2
        if segment.status == Segment.Status.MAINTENANCE:
            return 3
        if segment.status == Segment.Status.NORMAL:
            return 4
        return 5

    segment_rows = sorted(segments, key=lambda s: (_priority(s), s.name))
    for row in segment_rows:
        row.is_problem = _priority(row) <= 3

    # The alerts panel is capped; the total is what the standing sentence
    # and the "View all" link say, so nothing is silently hidden.
    open_events_total = open_events.count()
    open_events = list(open_events[:6])

    context = {
        "segments": segments,
        "segment_rows": segment_rows,
        "open_events": open_events,
        "open_events_total": open_events_total,
        # The quick filter's state comes from the address, so a reload or a
        # shared link keeps it. The rows themselves are all sent; the filter
        # hides on the page and the live poll keeps updating hidden rows.
        "filter_q": (request.GET.get("q") or "").strip(),
        "filter_problems": request.GET.get("problems") == "1",
        "recent_events": _scoped_events(request.user).select_related("segment")[:8],
        "devices": Device.objects.filter(segments__in=segments).distinct(),
        "ingest_url": f"{request.scheme}://{host}{reverse('api_ingest')}",
        "ingest_url_is_loopback": loopback,
        "barangay": barangay,
        "no_barangay": barangay is None,
        "upcoming": upcoming,
        "open_assignment_count": assignments.count(),
        "pending_complaints": _scoped_complaints(request.user).filter(
            status=Complaint.Status.PENDING
        ).count(),
        "household_count": _scoped_households(request.user).filter(
            account_status=Household.AccountStatus.ACTIVE
        ).count(),
        # Split so the page can lead with a sentence rather than three equal
        # numbers. "6 of 7 lines have no board yet" is the rollout going to
        # plan; "1 board has stopped reporting" is somebody's water going
        # unwatched. A single Offline count cannot tell those apart, and the
        # facilitator's whole morning depends on which one it is.
        "fitted_count": segments.filter(device__isnull=False).count(),
        "awaiting_device_count": segments.filter(device__isnull=True).count(),
        # A board that was fitted and has gone quiet. Counted separately from
        # a line that never had one, because only this is a fault - and
        # without it the page could announce "everything is reading normally"
        # while a household's board sat dead, which is the one sentence this
        # screen must never get wrong.
        "silent_count": segments.filter(
            device__isnull=False, status=Segment.Status.OFFLINE
        ).count(),
        "map_segments": [
            {
                "id": s.id,
                "module": s.module_index,
                "name": s.name,
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
                # So the live poll can find this household's hose and restyle
                # it when its status changes, without redrawing the map.
                "segment_id": h.segment_id,
                "module": h.segment.module_index if h.segment else None,
                "color": h.segment.color if h.segment else None,
                "status": h.segment.status if h.segment else "OFFLINE",
                # Lets the map draw an unfitted hose as a dashed line rather
                # than one that looks monitored but never changes colour.
                "has_device": bool(h.segment and h.segment.has_device),
                "coords": [h.latitude, h.longitude],
                # The road route to the house, stored on the record so the map
                # draws it from here with no further network; and the one-tap
                # directions link for the last stretch.
                "route": h.visit_route,
                "route_distance_m": h.visit_route_distance_m,
                "route_duration_s": h.visit_route_duration_s,
                "navigate_url": h.navigate_url,
            }
            for h in _scoped_households(request.user).select_related("segment", "segment__device")
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
            if source is not None and source.has_location
            else None
        ),
    }
    return render(request, "facilitator/dashboard.html", context)


@facilitator_required
def pressure_monitor(request):
    """Centralised pressure table across every household and supply line."""
    households = (
        _scoped_households(request.user).select_related("segment", "segment__latest")
        .filter(account_status=Household.AccountStatus.ACTIVE)
        .order_by("segment__module_index", "household_id")
    )
    return render(
        request,
        "facilitator/pressure.html",
        {
            "households": households,
            "segments": _scoped_segments(request.user).filter(is_active=True).select_related("latest"),
        },
    )


@facilitator_required
def household_list(request, edit_form=None, edit_household=None, activation_form=None, activate_household=None):
    """
    The household table, with the activate / edit / delete dialogs alongside.

    Households are entered by the administrator and arrive here pending; the
    facilitator activates each one. edit_form and activation_form come back
    from household_form() / household_activate() when a submission fails
    validation, so the dialog reopens with the values and errors in place.
    """
    query = request.GET.get("q", "").strip()
    show = (request.GET.get("show") or "").strip()
    scoped = _scoped_households(request.user)
    households = scoped.select_related("segment", "segment__device", "user")

    if show == "pending":
        households = households.filter(user__isnull=True, activated_at__isnull=True)
    elif show == "active":
        households = households.exclude(user__isnull=True, activated_at__isnull=True)
    if query:
        households = households.filter(
            Q(household_id__icontains=query)
            | Q(owner_name__icontains=query)
            | Q(address__icontains=query)
            | Q(contact_number__icontains=query)
        )
    # Counted here so the delete dialog can say exactly what goes with the
    # household, rather than warning vaguely about "related records".
    # order_by is explicit because aggregating adds a GROUP BY, and Django
    # warns that paginating an unordered queryset can repeat or skip rows.
    households = households.annotate(
        complaint_count=Count("complaints", distinct=True),
        notification_count=Count("notifications", distinct=True),
    ).order_by("household_id")

    page = Paginator(households, 25).get_page(request.GET.get("page"))
    # ?activate=<pk>: come back with that household's dialog open (after a
    # board was registered for it from the row).
    reopen = None
    if (request.GET.get("activate") or "").isdigit():
        reopen = scoped.filter(pk=int(request.GET["activate"]), user__isnull=True).select_related("segment", "segment__device").first()
    return render(
        request,
        "facilitator/households.html",
        {
            "page": page,
            "query": query,
            "show": show,
            "total": scoped.count(),
            "pending_total": scoped.filter(user__isnull=True, activated_at__isnull=True).count(),
            # auto_id keeps each dialog's field ids distinct from the others';
            # duplicate ids would break every label and every JS lookup.
            "edit_form": edit_form or HouseholdForm(auto_id="edit_%s"),
            "edit_form_errors": bool(edit_form and edit_form.errors),
            "edit_household": edit_household,
            "activation_form": activation_form or HouseholdActivationForm(auto_id="activate_%s"),
            "activation_errors": bool(activation_form and activation_form.errors),
            "activate_household": activate_household or reopen,
            "activation_autoopen": bool(activation_form and activation_form.errors) or reopen is not None,
        },
    )


@facilitator_required
def household_form(request, pk):
    """
    Edit a household's own details. Creating one is the administrator's
    job, and creating its login is activation's - this is neither.
    """
    household = get_object_or_404(_scoped_households(request.user), pk=pk)
    # Renders into the shared edit dialog, which namespaces its ids.
    form = HouseholdForm(request.POST or None, instance=household, auto_id="edit_%s")

    if request.method == "POST" and form.is_valid():
        record = form.save()
        # The road route to the house, refreshed in case the coordinates moved.
        routing.update_household_route(record)
        messages.success(request, "Household record saved.")
        return redirect("household_list")

    # A failed submission came from the dialog on the list page, so send the
    # bound form back there instead of to the standalone page - otherwise the
    # dialog appears to have thrown the typed values away.
    if request.method == "POST":
        return household_list(request, edit_form=form, edit_household=household)

    return render(
        request,
        "facilitator/household_form.html",
        {"form": form, "household": household},
    )


@facilitator_required
@require_POST
def household_activate(request, pk):
    """
    The facilitator's half of registering a household.

    The administrator entered who they are; the facilitator confirms where
    the house is and, in the same act, the owner's portal login comes into
    being. The temporary password is shown once here and sent to the
    household by text and by email, whichever they have.
    """
    household = get_object_or_404(
        _scoped_households(request.user).select_related("segment", "segment__device", "user"), pk=pk
    )
    if household.user_id is not None:
        messages.info(request, f"{household.household_id} already has an Owner Portal login.")
        return redirect("household_list")
    device = household.segment.device if household.segment_id else None
    if device is None:
        # The owner connects the board themselves, with a code sent at this
        # moment - so there has to be a board for the code to belong to.
        messages.error(
            request,
            f"{household.household_id} has no monitoring board yet. Register one for it first "
            f"(the Register board button on its row), then activate.",
        )
        return redirect(f"{reverse('household_list')}?q={household.household_id}")

    form = HouseholdActivationForm(request.POST, auto_id="activate_%s")
    if not form.is_valid():
        return household_list(request, activation_form=form, activate_household=household)

    username, password = owner_credentials(household.owner_name)
    first, _, rest = household.owner_name.strip().partition(" ")
    with transaction.atomic():
        owner = User.objects.create_user(
            username=username,
            password=password,
            role=User.Role.OWNER,
            first_name=first,
            last_name=rest,
            contact_number=household.contact_number,
            email=household.email,
            must_change_password=True,
        )
        household.latitude = form.cleaned_data["latitude"]
        household.longitude = form.cleaned_data["longitude"]
        household.user = owner
        household.account_status = Household.AccountStatus.ACTIVE
        household.activated_at = timezone.now()
        household.activated_by = request.user
        household.save()
    routing.update_household_route(household)

    sent = []
    if _text_credentials(request, household, username, password):
        sent.append(f"text to {household.contact_number}")
    if _email_credentials(request, household, username, password):
        sent.append(f"email to {household.email}")
    # The board's setup code: a second message, because one text carrying a
    # password and a code splits into several parts and reads as noise.
    ticket = device.issue_claim_code()
    code_sent = setup.send_setup_code(request, household, ticket, sent_by=request.user)
    AuditLog.record(
        request.user, "household.activate",
        f"Activated {household.household_id} ({household.owner_name}); login '{username}' created; "
        f"setup code issued for {device.device_id}",
        target=household.household_id,
    )
    messages.success(
        request,
        f"{household.household_id} activated. Owner Portal login - username: {username} / "
        f"temporary password: {password}. Write this down now; it is not shown again."
        + (f" Also sent by {' and '.join(sent)}." if sent
           else " The household has no number or email on file, so pass it on yourself.")
        + f" Board setup code for {device.device_id}: {ticket.code}"
        + (f", sent by {' and '.join(code_sent)}." if code_sent else " - pass it on with the login.")
        + " The owner connects the board themselves from Settings > My device.",
    )
    return redirect("household_list")


@facilitator_required
@require_POST
def household_register_board(request, pk):
    """
    Register a board for a household that has none, from its row.

    Activation needs a board to issue the code for, so this is the one step
    a facilitator takes before pressing Activate on a household the
    administrator registered without hardware. The hose line is created
    here too if the record somehow has none.
    """
    household = get_object_or_404(_scoped_households(request.user).select_related("segment", "segment__device"), pk=pk)
    if household.segment is None:
        household.segment = Segment.objects.create(
            module_index=1,
            name=f"{household.household_id} - {household.owner_name}",
            color=Segment.next_color(),
            barangay=household.barangay,
        )
        household.save(update_fields=["segment"])
    if household.segment.device is not None:
        messages.info(request, f"{household.household_id} already has board {household.segment.device.device_id}.")
    else:
        device = Device.objects.create(label=f"{household.household_id} {household.owner_name}"[:120])
        household.segment.device = device
        household.segment.save(update_fields=["device"])
        AuditLog.record(request.user, "board.register", f"Registered board {device.device_id} for {household.household_id}", target=device.device_id)
        messages.success(
            request,
            f"Board {device.device_id} registered for {household.household_id}. "
            f"Its setup code is sent to the owner when you activate the household.",
        )
    if household.user_id is None:
        return redirect(f"{reverse('household_list')}?activate={household.pk}")
    return redirect("household_list")


@facilitator_required
def household_detail(request, pk):
    household = get_object_or_404(
        _scoped_households(request.user).select_related("segment", "segment__latest", "user"), pk=pk
    )
    return render(
        request,
        "facilitator/household_detail.html",
        {
            "household": household,
            # The Edit button opens this, bound to the record. A failed save
            # lands on the list with the same dialog reopened and the errors.
            "edit_form": HouseholdForm(instance=household, auto_id="edit_%s"),
            "edit_household": household,
            "routing_ready": routing.describe_routing()[0],
            "routing_note": routing.describe_routing()[1],
            "complaints": household.complaints.all()[:10],
            # Same folding as the Notices page. Every reply in a thread also
            # becomes a notice for this household, so their own page was
            # mostly a card per "ok" - and this household's page is the one
            # place all of their replies land together.
            "notifications": _recent_notice_feed(
                limit=12, queryset=household.notifications.select_related("complaint")
            ),
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
    household = get_object_or_404(_scoped_households(request.user).select_related("user"), pk=pk)

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
    household = get_object_or_404(_scoped_households(request.user), pk=pk)
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
    barangay = _barangay_of(request.user)
    source = WaterSource.for_barangay(barangay)

    # Two forms post to this page's neighbourhood, and both have a "name"
    # field. Without this marker a failed add-line submission would bind to
    # the water source form and could save the line's name over the source.
    is_source_post = request.method == "POST" and request.POST.get("form_kind") == "source"
    # No barangay, no source to place: the page says so instead of a form.
    source_form = (
        WaterSourceForm(request.POST if is_source_post else None, instance=source)
        if source is not None else None
    )

    if is_source_post and source_form is not None and source_form.is_valid():
        source_form.save()
        # The source moved, so every route in this barangay now starts
        # somewhere else. Refetched here; a failure keeps the old route and
        # leaves a note on that household.
        for household in _scoped_households(request.user).filter(
            latitude__isnull=False, longitude__isnull=False
        ):
            routing.update_household_route(household)
        messages.success(request, "Water source location saved.")
        return redirect("segment_list")

    return render(
        request,
        "facilitator/segments.html",
        {
            # Counted so the delete dialog can say exactly how much history
            # goes with the line rather than warning about it vaguely.
            "segments": _scoped_segments(request.user).select_related(
                "device", "latest", "household"
            ).annotate(
                rollup_count=Count("rollups", distinct=True),
                event_count=Count("events", distinct=True),
            ).order_by("name"),
            "devices": _scoped_devices(request.user).select_related("claim_code").prefetch_related("segments"),
            "source": source,
            "source_form": source_form,
            "barangay": barangay,
            # Three forms with a "name" field share this page - the water
            # source, the add dialog and the edit dialog - so each needs its
            # own id prefix or their labels focus the wrong input.
            "create_form": create_form or SegmentCreateForm(
                device=Device.objects.first(), auto_id="new_%s"
            ),
            "create_form_errors": bool(create_form and create_form.errors),
            "edit_form": edit_form or _blank_segment_form(request.user),
            "edit_form_errors": bool(edit_form and edit_form.errors),
            "edit_segment": edit_segment,
        },
    )


@facilitator_required
@require_POST
def device_create(request):
    """
    Register a spare board.

    The normal way to get a board onto the system is to add the household
    that owns it. This is for the other case: buying two boards and claiming
    one now, or preparing a replacement before walking to a house.

    Nothing about the identity is taken from the request. The id and key are
    issued by the model - a key a person can choose is a key they can reuse
    across two boards, and two boards reporting as the same household
    overwrite each other's readings with nothing to show for it.
    """
    device = Device.objects.create(label=request.POST.get("label", "").strip()[:120])
    ticket = device.issue_claim_code()
    messages.success(
        request,
        f"Board {device.device_id} registered. Claim code: {ticket.code} - enter it on the "
        f"board's setup page along with the WiFi. It works once and stays on this page.",
    )
    return redirect("segment_list")


@facilitator_required
@require_POST
def device_regenerate_key(request, pk):
    """
    Issue a new key and invalidate the old one.

    Separate from editing on purpose. The board goes silent the instant this
    runs and stays silent until somebody walks to it and enters the new key,
    so it is a decision with a cost, not a field being changed.
    """
    device = get_object_or_404(_scoped_devices(request.user), pk=pk)
    device.regenerate_key()

    line = device.line
    where = f" {line.name} will not report" if line else " That board will not report"
    messages.warning(
        request,
        f"New key issued for {device.device_id}.{where} until you enter it on the "
        f"board's setup page. New key: {device.api_key}",
    )
    return redirect("segment_list")


@facilitator_required
@require_POST
def device_issue_claim_code(request, pk):
    """
    Issue a fresh claim code for a board.

    Needed when the first one was used and the board has to be set up again -
    moved to a different house, or reset. Unlike reissuing the key, this
    costs nothing: a board already claimed keeps working, because the code is
    only how an unclaimed board gets its identity in the first place.
    """
    device = get_object_or_404(_scoped_devices(request.user), pk=pk)
    ticket = device.issue_claim_code()
    messages.success(
        request,
        f"New claim code for {device.device_id}: {ticket.code}. "
        f"It works once, and expires in {ClaimCode.VALID_DAYS} days.",
    )
    return redirect("segment_list")


@facilitator_required
@require_POST
def device_delete(request, pk):
    """
    Remove a board from the system.

    Refused while it is fitted to a line: deleting it would silently detach
    that household's hose and leave the dashboard reporting "No device" with
    no explanation of where it went.
    """
    device = get_object_or_404(_scoped_devices(request.user), pk=pk)
    line = device.line
    if line is not None:
        messages.error(
            request,
            f"{device.device_id} is fitted to {line.name}. Detach it there first, "
            "so that household does not silently lose its board.",
        )
        return redirect("segment_list")

    device_id = device.device_id
    device.delete()
    messages.success(request, f"Board {device_id} removed.")
    return redirect("segment_list")


@facilitator_required
def segment_create(request):
    """
    Add a hose line. Posted from the dialog on the Lines page.

    Created without a device. A hose is dug and laid long before a board is
    fitted to it, and requiring hardware up front would mean either refusing
    to record real lines or attaching them to a board that is not there - the
    second of which makes the dashboard report a fault on a line nobody has
    installed yet. The board is attached from Edit once it arrives.
    """
    form = SegmentCreateForm(request.POST or None, auto_id="new_%s")
    if request.method == "POST" and form.is_valid():
        segment = form.save(commit=False)
        segment.barangay = _barangay_of(request.user)   # a line is laid where its facilitator works
        segment.save()
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
    segment = get_object_or_404(_scoped_segments(request.user), pk=pk)
    # auto_id namespaces the fields so the edit dialog cannot collide with the
    # add dialog, which renders the same fields on the same page.
    form = SegmentForm(request.POST or None, instance=segment, auto_id="edit_%s")
    # Only boards this facilitator may handle: their own lines' and spares.
    form.fields["device"].queryset = _scoped_devices(request.user)
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
    segment = get_object_or_404(_scoped_segments(request.user), pk=pk)

    label = segment.name
    rollups = segment.rollups.count()
    events = segment.events.count()
    household = getattr(segment, "household", None)

    with transaction.atomic():
        segment.delete()

    detail = f"{rollups} minute(s) of history and {events} event(s) were removed"
    if household is not None:
        detail += f"; {household.household_id} now has no hose line"
    messages.success(request, f"Deleted {label}. {detail}.")
    return redirect("segment_list")


@facilitator_required
@require_POST
def segment_valve_test(request, pk):
    """
    Ask this household's device to run its sealed-line pressure test now.

    The test is the only way to find a leak too small for the flow sensor to
    register, and it normally runs itself at night. This is the manual
    trigger for a demonstration, or for checking a line somebody has just
    repaired without waiting hours for the next scheduled run.

    Nothing is sent from here. The board dials out and is not reachable, so
    the request is parked on the line and handed over on the reply to its
    next upload - a couple of seconds at most.
    """
    segment = get_object_or_404(_scoped_segments(request.user), pk=pk)

    if not segment.has_device:
        messages.error(
            request, f"{segment.name} has no device fitted, so there is no valve to test."
        )
        return redirect(request.POST.get("next") or "dashboard")

    segment.valve_test_requested = True
    segment.save(update_fields=["valve_test_requested"])
    messages.success(
        request,
        f"Valve test requested for {segment.name}. The device will close the line "
        "briefly, then reopen it on its own.",
    )
    return redirect(request.POST.get("next") or "dashboard")


@facilitator_required
@require_POST
def segment_maintenance(request, pk):
    """
    Toggle a segment in and out of maintenance.

    While a segment is under maintenance the ingest endpoint stops raising
    events for it, so a repair crew opening the line does not generate a
    stream of false leak alerts.
    """
    segment = get_object_or_404(_scoped_segments(request.user), pk=pk)
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
    events = _scoped_events(request.user).select_related("segment", "acknowledged_by")
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
    event = get_object_or_404(_scoped_events(request.user), pk=pk)
    event.acknowledged_at = timezone.now()
    event.acknowledged_by = request.user
    note = request.POST.get("resolution_note", "").strip()
    if note:
        event.resolution_note = note
    event.save(update_fields=["acknowledged_at", "acknowledged_by", "resolution_note"])
    messages.success(request, f"Alert on {event.segment.name} acknowledged.")
    return redirect(request.POST.get("next") or "event_list")


def _complaint_threads(user, status=""):
    """
    The inbox list: every complaint as a conversation, newest activity first.
    Only this facilitator's barangay - see the scoping helpers above.

    Ordered by last_message_at rather than created_at so a thread rises when
    someone writes in it, which is the whole point of reading it as a
    message list instead of a ticket table.
    """
    threads = (
        _scoped_complaints(user).select_related("household", "household__segment")
        .annotate(
            unread=Count(
                "messages",
                filter=Q(messages__from_owner=True, messages__read_by_facilitator=False),
                distinct=True,
            )
        )
        .order_by("-last_message_at")
    )
    if status:
        threads = threads.filter(status=status)
    return threads


@facilitator_required
def complaint_list(request, pk=None):
    """
    Conversation inbox, and the open thread beside it.

    One view serves both so the list stays on screen while reading a thread,
    the way a mail client does. On a narrow screen the CSS shows one pane at
    a time.
    """
    status = request.GET.get("status", "")
    threads = _complaint_threads(request.user, status)

    complaint = None
    message_form = None
    status_form = None

    if pk is not None:
        complaint = get_object_or_404(
            _scoped_complaints(request.user).select_related("household", "household__segment"), pk=pk
        )
        # Opening a thread is what marks the owner's messages as seen.
        complaint.messages.filter(from_owner=True, read_by_facilitator=False).update(
            read_by_facilitator=True
        )
        message_form = ComplaintMessageForm()
        status_form = ComplaintStatusForm(instance=complaint)

    return render(
        request,
        "facilitator/complaints.html",
        {
            "threads": threads[:60],
            "complaint": complaint,
            "messages_list": complaint.messages.select_related("author") if complaint else None,
            "message_form": message_form,
            "status_form": status_form,
            "status": status,
            # (key, label, count) so the filter chips can show their totals -
            # a template cannot look a value up in a dict by variable key.
            "status_filters": [
                (key, label, _scoped_complaints(request.user).filter(status=key).count())
                for key, label in Complaint.Status.choices
            ],
        },
    )


@facilitator_required
@require_POST
def complaint_reply(request, pk):
    """Facilitator adds a message to a thread, and the owner is told."""
    complaint = get_object_or_404(
        _scoped_complaints(request.user).select_related("household", "household__segment"), pk=pk
    )
    form = ComplaintMessageForm(request.POST)

    if not form.is_valid():
        messages.error(request, "Write something before sending.")
        return redirect("complaint_thread", pk=pk)

    with transaction.atomic():
        message = form.save(commit=False)
        message.complaint = complaint
        message.author = request.user
        message.from_owner = False
        message.read_by_facilitator = True   # the sender has obviously read it
        message.save()

        # Mirror into the owner's inbox so a reply is visible from the bell
        # as well as inside the thread.
        Notification.objects.create(
            kind=Notification.Kind.TARGETED,
            title=f"Reply to your complaint {complaint.reference}",
            body=message.body,
            sent_by=request.user,
            recipient=complaint.household,
            segment=complaint.household.segment,
            complaint=complaint,
        )

    messages.success(request, "Reply sent. The owner has been notified.")
    return redirect("complaint_thread", pk=pk)


@facilitator_required
@require_POST
def complaint_status(request, pk):
    """Move a thread between Pending / In Progress / Resolved / Rejected."""
    complaint = get_object_or_404(_scoped_complaints(request.user), pk=pk)
    form = ComplaintStatusForm(request.POST, instance=complaint)
    if form.is_valid():
        form.save()
        messages.success(request, f"Marked as {complaint.get_status_display()}.")
    return redirect("complaint_thread", pk=pk)


@facilitator_required
def notification_center(request):
    """
    Write once, deliver on the chosen channels.

    The in-app notice and the text carry the same words on purpose. Two
    compose screens would eventually disagree with each other, and a
    household reading one thing on their phone and another in the app has no
    way to tell which is current.
    """
    barangay = _barangay_of(request.user)
    form = NotificationForm(request.POST or None, barangay=barangay)

    if request.method == "POST" and form.is_valid():
        recipients, kind = form.recipients()
        if not recipients:
            messages.error(request, "No active households match that audience.")
        else:
            title = form.cleaned_data["title"]
            body = form.cleaned_data["body"]
            want_app = form.cleaned_data["in_app"]
            want_sms = form.cleaned_data["by_sms"]
            want_email = form.cleaned_data.get("by_email")

            notices_by_household = {}
            if want_app:
                created = Notification.objects.bulk_create(
                    [
                        Notification(
                            kind=kind,
                            title=title,
                            body=body,
                            sent_by=request.user,
                            recipient=household,
                            segment=household.segment,
                        )
                        for household in recipients
                    ]
                )
                notices_by_household = {n.recipient_id: n for n in created}

            queued = 0
            no_number = []
            if want_sms:
                for household in recipients:
                    # The title is prefixed so a text stands on its own. An
                    # SMS arrives with no subject line and no app around it,
                    # so a bare body reads as a message from nobody.
                    sms_body = f"{title}: {body}"
                    parts = SmsMessage.queue(
                        household,
                        sms_body,
                        kind=SmsMessage.Kind.MANUAL,
                        sent_by=request.user,
                        notification=notices_by_household.get(household.id),
                    )
                    if parts:
                        queued += len(parts)
                    else:
                        no_number.append(household.household_id)

            emailed = 0
            if want_email:
                for household in recipients:
                    address = _email_for(household)
                    if address and mail.send_email(address, title, body):
                        emailed += 1

            done = []
            if want_app:
                done.append(f"shown in the app to {len(recipients)} household(s)")
            if want_sms:
                done.append(f"{queued} text message(s) queued")
            if want_email:
                done.append(f"{emailed} email(s) sent")
            messages.success(request, "Notice " + " and ".join(done) + ".")

            if no_number:
                # Said plainly rather than counted, because the fix is to go
                # and collect those numbers and the facilitator needs to know
                # whose are missing.
                messages.warning(
                    request,
                    "No contact number on file, so no text was sent to: "
                    + ", ".join(no_number) + ".",
                )
            return redirect("notification_center")

    # Only notices to this barangay's households; nothing for a facilitator
    # who has none yet.
    notices = (
        Notification.objects.filter(
            kind__in=[Notification.Kind.TARGETED, Notification.Kind.BROADCAST],
            recipient__barangay=barangay,
        ).select_related("recipient", "complaint")
        if barangay is not None else Notification.objects.none()
    )
    sent = _recent_notice_feed(queryset=notices)
    gateway_ready, gateway_note = sms.describe_gateway()
    return render(
        request,
        "facilitator/notifications.html",
        {
            "form": form,
            "sent": sent,
            "gateway_ready": gateway_ready,
            "gateway_note": gateway_note,
            "sms_messages": _scoped_sms(request.user).select_related("household").order_by("-created_at")[:40],
            "sms_queued": _scoped_sms(request.user).filter(status=SmsMessage.Status.QUEUED).count(),
            "sms_awaiting": _scoped_sms(request.user).filter(status=SmsMessage.Status.SENT).count(),
            "sms_failed": _scoped_sms(request.user).filter(
                status__in=[SmsMessage.Status.FAILED, SmsMessage.Status.UNDELIVERED]
            ).count(),
            "sms_single_limit": SmsMessage.SINGLE_LIMIT,
            "auto_sms_on": settings.SMS_AUTO_ON_LEAK,
        },
    )


def _recent_notice_feed(limit=40, queryset=None):
    """
    What has gone out, collapsed so a conversation does not fill the page.

    Every reply in a complaint thread also becomes a notice for the owner, so
    a five-message exchange used to produce five entries here, each with a
    full title ("Reply to your complaint CMP-260911-96A2") wrapped around a
    body like "ok". The list meant for notices the facilitator composed was
    almost entirely made of them.

    Consecutive replies to the same complaint are folded into one row showing
    the latest, with a count of the rest. Only consecutive ones: a run broken
    by something else is genuinely two separate moments, and merging across
    the gap would put a reply above a notice that came after it.
    """
    rows = []
    base = queryset if queryset is not None else Notification.objects.filter(
        kind__in=[Notification.Kind.TARGETED, Notification.Kind.BROADCAST]
    ).select_related("recipient", "complaint")

    # Over-fetch, because folding consecutive replies means the number of rows
    # produced is smaller than the number of records read.
    notifications = base.order_by("-created_at")[: limit * 3]

    for notification in notifications:
        if (
            notification.complaint_id
            and rows
            and rows[-1]["complaint"] is not None
            and rows[-1]["complaint"].pk == notification.complaint_id
        ):
            rows[-1]["also"] += 1
            continue

        rows.append(
            {
                "notification": notification,
                "complaint": notification.complaint,
                "also": 0,
            }
        )
        if len(rows) >= limit:
            break

    return rows


@facilitator_required
@require_POST
def sms_retry(request, pk):
    """
    Put a failed message back in the queue.

    Failures here are almost always the site, not the message - no signal, no
    load, the modem unplugged. Once that is fixed the facilitator should be
    able to send the same words again without retyping them.
    """
    message = get_object_or_404(_scoped_sms(request.user), pk=pk)
    message.status = SmsMessage.Status.QUEUED
    message.attempts = 0
    message.error = ""
    # A retry is a new message as far as the gateway is concerned, so the old
    # id must go - otherwise the delivery poll would keep reading the failed
    # attempt's verdict and overwrite the new one's.
    message.provider_message_id = ""
    message.provider_status = ""
    message.save(update_fields=["status", "attempts", "error",
                                "provider_message_id", "provider_status"])
    messages.success(request, f"Queued again for {message.recipient_label}.")
    return redirect("notification_center")


@facilitator_required
def reports(request):
    """Summary figures plus CSV export for the pressure and event logs."""
    since = timezone.now() - timezone.timedelta(days=30)
    per_segment = []
    for segment in _scoped_segments(request.user).filter(is_active=True):
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
            "complaint_counts": _scoped_complaints(request.user).values("status").annotate(n=Count("id")),
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
        for h in _scoped_households(request.user).select_related("segment"):
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
        for e in _scoped_events(request.user).select_related("segment", "acknowledged_by"):
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
        for r in _scoped_rollups(request.user).filter(minute__gte=since).select_related("segment").order_by("minute"):
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
    week_ago = timezone.now() - timezone.timedelta(days=7)

    # A visit the administrator has scheduled for this house: the one piece
    # of the admin's work an owner should see coming, so a knock on the door
    # is expected rather than a surprise.
    next_visit = (
        household.work_orders.filter(
            status__in=[WorkOrder.Status.SCHEDULED, WorkOrder.Status.IN_PROGRESS]
        )
        .filter(Q(scheduled_for__gte=timezone.localdate()) | Q(scheduled_for__isnull=True))
        .select_related("assigned_to")
        .order_by(F("scheduled_for").asc(nulls_last=True))
        .first()
    )
    return render(
        request,
        "owner/home.html",
        {
            "household": household,
            "segment": segment,
            "latest": latest,
            "next_visit": next_visit,
            "unread": Notification.objects.filter(recipient=household, read_at__isnull=True).count(),
            "notice_total": household.notifications.count(),
            "open_complaints": household.complaints.exclude(
                status__in=[Complaint.Status.RESOLVED, Complaint.Status.REJECTED]
            ).count(),
            "recent_notifications": household.notifications.all()[:5],
            # The past week in two numbers, so "has anything happened" has an
            # answer without reading the feed.
            "week_alerts": (
                segment.events.filter(
                    status=Segment.Status.POSSIBLE_LEAK, detected_at__gte=week_ago
                ).count() if segment else 0
            ),
            "week_notices": household.notifications.filter(created_at__gte=week_ago).count(),
        },
    )


@owner_required
def owner_notifications(request, household):
    notifications = household.notifications.select_related("segment", "complaint")
    page = Paginator(notifications, 20).get_page(request.GET.get("page"))

    # Read before marking: once these are flagged read the template can no
    # longer tell which ones were new on this visit, and "new since you last
    # looked" is the most useful thing the page can show.
    was_unread = {n.id for n in page if n.read_at is None}

    # Grouped by day so a long list reads as a timeline rather than a wall.
    today = timezone.localdate()
    yesterday = today - timezone.timedelta(days=1)
    groups = []
    for notification in page:
        day = timezone.localtime(notification.created_at).date()
        if day == today:
            label = "Today"
        elif day == yesterday:
            label = "Yesterday"
        else:
            # Django's formatter rather than strftime: %-d is not portable to
            # Windows, and this stays locale-aware.
            label = formats.date_format(day, "F j, Y")
        notification.was_unread = notification.id in was_unread
        if groups and groups[-1]["label"] == label:
            groups[-1]["items"].append(notification)
        else:
            groups.append({"label": label, "items": [notification]})

    if was_unread:
        Notification.objects.filter(id__in=was_unread).update(read_at=timezone.now())

    return render(
        request,
        "owner/notifications.html",
        {
            "page": page,
            "groups": groups,
            "household": household,
            "new_count": len(was_unread),
        },
    )


@owner_required
def owner_complaints(request, household, pk=None):
    """
    The owner's side of the same conversations, plus the form to start one.

    Threads are scoped to their own household by the queryset, so a guessed
    id from another household is a 404 rather than someone else's messages.
    """
    threads = (
        household.complaints.annotate(
            unread=Count(
                "messages",
                filter=Q(messages__from_owner=False, messages__read_by_owner=False),
                distinct=True,
            )
        )
        .order_by("-last_message_at")
    )

    complaint = None
    if pk is not None:
        complaint = get_object_or_404(threads, pk=pk)
        complaint.messages.filter(from_owner=False, read_by_owner=False).update(
            read_by_owner=True
        )

    form = ComplaintForm()
    message_form = ComplaintMessageForm() if complaint else None

    if request.method == "POST":
        # Either a reply in the open thread, or a brand new complaint.
        if complaint is not None and "body" in request.POST and "subject" not in request.POST:
            message_form = ComplaintMessageForm(request.POST)
            if message_form.is_valid():
                message = message_form.save(commit=False)
                message.complaint = complaint
                message.author = request.user
                message.from_owner = True
                message.read_by_owner = True
                message.save()
                messages.success(request, "Message sent to the facilitator.")
                return redirect("owner_complaint_thread", pk=complaint.pk)
        else:
            form = ComplaintForm(request.POST)
            if form.is_valid():
                with transaction.atomic():
                    new = form.save(commit=False)
                    new.household = household
                    new.save()
                    # The description is the opening message of the thread.
                    ComplaintMessage.objects.create(
                        complaint=new,
                        author=request.user,
                        from_owner=True,
                        body=new.body,
                        read_by_owner=True,
                    )
                messages.success(
                    request,
                    f"Complaint {new.reference} submitted. The facilitator has been notified.",
                )
                return redirect("owner_complaint_thread", pk=new.pk)

    return render(
        request,
        "owner/complaints.html",
        {
            "form": form,
            "household": household,
            "threads": threads,
            "complaint": complaint,
            "messages_list": complaint.messages.select_related("author") if complaint else None,
            "message_form": message_form,
            # ?new=1 is where the button's plain-link fallback lands, and it
            # opens the dialog on arrival so the two paths behave alike.
            "show_new_form": request.GET.get("new") == "1",
        },
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


@owner_required
def owner_settings(request, household):
    """
    Install the portal, and turn notifications on.

    Both of these were things the owner had no way to reach. The portal has
    been installable since the manifest was written, but nothing in the app
    ever said so, which meant nobody installed it - a feature only discovered
    by people who already knew to look in a browser menu is not a feature.
    """
    push_ready, push_note = push.describe_push()
    return render(
        request,
        "owner/settings.html",
        {
            "household": household,
            "device": setup.device_setup(household),
            "setup_ssid": settings.BOARD_SETUP_SSID,
            "setup_password": settings.BOARD_SETUP_PASSWORD,
            "push_ready": push_ready,
            "push_note": push_note,
            "vapid_public_key": settings.VAPID_PUBLIC_KEY,
            "subscriptions": household.push_subscriptions.all(),
        },
    )


# ---------------------------------------------------------------------------
# Admin console
#
# A third portal, above the facilitators. Where a facilitator works inside one
# barangay, the administrator sees every barangay at once: the barangays
# themselves, the facilitator accounts that run them, every login in the
# system, and the household register. Gated by admin_required, which admits
# superusers and anyone with the admin role - never a plain facilitator.
# ---------------------------------------------------------------------------


def _manageable(user, actor):
    """
    Whether `actor` may suspend or delete `user`.

    Only facilitator and owner logins are manageable from here, and never your
    own account or another administrator's. The console must not become a way
    to lock out every admin, or to delete the account you are signed in as.
    """
    if user.pk == actor.pk:
        return False
    if user.is_superuser or user.role == User.Role.ADMIN:
        return False
    return user.role in {User.Role.FACILITATOR, User.Role.OWNER}


def _visit_when(day, today):
    """
    'Today', 'Tomorrow', 'In 3 days', 'Overdue by 2 days' - the date as the
    reader experiences it. Returns (label, tone) where tone styles urgency.
    """
    if day is None:
        return "No date yet", "muted"
    delta = (day - today).days
    if delta < 0:
        n = -delta
        return f"Overdue by {n} day{'s' if n != 1 else ''}", "overdue"
    if delta == 0:
        return "Today", "today"
    if delta == 1:
        return "Tomorrow", "soon"
    if delta <= 6:
        return f"In {delta} days ({day.strftime('%a')})", "soon"
    # Not strftime("%-d"): that flag is not valid on Windows, where this runs.
    return f"{day.strftime('%b')} {day.day}", ""


@admin_required
def console_dashboard(request):
    """
    The whole system at a glance, and the way in to each part of it.

    Built for numbers that grow. Every list here is capped and says how much
    it is hiding, with the way to the rest; the tiles say which way things
    moved this week, not only where they stand; and what needs a decision is
    gathered into one short strip at the top rather than left for the
    administrator to notice in a table that keeps getting longer.
    """
    users = User.objects.all()
    today = timezone.localdate()
    week_ago = timezone.now() - timezone.timedelta(days=7)
    week_ago_date = today - timezone.timedelta(days=7)
    open_visits = WorkOrder.objects.filter(
        status__in=[WorkOrder.Status.SCHEDULED, WorkOrder.Status.IN_PROGRESS]
    )

    stats = {
        "facilitators": users.filter(role=User.Role.FACILITATOR).count(),
        "owners": users.filter(role=User.Role.OWNER).count(),
        "owners_week": users.filter(role=User.Role.OWNER, date_joined__gte=week_ago).count(),
        "households": Household.objects.count(),
        "households_week": Household.objects.filter(date_registered__gte=week_ago_date).count(),
        "suspended": users.filter(is_active=False).exclude(role=User.Role.ADMIN).count(),
        "lines": Segment.objects.filter(is_active=True).count(),
        "devices": Device.objects.count(),
        "upcoming_visits": open_visits.count(),
        "visits_done_week": WorkOrder.objects.filter(
            status=WorkOrder.Status.DONE, completed_at__gte=week_ago
        ).count(),
        "open_leaks": LeakEvent.objects.filter(
            cleared_at__isnull=True, status=Segment.Status.POSSIBLE_LEAK
        ).count(),
    }

    # What needs a decision, worst first, at most five. Each line is a count
    # and a link to the place it is dealt with.
    attention = []
    if stats["open_leaks"]:
        attention.append({
            "level": "danger", "count": stats["open_leaks"],
            "text": "possible leak alert(s) open - the facilitator has been texted",
            "url": reverse("analytics"), "action": "See analytics",
        })
    unassigned = WorkOrder.objects.filter(status=WorkOrder.Status.REQUESTED).count()
    if unassigned:
        attention.append({
            "level": "warn", "count": unassigned,
            "text": "visit(s) waiting for a facilitator and a date",
            "url": reverse("work_order_list") + "?status=requested", "action": "Assign",
        })
    overdue = open_visits.filter(scheduled_for__lt=today).count()
    if overdue:
        attention.append({
            "level": "warn", "count": overdue,
            "text": "scheduled visit(s) past their date and not marked done",
            "url": reverse("work_order_list") + "?status=scheduled", "action": "Review",
        })
    failed_texts = SmsMessage.objects.filter(
        created_at__gte=week_ago,
        status__in=[SmsMessage.Status.FAILED, SmsMessage.Status.UNDELIVERED],
    ).count()
    if failed_texts:
        attention.append({
            "level": "info", "count": failed_texts,
            "text": "text(s) this week could not be delivered",
            "url": reverse("analytics"), "action": "See SMS figures",
        })
    attention = attention[:5]

    upcoming = list(
        open_visits.select_related("household", "assigned_to")
        .order_by(F("scheduled_for").asc(nulls_last=True))[:5]
    )
    for order in upcoming:
        order.when_label, order.when_tone = _visit_when(order.scheduled_for, today)

    people = users.exclude(role=User.Role.ADMIN)
    recent_accounts = people.order_by("-date_joined")[:6]
    audit = AuditLog.objects.select_related("actor")[:6]

    return render(
        request,
        "admin/dashboard.html",
        {
            "stats": stats,
            "attention": attention,
            "barangay": Barangay.default(),
            "upcoming": upcoming,
            "recent_accounts": recent_accounts,
            "accounts_total": people.count(),
            "audit": audit,
            "audit_total": AuditLog.objects.count(),
        },
    )


@admin_required
def barangay_list(request):
    barangays = Barangay.objects.all().order_by("name")
    rows = [
        {
            "barangay": brgy,
            "facilitators": User.objects.filter(role=User.Role.FACILITATOR, barangay=brgy).count(),
            "households": Household.objects.filter(barangay=brgy).count(),
        }
        for brgy in barangays
    ]
    return render(request, "admin/barangays.html", {"rows": rows})


@admin_required
def barangay_form(request, pk=None):
    barangay = get_object_or_404(Barangay, pk=pk) if pk else None
    form = BarangayForm(request.POST or None, instance=barangay)
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        messages.success(
            request,
            f"Barangay {'updated' if barangay else 'added'}: {saved.name}.",
        )
        return redirect("barangay_list")
    return render(request, "admin/barangay_form.html", {"form": form, "barangay": barangay})


@admin_required
@require_POST
def barangay_toggle(request, pk):
    """Flip a barangay between active and inactive without deleting it."""
    barangay = get_object_or_404(Barangay, pk=pk)
    barangay.is_active = not barangay.is_active
    barangay.save(update_fields=["is_active"])
    state = "active" if barangay.is_active else "inactive"
    messages.success(request, f"{barangay.name} is now {state}.")
    return redirect("barangay_list")


@admin_required
def facilitator_list(request, create_form=None, edit_form=None, edit_user=None):
    """
    The facilitators, with the add dialog alongside.

    create_form is handed back by facilitator_create() when a submission
    fails validation, so the dialog reopens with the typed values and the
    errors still on screen rather than sending the administrator to a page.
    """
    facilitators = list(
        User.objects.filter(role=User.Role.FACILITATOR)
        .select_related("barangay")
        .annotate(open_visits=Count("assignments", filter=~Q(assignments__status__in=WorkOrder.CLOSED_STATUSES)))
        .order_by("facilitator_id", "username")
    )
    for user in facilitators:
        user.manageable = _manageable(user, request.user)
    return render(
        request,
        "admin/facilitators.html",
        {
            "facilitators": facilitators,
            "create_form": create_form or FacilitatorForm(),
            "create_form_errors": bool(create_form and create_form.errors),
            "edit_form": edit_form or FacilitatorForm(auto_id="edit_%s"),
            "edit_form_errors": bool(edit_form and edit_form.errors),
            "edit_user": edit_user,
            "next_facilitator_id": User.next_facilitator_id(),
            "open_modal": (request.GET.get("open") or "").strip(),
        },
    )


@admin_required
def facilitator_create(request):
    form = FacilitatorForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        AuditLog.record(
            request.user, "facilitator.create",
            f"Created facilitator '{user.username}' for {user.barangay.name}",
            target=user.username,
        )
        messages.success(
            request,
            f"Facilitator account created for {user.get_full_name()} "
            f"({user.barangay.name}) - username: {form.generated_username} "
            f"/ temporary password: {form.generated_password}. Write this down now; "
            f"it is not shown again.",
        )
        return redirect("facilitator_list")

    # A failed submission came from the dialog on the list page, so send the
    # bound form back there; the standalone page below is the no-JavaScript
    # fallback the dialog's button links to.
    if request.method == "POST":
        return facilitator_list(request, create_form=form)
    return render(request, "admin/facilitator_form.html", {"form": form})


@admin_required
def account_list(request, edit_form=None, edit_user=None):
    """
    Every login in the system, with edit, suspend and delete on each.

    edit_form/edit_user come back from account_edit() after a failed save,
    so the edit dialog reopens bound to that login with the errors shown.
    """
    query = (request.GET.get("q") or "").strip()
    role = (request.GET.get("role") or "").strip()

    accounts = User.objects.select_related("barangay", "household").order_by(
        "role", "username"
    )
    if role in dict(User.Role.choices):
        accounts = accounts.filter(role=role)
    if query:
        accounts = accounts.filter(
            Q(username__icontains=query)
            | Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(contact_number__icontains=query)
        )

    rows = [
        {
            "user": user,
            "manageable": _manageable(user, request.user),
            # The administrator may edit their own contact details too.
            "editable": user.pk == request.user.pk or _manageable(user, request.user),
        }
        for user in accounts
    ]

    paginator = Paginator(rows, 25)
    page = paginator.get_page(request.GET.get("page"))
    return render(
        request,
        "admin/accounts.html",
        {
            "page": page, "query": query, "role": role, "roles": User.Role.choices,
            "edit_form": edit_form or AccountContactForm(auto_id="edit_%s"),
            "edit_form_errors": bool(edit_form and edit_form.errors),
            "edit_user": edit_user,
        },
    )


@admin_required
@require_POST
def account_suspend(request, pk):
    """
    Block or restore a login.

    Suspending flips Django's is_active, which is what actually stops the
    account signing in - a role check alone would still let an open session
    keep working. Reversible on purpose: a suspension is a hold, not a delete.
    """
    user = get_object_or_404(User, pk=pk)
    if not _manageable(user, request.user):
        return HttpResponseForbidden("That account cannot be suspended from here.")

    user.is_active = not user.is_active
    user.save(update_fields=["is_active"])
    if user.is_active:
        AuditLog.record(request.user, "account.restore", f"Restored the login '{user.username}'", target=user.username)
        messages.success(request, f"Restored the login '{user.username}'.")
    else:
        AuditLog.record(request.user, "account.suspend", f"Suspended the login '{user.username}'", target=user.username)
        messages.success(request, f"Suspended the login '{user.username}'. They can no longer sign in.")
    return redirect("account_list")


@admin_required
@require_POST
def account_delete(request, pk):
    """
    Delete a login.

    For an owner this removes the account only; the household record survives
    (its link is SET_NULL) so its readings, complaints and notices are kept -
    deleting the household itself is a separate, heavier action on the
    facilitator side. For a facilitator it removes the account outright; the
    barangay and its households are untouched.
    """
    user = get_object_or_404(User, pk=pk)
    if not _manageable(user, request.user):
        return HttpResponseForbidden("That account cannot be deleted from here.")

    username = user.username
    user.delete()
    AuditLog.record(request.user, "account.delete", f"Deleted the login '{username}'", target=username)
    messages.success(request, f"Deleted the login '{username}'.")
    return redirect("account_list")


@admin_required
def admin_household_create(request):
    """
    Register a household from the console, during application or intake.

    The administrator's half: who the household is, in words. The hose line
    is created now (and a board, if one is to hand); the location and the
    owner's login are added by the facilitator when they activate it.
    """
    form = AdminHouseholdForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        record = form.save()
        routing.update_household_route(record)
        # Makes the hose line and, from the board choice on the form,
        # registers a new board (the default), attaches one already
        # registered, or leaves it without hardware. The board's setup code
        # is issued at activation and goes to the owner, not shown here.
        device = form.attach_hardware(record)
        # A newly registered board left unlabelled gets a recognisable one.
        if device is not None and not device.label:
            device.label = f"{record.household_id} {record.owner_name}"[:120]
            device.save(update_fields=["label"])
        AuditLog.record(
            request.user, "household.create",
            f"Registered household {record.household_id} ({record.owner_name})", target=record.household_id,
        )
        if device is not None:
            messages.success(
                request,
                f"Household {record.household_id} registered with board {device.device_id}. It is pending "
                f"activation: the facilitator confirms its location and creates the owner's login, and "
                f"the board's setup code is sent to the owner then.",
            )
        else:
            messages.success(
                request,
                f"Household {record.household_id} registered, with no board yet. It is pending activation: "
                f"the facilitator confirms its location and creates the owner's login.",
            )
        return redirect("admin_household_list")

    # A failed submission came from the dialog on the Households page, so
    # send the bound form back there; the standalone page is the fallback.
    if request.method == "POST":
        return admin_household_list(request, create_form=form)
    return render(
        request,
        "admin/household_form.html",
        {"form": form, "next_household_id": Household.next_household_id(), "barangay": Barangay.default()},
    )


# ---------------------------------------------------------------------------
# Field operations: work orders (visits, repairs, maintenance)
#
# One queue for the administrator and one for each facilitator. A REQUESTED
# order is a reported anomaly waiting to be triaged; approving it assigns a
# facilitator and a date and texts both the household and that facilitator.
# ---------------------------------------------------------------------------

WORK_ORDER_SELECT = ("barangay", "household", "segment", "assigned_to", "reported_by")


def _notify_work_order(work_order, actor):
    """Text the household owner and the assigned facilitator that work is set."""
    when = work_order.scheduled_for.strftime("%b %d") if work_order.scheduled_for else "soon"
    kind = work_order.get_kind_display().lower()
    brgy = work_order.barangay.name if work_order.barangay else "Barangay"

    household = work_order.target_household
    if household is not None:
        SmsMessage.queue(
            household,
            f"{brgy} water system: a {kind} is scheduled for your household on {when}. "
            f"{work_order.title}",
            kind=SmsMessage.Kind.MANUAL, sent_by=actor,
        )

    faci = work_order.assigned_to
    if faci is not None and faci.contact_number:
        SmsMessage.queue_to_number(
            faci.contact_number,
            f"Assignment ({work_order.get_kind_display()}): {work_order.title}. "
            f"Scheduled {when}. Where: {work_order.where_label}.",
            name=faci.get_full_name() or faci.username,
            kind=SmsMessage.Kind.MANUAL, sent_by=actor,
        )


@admin_required
def work_order_list(request, create_form=None):
    """
    Every piece of field work, filterable by status and kind.

    create_form is handed back by work_order_create() when a submission
    fails validation, so the schedule dialog reopens with the typed values
    and the errors still on screen. ?open=visit opens it empty on arrival.
    """
    status = (request.GET.get("status") or "").strip()
    kind = (request.GET.get("kind") or "").strip()

    orders = WorkOrder.objects.select_related(*WORK_ORDER_SELECT)
    if status in dict(WorkOrder.Status.choices):
        orders = orders.filter(status=status)
    if kind in dict(WorkOrder.Kind.choices):
        orders = orders.filter(kind=kind)

    paginator = Paginator(orders, 25)
    page = paginator.get_page(request.GET.get("page"))
    return render(
        request,
        "admin/workorders.html",
        {
            "page": page, "status": status, "kind": kind,
            "statuses": WorkOrder.Status.choices, "kinds": WorkOrder.Kind.choices,
            "create_form": create_form or WorkOrderForm(),
            "create_form_errors": bool(create_form and create_form.errors),
            "open_modal": (request.GET.get("open") or "").strip(),
        },
    )


@admin_required
def work_order_create(request):
    form = WorkOrderForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        order = form.save(commit=False)
        order.created_by = request.user
        order.kind = WorkOrder.Kind.VISIT          # the console schedules visits
        order.barangay = Barangay.default()
        order.segment = order.household.segment if order.household_id else None
        # An order created already assigned is scheduled; one created without
        # an assignee is a note to triage later, so it starts as requested.
        if order.assigned_to_id:
            order.status = WorkOrder.Status.SCHEDULED
            order.reviewed_by = request.user
            order.reviewed_at = timezone.now()
        else:
            order.status = WorkOrder.Status.REQUESTED
        order.save()
        if order.status == WorkOrder.Status.SCHEDULED:
            _notify_work_order(order, request.user)
            messages.success(request, f"Work order created and assigned to {order.assigned_to.get_full_name()}.")
        else:
            messages.success(request, "Work order created. Assign a facilitator to schedule it.")
        return redirect("work_order_detail", pk=order.pk)
    # A failed submission came from the dialog on the list, so send the
    # bound form back there; the standalone page is the no-JavaScript fallback.
    if request.method == "POST":
        return work_order_list(request, create_form=form)
    return render(request, "admin/workorder_form.html", {"form": form, "order": None})


@admin_required
def work_order_edit(request, pk):
    order = get_object_or_404(WorkOrder.objects.select_related(*WORK_ORDER_SELECT), pk=pk)
    form = WorkOrderForm(request.POST or None, instance=order, auto_id="edit_%s")
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Work order updated.")
        return redirect("work_order_detail", pk=order.pk)
    if request.method == "POST":
        return work_order_detail(request, pk, edit_form=form)
    return render(request, "admin/workorder_form.html", {"form": form, "order": order})


@admin_required
def work_order_detail(request, pk, edit_form=None):
    order = get_object_or_404(
        WorkOrder.objects.select_related(*WORK_ORDER_SELECT, "reviewed_by", "created_by", "source_complaint"),
        pk=pk,
    )
    facilitators = User.objects.filter(
        role=User.Role.FACILITATOR, is_active=True
    ).order_by("first_name", "username")
    return render(
        request,
        "admin/workorder_detail.html",
        {
            "order": order, "facilitators": facilitators, "statuses": WorkOrder.Status.choices,
            # The edit dialog, bound to this order; reopened with errors after a failed save.
            "edit_form": edit_form or WorkOrderForm(instance=order, auto_id="edit_%s"),
            "edit_form_errors": bool(edit_form and edit_form.errors),
        },
    )


@admin_required
@require_POST
def work_order_approve(request, pk):
    """
    Approve a reported order: assign a facilitator, set a date, notify both.

    A work order with nobody to do it is not scheduled, so an assignee is
    required here - that is the whole point of approval.
    """
    from django.utils.dateparse import parse_date

    order = get_object_or_404(WorkOrder, pk=pk)
    assignee_id = (request.POST.get("assigned_to") or "").strip()
    assignee = (
        User.objects.filter(pk=assignee_id, role=User.Role.FACILITATOR, is_active=True).first()
        if assignee_id else None
    )
    if assignee is None:
        messages.error(request, "Choose a facilitator to assign this to before approving.")
        return redirect("work_order_detail", pk=pk)

    order.assigned_to = assignee
    scheduled = parse_date((request.POST.get("scheduled_for") or "").strip())
    if scheduled:
        order.scheduled_for = scheduled
    order.status = WorkOrder.Status.SCHEDULED
    order.reviewed_by = request.user
    order.reviewed_at = timezone.now()
    order.save()
    _notify_work_order(order, request.user)
    AuditLog.record(
        request.user, "work.approve",
        f"Approved '{order.title}', assigned to {assignee.get_full_name() or assignee.username}",
        target=order.where_label,
    )
    messages.success(
        request,
        f"Scheduled and assigned to {assignee.get_full_name() or assignee.username}. "
        f"The household and the facilitator have been texted.",
    )
    return redirect("work_order_detail", pk=pk)


@admin_required
@require_POST
def work_order_reject(request, pk):
    order = get_object_or_404(WorkOrder, pk=pk)
    order.status = WorkOrder.Status.REJECTED
    order.review_note = (request.POST.get("review_note") or "").strip()[:300]
    order.reviewed_by = request.user
    order.reviewed_at = timezone.now()
    order.save()
    AuditLog.record(request.user, "work.reject", f"Rejected '{order.title}'", target=order.where_label)
    messages.success(request, "Marked as rejected.")
    return redirect("work_order_detail", pk=pk)


@admin_required
@require_POST
def work_order_status(request, pk):
    """Admin moves an order along: in progress, done, or cancelled."""
    order = get_object_or_404(WorkOrder, pk=pk)
    new_status = (request.POST.get("status") or "").strip()
    allowed = {WorkOrder.Status.SCHEDULED, WorkOrder.Status.IN_PROGRESS,
               WorkOrder.Status.DONE, WorkOrder.Status.CANCELLED}
    if new_status not in {str(s) for s in allowed}:
        return HttpResponseForbidden("Not a status this order can be moved to.")

    order.status = new_status
    note = (request.POST.get("resolution_note") or "").strip()
    if note:
        order.resolution_note = note
    order.completed_at = timezone.now() if new_status == WorkOrder.Status.DONE else None
    order.save()
    messages.success(request, f"Work order marked {order.get_status_display().lower()}.")
    return redirect("work_order_detail", pk=pk)


# -------------------------------------------------- facilitator: field work


@facilitator_required
def my_assignments(request):
    """
    The facilitator's schedule: who they will visit, and when.

    Open work grouped by day - overdue, today, tomorrow, later this week,
    later, and anything the administrator has not dated yet - so the page
    answers "what am I doing today" before anything else. Finished work sits
    underneath, briefly, as a record.
    """
    orders = (
        WorkOrder.objects.filter(assigned_to=request.user)
        .select_related("barangay", "household", "segment")
        .order_by(F("scheduled_for").asc(nulls_last=True), "created_at")
    )
    today = timezone.localdate()
    tomorrow = today + timezone.timedelta(days=1)
    week_end = today + timezone.timedelta(days=7)

    buckets = {"overdue": [], "today": [], "tomorrow": [], "week": [], "later": [], "unscheduled": []}
    done_orders = []
    for order in orders:
        if not order.is_open:
            done_orders.append(order)
            continue
        day = order.scheduled_for
        if day is None:
            buckets["unscheduled"].append(order)
        elif day < today:
            buckets["overdue"].append(order)
        elif day == today:
            buckets["today"].append(order)
        elif day == tomorrow:
            buckets["tomorrow"].append(order)
        elif day <= week_end:
            buckets["week"].append(order)
        else:
            buckets["later"].append(order)

    sections = [
        ("Overdue", buckets["overdue"], "These were due before today."),
        ("Today", buckets["today"], ""),
        ("Tomorrow", buckets["tomorrow"], ""),
        ("Later this week", buckets["week"], ""),
        ("Later", buckets["later"], ""),
        ("Not yet scheduled", buckets["unscheduled"],
         "Assigned to you, but the administrator has not set a day yet."),
    ]
    done_orders.sort(key=lambda o: o.completed_at or o.updated_at, reverse=True)
    return render(
        request,
        "facilitator/assignments.html",
        {
            "sections": sections,
            "open_count": sum(len(rows) for rows in buckets.values()),
            "done_orders": done_orders[:20],
            "today": today,
        },
    )


@facilitator_required
@require_POST
def assignment_update(request, pk):
    """The assigned facilitator moves their own job to in progress or done."""
    order = get_object_or_404(WorkOrder, pk=pk)
    if order.assigned_to_id != request.user.id:
        return HttpResponseForbidden("This assignment is not yours.")

    new_status = (request.POST.get("status") or "").strip()
    if new_status not in {str(WorkOrder.Status.IN_PROGRESS), str(WorkOrder.Status.DONE)}:
        return HttpResponseForbidden("Not a status you can set.")

    order.status = new_status
    note = (request.POST.get("resolution_note") or "").strip()
    if note:
        order.resolution_note = note
    order.completed_at = timezone.now() if new_status == WorkOrder.Status.DONE else None
    order.save()
    messages.success(request, f"Marked {order.get_status_display().lower()}.")
    return redirect("my_assignments")


# ---------------------------------------------------------------------------
# Communications: announcements
#
# The administrator speaking to households across one barangay or all of them.
# Delivery reuses what households already read - a Notification per household,
# and an optional SMS through the same queue - so nothing changes on the owner
# side. The Announcement row is kept for the history and the reach counts.
# ---------------------------------------------------------------------------


@admin_required
def announcement_list(request, create_form=None):
    """The announcements sent so far, with the compose dialog alongside."""
    announcements = Announcement.objects.select_related("barangay", "sent_by")
    paginator = Paginator(announcements, 20)
    page = paginator.get_page(request.GET.get("page"))
    return render(
        request,
        "admin/announcements.html",
        {
            "page": page,
            "create_form": create_form or AnnouncementForm(),
            "create_form_errors": bool(create_form and create_form.errors),
            "open_modal": (request.GET.get("open") or "").strip(),
        },
    )


@admin_required
def announcement_create(request):
    form = AnnouncementForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        recipients = form.recipients()
        if not recipients:
            messages.error(request, "No active households match that audience.")
        else:
            title = form.cleaned_data["title"]
            body = form.cleaned_data["body"]
            want_app = form.cleaned_data["in_app"]
            want_sms = form.cleaned_data["by_sms"]

            notices_by_household = {}
            if want_app:
                created = Notification.objects.bulk_create([
                    Notification(
                        kind=Notification.Kind.BROADCAST,
                        title=title, body=body, sent_by=request.user,
                        recipient=household, segment=household.segment,
                    )
                    for household in recipients
                ])
                notices_by_household = {n.recipient_id: n for n in created}

            sms_count = 0
            if want_sms:
                for household in recipients:
                    parts = SmsMessage.queue(
                        household,
                        f"{title}: {body}",
                        kind=SmsMessage.Kind.MANUAL,
                        sent_by=request.user,
                        notification=notices_by_household.get(household.id),
                    )
                    sms_count += len(parts)

            # By email, to households that have one - on the record, or on
            # the owner's login. Sent inline; a failure just does not count.
            email_count = 0
            if form.cleaned_data.get("by_email"):
                for household in recipients:
                    address = _email_for(household)
                    if address and mail.send_email(address, title, body):
                        email_count += 1

            announcement = form.save(commit=False)
            announcement.sent_by = request.user
            announcement.recipients_count = len(recipients)
            announcement.sms_count = sms_count
            announcement.email_count = email_count
            announcement.save()
            AuditLog.record(
                request.user, "announcement.send",
                f"Sent '{announcement.title}' to {len(recipients)} household(s)",
                target=announcement.audience_label,
            )

            done = []
            if want_app:
                done.append(f"shown in the app to {len(recipients)} household(s)")
            if want_sms:
                done.append(f"{sms_count} text message(s) queued")
            if form.cleaned_data.get("by_email"):
                done.append(f"{email_count} email(s) sent")
            messages.success(request, "Announcement " + " and ".join(done) + ".")
            return redirect("announcement_list")

    # A failed submission (or an audience with nobody in it) came from the
    # dialog on the list, so reopen it there with everything typed intact.
    if request.method == "POST":
        return announcement_list(request, create_form=form)
    return render(request, "admin/announcement_form.html", {"form": form})


# ---------------------------------------------------------------------------
# Analytics: the system in numbers and simple bars
#
# Drawn in-page with plain markup rather than a chart library, the same way
# the rest of the project keeps its figures - every bar is a width the server
# already worked out, so it needs no JavaScript and reads in both themes.
# ---------------------------------------------------------------------------


def _bars(pairs):
    """Turn (label, value) pairs into rows with a width percentage for a bar."""
    pairs = list(pairs)
    top = max((value for _, value in pairs), default=0)
    return [
        {"label": label, "value": value, "pct": round(value / top * 100) if top else 0}
        for label, value in pairs
    ]


@admin_required
def admin_pressure(request):
    """
    The administrator's read-only view of pressure and flow for every
    household, across every barangay. The facilitator acts on these; the
    admin oversees them. Live values arrive through /api/live/, which gives
    an admin all lines.
    """
    households = (
        Household.objects.select_related("segment", "segment__latest")
        .filter(account_status=Household.AccountStatus.ACTIVE)
        .order_by("segment__module_index", "household_id")
    )
    segments = (
        Segment.objects.filter(is_active=True)
        .select_related("latest", "household").order_by("name")
    )
    return render(request, "admin/pressure.html", {"households": households, "segments": segments})


@admin_required
def analytics(request):
    """Infographics across every barangay: totals, and a few plain bar charts."""
    since = timezone.now() - timezone.timedelta(days=30)

    devices = list(Device.objects.all())
    online = sum(1 for d in devices if d.is_online)

    kpis = {
        "households": Household.objects.count(),
        "lines": Segment.objects.filter(is_active=True).count(),
        "boards_online": online,
        "boards_total": len(devices),
        "open_leaks": LeakEvent.objects.filter(
            cleared_at__isnull=True, status=Segment.Status.POSSIBLE_LEAK
        ).count(),
        "open_work": WorkOrder.objects.exclude(status__in=WorkOrder.CLOSED_STATUSES).count(),
        "announcements": Announcement.objects.count(),
    }

    # Leak alerts in the last 30 days, per household - where the trouble is.
    leak_rows = (
        LeakEvent.objects.filter(status=Segment.Status.POSSIBLE_LEAK, detected_at__gte=since)
        .values("segment__household__household_id")
        .annotate(n=Count("id"))
        .order_by("-n")[:10]
    )
    leaks_by_household = _bars(
        (row["segment__household__household_id"] or "No household", row["n"]) for row in leak_rows
    )

    # Households by account status.
    hh_status = {row["account_status"]: row["n"] for row in Household.objects.values("account_status").annotate(n=Count("id"))}
    households_by_status = _bars(
        (label, hh_status.get(value, 0)) for value, label in Household.AccountStatus.choices
    )

    # Work orders by status and by kind.
    wo_status = {row["status"]: row["n"] for row in WorkOrder.objects.values("status").annotate(n=Count("id"))}
    work_by_status = _bars(
        (label, wo_status.get(value, 0)) for value, label in WorkOrder.Status.choices
    )
    wo_kind = {row["kind"]: row["n"] for row in WorkOrder.objects.values("kind").annotate(n=Count("id"))}
    work_by_kind = _bars(
        (label, wo_kind.get(value, 0)) for value, label in WorkOrder.Kind.choices
    )

    # Complaints by status.
    comp = {row["status"]: row["n"] for row in Complaint.objects.values("status").annotate(n=Count("id"))}
    complaints_by_status = _bars(
        (label, comp.get(value, 0)) for value, label in Complaint.Status.choices
    )

    # SMS over the last 30 days.
    sms_30d = SmsMessage.objects.filter(created_at__gte=since)
    sms = {
        "delivered": sms_30d.filter(status=SmsMessage.Status.DELIVERED).count(),
        "sent": sms_30d.filter(status=SmsMessage.Status.SENT).count(),
        "queued": sms_30d.filter(status=SmsMessage.Status.QUEUED).count(),
        "failed": sms_30d.filter(
            status__in=[SmsMessage.Status.FAILED, SmsMessage.Status.UNDELIVERED]
        ).count(),
    }
    sms_bars = _bars([
        ("Delivered", sms["delivered"]),
        ("Sent to network", sms["sent"]),
        ("Queued", sms["queued"]),
        ("Failed / not delivered", sms["failed"]),
    ])

    return render(
        request,
        "admin/analytics.html",
        {
            "kpis": kpis,
            "households_by_status": households_by_status,
            "leaks_by_household": leaks_by_household,
            "work_by_status": work_by_status,
            "work_by_kind": work_by_kind,
            "complaints_by_status": complaints_by_status,
            "sms_bars": sms_bars,
            "since": since,
        },
    )


# ---------------------------------------------------------------------------
# Data & system operations
#
# Import/export, database backup & restore, the audit log, and maintenance
# mode with an error log. Built on Django's own tools - csv, dumpdata/loaddata
# - rather than extra packages, the same way the rest of the project keeps its
# dependencies to nothing it cannot see the source of.
# ---------------------------------------------------------------------------


@admin_required
def data_tools(request):
    return render(
        request,
        "admin/data.html",
        {
            "import_form": HouseholdImportForm(),
            "restore_form": RestoreForm(),
            "household_count": Household.objects.count(),
        },
    )


@admin_required
def export_console(request, kind):
    """CSV export across every barangay, for the LGU's own records."""
    import csv

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="shl-{kind}.csv"'
    writer = csv.writer(response)

    if kind == "households":
        writer.writerow(["household_id", "owner_name", "address", "contact_number",
                          "barangay", "account_status", "line", "portal_login"])
        for h in Household.objects.select_related("barangay", "segment", "user").order_by("household_id"):
            writer.writerow([
                h.household_id, h.owner_name, h.address, h.contact_number,
                h.barangay.name if h.barangay else "", h.account_status,
                h.segment.name if h.segment else "", h.user.username if h.user else "",
            ])
    elif kind == "workorders":
        writer.writerow(["id", "kind", "status", "title", "barangay", "where",
                         "assigned_to", "scheduled_for", "created_at"])
        for o in WorkOrder.objects.select_related("barangay", "household", "segment", "assigned_to").order_by("created_at"):
            writer.writerow([
                o.pk, o.kind, o.status, o.title, o.barangay.name if o.barangay else "",
                o.where_label, o.assigned_to.get_full_name() if o.assigned_to else "",
                o.scheduled_for or "", o.created_at.strftime("%Y-%m-%d %H:%M"),
            ])
    elif kind == "pressure":
        since = timezone.now() - timezone.timedelta(hours=24)
        writer.writerow(["minute", "household", "line", "module", "avg_pressure_bar",
                         "avg_pressure_psi", "avg_flow_lpm"])
        for r in (ReadingRollup.objects.filter(minute__gte=since)
                  .select_related("segment", "segment__household").order_by("minute")):
            house = getattr(r.segment, "household", None)
            writer.writerow([
                r.minute.strftime("%Y-%m-%d %H:%M"),
                house.household_id if house else "",
                r.segment.name, r.segment.module_index,
                round(r.avg_pressure_bar, 3), psi(r.avg_pressure_bar), round(r.avg_flow_lpm, 3),
            ])
    elif kind == "audit":
        writer.writerow(["when", "actor", "action", "summary", "target"])
        for e in AuditLog.objects.all().order_by("created_at"):
            writer.writerow([
                e.created_at.strftime("%Y-%m-%d %H:%M"), e.actor_name,
                e.action, e.summary, e.target,
            ])
    else:
        raise Http404("No such export.")

    AuditLog.record(request.user, "data.export", f"Exported {kind} as CSV")
    return response


@admin_required
@require_POST
def household_import(request):
    import csv
    import io

    form = HouseholdImportForm(request.POST, request.FILES)
    if not form.is_valid():
        return render(
            request, "admin/data.html",
            {"import_form": form, "restore_form": RestoreForm(),
             "household_count": Household.objects.count()},
        )

    default_barangay = form.cleaned_data["default_barangay"]
    upload = form.cleaned_data["csv_file"]
    try:
        text = upload.read().decode("utf-8-sig")
    except (UnicodeDecodeError, AttributeError):
        messages.error(request, "That file is not readable as UTF-8 text. Export it as CSV and try again.")
        return redirect("data_tools")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "owner_name" not in [h.strip() for h in reader.fieldnames]:
        messages.error(request, "The CSV needs a header row with at least an 'owner_name' column.")
        return redirect("data_tools")

    created = 0
    skipped = []
    for line_no, row in enumerate(reader, start=2):
        clean = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        owner = clean.get("owner_name", "")
        if not owner:
            skipped.append(f"row {line_no}: no owner_name")
            continue
        barangay = default_barangay
        named = clean.get("barangay", "")
        if named:
            match = Barangay.objects.filter(name__iexact=named).first()
            if match:
                barangay = match
        Household.objects.create(
            owner_name=owner,
            address=clean.get("address", ""),
            contact_number=clean.get("contact_number", ""),
            barangay=barangay,
        )
        created += 1

    AuditLog.record(request.user, "data.import", f"Imported {created} household(s) from CSV")
    messages.success(request, f"Imported {created} household(s).")
    if skipped:
        messages.warning(request, f"Skipped {len(skipped)}: " + "; ".join(skipped[:10])
                         + (" ..." if len(skipped) > 10 else ""))
    return redirect("data_tools")


@admin_required
def backup_download(request):
    """A full dump of the application data as JSON, for safekeeping."""
    import io
    from django.core.management import call_command

    buffer = io.StringIO()
    call_command("dumpdata", "core", indent=2, stdout=buffer)
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
    response = HttpResponse(buffer.getvalue(), content_type="application/json")
    response["Content-Disposition"] = f'attachment; filename="shl-backup-{stamp}.json"'
    AuditLog.record(request.user, "data.backup", "Downloaded a database backup")
    return response


@admin_required
@require_POST
def restore_upload(request):
    """
    Restore records from an uploaded backup.

    loaddata overwrites rows that share an id with the backup and adds any it
    is missing; it does not delete rows created since. Said plainly on the
    page, and guarded by a typed word, because it is the one action here that
    changes existing data rather than only reading or adding it.
    """
    import os
    import tempfile
    from django.core.management import call_command

    form = RestoreForm(request.POST, request.FILES)
    if not form.is_valid():
        return render(
            request, "admin/data.html",
            {"import_form": HouseholdImportForm(), "restore_form": form,
             "household_count": Household.objects.count()},
        )

    upload = form.cleaned_data["backup_file"]
    tmp = tempfile.NamedTemporaryFile(mode="wb", suffix=".json", delete=False)
    try:
        for chunk in upload.chunks():
            tmp.write(chunk)
        tmp.close()
        with transaction.atomic():
            call_command("loaddata", tmp.name)
    except Exception as exc:  # a bad file must not 500; report what happened
        messages.error(request, f"Restore failed - nothing was changed: {exc}")
        return redirect("data_tools")
    finally:
        tmp.close()
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    AuditLog.record(request.user, "data.restore", "Restored the database from a backup")
    messages.success(request, "Database restored from the backup.")
    return redirect("data_tools")


@admin_required
def audit_log(request):
    action = (request.GET.get("action") or "").strip()
    entries = AuditLog.objects.select_related("actor")
    if action:
        entries = entries.filter(action=action)
    actions = AuditLog.objects.values_list("action", flat=True).distinct().order_by("action")
    paginator = Paginator(entries, 40)
    page = paginator.get_page(request.GET.get("page"))
    return render(request, "admin/audit.html", {"page": page, "action": action, "actions": actions})


@admin_required
def system_status(request, detection_form=None):
    setting = SystemSetting.load()
    errors = ErrorLog.objects.select_related("user")[:50]
    # The outside services the system leans on, each answering for itself.
    services = []
    for name, describe in (
        ("Email", mail.describe_email),
        ("Text messages (SMS)", sms.describe_gateway),
        ("Road routes on the map", routing.describe_routing),
    ):
        ready, note = describe()
        services.append({"name": name, "ready": ready, "note": note})
    return render(
        request, "admin/system.html",
        {
            "setting": setting,
            "errors": errors,
            "services": services,
            "detection_form": detection_form or DetectionSettingsForm(instance=setting),
        },
    )


@admin_required
@require_POST
def detection_settings(request):
    setting = SystemSetting.load()
    form = DetectionSettingsForm(request.POST, instance=setting)
    if not form.is_valid():
        return system_status(request, detection_form=form)
    setting = form.save(commit=False)
    setting.updated_by = request.user
    setting.save()
    AuditLog.record(request.user, "system.detection", "Updated leak detection settings")
    messages.success(
        request,
        "Detection settings saved. Boards pick up the new values on their next reading.",
    )
    return redirect("system_status")


@admin_required
@require_POST
def maintenance_toggle(request):
    setting = SystemSetting.load()
    setting.maintenance_mode = not setting.maintenance_mode
    message = (request.POST.get("maintenance_message") or "").strip()
    if message:
        setting.maintenance_message = message[:255]
    setting.updated_by = request.user
    setting.save()
    state = "on" if setting.maintenance_mode else "off"
    AuditLog.record(request.user, "system.maintenance", f"Turned maintenance mode {state}")
    messages.success(
        request,
        f"Maintenance mode is {state}."
        + (" Households and facilitators now see the maintenance notice." if setting.maintenance_mode else ""),
    )
    return redirect("system_status")


# ---------------------------------------------------------------------------
# Facilitator: credentials by text, escalation, repairs
# ---------------------------------------------------------------------------


def _blank_segment_form(user):
    """The shared edit dialog's empty form, offering only this facilitator's boards."""
    form = SegmentForm(auto_id="edit_%s")
    form.fields["device"].queryset = _scoped_devices(user)
    return form


def _text_credentials(request, household, username, password):
    """
    Text a household its portal login. Returns True if a text was queued.

    The household has no account to read a notice in yet - that is the whole
    point of this message - so SMS is the only channel that reaches them.
    Nothing is sent without a number on file.
    """
    if not (household.contact_number or "").strip():
        return False
    site = request.build_absolute_uri("/").rstrip("/")
    body = (
        f"Smart Hose Leak portal login - username: {username}, temporary password: {password}. "
        f"Sign in at {site} and change it."
    )
    parts = SmsMessage.queue(household, body, kind=SmsMessage.Kind.MANUAL, sent_by=request.user)
    return bool(parts)


def _email_credentials(request, household, username, password):
    """Email a household its portal login, if it has an address. Returns True if handed over."""
    address = (household.email or "").strip()
    if not address:
        return False
    site = request.build_absolute_uri("/").rstrip("/")
    return mail.send_email(
        address,
        "Your Smart Hose Leak portal login",
        f"Your household ({household.household_id}) is now active on the Smart Hose Leak system.\n\n"
        f"Username: {username}\nTemporary password: {password}\n\n"
        f"Sign in at {site} and you will be asked to choose a new password.",
    )


@facilitator_required
@require_POST
def household_send_credentials(request, pk):
    """
    Issue a fresh temporary password and text it to the household.

    Passwords cannot be read back, so "send the login" always means a new
    temporary one - the same thing the reset button does, delivered.
    """
    import secrets

    household = get_object_or_404(
        _scoped_households(request.user).select_related("user"), pk=pk
    )
    if household.user is None:
        messages.error(request, "This household has no Owner Portal account yet.")
        return redirect("household_detail", pk=pk)
    if not (household.contact_number or "").strip():
        messages.error(
            request, "No contact number on file for this household, so there is nowhere to text it."
        )
        return redirect("household_detail", pk=pk)

    password = secrets.token_urlsafe(6)
    household.user.set_password(password)
    household.user.must_change_password = True
    household.user.save()
    _text_credentials(request, household, household.user.username, password)
    messages.success(
        request,
        f"Login details texted to {household.contact_number}: username {household.user.username}, "
        f"temporary password {password}. It is not shown again.",
    )
    return redirect("household_detail", pk=pk)


@facilitator_required
@require_POST
def household_refresh_route(request, pk):
    """
    Fetch (again) the road route to a household.

    For when the first attempt failed - no key yet, service down - or the
    road data has improved. One call, inline; the result is on the page the
    facilitator is sent back to.
    """
    household = get_object_or_404(_scoped_households(request.user), pk=pk)
    if not household.has_location:
        messages.error(request, "This household has no coordinates yet, so there is nothing to route to.")
        return redirect("household_detail", pk=pk)

    if routing.update_household_route(household):
        messages.success(
            request,
            f"Route fetched: {household.visit_route_label} by road. It is drawn on the dashboard map.",
        )
    else:
        messages.error(request, f"No route fetched: {household.visit_route_error or 'nothing to route between'}.")
    return redirect("household_detail", pk=pk)


def _email_for(household):
    """The address a household can be emailed at: its own, or its owner's login."""
    address = (household.email or "").strip()
    if not address and household.user_id:
        address = (household.user.email or "").strip()
    return address or None


# ---------------------------------------------------------------------------
# Sign-in, step two: the one-time code
#
# LandingLoginView parks the user's id in the session after the password is
# right; nothing is signed in until the code below matches. Ten minutes, five
# guesses, one use (core/otp.py). "Remember this device" sets a signed cookie
# so that phone skips the code for a month.
# ---------------------------------------------------------------------------


def _pending_user(request):
    user_id = request.session.get("otp_user")
    if not user_id:
        return None
    return User.objects.filter(pk=user_id, is_active=True).first()


def login_code(request):
    user = _pending_user(request)
    if user is None:
        messages.error(request, "Sign in with your password first.")
        return redirect("login")

    error = ""
    if request.method == "POST":
        ok, error = otp.verify(user, request.POST.get("code"))
        if ok:
            auth_login(request, user)
            destination = request.session.pop("otp_next", None) or reverse("post_login")
            remember = request.session.pop("otp_remember", False)
            request.session.pop("otp_user", None)
            response = redirect(destination)
            if remember:
                response.set_cookie(
                    otp.TRUSTED_COOKIE,
                    otp.trusted_cookie_value(user),
                    max_age=settings.OTP_TRUSTED_DEVICE_DAYS * 86400,
                    httponly=True,
                    samesite="Lax",
                    secure=request.is_secure(),
                )
            return response

    code = user.login_codes.filter(used_at__isnull=True).order_by("-created_at").first()
    return render(
        request,
        "registration/login_code.html",
        {
            "pending_user": user,
            "code": code,
            "error": error,
            "can_resend": otp.can_resend(user),
            "channels": otp.channels_for(user),
            "minutes": settings.OTP_VALIDITY_MINUTES,
        },
    )


@require_POST
def login_code_resend(request):
    user = _pending_user(request)
    if user is None:
        return redirect("login")
    if not otp.can_resend(user):
        messages.warning(request, "A code was sent less than a minute ago. Give it a moment to arrive.")
        return redirect("login_code")

    preferred = request.POST.get("otp_channel") or LoginCode.Channel.SMS
    try:
        code = otp.send_code(user, preferred)
    except otp.DeliveryError as exc:
        messages.error(request, str(exc))
        return redirect("login_code")
    if code is None:
        messages.error(request, "This account has no contact number or email to send a code to.")
        return redirect("login")
    sent_by = "text" if code.channel == LoginCode.Channel.SMS else "email"
    messages.success(request, f"A new code was sent by {sent_by} to {code.masked_destination()}.")
    if settings.DEBUG:
        messages.info(request, f"DEBUG only: your sign-in code is {code.code}.")
    return redirect("login_code")


@admin_required
def account_edit(request, pk):
    """
    Edit how a login can be reached: name, contact number, email.

    Any facilitator or owner login, and the administrator's own - but not
    another administrator's, for the same reason they cannot be suspended
    from here. Needed because the sign-in code has to go somewhere, and an
    account made before numbers and emails were collected has nowhere.
    """
    user = get_object_or_404(User, pk=pk)
    if user.pk != request.user.pk and not _manageable(user, request.user):
        return HttpResponseForbidden("That account cannot be edited from here.")

    form = AccountContactForm(request.POST or None, instance=user, auto_id="edit_%s")
    if request.method == "POST" and form.is_valid():
        form.save()
        AuditLog.record(
            request.user, "account.edit",
            f"Updated contact details for '{user.username}'", target=user.username,
        )
        messages.success(request, f"Saved contact details for {user.username}.")
        return redirect("account_list")
    if request.method == "POST":
        return account_list(request, edit_form=form, edit_user=user)
    return render(request, "admin/account_form.html", {"form": form, "account": user})


@admin_required
def admin_household_list(request, create_form=None, edit_form=None, edit_household=None):
    """
    Every household, with the register / edit / delete dialogs alongside.

    The administrator's counterpart to the facilitator's Households page.
    The sidebar brings you here - to what has been registered - and the
    Register button opens the dialog; create_form and edit_form are handed
    back by the create and edit views when a save fails validation, so the
    dialog reopens with the typed values and the errors still on screen.
    """
    query = (request.GET.get("q") or "").strip()
    households = Household.objects.select_related("segment", "segment__device", "user")
    if query:
        households = households.filter(
            Q(household_id__icontains=query)
            | Q(owner_name__icontains=query)
            | Q(address__icontains=query)
            | Q(contact_number__icontains=query)
        )
    households = households.annotate(
        complaint_count=Count("complaints", distinct=True),
        notification_count=Count("notifications", distinct=True),
    ).order_by("household_id")
    page = Paginator(households, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "admin/households.html",
        {
            "page": page,
            "query": query,
            "total": Household.objects.count(),
            "barangay": Barangay.default(),
            "create_form": create_form or AdminHouseholdForm(),
            "create_form_errors": bool(create_form and create_form.errors),
            "edit_form": edit_form or AdminHouseholdForm(auto_id="edit_%s"),
            "edit_form_errors": bool(edit_form and edit_form.errors),
            "edit_household": edit_household,
            "next_household_id": Household.next_household_id(),
            "open_modal": (request.GET.get("open") or "").strip(),
        },
    )


@admin_required
def admin_household_edit(request, pk):
    household = get_object_or_404(Household, pk=pk)
    form = AdminHouseholdForm(request.POST or None, instance=household, auto_id="edit_%s")
    if request.method == "POST" and form.is_valid():
        record = form.save()
        routing.update_household_route(record)   # coordinates may have changed
        AuditLog.record(
            request.user, "household.edit",
            f"Updated household {record.household_id}", target=record.household_id,
        )
        messages.success(request, f"{record.household_id} updated.")
        return redirect("admin_household_list")
    if request.method == "POST":
        return admin_household_list(request, edit_form=form, edit_household=household)
    return render(
        request,
        "admin/household_form.html",
        {"form": form, "household": household, "next_household_id": household.household_id,
         "barangay": Barangay.default()},
    )


@admin_required
@require_POST
def admin_household_delete(request, pk):
    """
    Remove a household and everything that only exists because of it - the
    same rules as the facilitator's delete: complaints and notices cascade,
    and the owner's login is removed explicitly rather than left able to
    sign in to nothing.
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

    AuditLog.record(request.user, "household.delete", f"Deleted {label}", target=label)
    removed = [f"{complaints} complaint(s)", f"{notifications} notice(s)"]
    if username:
        removed.append(f"the login '{username}'")
    messages.success(request, f"Deleted {label}, along with {', '.join(removed)}.")
    return redirect("admin_household_list")


@admin_required
def facilitator_edit(request, pk):
    """Change a facilitator's name or contact details. The login and the FAC number are fixed."""
    user = get_object_or_404(User, pk=pk, role=User.Role.FACILITATOR)
    form = FacilitatorForm(request.POST or None, instance=user, auto_id="edit_%s")
    if request.method == "POST" and form.is_valid():
        form.save()
        AuditLog.record(
            request.user, "facilitator.edit",
            f"Updated facilitator {user.facilitator_id} ({user.username})", target=user.username,
        )
        messages.success(request, f"{user.facilitator_id} updated.")
        return redirect("facilitator_list")
    if request.method == "POST":
        return facilitator_list(request, edit_form=form, edit_user=user)
    return render(request, "admin/facilitator_form.html", {"form": form, "facilitator": user})


@admin_required
@require_POST
def facilitator_delete(request, pk):
    """
    Remove a facilitator account. Visits assigned to them stay on the
    schedule but become unassigned (the link is SET_NULL), so nothing that
    was promised to a household disappears with the person.
    """
    user = get_object_or_404(User, pk=pk, role=User.Role.FACILITATOR)
    if not _manageable(user, request.user):
        return HttpResponseForbidden("That account cannot be deleted from here.")
    label = f"{user.facilitator_id} ({user.get_full_name() or user.username})"
    username = user.username
    open_visits = user.assignments.exclude(status__in=WorkOrder.CLOSED_STATUSES).count()
    user.delete()
    AuditLog.record(request.user, "facilitator.delete", f"Deleted facilitator {label}", target=username)
    note = f" {open_visits} open visit(s) are now unassigned." if open_visits else ""
    messages.success(request, f"Deleted {label} and the login '{username}'.{note}")
    return redirect("facilitator_list")


# ---------------------------------------------------------------------------
# Owner: my board (set it up, watch it connect)
# ---------------------------------------------------------------------------


@owner_required
def owner_device_status(request, household):
    """The board's state as JSON, polled by Settings > My device."""
    return JsonResponse(setup.device_setup(household))


def _owner_device(household):
    segment = household.segment
    return segment.device if segment is not None else None


@owner_required
@require_POST
def owner_device_reissue(request, household):
    """
    A fresh setup code, for an expired one or a board that never connected.

    Refused once the board has ever sent a reading: a new code lets any
    blank board take over this one's identity, so after it is connected
    that is the facilitator's call, not something to do from a phone.
    """
    device = _owner_device(household)
    state = setup.device_setup(household)
    if device is None or not state["can_reissue"]:
        messages.error(request, "A new setup code is not available for your board right now.")
        return redirect("owner_settings")
    ticket = device.issue_claim_code()
    sent = setup.send_setup_code(request, household, ticket, sent_by=request.user)
    AuditLog.record(request.user, "board.code_reissued", f"Owner requested a new setup code for {device.device_id}", target=device.device_id)
    messages.success(
        request,
        f"Your new setup code is {ticket.code}."
        + (f" It was also sent by {' and '.join(sent)}." if sent else ""),
    )
    return redirect("owner_settings")


@owner_required
@require_POST
def owner_device_resend(request, household):
    """Send the current, still-valid setup code again."""
    device = _owner_device(household)
    ticket = getattr(device, "claim_code", None) if device is not None else None
    if ticket is None or not ticket.is_usable:
        messages.error(request, "There is no current setup code to send. Get a new one instead.")
        return redirect("owner_settings")
    sent = setup.send_setup_code(request, household, ticket, sent_by=request.user)
    if sent:
        messages.success(request, f"Setup code {ticket.code} sent by {' and '.join(sent)}.")
    else:
        messages.warning(request, "There is no number or email on your record to send it to. The code is shown below.")
    return redirect("owner_settings")
