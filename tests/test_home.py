"""Smart home and Mac control.

No real device is touched and no real command runs: the Home Assistant HTTP
call, the `shortcuts` CLI and `osascript` are all replaced. Credentials are
synthetic, with the real environment cleared first.
"""

import subprocess

import pytest

from ada import agent, safety, settings, store
from ada.tools import home, mac


@pytest.fixture(autouse=True)
def no_real_home(monkeypatch):
    monkeypatch.delenv(home.URL_KEY_NAME, raising=False)
    monkeypatch.delenv(home.TOKEN_KEY_NAME, raising=False)


@pytest.fixture
def hass(monkeypatch):
    """A fake Home Assistant. Returns (states, calls-made)."""
    monkeypatch.setenv(home.URL_KEY_NAME, "http://synthetic.local:8123")
    monkeypatch.setenv(home.TOKEN_KEY_NAME, "synthetic-token")

    states = [
        {"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen"}},
        {"entity_id": "light.bedroom", "state": "off", "attributes": {"friendly_name": "Bedroom lamp"}},
        {"entity_id": "lock.front_door", "state": "locked", "attributes": {"friendly_name": "Front door"}},
        {"entity_id": "sensor.temperature", "state": "19", "attributes": {"friendly_name": "Temp"}},
    ]
    calls = []

    def fake(path, payload=None):
        calls.append({"path": path, "payload": payload})
        return states if path == "/api/states" else {}

    monkeypatch.setattr(home, "_hass", fake)
    return states, calls


# --- listing ----------------------------------------------------------------


def test_devices_are_listed_with_their_state(hass):
    result = home.list_home_devices()

    assert "Kitchen" in result and "light.kitchen" in result and "on" in result
    assert "Front door" in result


def test_sensors_are_left_out(hass):
    """'What can I control' shouldn't be buried under diagnostics."""
    assert "sensor.temperature" not in home.list_home_devices()


def test_the_list_can_be_filtered(hass):
    result = home.list_home_devices(filter="bedroom")

    assert "Bedroom lamp" in result
    assert "Kitchen" not in result


def test_with_nothing_configured_it_falls_back_to_shortcuts(monkeypatch):
    monkeypatch.setattr(home, "_shortcuts_available", lambda: True)
    monkeypatch.setattr(home, "_shortcut_names", lambda: ["Goodnight", "Movie Time"])

    result = home.list_home_devices()

    assert "Shortcuts" in result
    assert "Goodnight" in result


def test_with_nothing_at_all_it_explains_both_options(monkeypatch):
    monkeypatch.setattr(home, "_shortcuts_available", lambda: False)

    with pytest.raises(home.HomeUnavailable, match="Home Assistant"):
        home.list_home_devices()


# --- controlling ------------------------------------------------------------


def test_turning_a_light_on_calls_the_right_service(hass):
    _states, calls = hass

    result = home.control_home_device("light.kitchen", "on")

    assert calls[-1]["path"] == "/api/services/light/turn_on"
    assert calls[-1]["payload"] == {"entity_id": "light.kitchen"}
    assert "on" in result


def test_brightness_is_passed_and_clamped(hass):
    _states, calls = hass

    home.control_home_device("light.kitchen", "on", brightness=500)

    assert calls[-1]["payload"]["brightness_pct"] == 100


def test_a_scene_is_always_activated_never_turned_off(hass):
    _states, calls = hass

    home.control_home_device("scene.movie_night", "off")

    assert calls[-1]["path"] == "/api/services/scene/turn_on"


def test_a_guessed_entity_id_is_refused(hass):
    with pytest.raises(ValueError, match="isn't an entity id"):
        home.control_home_device("the kitchen light", "on")


def test_an_invalid_action_is_refused(hass):
    with pytest.raises(ValueError, match="isn't a valid action"):
        home.control_home_device("light.kitchen", "dim")


def test_control_without_home_assistant_points_at_shortcuts():
    with pytest.raises(home.HomeUnavailable, match="run_shortcut"):
        home.control_home_device("light.kitchen", "on")


