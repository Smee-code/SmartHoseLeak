"""
Road-following routes from the water source to a household, for visits.

Why this exists
---------------
The leak map draws each household's hose as a straight line from the source,
and that is correct: the hose goes cross-country downhill, and the line means
"this house is on this hose". It says nothing about how a person gets there.
A facilitator walking out to a flagged house needs the road, which is a
different thing - so the route is a separate layer, never a replacement.

Why fetched once and stored
---------------------------
A route is a call to a routing service over the internet. The dashboard has
to work on a weak barangay signal, and it redraws the map on every visit, so
calling out each time would be slow at best and blank at worst. Instead the
route is fetched when a household's coordinates are saved (or the barangay's
source moves) and kept on the household record; the map then draws it from
the page data with no further network, exactly as it draws the hoses.

Why OpenRouteService and plain urllib
-------------------------------------
Free key, generous daily allowance (we need a handful of calls, ever), and a
plain JSON API - so no client library, the same way SMS talks to Semaphore.
Without a key nothing is fetched and the map simply has no routes to show;
the hoses are unaffected. OpenStreetMap road data in rural Sison is thin in
places: a house on a footpath the map does not know will route to the
nearest known road and end in a short straight stub. That is the data, not a
fault, and the Navigate link (Google Maps) is there for the last stretch.
"""

import json
import logging
import urllib.error
import urllib.request

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


class RoutingUnavailable(Exception):
    """The route could not be fetched. The household keeps whatever it had."""


def describe_routing():
    """One honest sentence about whether routes can be fetched right now."""
    if not settings.ROUTING_ENABLED:
        return False, "Visit routes are switched off in settings (ROUTING_ENABLED)."
    if not settings.ORS_API_KEY:
        return False, (
            "No OpenRouteService key set. Add ORS_API_KEY to your .env file - "
            "sign up free at openrouteservice.org to get one."
        )
    return True, "Visit routes are fetched when a household's coordinates are saved."


def fetch_route(start, end):
    """
    The road route from `start` to `end`, each a (lat, lng) pair.

    Returns {"points": [[lat, lng], ...], "distance_m": float, "duration_s": float}.
    Raises RoutingUnavailable for anything that went wrong, with the
    service's own words where it gave any.
    """
    if not settings.ROUTING_ENABLED or not settings.ORS_API_KEY:
        raise RoutingUnavailable("routing is not configured")

    url = f"{settings.ORS_BASE_URL.rstrip('/')}/v2/directions/{settings.ROUTING_PROFILE}/geojson"
    # GeoJSON and the API speak [lng, lat]; everything in this project is [lat, lng].
    body = json.dumps({"coordinates": [[start[1], start[0]], [end[1], end[0]]]}).encode()
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Authorization": settings.ORS_API_KEY,
            "Content-Type": "application/json",
            "Accept": "application/geo+json, application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=settings.ROUTING_TIMEOUT_S) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        raise RoutingUnavailable(f"routing service refused it ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise RoutingUnavailable(f"cannot reach the routing service: {exc.reason}") from exc
    except (ValueError, TimeoutError, OSError) as exc:
        raise RoutingUnavailable(f"routing service gave an unreadable answer: {exc}") from exc

    return parse_route(payload)


def parse_route(payload):
    """Pull the shape and the summary out of a directions GeoJSON answer."""
    features = payload.get("features") or []
    if not features:
        raise RoutingUnavailable("no route in the answer")
    feature = features[0]
    coords = (feature.get("geometry") or {}).get("coordinates") or []
    points = [[lat, lng] for lng, lat in coords]
    if len(points) < 2:
        raise RoutingUnavailable("the route has no shape")
    summary = (feature.get("properties") or {}).get("summary") or {}
    return {
        "points": points,
        "distance_m": float(summary.get("distance") or 0),
        "duration_s": float(summary.get("duration") or 0),
    }


ROUTE_FIELDS = [
    "visit_route", "visit_route_distance_m", "visit_route_duration_s",
    "visit_route_fetched_at", "visit_route_error",
]


def update_household_route(household, save=True):
    """
    Fetch and store the visit route for one household. Never raises.

    Returns True when a fresh route was stored. Anything else - no
    coordinates, no source, no key, service down - leaves the household
    with its previous route (or none) and a short note saying why, so the
    household page can show it and the facilitator can try again.
    """
    from .models import WaterSource  # local: models does not import this module

    source = (
        WaterSource.objects.filter(barangay=household.barangay).first()
        if household.barangay_id else None
    )
    start = (source.latitude, source.longitude) if source is not None and source.has_location else None
    end = (
        (household.latitude, household.longitude)
        if household.latitude is not None and household.longitude is not None else None
    )

    if end is None or start is None:
        # Nothing to route between. Cleared rather than kept, so a route to a
        # location that has since been removed does not linger on the map.
        household.visit_route = None
        household.visit_route_distance_m = None
        household.visit_route_duration_s = None
        household.visit_route_fetched_at = None
        household.visit_route_error = (
            "" if end is None else "the barangay's water source has no location yet"
        )
        if save:
            household.save(update_fields=ROUTE_FIELDS)
        return False

    try:
        route = fetch_route(start, end)
    except RoutingUnavailable as exc:
        household.visit_route_error = str(exc)[:200]
        if save:
            household.save(update_fields=["visit_route_error"])
        logger.warning("Route for %s not fetched: %s", household.household_id, exc)
        return False

    household.visit_route = route["points"]
    household.visit_route_distance_m = route["distance_m"]
    household.visit_route_duration_s = route["duration_s"]
    household.visit_route_fetched_at = timezone.now()
    household.visit_route_error = ""
    if save:
        household.save(update_fields=ROUTE_FIELDS)
    return True
