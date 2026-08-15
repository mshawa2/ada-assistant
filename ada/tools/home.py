"""Smart home control.

A provider seam, same shape as `search.py`:

- **Home Assistant** when `HASS_URL` and `HASS_TOKEN` are set. It's the one
  interface that already speaks to nearly everything, and its REST API is
  plain JSON over HTTP — no dependency, no OAuth.
- **Apple Shortcuts** otherwise, via the `shortcuts` CLI. Keyless, already on
  every modern Mac, and the practical route to HomeKit from macOS — the
  HomeKit framework itself is effectively iOS-only. The trade-off is real and
  worth stating: Ada can only run shortcuts *you* already wrote, and can't see
  what any of them do.

**Risk is tiered by what the device is, not by whether it's "smart home".**
Turning a lamp off and unlocking the front door are not the same act, and
gating both behind a y/n is how people learn to stop reading the prompt. So
lights, switches and scenes run freely, while locks, garage doors, alarms and
thermostats always confirm — keyed on the Home Assistant *domain*, which is
exactly the information needed to tell them apart. The list is config
(`home.confirm_domains`), so anyone who disagrees can move the line without
touching code.

`run_shortcut` always confirms, because a shortcut is opaque: it could set a
timer or it could open the garage, and this module has no way to know which.
"""

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request

from ada import settings
from ada.tools.registry import tool

_TIMEOUT = 15
MAX_DEVICES = 60

URL_KEY_NAME = "HASS_URL"
TOKEN_KEY_NAME = "HASS_TOKEN"

# Controllable domains. Sensors and diagnostics are noise for "what can I
# actually do something to".
CONTROLLABLE = ("light", "switch", "scene", "fan", "lock", "cover", "climate", "media_player")

# Anything in these domains asks first, whatever else is set: physical
# security and heating — the places where being wrong is expensive.
DEFAULT_CONFIRM_DOMAINS = ["lock", "cover", "alarm_control_panel", "climate", "water_heater"]

_SETUP_HELP = (
    "No smart home is connected. Two options:\n"
    f"  1. Home Assistant — set {URL_KEY_NAME} (e.g. http://homeassistant.local:8123) and "
    f"{TOKEN_KEY_NAME} (a long-lived access token) in .env.\n"
    "  2. Apple Shortcuts — make a Shortcut that does what you want, then ask Ada to run it "
    "by name. That's the route to HomeKit from a Mac.\n"
    "Tell the user this plainly rather than pretending to have controlled anything."
)


class HomeUnavailable(Exception):
    """Raised when no smart home backend is configured or reachable."""


def _hass_configured() -> bool:
    return bool(os.environ.get(URL_KEY_NAME) and os.environ.get(TOKEN_KEY_NAME))


def _confirm_domains() -> list[str]:
    configured = settings.get("home.confirm_domains")
    return configured if isinstance(configured, list) else DEFAULT_CONFIRM_DOMAINS


# "on" and "off" are the wrong words for a lock or a garage door. On the one
# prompt where being misread actually matters, the verb should say what will
# physically happen — "UNLOCK the front door", not "OFF lock.front_door".
_PLAIN_VERBS = {
    "lock": {"on": "LOCK", "off": "UNLOCK", "toggle": "TOGGLE THE LOCK ON"},
    "cover": {"on": "OPEN", "off": "CLOSE", "toggle": "TOGGLE"},
    "alarm_control_panel": {"on": "ARM", "off": "DISARM", "toggle": "TOGGLE"},
}

_WHY_GATED = {
    "lock": "a lock",
    "cover": "a door or garage",
    "alarm_control_panel": "an alarm",
    "climate": "heating/cooling",
    "water_heater": "a water heater",
}


def _describe_control(tool_input: dict) -> str:
    entity_id = str(tool_input.get("entity_id", "?"))
    action = str(tool_input.get("action", "?")).lower()
    domain = entity_id.split(".", 1)[0].lower()

    verb = _PLAIN_VERBS.get(domain, {}).get(action, action.upper())
    why = _WHY_GATED.get(domain, "a device that always asks first")
    return f"Ada wants to {verb}: {entity_id}\n    This is {why}."


