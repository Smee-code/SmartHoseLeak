"""
Which language a person sees, and which language they are written to in.

Two things decide it, and they are kept apart on purpose:

  * The EN/FIL toggle in the header sets a cookie. That is Django's own
    LocaleMiddleware at work, and it is what every visitor and every
    facilitator or administrator gets.

  * A HOUSEHOLD has a language of its own, on the record. It is what the
    portal opens in before the owner has touched the toggle (Filipino, by
    default - this is a barangay system), and, more importantly, the
    language of every text and notice the system writes to that household
    at two in the morning, when nobody is looking at a toggle. When an
    owner presses the toggle, the household's language follows it, so the
    screen and the texts never disagree.
"""

from django.conf import settings
from django.utils import translation
from django.views.i18n import set_language as django_set_language


class HouseholdLanguageMiddleware:
    """
    Open the portal in the household's language until the owner chooses.

    Sits after authentication. LocaleMiddleware has already picked a
    language from the cookie or the browser; this only steps in for a
    signed-in owner who has NOT set the cookie, because a browser whose
    Accept-Language says "en" is not a choice the owner made.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        household = getattr(user, "household", None) if user is not None and user.is_authenticated else None
        if household is not None and settings.LANGUAGE_COOKIE_NAME not in request.COOKIES:
            code = household.language or settings.LANGUAGE_CODE
            translation.activate(code)
            request.LANGUAGE_CODE = code
        return self.get_response(request)


def set_language(request):
    """
    Django's toggle view, plus: an owner's choice is written to their
    household, so the texts they receive switch with the screen.
    """
    response = django_set_language(request)
    if request.method == "POST":
        code = request.POST.get("language")
        household = getattr(request.user, "household", None) if request.user.is_authenticated else None
        if household is not None and code in dict(settings.LANGUAGES) and household.language != code:
            household.language = code
            household.save(update_fields=["language"])
    return response
