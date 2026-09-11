import hashlib
from pathlib import Path

from django.conf import settings

from .models import Complaint, Notification

# Files the service worker keeps in its offline shell cache. Their combined
# fingerprint names that cache, so editing any of them retires the old one.
_SHELL_ASSETS = ("css/app.css", "js/app.js", "js/chart.js", "js/map.js")


def asset_version():
    """
    Fingerprint of the cached front-end assets, as a short hex string.

    The service worker caches /static/ files cache-first so the portal still
    opens on a weak signal. That is the right trade-off in the field, but it
    means a fixed cache name would keep serving last week's CSS after an
    edit. Naming the cache after the file contents makes a change invalidate
    it: the new worker installs under a new name and its activate handler
    deletes every older one.

    Falls back to a constant if a file is missing, so a partial checkout
    cannot take the whole site down over a cache name.
    """
    digest = hashlib.sha256()
    for relative in _SHELL_ASSETS:
        for root in settings.STATICFILES_DIRS:
            path = Path(root) / relative
            if path.exists():
                digest.update(path.read_bytes())
                break
    return digest.hexdigest()[:12] or "static"


def site_context(request):
    """Values every template needs: site identity, role flags, unread badges."""
    context = {
        "SITE_NAME": settings.SITE_NAME,
        "SITE_LOCALE": settings.SITE_LOCALE,
        "LIVE_POLL_INTERVAL_MS": settings.LIVE_POLL_INTERVAL_MS,
        "ASSET_VERSION": asset_version(),
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