def needs_confirmation(entity_id: str) -> bool:
    """True if this entity is in a domain that always asks first."""
    domain = str(entity_id).split(".", 1)[0].lower()
    return domain in [d.lower() for d in _confirm_domains()]


# --- Home Assistant ---------------------------------------------------------


def _hass(path: str, payload: dict | None = None):
    url = os.environ[URL_KEY_NAME].rstrip("/") + path
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {os.environ[TOKEN_KEY_NAME]}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            body = response.read().decode("utf-8", errors="replace")
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise HomeUnavailable(f"Home Assistant rejected the token — check {TOKEN_KEY_NAME} in .env.") from e
        if e.code == 404:
            raise ValueError("Home Assistant didn't recognise that entity or service.") from e
        raise ValueError(f"Home Assistant returned an error: HTTP {e.code}.") from e
    except urllib.error.URLError as e:
        raise HomeUnavailable(f"Couldn't reach Home Assistant at {url}: {e.reason}") from e
    except TimeoutError as e:
        raise HomeUnavailable("Home Assistant timed out.") from e
    except OSError as e:
        # ssl.SSLError is an OSError but not a URLError — same backstop as
        # every other network module here.
        raise HomeUnavailable(f"Network problem reaching Home Assistant: {e}") from e
    except json.JSONDecodeError as e:
        raise ValueError("Home Assistant returned something that wasn't JSON.") from e


def _hass_devices(area_filter: str = "") -> list[dict]:
    needle = area_filter.strip().lower()

    devices = []
    for entity in _hass("/api/states") or []:
        entity_id = entity.get("entity_id", "")
        if entity_id.split(".", 1)[0] not in CONTROLLABLE:
            continue
        name = (entity.get("attributes") or {}).get("friendly_name") or entity_id
        if needle and needle not in f"{name} {entity_id}".lower():
            continue
        devices.append({"entity_id": entity_id, "name": name, "state": entity.get("state", "?")})

    devices.sort(key=lambda d: d["entity_id"])
    return devices


def _hass_call(entity_id: str, action: str, brightness) -> str:
    domain = entity_id.split(".", 1)[0]
    # scene.turn_off isn't a thing; a scene is only ever activated.
    service = "turn_on" if domain == "scene" else {"on": "turn_on", "off": "turn_off", "toggle": "toggle"}[action]

    payload: dict = {"entity_id": entity_id}
    if brightness is not None:
        payload["brightness_pct"] = max(1, min(int(brightness), 100))

    _hass(f"/api/services/{domain}/{service}", payload)

    detail = f" at {payload['brightness_pct']}%" if "brightness_pct" in payload else ""
    return f"{entity_id} → {action}{detail}."


# --- Apple Shortcuts --------------------------------------------------------


def _shortcuts_available() -> bool:
    return bool(shutil.which("shortcuts"))


def _shortcut_names() -> list[str]:
    result = subprocess.run(["shortcuts", "list"], capture_output=True, text=True, timeout=_TIMEOUT)
    if result.returncode != 0:
        raise HomeUnavailable(f"Couldn't list Shortcuts: {result.stderr.strip()}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


# --- tools ------------------------------------------------------------------


@tool(
    name="list_home_devices",
    description=(
        "List the smart home devices the user can control, with their current state — or "
        "the Shortcuts available, if Home Assistant isn't connected. Use this before "
        "controlling anything so you use the right name, and when the user asks what's "
        "on, off, or available."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "filter": {
                "type": "string",
                "description": "Optional word to narrow the list, e.g. 'kitchen' or 'lamp'.",
            }
        },
        "required": [],
    },
)
def list_home_devices(filter: str = "") -> str:
    if _hass_configured():
        devices = _hass_devices(filter)
        if not devices:
            return f"No devices matching '{filter}'." if filter else "No controllable devices found."

        shown = devices[:MAX_DEVICES]
        lines = [f"{d['name']}  ({d['entity_id']}) — {d['state']}" for d in shown]
        if len(devices) > len(shown):
            lines.append(f"({len(devices) - len(shown)} more — narrow it down with a filter.)")
        return "\n".join(lines)

    if _shortcuts_available():
        names = _shortcut_names()
        if filter:
            names = [n for n in names if filter.lower() in n.lower()]
        if not names:
            return f"No Shortcuts matching '{filter}'."
        listed = "\n".join(f"  {n}" for n in names[:MAX_DEVICES])
        return (
            "Home Assistant isn't connected, so this is Apple Shortcuts instead. "
            "Available shortcuts:\n" + listed + "\n\nRun one with run_shortcut."
        )

    raise HomeUnavailable(_SETUP_HELP)