# --- risk is tiered by device, not by tool ----------------------------------


@pytest.mark.parametrize(
    "entity", ["lock.front_door", "cover.garage", "alarm_control_panel.house", "climate.hall"]
)
def test_locks_doors_alarms_and_heating_always_confirm(entity):
    assert home.needs_confirmation(entity)


@pytest.mark.parametrize("entity", ["light.kitchen", "switch.fan", "scene.movie", "media_player.tv"])
def test_lights_and_switches_do_not(entity):
    """Gating a lamp behind y/n is how people learn to stop reading prompts."""
    assert not home.needs_confirmation(entity)


def test_the_confirm_list_is_config_not_code():
    store.write_json(settings._CONFIG_FILE, {"home": {"confirm_domains": ["light"]}})
    settings._cache["stamp"] = None

    assert home.needs_confirmation("light.kitchen")
    assert not home.needs_confirmation("lock.front_door")


def test_the_agent_confirms_a_lock_but_not_a_lamp():
    """The per-call predicate, through the agent's own gate."""
    assert agent._tool_asks_to_confirm("control_home_device", {"entity_id": "lock.front_door"})
    assert not agent._tool_asks_to_confirm("control_home_device", {"entity_id": "light.kitchen"})


def test_a_broken_predicate_fails_towards_asking(monkeypatch):
    """If the thing that decides has itself broken, the only safe answer is
    to ask."""
    from ada.tools.registry import get as get_tool

    monkeypatch.setattr(
        get_tool("control_home_device"),
        "confirm_when",
        lambda _i: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    assert agent._tool_asks_to_confirm("control_home_device", {"entity_id": "light.kitchen"})


def test_the_lock_confirmation_names_the_device():
    description = agent._describe_action(
        "control_home_device", {"entity_id": "lock.front_door", "action": "off"}
    )

    assert "lock.front_door" in description
    assert "UNLOCK" in description, "'off' is the wrong word for a lock"
    assert "This is a lock" in description


# --- shortcuts --------------------------------------------------------------


def test_running_a_shortcut_is_always_gated():
    """A shortcut is opaque — it could be a timer or the garage door."""
    assert safety.requires_confirmation("run_shortcut")
    assert settings.DEFAULTS["autonomy"]["tools"]["run_shortcut"] == settings.CONFIRM


def test_a_shortcut_runs_by_exact_name(monkeypatch):
    ran = []
    monkeypatch.setattr(home, "_shortcuts_available", lambda: True)
    monkeypatch.setattr(home, "_shortcut_names", lambda: ["Goodnight"])
    monkeypatch.setattr(
        home.subprocess,
        "run",
        lambda cmd, **kw: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0, "done", ""),
    )

    result = home.run_shortcut("Goodnight")

    assert ran[0] == ["shortcuts", "run", "Goodnight"]
    assert "Goodnight" in result


def test_an_unknown_shortcut_suggests_a_close_match(monkeypatch):
    monkeypatch.setattr(home, "_shortcuts_available", lambda: True)
    monkeypatch.setattr(home, "_shortcut_names", lambda: ["Movie Time"])

    with pytest.raises(ValueError, match="Did you mean: Movie Time"):
        home.run_shortcut("movie")


def test_a_failing_shortcut_reports_why(monkeypatch):
    monkeypatch.setattr(home, "_shortcuts_available", lambda: True)
    monkeypatch.setattr(home, "_shortcut_names", lambda: ["Broken"])
    monkeypatch.setattr(
        home.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "no such device"),
    )

    with pytest.raises(ValueError, match="no such device"):
        home.run_shortcut("Broken")


# --- the Mac ----------------------------------------------------------------


@pytest.fixture
def osascript(monkeypatch):
    """Capture osascript invocations instead of running them."""
    calls = []
    monkeypatch.setattr(mac.shutil, "which", lambda _n: "/usr/bin/osascript")

    def fake(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, fake.output, "")

    fake.output = ""
    monkeypatch.setattr(mac.subprocess, "run", fake)
    return fake, calls


