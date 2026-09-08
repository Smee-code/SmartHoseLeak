from django.conf import settings

from .models import Complaint, Notification


def site_context(request):
    """Values every template needs: site identity, role flags, unread badges."""
    context = {
        "SITE_NAME": settings.SITE_NAME,
        "SITE_LOCALE": settings.SITE_LOCALE,
        "LIVE_POLL_INTERVAL_MS": settings.LIVE_POLL_INTERVAL_MS,
        "is_facilitator": False,
        "household": None,
        "unread_count": 0,
        "pending_complaint_count": 0,
    }
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return context

    context["is_facilitator"] = user.is_facilitator
    if user.is_facilitator:
        context["pending_complaint_count"] = Complaint.objects.filter(
            status=Complaint.Status.PENDING
        ).count()
    else:
        household = getattr(user, "household", None)
        context["household"] = household
        if household is not None:
            context["unread_count"] = Notification.objects.filter(
                recipient=household, read_at__isnull=True
            ).count()
    return context