@tool(
    name="control_home_device",
    description=(
        "Turn a smart home device on or off, toggle it, activate a scene, or set a "
        "light's brightness. Use the entity id from list_home_devices — don't guess one. "
        "Locks, garage doors, alarms and thermostats will ask the user to confirm; "
        "lights and switches won't."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "entity_id": {
                "type": "string",
                "description": "The device's id from list_home_devices, e.g. 'light.kitchen'.",
            },
            "action": {
                "type": "string",
                "enum": ["on", "off", "toggle"],
                "description": "What to do. Scenes are always activated regardless.",
            },
            "brightness": {
                "type": "integer",
                "description": "Optional brightness percentage 1-100, for lights being turned on.",
            },
        },
        "required": ["entity_id", "action"],
    },
    confirm_when=lambda tool_input: needs_confirmation(tool_input.get("entity_id", "")),
    confirm_description=lambda tool_input: _describe_control(tool_input),
)
def control_home_device(entity_id: str, action: str, brightness=None) -> str:
    entity_id = (entity_id or "").strip()
    action = (action or "").strip().lower()

    if not _hass_configured():
        raise HomeUnavailable(
            "Direct device control needs Home Assistant. Without it, use run_shortcut "
            "with a Shortcut the user has already made.\n\n" + _SETUP_HELP
        )
    if "." not in entity_id:
        raise ValueError(
            f"'{entity_id}' isn't an entity id — they look like 'light.kitchen'. "
            "Get the real one from list_home_devices rather than guessing."
        )
    if action not in ("on", "off", "toggle"):
        raise ValueError(f"'{action}' isn't a valid action. Use 'on', 'off' or 'toggle'.")

    return _hass_call(entity_id, action, brightness)


@tool(
    name="run_shortcut",
    description=(
        "Run one of the user's Apple Shortcuts by name — the way to reach HomeKit, and "
        "anything else they've automated, from a Mac. Use list_home_devices to see what "
        "exists. This always asks the user to confirm, because a shortcut can do "
        "anything and you can't see what's inside it."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The shortcut's exact name, as listed."},
            "input": {"type": "string", "description": "Optional text to pass into the shortcut."},
        },
        "required": ["name"],
    },
)
def run_shortcut(name: str, input: str = "") -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("No shortcut name given.")
    if not _shortcuts_available():
        raise HomeUnavailable("The `shortcuts` command isn't available — this needs macOS 12 or later.")

    known = _shortcut_names()
    if name not in known:
        close = [n for n in known if name.lower() in n.lower()]
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        raise ValueError(f"No shortcut called '{name}'.{hint}")

    command = ["shortcuts", "run", name]
    if input:
        # Passed on stdin rather than interpolated into the command, the same
        # reasoning as every other subprocess call in this project.
        command += ["--input-path", "-"]

    result = subprocess.run(
        command,
        input=input if input else None,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT,
    )
    if result.returncode != 0:
        raise ValueError(f"'{name}' failed: {result.stderr.strip() or 'no detail given'}")

    output = (result.stdout or "").strip()
    return f"Ran '{name}'." + (f" It returned: {output}" if output else "")