def test_now_playing_is_reported_readably(osascript):
    fake, _calls = osascript
    fake.output = "playing | Blue Monday | New Order"

    assert mac.media_control("status") == "Playing: Blue Monday — New Order"


def test_music_not_running_says_so(osascript):
    fake, _calls = osascript
    fake.output = "not-running"

    assert "isn't running" in mac.media_control("status")


@pytest.mark.parametrize("action", ["play", "pause", "next", "previous"])
def test_transport_controls_use_fixed_scripts(osascript, action):
    _fake, calls = osascript

    mac.media_control(action)

    assert calls[-1][2] == mac._MEDIA_SCRIPTS[action], "the script is a constant, never built"


def test_an_unknown_media_action_is_refused(osascript):
    with pytest.raises(ValueError, match="isn't a media action"):
        mac.media_control("shuffle")


def test_volume_is_reported(osascript):
    fake, _calls = osascript
    fake.output = "31"

    assert "31%" in mac.system_control("status")


def test_volume_is_set_as_an_argument_not_in_the_script(osascript):
    _fake, calls = osascript

    mac.system_control("set", level=250)

    assert calls[-1][2] == mac._SET_VOLUME, "fixed script"
    assert calls[-1][3] == "100", "clamped, and passed as argv"


def test_setting_volume_without_a_level_is_refused(osascript):
    with pytest.raises(ValueError, match="needs a level"):
        mac.system_control("set")


def test_an_automation_permission_error_names_the_settings_pane(monkeypatch):
    monkeypatch.setattr(mac.shutil, "which", lambda _n: "/usr/bin/osascript")
    monkeypatch.setattr(
        mac.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "Not authorized to send Apple events"),
    )

    with pytest.raises(ValueError, match="Privacy & Security > Automation"):
        mac.media_control("play")


# --- consolidation ----------------------------------------------------------


def test_the_heartbeat_is_one_tool_not_three():
    from ada.tools import specs

    names = {spec["name"] for spec in specs()}
    assert "heartbeat" in names
    assert not {"pause_heartbeat", "resume_heartbeat", "heartbeat_status"} & names


def test_the_heartbeat_tool_reports_and_changes_state():
    from ada.tools.heartbeat_control import heartbeat

    assert "running" in heartbeat("status").lower()

    assert "paused" in heartbeat("pause").lower()
    assert "paused" in heartbeat("status").lower(), "status must reflect the change"

    assert "resumed" in heartbeat("resume").lower()
    assert "running" in heartbeat("status").lower()


def test_the_heartbeat_defaults_to_reporting_rather_than_changing():
    """A bare call must not switch anything off."""
    from ada.tools.heartbeat_control import heartbeat

    assert "running" in heartbeat().lower()


def test_an_unknown_heartbeat_action_is_refused():
    from ada.tools.heartbeat_control import heartbeat

    with pytest.raises(ValueError, match="isn't a heartbeat action"):
        heartbeat("obliterate")


@pytest.mark.parametrize(
    "entity,action,verb",
    [
        ("lock.front_door", "on", "LOCK"),
        ("lock.front_door", "off", "UNLOCK"),
        ("cover.garage", "on", "OPEN"),
        ("cover.garage", "off", "CLOSE"),
        ("alarm_control_panel.house", "off", "DISARM"),
    ],
)
def test_the_confirmation_says_what_will_physically_happen(entity, action, verb):
    """'OFF lock.front_door' is misreadable on the one prompt where being
    misread actually matters."""
    description = agent._describe_action("control_home_device", {"entity_id": entity, "action": action})

    assert verb in description


# --- history ----------------------------------------------------------------


def _history_fixture(monkeypatch, series):
    """Point the HA call at a canned history series."""
    monkeypatch.setenv(home.URL_KEY_NAME, "http://synthetic.local:8123")
    monkeypatch.setenv(home.TOKEN_KEY_NAME, "synthetic-token")
    paths = []

    def fake(path, payload=None):
        paths.append(path)
        return [series]

    monkeypatch.setattr(home, "_hass", fake)
    return paths


