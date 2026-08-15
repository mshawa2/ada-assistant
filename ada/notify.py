"""Getting a notice off this machine and in front of you.

A notice that only lands in the terminal or on the dashboard is one you see
whenever you next happen to look — which, for anything time-sensitive, is the
same as not being told. This delivers to channels that actually interrupt.

Levels decide loudness, matching the greeting's existing rules so there's one
story about what gets to interrupt you:

  normal    never pushes — it's log-only, you'll see it when you ask
  urgent    pushes, unless quiet hours are on, in which case it's *held*
  critical  always pushes, quiet hours or not

"Held" is the important word. A suppressed push isn't dropped: the notice
stays unpushed in the store and the heartbeat retries it once quiet hours
pass, the same promise the startup greeting already makes.

Nothing in here is allowed to take down the heartbeat. Every channel is
wrapped, and a channel that fails is reported and skipped.
"""

import os
import shutil
import subprocess
import urllib.error
import urllib.request

from ada.heartbeat.config import load_config
from ada.heartbeat.quiet_hours import in_quiet_hours

TITLE = "Ada"
_TIMEOUT_SECONDS = 10

DEFAULT_CHANNELS = ["terminal"]
DEFAULT_PUSH_LEVELS = ["urgent", "critical"]

# Disposition of a notice, decided before any channel is touched.
SEND = "send"
HOLD = "hold"
NEVER = "never"


def _settings() -> dict:
    return load_config().get("notifications", {})


def push_disposition(notice: dict) -> str:
    """Whether this notice should go out now, wait, or never push at all.

    Split out from `deliver` so the caller can tell "held until quiet hours
    end" apart from "this level never pushes" — one is worth retrying on every
    tick, the other would be retried forever for nothing.
    """
    config = load_config()
    if notice.get("level") not in config.get("notifications", {}).get("push_levels", DEFAULT_PUSH_LEVELS):
        return NEVER
    if notice.get("level") != "critical" and in_quiet_hours(config.get("quiet_hours", {})):
        return HOLD
    return SEND


def deliver(notice: dict) -> bool:
    """Push `notice` to every configured channel. True if any channel took it.

    A partial success still counts: if the phone push failed but the desktop
    banner landed, you were told, and retrying would only duplicate the banner.
    """
    settings = _settings()
    channels = settings.get("channels", DEFAULT_CHANNELS)
    text = notice.get("text", "")
    urgent = notice.get("level") == "critical"

    delivered = False
    for channel in channels:
        sender = _CHANNELS.get(channel)
        if sender is None:
            print(f"\n[ada: unknown notification channel '{channel}' — skipping]", flush=True)
            continue
        try:
            if sender(text, settings, urgent):
                delivered = True
        except Exception as e:
            # A dead channel must never stop the heartbeat, or take the other
            # channels down with it.
            print(f"\n[ada: couldn't notify via {channel}: {e}]", flush=True)
    return delivered


def _send_terminal(text: str, _settings: dict, _urgent: bool) -> bool:
    print(f"\n[ada] {text}", flush=True)
    return True


def _send_macos(text: str, _settings: dict, _urgent: bool) -> bool:
    """A real Notification Centre banner, via osascript.

    The message is passed as an argv item rather than interpolated into the
    AppleScript source — a notice containing a quote would otherwise break the
    script, and a notice built from fetched web content shouldn't be able to
    inject AppleScript at all.
    """
    if not shutil.which("osascript"):
        return False

    script = "on run argv\n  display notification (item 1 of argv) with title (item 2 of argv)\nend run"
    result = subprocess.run(
        ["osascript", "-e", script, text, TITLE],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "osascript failed")
    return True


def _send_ntfy(text: str, settings: dict, urgent: bool) -> bool:
    """Push to a phone via ntfy.sh — no account, no API key, just a topic.

    Chosen for the same reason web.py uses no search API and weather.py uses
    Open-Meteo: it needs no signup. The trade-off is real and worth stating —
    a topic name is effectively a shared secret, so anyone who learns yours can
    read your notices. Pick an unguessable one.
    """
    # Env first: ada_config.json is tracked in git, and a topic name is
    # effectively a password — anyone who learns yours can read your notices.
    # ADA_NTFY_TOPIC in .env keeps it out of the repo.
    topic = (os.environ.get("ADA_NTFY_TOPIC") or settings.get("ntfy_topic", "")).strip()
    if not topic:
        return False

    request = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=text.encode(),
        headers={"Title": TITLE, "Priority": "urgent" if urgent else "default"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            return 200 <= response.status < 300
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"ntfy returned {e.code}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"couldn't reach ntfy: {e.reason}") from e
    except OSError as e:
        # Same broad backstop as stt/tts/web/weather: ssl.SSLError is an
        # OSError but not a URLError, and would otherwise escape uncaught.
        raise RuntimeError(f"couldn't reach ntfy: {e}") from e


_CHANNELS = {
    "terminal": _send_terminal,
    "macos": _send_macos,
    "ntfy": _send_ntfy,
}
