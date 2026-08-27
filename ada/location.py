"""Where this Mac actually is, for the brief's weather line.

## CoreLocation was tried first, and cannot work

The obvious route is `CLLocationManager`. It was built and measured before
being rejected: authorization stayed at `notDetermined`, no prompt ever
appeared, and the first fix came back
`kCLErrorDomain Code=1` — `kCLErrorDenied`.

That isn't a missing entitlement to go and add. macOS attributes a Location
Services prompt to a **signed app bundle** carrying
`NSLocationWhenInUseUsageDescription`, and Ada is a Python process started by
launchd. The daemon is the process that needs the location at 08:00, and it is
the least bundle-like part of the whole system.

## Shortcuts can, because the permission belongs to Shortcuts

`shortcuts run` executes inside the Shortcuts app, which *is* a signed bundle
and *can* hold Location Services permission. So the fix is to borrow its
authorization rather than try to obtain our own — the same shape as
`home.py` borrowing Shortcuts to reach HomeKit, for the same reason.

The cost is one manual setup step that can't be automated: importing an
unsigned shortcut needs "Allow Untrusted Shortcuts", which is a worse thing to
ask someone to turn on than "build these two actions once". `SETUP` below is
the instruction, and it's surfaced in the error rather than buried in a README,
because the moment you find out this isn't configured is the moment you want to
be told how to configure it.

## It accepts either shape of answer

A shortcut ending in *Get Details of Location → City* returns a place name; one
returning the location itself stringifies to coordinates. Both are useful and
neither is worth insisting on, so `here()` returns the raw text and
`looks_like_coordinates` decides which it is. Being forgiving here costs a
regex and saves the user rebuilding their shortcut because Ada wanted the other
format.
"""

import re
import shutil
import subprocess

from ada import settings

DEFAULT_SHORTCUT = "Ada Location"
# A real fix is quick once the shortcut exists; the long waits were all the
# missing-shortcut case, which is now caught before running anything.
_TIMEOUT_SECONDS = 12
_LIST_TIMEOUT_SECONDS = 10

SETUP = (
    "Create a Shortcut named '{name}' in the Shortcuts app: add 'Get Current Location', "
    "then 'Stop and Output' that location (or add 'Get Details of Location' → City first "
    "if you'd rather it returned a place name). Run it once by hand so macOS asks for "
    "Location permission and you can allow it."
)

# "30.2672, -97.7431" — two signed decimals, which no place name looks like.
_COORDINATES = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


class LocationUnavailable(Exception):
    """The Mac's location couldn't be read. Always carries what to do about it,
    because every cause here is a setup step rather than a fault."""


def shortcut_name() -> str:
    return (settings.get("locale.location_shortcut", "") or DEFAULT_SHORTCUT).strip()


def looks_like_coordinates(text: str) -> tuple[float, float] | None:
    """`(lat, lon)` if the text is a coordinate pair, else None."""
    match = _COORDINATES.match(text or "")
    if not match:
        return None
    lat, lon = float(match.group(1)), float(match.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def _known_shortcuts() -> list[str]:
    result = subprocess.run(
        ["shortcuts", "list"], capture_output=True, text=True, timeout=_LIST_TIMEOUT_SECONDS
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]


def here() -> str:
    """Whatever the location shortcut returns, as text. Raises if it can't."""
    name = shortcut_name()
    if not shutil.which("shortcuts"):
        raise LocationUnavailable("the `shortcuts` command isn't available — this needs macOS 12 or later")

    # Checked before running, because `shortcuts run` on a name that doesn't
    # exist doesn't fail — it sits there until the timeout, and then the brief
    # reports a slow location fix for a shortcut that was never created. Twenty
    # seconds of a morning brief spent waiting on a typo is its own bug.
    if name not in _known_shortcuts():
        raise LocationUnavailable(f"there's no shortcut called '{name}'. " + SETUP.format(name=name))

    try:
        result = subprocess.run(
            # --output-path - is what makes a shortcut's result reach stdout at
            # all; without it `shortcuts run` succeeds and prints nothing.
            ["shortcuts", "run", name, "--output-path", "-"],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as e:
        raise LocationUnavailable(
            f"'{name}' didn't finish in {_TIMEOUT_SECONDS}s — a location fix can be slow indoors"
        ) from e

    if result.returncode != 0:
        detail = (result.stderr or "").strip() or "no detail given"
        raise LocationUnavailable(f"'{name}' failed ({detail}). " + SETUP.format(name=name))

    text = (result.stdout or "").strip()
    if not text:
        raise LocationUnavailable(f"'{name}' returned nothing. " + SETUP.format(name=name))
    return text