def _at(hours_ago, state):
    import time as _t

    return {"state": state, "last_changed": home.datetime.fromtimestamp(
        _t.time() - hours_ago * 3600, home.timezone.utc).isoformat()}


def test_history_reports_how_long_each_state_lasted(monkeypatch):
    """The raw list of changes isn't the question — 'how long was it on' is."""
    _history_fixture(monkeypatch, [_at(24, "off"), _at(20, "on"), _at(17, "off")])

    result = home.device_history("climate.hall", hours_back=24)

    assert "climate.hall" in result
    assert "on for 3h" in result, f"expected a 3h on-period, got: {result}"


def test_a_device_that_never_changed_is_still_reported(monkeypatch):
    """One state change in the window means it's been that way throughout —
    reporting 'no time on' would be the obvious way to get this wrong."""
    _history_fixture(monkeypatch, [_at(24, "on")])

    result = home.device_history("light.kitchen", hours_back=24)

    assert "on for" in result
    assert "been on throughout" in result


def test_the_change_timeline_is_shown_with_local_times(monkeypatch):
    _history_fixture(monkeypatch, [_at(10, "off"), _at(5, "on")])

    result = home.device_history("light.kitchen", hours_back=12)

    assert "Currently on" in result
    assert "→ on" in result


def test_an_empty_history_explains_why(monkeypatch):
    _history_fixture(monkeypatch, [])

    result = home.device_history("light.kitchen")

    assert "No history" in result
    assert "recorder" in result


def test_the_window_is_clamped(monkeypatch):
    paths = _history_fixture(monkeypatch, [_at(1, "on")])

    home.device_history("light.kitchen", hours_back=99999)
    home.device_history("light.kitchen", hours_back=-5)

    assert len(paths) == 2, "both calls should have gone through, just bounded"


def test_history_asks_for_only_the_one_entity(monkeypatch):
    paths = _history_fixture(monkeypatch, [_at(1, "on")])

    home.device_history("light.kitchen")

    assert "filter_entity_id=light.kitchen" in paths[0]
    assert "minimal_response" in paths[0]


def test_a_guessed_entity_id_is_refused_for_history(monkeypatch):
    _history_fixture(monkeypatch, [])

    with pytest.raises(ValueError, match="isn't an entity id"):
        home.device_history("the heating")


def test_history_without_home_assistant_says_shortcuts_cannot_do_it():
    with pytest.raises(home.HomeUnavailable, match="can't answer what a device did in the past"):
        home.device_history("climate.hall")


def test_history_is_read_only_and_ungated():
    from ada.tools import specs

    assert "device_history" in {s["name"] for s in specs()}
    assert not safety.requires_confirmation("device_history")


@pytest.mark.parametrize(
    "seconds,expected",
    [(30, "30s"), (90, "1m"), (3600, "1h"), (12000, "3h 20m"), (90000, "1d 1h")],
)
def test_durations_read_the_way_a_person_would_say_them(seconds, expected):
    assert home._duration(seconds) == expected


def test_a_malformed_timestamp_is_skipped_not_crashed_on(monkeypatch):
    _history_fixture(monkeypatch, [{"state": "on", "last_changed": "not-a-date"}, _at(2, "off")])

    result = home.device_history("light.kitchen", hours_back=6)

    assert "off for" in result, "the good entry still counts"


# --- picking music ----------------------------------------------------------


