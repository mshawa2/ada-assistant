"""Telling you when to set off, not when to arrive.

A calendar tells you a meeting is at three. The number you act on is when to
leave, and nothing computes it — so you either work it out every time or you
find out you were wrong on the way. This is the alert Ada was missing that a
phone in your pocket can actually fix, now that pushes reach you away from the
Mac.

## It stays quiet, aggressively

Most calendar entries are not journeys. `looks_like_a_place` rejects the
conference links, "TBC" and room names before anything touches the network, a
geocoding miss is cached so "Zoom" is asked about once and never again, and
nothing at all is looked up unless an event with a real location is inside the
horizon. A day of video calls costs zero requests.

## One alert per event, and it errs early

The state records which events have been alerted, keyed by the calendar's own
identifier — an alert repeated every minute for an hour is worse than no alert,
and this is a check that runs on a one-minute tick.

It fires at `lead_minutes` *before* the computed leaving time rather than at
it, because the estimate comes from OSRM's public server, which routes on
free-flow speeds and knows nothing about traffic. Optimistic is the dangerous
direction here — being told to leave later than you should is the failure that
makes you late — so `buffer_minutes` pads the journey and the lead gives you
warning on top. Both are settings, because how much slack someone wants before
leaving is not a thing to have an opinion about on their behalf.
"""

import time

from ada import location, travel
from ada.heartbeat.notices import add_notice
from ada.heartbeat.state import get_check_state, update_check_state
from ada.tools import calendar_tool

CHECK_NAME = "leave_by"

DEFAULT_HORIZON_HOURS = 4
DEFAULT_LEAD_MINUTES = 10
DEFAULT_BUFFER_MINUTES = 10
DEFAULT_MAX_KM = 300


def _origin() -> tuple[float, float]:
    """Where you're setting off from, as precisely as can be had.

    The **whole address** is tried first, and that matters more than it looks.
    `location.place_candidates` exists because Open-Meteo's geocoder only
    matches place names, so it reduces "1234 N Sample Ave, Chicago IL 60601" to
    "Chicago" — which is correct for a weather forecast and 11.1km wrong for a
    journey. Measured, not estimated: the full address resolves to
    (41.9180, -87.7463) and the city name to (41.8756, -87.6244). At driving
    speeds that is the difference between "leave in half an hour" and "leave
    now".

    Nominatim, unlike Open-Meteo, understands street addresses. The candidates
    stay as the fallback for when it doesn't recognise one — a rough origin
    beats no alert — but they are the second answer here, not the first.
    """
    answer = location.here()
    point = location.looks_like_coordinates(answer)
    if point:
        return point

    one_line = ", ".join(line.strip() for line in answer.splitlines() if line.strip())
    for candidate in [one_line, *location.place_candidates(answer)]:
        try:
            return travel.geocode(candidate)
        except travel.TravelUnavailable:
            continue
    raise travel.TravelUnavailable("couldn't work out where you are")


def _describe(minutes_until_leaving: int, title: str, journey_minutes: int, mode: str) -> str:
    when_to_go = (
        "now" if minutes_until_leaving <= 0 else f"in about {minutes_until_leaving} minute"
        f"{'s' if minutes_until_leaving != 1 else ''}"
    )
    return f"Leave {when_to_go} for {title} — about {journey_minutes} minutes {mode} from where you are."


def check_leave_by(params: dict) -> None:
    horizon = float(params.get("horizon_hours", DEFAULT_HORIZON_HOURS)) * 3600
    lead = float(params.get("lead_minutes", DEFAULT_LEAD_MINUTES)) * 60
    buffer = float(params.get("buffer_minutes", DEFAULT_BUFFER_MINUTES)) * 60
    mode = params.get("mode", "driving")
    max_km = float(params.get("max_km", DEFAULT_MAX_KM))

    now = time.time()
    upcoming = [
        event
        for event in calendar_tool._events_between(now, now + horizon)
        if not event.isAllDay()
        and event.startDate().timeIntervalSince1970() > now
        and travel.looks_like_a_place(event.location())
    ]
    if not upcoming:
        # The common case, and it costs one calendar read and no network at all.
        return

    try:
        origin = _origin()
    except (travel.TravelUnavailable, location.LocationUnavailable):
        # Said nowhere: a notice every minute about a location lookup that
        # isn't set up would be its own problem, and `brief._weather` already
        # reports a broken location setup somewhere the user actually reads.
        return

    state = get_check_state(CHECK_NAME)
    alerted = dict(state.get("alerted", {}))

    for event in upcoming:
        identifier = event.eventIdentifier()
        if identifier in alerted:
            continue

        start = event.startDate().timeIntervalSince1970()
        try:
            destination = travel.geocode(event.location())
            if travel.haversine_km(origin, destination) > max_km:
                continue
            journey = travel.travel_seconds(origin, destination, mode)
        except travel.TravelUnavailable:
            # One unroutable meeting must not stop the others being checked.
            continue

        leave_at = start - journey - buffer
        if now < leave_at - lead:
            continue

        alerted[identifier] = start
        add_notice(
            CHECK_NAME,
            _describe(int((leave_at - now) // 60), event.title(), int(journey // 60), mode),
            level=params.get("level", "critical"),
        )

    # Forget events that have started. Keeping them forever would grow the
    # state file without bound, and an identifier can't come round again.
    update_check_state(
        CHECK_NAME, alerted={k: v for k, v in alerted.items() if v > now - 3600}
    )
