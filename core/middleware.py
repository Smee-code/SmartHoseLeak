"""
Two small pieces of system plumbing, kept out of the views.

MaintenanceModeMiddleware puts the household and facilitator sides behind a
short notice when the administrator turns maintenance on, while deliberately
leaving two things working: the admin console (so whoever flipped the switch
can still work and turn it off) and the device ingest endpoint (so the boards
never stop recording water just because the website is down for a few minutes).

ErrorLogMiddleware writes one row per unhandled exception so a 500 leaves a
trace in the console, then gets out of the way and lets Django's own error
handling render the page exactly as before.
"""

from django.shortcuts import render


class MaintenanceModeMiddleware:
    # Paths that work even during maintenance. The device ingest and the
    # static assets must never be held; the admin console and the login flow
    # stay open so an administrator can get in and turn it back off.
    ALLOW_PREFIXES = ("/api/", "/static/", "/admin/", "/console/", "/sw.js", "/manifest")
    ALLOW_EXACT = ("/login/", "/logout/", "/post-login/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self._blocked(request):
            from .models import SystemSetting
            setting = SystemSetting.load()
            return render(
                request, "maintenance.html",
                {"message": setting.maintenance_message}, status=503,
            )
        return self.get_response(request)

    def _blocked(self, request):
        path = request.path
        if path.startswith(self.ALLOW_PREFIXES) or path in self.ALLOW_EXACT:
            return False
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and user.is_admin:
            return False
        try:
            from .models import SystemSetting
            return SystemSetting.load().maintenance_mode
        except Exception:
            # The table may not exist yet (first migrate). Never take the site
            # down over the maintenance switch itself.
            return False


class ErrorLogMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        try:
            from .models import ErrorLog
            user = getattr(request, "user", None)
            ErrorLog.objects.create(
                path=request.path[:255],
                method=request.method[:8],
                exception_type=type(exception).__name__[:120],
                message=str(exception)[:5000],
                user=user if (user is not None and user.is_authenticated) else None,
            )
        except Exception:
            # Recording the error must never itself raise and mask the original.
            pass
        return None  # let Django's normal 500 handling proceed
