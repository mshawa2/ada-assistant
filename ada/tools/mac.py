"""Controlling the Mac itself — music and volume.

Transport and volume are one tool each, not eight. "Pause the music", "skip
this", "what's playing" and "turn it down" are one intent each; a model picks
an action from an enum more reliably than it picks between six near-identical
tool names, and the tool list stays readable.

Choosing *what* plays is separate (`search_music` / `play_music`), because
that's a different intent from transport — and it follows this project's
usual shape for acting on a specific thing: search hands back a stable id,
play takes it, so "no, the other one" is exact rather than a second guess.

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
        "Transport controls for the user's music (Apple Music). Use action='status' when "
        "they ask what's playing, and play/pause/next/previous to control it. To start "
        "something *specific* — a song, artist, album or playlist — use play_music "
        "instead; 'play' here just resumes whatever was already queued."
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


# --- picking something to play ----------------------------------------------
#
# Transport controls were the easy half. Choosing what plays needs a search,
# and a search over 1,631 tracks runs in 0.12s here — measured before building
# on it, because a library `whose` query is the same shape that took 37
# seconds on Calendar. It isn't; AppleScript is fine for this.
#
# Two tools rather than one, matching how the rest of this project handles
# "act on a specific thing": search returns a stable id, play takes it. That
# makes "no, the other one" exact instead of a second guess. Playing straight
# from a query is still allowed, because getting the wrong song is a trivial,
# instantly reversible harm — unlike texting the wrong person.

MUSIC_KINDS = ("any", "track", "artist", "album")
MAX_MUSIC_RESULTS = 25

# Tab-separated on the wire: a track name can contain almost any character,
# but not a tab, so parsing stays unambiguous.
# `kind` is a reserved word in AppleScript — every object has a `kind`
# property, so `set kind to ...` fails with -10006. Found by running this
# against the real library; the faked tests couldn't have caught it, which is
# the whole argument for checking the mechanism for real. Variable names here
# are deliberately unlikely to collide with anything Music defines.
_SEARCH_MUSIC = """
on run argv
  set theQuery to item 1 of argv
  set theKind to item 2 of argv
  tell application "Music"
    if theKind is "artist" then
      set theHits to (every track of library playlist 1 whose artist contains theQuery)
    else if theKind is "album" then
      set theHits to (every track of library playlist 1 whose album contains theQuery)
    else if theKind is "track" then
      set theHits to (every track of library playlist 1 whose name contains theQuery)
    else
      set theHits to (every track of library playlist 1 whose name contains theQuery or artist contains theQuery or album contains theQuery)
    end if
    set theOutput to ""
    repeat with aTrack in theHits
      set theOutput to theOutput & (persistent ID of aTrack) & tab & (name of aTrack) & tab & (artist of aTrack) & tab & (album of aTrack) & linefeed
    end repeat
    return theOutput
  end tell
end run
"""

_PLAY_TRACK = """
on run argv
  tell application "Music"
    set aTrack to (first track of library playlist 1 whose persistent ID is (item 1 of argv))
    play aTrack
    return (name of aTrack) & tab & (artist of aTrack)
  end tell
end run
"""

_PLAY_PLAYLIST = """
on run argv
  tell application "Music"
    play playlist (item 1 of argv)
    return name of current playlist
  end tell
