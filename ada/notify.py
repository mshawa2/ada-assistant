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


class ChannelUnavailable(Exception):
    """A channel that is listed in the config but can't act: ntfy with no
    topic, macOS banners on something that isn't a Mac.

    It exists because the alternative was returning False, which is
    indistinguishable from "this channel had nothing to do" and so said
    nothing at all. That is the worse half of a silent failure: a phone push
    that has quietly never been configured looks exactly like one that works,
    right up until the morning you needed it.

    Distinct from a plain exception because it isn't a fault — nothing broke,
    something was never set up — and the fix is a line of config rather than
    an investigation, so it's worth saying differently.
    """


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
    That's also why an unconfigured channel doesn't make this False — the
    notice was still dealt with, and returning False would have
    `flush_pending_pushes` retrying it on every tick forever.

    Which is exactly why an inert channel has to be *said*. It can't be
    inferred from the return value, and on a launchd daemon the terminal
    channel "succeeding" means a line in heartbeat.out that nobody reads.
    `unavailable_channels` is the version of this you can ask for rather than
    having to catch in a log.
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
        except ChannelUnavailable as e:
            print(f"\n[ada: '{channel}' is one of your notification channels but can't send: {e}]", flush=True)
        except Exception as e:
            # A dead channel must never stop the heartbeat, or take the other
            # channels down with it.
            print(f"\n[ada: couldn't notify via {channel}: {e}]", flush=True)
    return delivered


def unavailable_channels() -> list[tuple[str, str]]:
    """Configured channels that can't actually send, and why — without sending.

    The question "is my alerting working?" should be answerable before the
    alert you needed, not after it didn't arrive.
    """
    settings = _settings()
    broken = []
    for channel in settings.get("channels", DEFAULT_CHANNELS):
        if channel not in _CHANNELS:
            broken.append((channel, "not a channel Ada knows about"))
            continue
        reason = _REASONS.get(channel, lambda _settings: None)(settings)
        if reason:
            broken.append((channel, reason))
    return broken


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
    reason = _macos_reason(_settings)
    if reason:
        raise ChannelUnavailable(reason)

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
    topic = _ntfy_topic(settings)
    if not topic:
        raise ChannelUnavailable(_ntfy_reason(settings))

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


def _ntfy_topic(settings: dict) -> str:
    """The topic, from the environment first.

    `ada_config.json` is tracked in git and a topic name is effectively a
    password — anyone who learns yours can read your notices and post to them.
    ADA_NTFY_TOPIC in .env keeps it out of the repo; the config key stays as a
    fallback for anyone who doesn't mind.
    """
    return (os.environ.get("ADA_NTFY_TOPIC") or settings.get("ntfy_topic", "") or "").strip()


def _ntfy_reason(settings: dict) -> str | None:
    if _ntfy_topic(settings):
        return None
    return "no topic is set — put an unguessable ADA_NTFY_TOPIC in .env and subscribe to it in the ntfy app"


def _macos_reason(_settings: dict) -> str | None:
    if shutil.which("osascript"):
        return None
    return "osascript isn't on PATH, so this isn't a Mac — or PATH is too narrow (see launchd/README.md)"


_CHANNELS = {
    "terminal": _send_terminal,
    "macos": _send_macos,
    "ntfy": _send_ntfy,
}

# How to ask a channel whether it could send, without sending. Absent means
# "always can" — the terminal is always there.
_REASONS = {
    "macos": _macos_reason,
    "ntfy": _ntfy_reason,
}
