"""How long it takes to get somewhere, so an alert can be about leaving.

A calendar event with a location knows *when* you have to be there and never
tells you when to set off, which is the number you actually act on. That's a
geocode and a route away, and both can be had without an API key — the same
trade `weather.py` makes with Open-Meteo and `notify.py` with ntfy.

## Two services, and what's honest about them

**Nominatim** (OpenStreetMap) turns "233 S Wacker Dr, Chicago" into
coordinates. Open-Meteo's geocoder can't: it matches *place names*, and returns
nothing for a street address — measured, not assumed, and the same limitation
that made `location.place_candidates` necessary. Nominatim's usage policy asks
for an identifying User-Agent and at most one request a second, so results are
cached on disk permanently. Buildings do not move.

**OSRM's public demo server** does the routing. It is explicitly *not* offered
for production use, and this treats it accordingly: every failure is soft, a
route that can't be had produces no leave-by line rather than an error, and
results are cached so a check running every minute doesn't ask twice for the
same journey.

**It does not know about traffic.** The public OSRM instance routes on free-flow
speeds, so its estimate is the optimistic one. That is the wrong direction for
this particular feature — being told to leave later than you should is the
failure that makes you late — which is what `buffer_minutes` exists to correct,
and why it defaults to something rather than zero.

Neither service is asked anything until an event with a location is actually
coming up, so a calendar of untethered meetings costs nothing.
"""

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request

from ada import store

_GEOCODE_URL = "https://nominatim.openstreetmap.org/search"
_ROUTE_URL = "https://router.project-osrm.org/route/v1"
_TIMEOUT_SECONDS = 15

# Nominatim asks that clients identify themselves rather than arrive anonymous.
_HEADERS = {"User-Agent": "ada-assistant (personal calendar assistant)"}

_GEOCODE_CACHE = store.DATA_DIR / "geocode_cache.json"
_cache_lock = store.lock_for(_GEOCODE_CACHE)

# Routes are cached in memory only: they depend on where you currently are, so
# they go stale by moving rather than by time, and a restart should re-ask.
_ROUTE_TTL_SECONDS = 600
_routes: dict = {}

MODES = {"driving": "driving", "walking": "foot", "cycling": "bike"}


class TravelUnavailable(Exception):
    """No route could be had. Always soft — the caller drops the leave-by line
    rather than failing, because a missing estimate is not an emergency."""


def _get(url: str):
    request = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as e:
        raise TravelUnavailable(f"the routing service answered {e.code}") from e
    except urllib.error.URLError as e:
        raise TravelUnavailable(f"couldn't reach the routing service: {e.reason}") from e
    except (OSError, ValueError) as e:
        raise TravelUnavailable(f"couldn't reach the routing service: {e}") from e


def geocode(place: str) -> tuple[float, float]:
    """Coordinates for a free-text place or street address.

    Cached on disk without expiry: the answer for an address doesn't change,
    and Nominatim's usage policy asks callers not to re-ask for what they've
    already been told.
    """
    key = " ".join((place or "").split()).lower()
    if not key:
        raise TravelUnavailable("no destination given")

    with _cache_lock:
        cached = store.read_json(_GEOCODE_CACHE, {})
    if key in cached:
        hit = cached[key]
        if hit is None:
            raise TravelUnavailable(f"couldn't find '{place}' on the map")
        return hit[0], hit[1]

    url = f"{_GEOCODE_URL}?{urllib.parse.urlencode({'q': place, 'format': 'json', 'limit': 1})}"
    results = _get(url)
    found = (float(results[0]["lat"]), float(results[0]["lon"])) if results else None

    with _cache_lock:
        cached = store.read_json(_GEOCODE_CACHE, {})
        # A miss is cached too. A location that isn't a place — "Zoom", "TBC",
        # someone's desk — is most of a real calendar, and re-asking Nominatim
        # about "Zoom" every minute is exactly the behaviour its policy is
        # asking clients not to have.
        cached[key] = list(found) if found else None
        store.write_json(_GEOCODE_CACHE, cached)

    if not found:
        raise TravelUnavailable(f"couldn't find '{place}' on the map")
    return found


def travel_seconds(origin: tuple[float, float], destination: tuple[float, float], mode: str = "driving") -> float:
    """Free-flow travel time between two points. See the module docstring on
    why free-flow is the optimistic number and what corrects for it."""
    profile = MODES.get(mode, "driving")
    key = (round(origin[0], 3), round(origin[1], 3), round(destination[0], 4), round(destination[1], 4), profile)

    hit = _routes.get(key)
    if hit and (time.time() - hit[0]) < _ROUTE_TTL_SECONDS:
        return hit[1]

    url = (
        f"{_ROUTE_URL}/{profile}/"
        f"{origin[1]},{origin[0]};{destination[1]},{destination[0]}?overview=false"
    )
    answer = _get(url)
    if answer.get("code") != "Ok" or not answer.get("routes"):
        raise TravelUnavailable(f"no {mode} route to there")

    seconds = float(answer["routes"][0]["duration"])
    _routes[key] = (time.time(), seconds)
    return seconds


def looks_like_a_place(location: str) -> bool:
    """A cheap filter before anything is asked of the network.

    Most calendar locations aren't places. A conference link, a room name or a
    phone number shouldn't cost a geocode to find that out, and shouldn't
    produce "you need to leave for Zoom in 20 minutes".
    """
    text = (location or "").strip()
    if len(text) < 3:
        return False
    lowered = text.lower()
    if "://" in lowered or "zoom.us" in lowered or "meet.google" in lowered or "teams.microsoft" in lowered:
        return False
    if lowered in {"tbc", "tbd", "online", "remote", "home", "zoom", "phone", "call", "virtual", "n/a"}:
        return False
    return any(c.isalpha() for c in text)


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Straight-line distance, used only to reject the absurd before routing.

    A destination on another continent is a calendar entry about a trip, not a
    journey you are about to make, and asking OSRM to route across an ocean
    wastes a call to answer a question nobody asked.
    """
    lat1, lon1, lat2, lon2 = (math.radians(x) for x in (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))