def _music(monkeypatch, tracks=(), playlists=()):
    """Fake the Music library. Returns the list of osascript calls made."""
    calls = []
    rows = "\n".join(f"{t['id']}\t{t['title']}\t{t['artist']}\t{t['album']}" for t in tracks)

    def fake(cmd, **kwargs):
        calls.append(cmd)
        script = cmd[2]
        if script == mac._SEARCH_MUSIC:
            query, kind = cmd[3].lower(), cmd[4]
            matched = []
            for t in tracks:
                field = {
                    "track": t["title"],
                    "artist": t["artist"],
                    "album": t["album"],
                }.get(kind, f"{t['title']} {t['artist']} {t['album']}")
                if query in field.lower():
                    matched.append(t)
            out = "\n".join(f"{t['id']}\t{t['title']}\t{t['artist']}\t{t['album']}" for t in matched)
            return subprocess.CompletedProcess(cmd, 0, out, "")
        if script == mac._LIST_PLAYLISTS:
            return subprocess.CompletedProcess(cmd, 0, ", ".join(playlists), "")
        if script == mac._PLAY_TRACK:
            found = next((t for t in tracks if t["id"] == cmd[3]), None)
            if not found:
                return subprocess.CompletedProcess(cmd, 1, "", "no such track")
            return subprocess.CompletedProcess(cmd, 0, f"{found['title']}\t{found['artist']}", "")
        if script == mac._PLAY_PLAYLIST:
            return subprocess.CompletedProcess(cmd, 0, cmd[3], "")
        return subprocess.CompletedProcess(cmd, 0, rows, "")

    monkeypatch.setattr(mac.shutil, "which", lambda _n: "/usr/bin/osascript")
    monkeypatch.setattr(mac.subprocess, "run", fake)
    return calls


TRACKS = [
    {"id": "AAA1", "title": "Blue Monday", "artist": "New Order", "album": "Power Corruption"},
    {"id": "BBB2", "title": "Blue Monday (remix)", "artist": "New Order", "album": "Singles"},
    {"id": "CCC3", "title": "Bang!", "artist": "AJR", "album": "OK Orchestra"},
]


def test_searching_lists_tracks_with_ids(monkeypatch):
    _music(monkeypatch, TRACKS)

    result = mac.search_music("blue monday")

    assert "Blue Monday — New Order" in result
    assert "id=AAA1" in result, "an id is what makes 'the other one' exact"


def test_search_can_be_narrowed_to_one_field(monkeypatch):
    _music(monkeypatch, TRACKS)

    assert "Bang!" in mac.search_music("AJR", kind="artist")
    assert "Bang!" not in mac.search_music("AJR", kind="track")


def test_search_is_honest_about_only_seeing_the_library(monkeypatch):
    """Implying it searched all of streaming would be a quiet lie."""
    _music(monkeypatch, TRACKS)

    result = mac.search_music("something nobody owns")

    assert "Nothing in the library" in result
    assert "not the whole Apple Music catalogue" in result


def test_an_empty_music_query_is_refused(monkeypatch):
    _music(monkeypatch, TRACKS)

    with pytest.raises(ValueError, match="No search query"):
        mac.search_music("  ")


def test_playing_by_id_is_exact(monkeypatch):
    calls = _music(monkeypatch, TRACKS)

    result = mac.play_music(track_id="BBB2")

    assert "Blue Monday (remix)" in result
    assert calls[-1][2] == mac._PLAY_TRACK
    assert calls[-1][3] == "BBB2"


def test_an_id_beats_a_query(monkeypatch):
    """search_music hands back an id precisely so this can't re-guess."""
    calls = _music(monkeypatch, TRACKS)

    mac.play_music(query="Bang!", track_id="AAA1")

    assert calls[-1][3] == "AAA1"


def test_playing_by_query_takes_the_best_match_and_says_what_else_matched(monkeypatch):
    _music(monkeypatch, TRACKS)

    result = mac.play_music(query="blue monday")

    assert "Playing: Blue Monday — New Order" in result
    assert "1 other match" in result, "the user needs to know it picked"


def test_a_single_match_has_no_noise_about_alternatives(monkeypatch):
    _music(monkeypatch, TRACKS)

    result = mac.play_music(query="Bang!")

    assert "other match" not in result


def test_a_playlist_can_be_played_by_name(monkeypatch):
    calls = _music(monkeypatch, TRACKS, playlists=["TikTok Songs", "90's Music"])

    result = mac.play_music(query="tiktok songs", kind="playlist")

    assert "Playing playlist: TikTok Songs" in result
    assert calls[-1][2] == mac._PLAY_PLAYLIST


