"""One place for everything you'd want to change about Ada.

Settings used to live in three places that didn't know about each other:
`heartbeat_config.json`, `safety_config.json`, and constants buried in the
code. This is the single file, and every one of those readers now comes
through here.

Three things make it more than a JSON load:

**Layering.** Effective settings are `DEFAULTS` <- your file <- the active
mode's preset. That means the file only has to contain what you actually want
to change, a new setting added in code appears without you editing anything,
and a mode is a small patch rather than a whole duplicated config.

**Hot reload.** The file's mtime and size are checked on every read and the
parse is cached against them. Editing `ada_config.json` takes effect on the
next call, in a running process, with no restart — the thing the heartbeat's
old load-once-at-startup config couldn't do.

**Failing safe.** An autonomy value that isn't recognised becomes `confirm`,
never `auto`. A typo in the one file that decides what Ada may do without
asking must not be able to quietly widen her permissions.
"""

import os
import threading
from copy import deepcopy
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

from ada import store

_ROOT = Path(__file__).resolve().parent.parent

# Lives next to the code, not under data/, because it's yours to hand-edit and
# worth keeping in git. `ADA_CONFIG_FILE` relocates it for the same reasons
# ADA_DATA_DIR exists: trying something out without touching your real setup,
# and running more than one Ada from one checkout.
_CONFIG_FILE = Path(os.environ.get("ADA_CONFIG_FILE") or _ROOT / "ada_config.json")
_LEGACY_HEARTBEAT = _ROOT / "heartbeat_config.json"
_LEGACY_SAFETY = _ROOT / "safety_config.json"

# Autonomy levels — what Ada may do with a given tool.
AUTO = "auto"  # run it, no questions
CONFIRM = "confirm"  # ask first, every time (the Tier 6 gate)
DENY = "deny"  # never run it; the model is told it's switched off
LEVELS = (AUTO, CONFIRM, DENY)

DEFAULTS: dict[str, Any] = {
    "identity": {
        "assistant_name": "Ada",
        "user_name": "",
        "user_pronouns": "",
        "tone": "playful and witty — warm, brief, a little fun, never stiff or corporate",
        "verbosity": "brief",
    },
    "locale": {
        # Empty means "whatever this machine is set to". Set it to an IANA name
        # like "Europe/London" to pin it — worth doing on an always-on host in
        # a different timezone from you.
        "timezone": "",
        "time_format": "24h",
        "units": "metric",
    },
    "model": {
        "conversation": "claude-sonnet-5",
        "max_tokens": 4096,
    },
    "autonomy": {
        "default": AUTO,
        # Every consequential tool is listed here, not only in the shipped
        # ada_config.json. If that file is deleted or hand-broken, the fallback
        # still has to be the safe one — a missing config must never be the
        # thing that quietly ungates deleting a note or writing to a calendar.
        "tools": {
            "forget": CONFIRM,
            "delete_note": CONFIRM,
            "set_setting": CONFIRM,
            "create_event": CONFIRM,
            "delete_event": CONFIRM,
            "send_message": CONFIRM,
        },
        # A ceiling on top of confirmation. `confirm` protects against one
        # wrong action; it does nothing about thirty, because the human in the
        # loop is the part that gets tired and starts saying yes.
        "limits": {
            "send_message": {"per_minute": 3, "per_hour": 10, "per_day": 30},
            # Not a safety rail — this one protects the free tier on your
            # search provider from a model that decides to search twelve times
            # to answer one question.
            "web_search": {"per_minute": 10, "per_day": 300},
        },
    },
    "search": {
        # "auto" picks Brave or Tavily if their key is in .env, else Wikipedia.
        "provider": "auto",
    },
    "notifications": {
        "channels": ["terminal", "macos"],
        "push_levels": ["urgent", "critical"],
        "ntfy_topic": "",
    },
    "quiet_hours": {"start": "22:00", "end": "08:00"},
    "heartbeat": {
        "tick_seconds": 30,
        "checks": {},
    },
    "modes": {
        "active": "default",
        "presets": {},
    },
}

_lock = threading.RLock()
_cache: dict[str, Any] = {"stamp": None, "value": None}


def _deep_merge(base: dict, override: dict) -> dict:
    """`override` layered onto `base`, recursing into nested dicts.

    Lists replace rather than concatenate: "channels are terminal and macos"
    should be overridable to "channels are just ntfy", which appending
    couldn't express.
    """
    merged = deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _stamp():
    try:
        info = _CONFIG_FILE.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