end run
"""

_LIST_PLAYLISTS = 'tell application "Music" to get name of every playlist'

_SET_SHUFFLE = 'on run argv\n  tell application "Music" to set shuffle enabled to (item 1 of argv is "true")\nend run'


def _find_tracks(query: str, kind: str) -> list[dict]:
    raw = _osascript(_SEARCH_MUSIC, query, kind)
    tracks = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) >= 4 and parts[0].strip():
            tracks.append(
                {"id": parts[0].strip(), "title": parts[1], "artist": parts[2], "album": parts[3]}
            )
    return tracks


def _find_playlist(name: str) -> str | None:
    """A playlist by name — exact match first, then a contains match."""
    needle = name.strip().lower()
    playlists = [p.strip() for p in _osascript(_LIST_PLAYLISTS).split(", ") if p.strip()]

    for playlist in playlists:
        if playlist.lower() == needle:
            return playlist
    for playlist in playlists:
        if needle in playlist.lower():
            return playlist
    return None


@tool(
    name="search_music",
    description=(
        "Search the user's Apple Music library for tracks, without playing anything. Use "
        "this when they ask what music they have, or when 'play X' is ambiguous and you "
        "want to offer them a choice. Each result has an id you can pass to play_music "
        "for an exact match.\n\n"
        "This only sees their own library — songs they've added — not the whole Apple "
        "Music catalogue. If something isn't there, say so rather than implying you "
        "searched all of streaming."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to look for — a song, artist or album."},
            "kind": {
                "type": "string",
                "enum": list(MUSIC_KINDS),
                "description": "Narrow the search to one field. Defaults to 'any'.",
            },
        },
        "required": ["query"],
    },
)
def search_music(query: str, kind: str = "any") -> str:
    query = (query or "").strip()
    kind = (kind or "any").strip().lower()

    if not query:
        raise ValueError("No search query given.")
    if kind not in MUSIC_KINDS:
        raise ValueError(f"'{kind}' isn't a music search kind. Use one of: {', '.join(MUSIC_KINDS)}.")

    tracks = _find_tracks(query, kind)
    if not tracks:
        return (
            f"Nothing in the library matching '{query}'. That's their own library only, "
            "not the whole Apple Music catalogue."
        )

    shown = tracks[:MAX_MUSIC_RESULTS]
    lines = [f"{t['title']} — {t['artist']}  ({t['album']})  id={t['id']}" for t in shown]
    if len(tracks) > len(shown):
        lines.append(f"({len(tracks) - len(shown)} more — narrow it down.)")
    return "\n".join(lines)


@tool(
    name="play_music",
    description=(
        "Play something specific from the user's Apple Music library — a song, artist, "
        "album or playlist. Pass track_id for an exact track from search_music, or a "
        "query to play the best match. Always say what you actually started playing, so "
        "they can correct you if it's the wrong one.\n\n"
        "This plays from their own library, not the whole Apple Music catalogue. Use "
        "media_control for pause, skip and what's-playing."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to play — a song, artist, album or playlist name.",
            },
            "track_id": {
                "type": "string",
                "description": "An exact track id from search_music. Takes precedence over query.",
            },
            "kind": {
                "type": "string",
                "enum": list(MUSIC_KINDS) + ["playlist"],
                "description": "What the query refers to. Defaults to 'any'.",
            },
            "shuffle": {"type": "boolean", "description": "Shuffle before playing. Defaults to false."},
        },
        "required": [],
    },
)
def play_music(query: str = "", track_id: str = "", kind: str = "any", shuffle: bool = False) -> str:
    query = (query or "").strip()
    track_id = (track_id or "").strip()
    kind = (kind or "any").strip().lower()

    if not query and not track_id:
        raise ValueError("Nothing to play — give a query or a track_id.")
    if kind not in MUSIC_KINDS and kind != "playlist":
        raise ValueError(f"'{kind}' isn't a valid kind. Use one of: {', '.join(MUSIC_KINDS)}, playlist.")

    if shuffle:
        _osascript(_SET_SHUFFLE, "true")

    # An exact id beats searching again — this is the whole point of
    # search_music handing one back.
    if track_id:
        played = _osascript(_PLAY_TRACK, track_id)
        parts = played.split("\t")
        return f"Playing: {parts[0]} — {parts[1]}" if len(parts) >= 2 else f"Playing: {played}"

    if kind == "playlist":
        name = _find_playlist(query)
        if not name:
            raise ValueError(f"No playlist called '{query}'. Ask which playlist, or search for a track instead.")
        _osascript(_PLAY_PLAYLIST, name)
        return f"Playing playlist: {name}"

    tracks = _find_tracks(query, kind)
    if not tracks:
        # A playlist is the obvious thing a bare query might have meant.
        name = _find_playlist(query)
        if name:
            _osascript(_PLAY_PLAYLIST, name)
            return f"Playing playlist: {name}"
        return (
            f"Nothing in the library matching '{query}' — that's their own library, not "
            "the whole Apple Music catalogue. Ask if they meant something else."
        )

    first = tracks[0]
    _osascript(_PLAY_TRACK, first["id"])

    others = len(tracks) - 1
    tail = f" ({others} other match{'es' if others != 1 else ''} — say if you meant a different one.)" if others else ""
    return f"Playing: {first['title']} — {first['artist']}{tail}"
