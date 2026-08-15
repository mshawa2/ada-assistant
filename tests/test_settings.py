"""The settings layer: layering, hot reload, failing safe, and the autonomy
levels that decide what Ada may do without asking."""

import json

import pytest

from ada import agent, safety, settings, store
from ada.tools import settings_tool
from tests.conftest import FakeBlock, FakeMessage, fake_stream_turn


def _write(config: dict) -> None:
    store.write_json(settings._CONFIG_FILE, config)
    settings._cache["stamp"] = None


# --- layering ---------------------------------------------------------------


def test_defaults_apply_when_there_is_no_file():
    assert settings.get("identity.assistant_name") == "Ada"
    assert settings.get("model.max_tokens") == 4096


def test_the_file_only_needs_what_you_want_to_change():
    """A partial file must not wipe out every setting it doesn't mention."""
    _write({"identity": {"user_name": "Mansur"}})

    assert settings.get("identity.user_name") == "Mansur"
    assert settings.get("identity.assistant_name") == "Ada", "unmentioned defaults survive"
    assert settings.get("model.conversation") == "claude-sonnet-5"


def test_lists_replace_rather_than_append():
    """'channels are just ntfy' has to be expressible."""
    _write({"notifications": {"channels": ["ntfy"]}})

    assert settings.get("notifications.channels") == ["ntfy"]


def test_saving_does_not_bake_every_default_into_the_users_file():
    """Otherwise the file freezes against defaults that improve later."""
    settings.set_path("identity.user_name", "Mansur")

    assert settings.load_raw() == {"identity": {"user_name": "Mansur"}}


def test_a_corrupt_settings_file_falls_back_to_defaults(capsys):
    settings._CONFIG_FILE.write_text("{not json at all")
    settings._cache["stamp"] = None

    assert settings.get("identity.assistant_name") == "Ada"
    assert "unreadable" in capsys.readouterr().out


# --- hot reload -------------------------------------------------------------


def test_editing_the_file_takes_effect_without_a_restart():
    """The whole point — the old heartbeat config was read once at start()."""
    _write({"heartbeat": {"tick_seconds": 30}})
    assert settings.get("heartbeat.tick_seconds") == 30

    _write({"heartbeat": {"tick_seconds": 5}})

    assert settings.get("heartbeat.tick_seconds") == 5


def test_repeated_reads_are_cached_between_edits():
    _write({"identity": {"user_name": "Mansur"}})
    first = settings.load()

    assert settings.load() is first, "unchanged file should not be re-parsed"


# --- modes ------------------------------------------------------------------


def test_a_mode_patches_settings_without_duplicating_them():
    _write(
        {
            "notifications": {"channels": ["terminal", "macos"], "push_levels": ["urgent", "critical"]},
            "modes": {"active": "focus", "presets": {"focus": {"notifications": {"push_levels": ["critical"]}}}},
        }
    )

    assert settings.get("notifications.push_levels") == ["critical"], "the mode's patch wins"
    assert settings.get("notifications.channels") == ["terminal", "macos"], "the rest is untouched"


def test_switching_back_to_default_restores_the_base_settings():
    _write(
        {
            "notifications": {"push_levels": ["urgent", "critical"]},
            "modes": {"active": "focus", "presets": {"focus": {"notifications": {"push_levels": ["critical"]}}}},
        }
    )
    assert settings.get("notifications.push_levels") == ["critical"]

    settings.set_path("modes.active", "default")

    assert settings.get("notifications.push_levels") == ["urgent", "critical"]


def test_an_unknown_active_mode_is_ignored_rather_than_fatal():
    _write({"modes": {"active": "nonsense", "presets": {}}})

    assert settings.get("identity.assistant_name") == "Ada"


# --- autonomy ---------------------------------------------------------------


def test_autonomy_defaults_to_auto_for_unlisted_tools():
    assert settings.autonomy_for("list_reminders") == settings.AUTO


def test_the_irreversible_tools_default_to_confirm():
    assert settings.autonomy_for("forget") == settings.CONFIRM
    assert settings.autonomy_for("delete_note") == settings.CONFIRM


def test_a_denied_tool_reports_as_denied():
    _write({"autonomy": {"tools": {"fetch_url": "deny"}}})

    assert settings.autonomy_for("fetch_url") == settings.DENY
    assert safety.is_denied("fetch_url") is True
    assert safety.requires_confirmation("fetch_url") is False


def test_an_invalid_autonomy_level_fails_towards_asking(capsys):
    """A typo in the file that decides what runs unattended must never widen
    permissions — it has to land on 'confirm', not 'auto'."""
    _write({"autonomy": {"tools": {"delete_note": "atuo"}}})

    assert settings.autonomy_for("delete_note") == settings.CONFIRM
    assert "isn't a valid autonomy level" in capsys.readouterr().out


def test_the_default_level_can_lock_everything_down():
    _write({"autonomy": {"default": "confirm", "tools": {"list_reminders": "auto"}}})

    assert settings.autonomy_for("add_reminder") == settings.CONFIRM
    assert settings.autonomy_for("list_reminders") == settings.AUTO


# --- the deny path through the agent ----------------------------------------


