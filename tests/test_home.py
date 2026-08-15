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
