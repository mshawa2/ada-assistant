"""Controlling the Mac itself — music and volume.

Two tools rather than eight. "Pause the music", "skip this", "what's playing"
and "turn it down" are one intent each, not one tool each; a model picks an
action from an enum more reliably than it picks between six near-identical
tool names, and the tool list stays readable.

AppleScript is fine here, unlike the calendar: System Events answers in about
0.3s because these are commands, not queries over a large collection. Values
still travel as argv rather than being interpolated into script source — a
track name with a quote in it shouldn't be able to break the script, and
nothing read from outside should be able to become code.

Deliberately not included: quitting apps, changing system settings, running
arbitrary AppleScript. Those are a much wider blast radius than "turn it
down", and `run_shortcut` already covers the case where the user genuinely
wants something bespoke — behind a confirmation, and only for shortcuts they
wrote themselves.
"""

import shutil
import subprocess

from ada.tools.registry import tool

_TIMEOUT = 15

MEDIA_ACTIONS = ("status", "play", "pause", "next", "previous")
SYSTEM_ACTIONS = ("status", "set", "mute", "unmute")

# One script per action, each a fixed constant. No string building, so there's
# nothing for a track name or a user phrase to escape out of.
_MEDIA_SCRIPTS = {
    "status": """
        tell application "System Events"
          if not (exists process "Music") then return "not-running"
        end tell
        tell application "Music"
          if player state is stopped then return "stopped"
          return (player state as text) & " | " & (name of current track) & " | " & (artist of current track)
        end tell
    """,
    "play": 'tell application "Music" to play',
    "pause": 'tell application "Music" to pause',
    "next": 'tell application "Music" to next track',
    "previous": 'tell application "Music" to previous track',
}

_GET_VOLUME = "output volume of (get volume settings)"
_GET_MUTED = "output muted of (get volume settings)"
_SET_VOLUME = 'on run argv\n  set volume output volume (item 1 of argv as integer)\nend run'
_SET_MUTED = 'on run argv\n  set volume output muted (item 1 of argv is "true")\nend run'


class MacControlUnavailable(Exception):
    """Raised when osascript isn't available — i.e. this isn't a Mac."""


def _osascript(script: str, *args: str) -> str:
    if not shutil.which("osascript"):
        raise MacControlUnavailable("This needs macOS (osascript wasn't found).")

    result = subprocess.run(
        ["osascript", "-e", script, *args],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        if "not authorized" in detail.lower() or "-1743" in detail:
            raise ValueError(
                "macOS hasn't allowed Ada to control that app. Grant it in System Settings > "
                "Privacy & Security > Automation, under whichever app you run Ada from."
            )
        raise ValueError(detail or "The command failed with no detail given.")
    return (result.stdout or "").strip()


@tool(
    name="media_control",
    description=(
        "Control or check the user's music (Apple Music). Use action='status' when they "
        "ask what's playing, and play/pause/next/previous to control it. If they want a "
        "specific song or playlist rather than transport controls, say you can't pick "
        "tracks yet rather than guessing."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(MEDIA_ACTIONS),
                "description": "What to do. Defaults to 'status'.",
            }
        },
        "required": [],
    },
)
def media_control(action: str = "status") -> str:
    action = (action or "status").strip().lower()
    if action not in MEDIA_ACTIONS:
        raise ValueError(f"'{action}' isn't a media action. Use one of: {', '.join(MEDIA_ACTIONS)}.")

    output = _osascript(_MEDIA_SCRIPTS[action])

    if action != "status":
        return f"Music: {action}."
    if output == "not-running":
        return "Music isn't running."
    if output == "stopped":
        return "Music is open but nothing is playing."

    parts = [p.strip() for p in output.split("|")]
    if len(parts) == 3:
        state, track, artist = parts
        return f"{state.capitalize()}: {track} — {artist}"
    return output


@tool(
    name="system_control",
    description=(
        "Check or change the Mac's volume, or mute it. Use action='status' to report the "
        "current volume, 'set' with a level 0-100, 'mute' or 'unmute'. Use this when the "
        "user says things like 'turn it down', 'mute that' or 'how loud is it'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(SYSTEM_ACTIONS),
                "description": "What to do. Defaults to 'status'.",
            },
            "level": {
                "type": "integer",
                "description": "Volume 0-100, required when action is 'set'.",
            },
        },
        "required": [],
    },
)
def system_control(action: str = "status", level=None) -> str:
    action = (action or "status").strip().lower()

    if action == "status":
        volume = _osascript(_GET_VOLUME)
        muted = _osascript(_GET_MUTED).lower() == "true"
        return f"Volume {volume}%" + (" (muted)" if muted else "")

    if action == "set":
        if level is None:
            raise ValueError("Setting the volume needs a level between 0 and 100.")
        clamped = max(0, min(int(level), 100))
        _osascript(_SET_VOLUME, str(clamped))
        return f"Volume set to {clamped}%."

    if action in ("mute", "unmute"):
        _osascript(_SET_MUTED, "true" if action == "mute" else "false")
        return "Muted." if action == "mute" else "Unmuted."

    raise ValueError(f"'{action}' isn't a system action. Use one of: {', '.join(SYSTEM_ACTIONS)}.")