def test_a_denied_tool_never_runs_and_the_model_is_told(monkeypatch):
    from ada.tools import reminders

    _write({"autonomy": {"tools": {"add_reminder": "deny"}}})
    monkeypatch.setattr(
        agent,
        "stream_turn",
        fake_stream_turn(
            FakeMessage([FakeBlock("tool_use", id="t1", name="add_reminder", input={"text": "x"})], "tool_use"),
            FakeMessage([FakeBlock("text", text="I can't, that's switched off.")], "end_turn"),
        ),
    )
    captured = {"text": [], "status": []}
    messages = [{"role": "user", "content": "remind me"}]

    agent.run_turn(
        messages,
        on_text=captured["text"].append,
        on_status=captured["status"].append,
        confirm=lambda _d: pytest.fail("a denied tool must never reach the confirmation prompt"),
    )

    assert reminders.load_reminders() == []
    denied = [
        b
        for m in messages
        if isinstance(m["content"], list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    assert denied and denied[0]["is_error"] is True
    assert "switched off" in denied[0]["content"]
    assert any("switched off" in s for s in captured["status"])


# --- locale -----------------------------------------------------------------


def test_timezone_is_the_machines_own_unless_configured():
    assert settings.timezone() is None


def test_a_configured_timezone_is_used():
    _write({"locale": {"timezone": "Asia/Tokyo"}})

    assert settings.now().strftime("%Z") in ("JST", "+09")


def test_an_unknown_timezone_warns_and_falls_back(capsys):
    _write({"locale": {"timezone": "Middle/Earth"}})

    assert settings.timezone() is None
    assert "unknown timezone" in capsys.readouterr().out


def test_clock_format_follows_the_setting():
    assert settings.time_format() == "%H:%M"

    _write({"locale": {"time_format": "12h"}})

    assert settings.time_format() == "%I:%M %p"


# --- migration --------------------------------------------------------------


def test_the_old_config_files_are_folded_in_on_first_run(capsys):
    store.write_json(
        settings._LEGACY_HEARTBEAT,
        {
            "tick_seconds": 15,
            "quiet_hours": {"start": "23:00", "end": "07:00"},
            "checks": {"new_notes": {"enabled": False}},
        },
    )
    store.write_json(settings._LEGACY_SAFETY, {"confirmation_required_tools": ["forget", "save_draft"]})

    assert settings.get("heartbeat.tick_seconds") == 15, "hand-tuned values must survive the upgrade"
    assert settings.get("quiet_hours.start") == "23:00"
    assert settings.autonomy_for("save_draft") == settings.CONFIRM
    assert settings._CONFIG_FILE.exists()
    assert "settings moved into" in capsys.readouterr().out


def test_migration_never_clobbers_an_existing_settings_file():
    _write({"heartbeat": {"tick_seconds": 99}})
    store.write_json(settings._LEGACY_HEARTBEAT, {"tick_seconds": 15})

    assert settings.get("heartbeat.tick_seconds") == 99


# --- the tools --------------------------------------------------------------


def test_get_settings_returns_the_effective_config():
    parsed = json.loads(settings_tool.get_settings())

    assert parsed["identity"]["assistant_name"] == "Ada"


def test_get_settings_can_show_one_section():
    parsed = json.loads(settings_tool.get_settings(section="locale"))

    assert set(parsed) == {"locale"}


def test_get_settings_rejects_an_unknown_section():
    with pytest.raises(ValueError, match="No settings section"):
        settings_tool.get_settings(section="nope")


def test_set_setting_writes_a_typed_value():
    settings_tool.set_setting("heartbeat.tick_seconds", "5")

    assert settings.get("heartbeat.tick_seconds") == 5


def test_set_setting_accepts_a_bare_word_as_a_string():
    """Models routinely forget the JSON quotes; the meaning is unambiguous."""
    settings_tool.set_setting("locale.timezone", "Europe/London")

    assert settings.get("locale.timezone") == "Europe/London"


def test_set_setting_flags_a_path_that_looks_like_a_typo():
    result = settings_tool.set_setting("locale.timezoen", '"Europe/London"')

    assert "worth checking it isn't a typo" in result


def test_set_setting_refuses_to_change_the_autonomy_rules():
    """The model must not be able to edit the rules that govern the model."""
    with pytest.raises(ValueError, match="can't be changed from a conversation"):
        settings_tool.set_setting("autonomy.tools.delete_note", '"auto"')

    assert settings.autonomy_for("delete_note") == settings.CONFIRM


def test_set_mode_switches_and_reports_what_changed():
    _write({"modes": {"active": "default", "presets": {"focus": {"notifications": {"push_levels": ["critical"]}}}}})

    result = settings_tool.set_mode("focus")

    assert settings.get("modes.active") == "focus"
    assert "notifications" in result


def test_set_mode_rejects_an_unknown_mode():
    with pytest.raises(ValueError, match="No mode called"):
        settings_tool.set_mode("hyperfocus")


def test_list_modes_marks_the_active_one():
    _write({"modes": {"active": "focus", "presets": {"focus": {"notifications": {}}}}})

    listed = settings_tool.list_modes()

    assert "focus (active)" in listed