def test_a_query_matching_no_track_falls_back_to_a_playlist(monkeypatch):
    """'play my 90s music' is a playlist, not a song, and the user shouldn't
    have to know which kind of thing it is."""
    _music(monkeypatch, TRACKS, playlists=["90's Music"])

    result = mac.play_music(query="90's Music")

    assert "Playing playlist: 90's Music" in result


def test_an_unknown_playlist_is_refused_rather_than_guessed(monkeypatch):
    _music(monkeypatch, TRACKS, playlists=["TikTok Songs"])

    with pytest.raises(ValueError, match="No playlist called"):
        mac.play_music(query="Jazz Classics", kind="playlist")


def test_nothing_matching_says_so_rather_than_playing_something_random(monkeypatch):
    _music(monkeypatch, TRACKS, playlists=[])

    result = mac.play_music(query="obscure thing nobody has")

    assert "Nothing in the library" in result


def test_playing_needs_something_to_play(monkeypatch):
    _music(monkeypatch, TRACKS)

    with pytest.raises(ValueError, match="Nothing to play"):
        mac.play_music()


def test_shuffle_is_set_before_playing(monkeypatch):
    calls = _music(monkeypatch, TRACKS)

    mac.play_music(query="Bang!", shuffle=True)

    scripts = [c[2] for c in calls]
    assert mac._SET_SHUFFLE in scripts
    assert scripts.index(mac._SET_SHUFFLE) < scripts.index(mac._PLAY_TRACK)


def test_music_queries_travel_as_arguments_not_script_source(monkeypatch):
    """A track name — or a user phrase — must never become AppleScript."""
    calls = _music(monkeypatch, TRACKS)
    payload = 'x" & (do shell script "rm -rf ~") & "'

    mac.search_music(payload)

    assert calls[-1][2] == mac._SEARCH_MUSIC, "the script is a fixed constant"
    assert payload in calls[-1], "the query is its own argv item"
    assert "rm -rf" not in calls[-1][2]


def test_an_invalid_music_kind_is_refused(monkeypatch):
    _music(monkeypatch, TRACKS)

    with pytest.raises(ValueError, match="isn't a music search kind"):
        mac.search_music("anything", kind="lyrics")


def test_the_music_tools_are_registered_and_ungated():
    from ada.tools import specs

    names = {s["name"] for s in specs()}
    assert {"search_music", "play_music"} <= names
    assert not safety.requires_confirmation("play_music"), "wrong song is trivially reversible"


def test_no_applescript_constant_assigns_to_a_reserved_word():
    """A real bug, caught only by running against the real library: `kind` is
    an AppleScript reserved word (every object has a `kind` property), so
    `set kind to ...` failed with -10006 while every faked test passed.

    The fakes can't execute AppleScript, so this is a static guard on the
    class of mistake rather than on that one instance.
    """
    reserved = [
        "kind", "name", "id", "class", "count", "text", "date", "time", "length",
        "result", "item", "items", "index", "version", "size", "color", "state",
        "container", "properties", "character", "word", "paragraph",
    ]
    scripts = {
        "_SEARCH_MUSIC": mac._SEARCH_MUSIC,
        "_PLAY_TRACK": mac._PLAY_TRACK,
        "_PLAY_PLAYLIST": mac._PLAY_PLAYLIST,
        "_SET_SHUFFLE": mac._SET_SHUFFLE,
        "_SET_VOLUME": mac._SET_VOLUME,
        "_SET_MUTED": mac._SET_MUTED,
        **{f"_MEDIA_SCRIPTS[{k}]": v for k, v in mac._MEDIA_SCRIPTS.items()},
    }

    for script_name, script in scripts.items():
        for word in reserved:
            assert f"set {word} to " not in script, (
                f"{script_name} assigns to AppleScript's reserved word '{word}' — "
                "this fails at runtime and no faked test will catch it"
            )