def _migrate_legacy() -> None:
    """Fold the two old config files into `ada_config.json`, once.

    Only runs when the new file is genuinely absent, so it can't clobber
    anything, and it carries over hand-edits to the old files rather than
    resetting someone's tuning to defaults on upgrade.
    """
    if _CONFIG_FILE.exists():
        return
    if not (_LEGACY_HEARTBEAT.exists() or _LEGACY_SAFETY.exists()):
        return

    heartbeat = store.read_json(_LEGACY_HEARTBEAT, {})
    safety = store.read_json(_LEGACY_SAFETY, {})

    migrated: dict[str, Any] = {}
    if "quiet_hours" in heartbeat:
        migrated["quiet_hours"] = heartbeat["quiet_hours"]
    if "notifications" in heartbeat:
        migrated["notifications"] = heartbeat["notifications"]
    heartbeat_block = {k: heartbeat[k] for k in ("tick_seconds", "checks") if k in heartbeat}
    if heartbeat_block:
        migrated["heartbeat"] = heartbeat_block
    gated = safety.get("confirmation_required_tools", [])
    if gated:
        migrated["autonomy"] = {"tools": {name: CONFIRM for name in gated}}

    store.write_json(_CONFIG_FILE, migrated)
    print(
        f"\n[ada: settings moved into {_CONFIG_FILE.name} — your old "
        f"{_LEGACY_HEARTBEAT.name} and {_LEGACY_SAFETY.name} values were carried over]",
        flush=True,
    )


def load_raw() -> dict:
    """Exactly what's in the file — no defaults, no mode applied.

    This is what a write path edits, so saving can't accidentally bake every
    default into the user's file and freeze it against future changes.
    """
    with _lock:
        _migrate_legacy()
        raw = store.read_json(_CONFIG_FILE, {})
    return raw if isinstance(raw, dict) else {}


def save_raw(raw: dict) -> None:
    with _lock:
        store.write_json(_CONFIG_FILE, raw)
        _cache["stamp"] = None  # force the next read to re-parse


def load() -> dict:
    """The effective settings: defaults <- file <- active mode."""
    with _lock:
        stamp = _stamp()
        if stamp is not None and _cache["stamp"] == stamp and _cache["value"] is not None:
            return _cache["value"]

        merged = _deep_merge(DEFAULTS, load_raw())
        merged = _apply_active_mode(merged)
        _cache["stamp"] = _stamp()
        _cache["value"] = merged
        return merged


def _apply_active_mode(config: dict) -> dict:
    modes = config.get("modes", {})
    active = modes.get("active", "default")
    preset = modes.get("presets", {}).get(active)
    if not preset:
        return config
    applied = _deep_merge(config, preset)
    # The mode's own identity survives the merge, so `active` still reports
    # which preset produced these values.
    applied["modes"]["active"] = active
    return applied


def get(path: str, default: Any = None) -> Any:
    """A dotted lookup into the effective settings, e.g. 'locale.timezone'."""
    node: Any = load()
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set_path(path: str, value: Any) -> None:
    """Write one dotted path into the file, leaving everything else alone."""
    raw = load_raw()
    node = raw
    parts = path.split(".")
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value
    save_raw(raw)


# --- autonomy ---------------------------------------------------------------


def autonomy_for(tool_name: str) -> str:
    """What Ada may do with this tool: auto, confirm, or deny.

    An unrecognised value is treated as `confirm` and reported. This is the
    one file that decides what runs without asking, so a typo has to fail
    towards asking, never towards acting.
    """
    autonomy = load().get("autonomy", {})
    level = autonomy.get("tools", {}).get(tool_name, autonomy.get("default", AUTO))
    if level not in LEVELS:
        print(
            f"\n[ada: '{level}' isn't a valid autonomy level for '{tool_name}' "
            f"(expected one of {', '.join(LEVELS)}) — asking for confirmation instead]",
            flush=True,
        )
        return CONFIRM
    return level


# --- locale -----------------------------------------------------------------


def timezone() -> tzinfo | None:
    """The configured timezone, or None to mean this machine's own.

    An unknown name falls back to the system zone with a warning rather than
    raising: a typo here shouldn't stop Ada working, but it also shouldn't
    silently schedule reminders in the wrong zone.
    """
    name = get("locale.timezone", "")
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        print(f"\n[ada: unknown timezone '{name}' — using this machine's timezone instead]", flush=True)
        return None


def now() -> datetime:
    """The current time in the user's configured timezone."""
    zone = timezone()
    return datetime.now(zone) if zone else datetime.now().astimezone()


def time_format() -> str:
    """A strftime format for a time-of-day, honouring the 12h/24h setting."""
    return "%I:%M %p" if get("locale.time_format") == "12h" else "%H:%M"
