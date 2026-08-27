"""Notifications: who gets interrupted, who gets held, and who never pushes.
Plus the single-instance guard that makes a background daemon safe."""

import os
import subprocess
import sys
import textwrap

import pytest

from ada import notify
from ada.heartbeat import notices, singleton
from tests.conftest import TEST_DATA_DIR

ALWAYS_QUIET = {"start": "00:00", "end": "23:59"}
NEVER_QUIET = {"start": "22:00", "end": "22:00"}


def _config(quiet=False, push_levels=("urgent", "critical"), channels=("terminal",), **extra):
    return {
        "quiet_hours": ALWAYS_QUIET if quiet else NEVER_QUIET,
        "notifications": {"channels": list(channels), "push_levels": list(push_levels), **extra},
    }


def _notice(level="urgent", text="something happened"):
    return {"id": 1, "check": "t", "text": text, "level": level, "dismissed": False}


def _subprocess_acquire() -> subprocess.CompletedProcess:
    """Try to take the heartbeat lock from a genuinely separate process."""
    script = textwrap.dedent(
        f"""
        import os, sys
        os.environ["ADA_DATA_DIR"] = {str(TEST_DATA_DIR)!r}
        os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-never-used")
        sys.path.insert(0, {os.getcwd()!r})
        from ada.heartbeat import singleton
        print("acquired" if singleton.acquire() else "denied")
        """
    )
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)


# --- who gets pushed --------------------------------------------------------


def test_normal_notices_never_push():
    """They're log-only by design — you see them when you ask."""
    assert notify.push_disposition(_notice(level="normal")) == notify.NEVER


def test_urgent_notices_push():
    assert notify.push_disposition(_notice(level="urgent")) == notify.SEND


def test_urgent_is_held_during_quiet_hours(monkeypatch):
    monkeypatch.setattr(notify, "load_config", lambda: _config(quiet=True))

    assert notify.push_disposition(_notice(level="urgent")) == notify.HOLD


def test_critical_pushes_through_quiet_hours(monkeypatch):
    monkeypatch.setattr(notify, "load_config", lambda: _config(quiet=True))

    assert notify.push_disposition(_notice(level="critical")) == notify.SEND


def test_push_levels_are_config_not_code(monkeypatch):
    monkeypatch.setattr(notify, "load_config", lambda: _config(push_levels=["normal", "urgent", "critical"]))

    assert notify.push_disposition(_notice(level="normal")) == notify.SEND


# --- delivery ---------------------------------------------------------------


def test_delivering_reaches_the_configured_channel(sent_notifications):
    notify.deliver(_notice(text="the kettle boiled"))

    assert sent_notifications == [{"text": "the kettle boiled", "urgent": False}]


def test_critical_notices_are_marked_urgent_to_the_channel(sent_notifications):
    notify.deliver(_notice(level="critical"))

    assert sent_notifications[0]["urgent"] is True


def test_a_broken_channel_does_not_stop_the_others(monkeypatch, capsys):
    reached = []

    def broken(*_args):
        raise RuntimeError("phone is off")

    def working(text, _settings, _urgent):
        reached.append(text)
        return True

    monkeypatch.setattr(notify, "_CHANNELS", {"ntfy": broken, "terminal": working})
    monkeypatch.setattr(notify, "load_config", lambda: _config(channels=["ntfy", "terminal"]))

    assert notify.deliver(_notice()) is True, "one live channel still counts as delivered"
    assert reached == ["something happened"]
    assert "phone is off" in capsys.readouterr().out


def test_an_unknown_channel_is_reported_not_crashed_on(monkeypatch, capsys):
    monkeypatch.setattr(notify, "load_config", lambda: _config(channels=["carrier-pigeon"]))

    assert notify.deliver(_notice()) is False
    assert "unknown notification channel" in capsys.readouterr().out


def test_an_unconfigured_phone_push_says_so_rather_than_going_quiet(monkeypatch):
    """It used to return False, which `deliver` couldn't tell apart from "this
    channel had nothing to do" — so a phone push that had never been configured
    looked exactly like one that worked.

    The delenv is load-bearing, not tidiness: `ada.config` calls `load_dotenv()`
    at import, so a real ADA_NTFY_TOPIC from the developer's own .env is live in
    the test process. Without this the test posts a genuine push to their phone
    every time the suite runs.
    """
    monkeypatch.delenv("ADA_NTFY_TOPIC", raising=False)

    with pytest.raises(notify.ChannelUnavailable) as raised:
        notify._send_ntfy("hi", {"ntfy_topic": "  "}, False)

    assert "ADA_NTFY_TOPIC" in str(raised.value), "the message has to name the fix"


def test_macos_channel_passes_text_as_an_argument_not_as_script_source(monkeypatch):
    """A notice built from a fetched web page must not be able to inject
    AppleScript, and a quote in a reminder must not break the banner."""
    captured = {}
    monkeypatch.setattr(notify.shutil, "which", lambda _name: "/usr/bin/osascript")
    monkeypatch.setattr(
        notify.subprocess,
        "run",
        lambda argv, **kw: captured.update(argv=argv) or subprocess.CompletedProcess(argv, 0, "", ""),
    )

    payload = 'say "hello" & do shell script "rm -rf /"'
    notify._send_macos(payload, {}, False)

    assert captured["argv"][0] == "osascript"
    assert payload in captured["argv"], "the text travels as argv, where it can't be executed"
    assert "rm -rf" not in captured["argv"][2], "the payload must never land in the script body"


# --- held, not dropped ------------------------------------------------------


def test_a_notice_held_by_quiet_hours_goes_out_when_they_end(monkeypatch, sent_notifications):
    monkeypatch.setattr(notify, "load_config", lambda: _config(quiet=True))
    notices.add_notice("t", "the dishwasher finished", level="urgent")

    assert sent_notifications == [], "nothing should interrupt during quiet hours"
    assert notices.flush_pending_pushes() == 0, "still quiet — still held"

    monkeypatch.setattr(notify, "load_config", lambda: _config(quiet=False))

    assert notices.flush_pending_pushes() == 1
    assert sent_notifications[0]["text"] == "the dishwasher finished"


def test_a_delivered_notice_is_not_pushed_twice(sent_notifications):
    notices.add_notice("t", "only once please", level="urgent")

    notices.flush_pending_pushes()
    notices.flush_pending_pushes()

    assert len(sent_notifications) == 1


def test_normal_notices_are_settled_rather_than_reconsidered_forever(sent_notifications):
    notices.add_notice("t", "a calm detail", level="normal")

    assert notices.flush_pending_pushes() == 0
    assert notices.list_notices()[0]["pushed"] is True
    assert sent_notifications == []


def test_a_dismissed_notice_is_never_pushed_later(monkeypatch, sent_notifications):
    monkeypatch.setattr(notify, "load_config", lambda: _config(quiet=True))
    notice = notices.add_notice("t", "held then dismissed", level="urgent")
    notices.dismiss_notice(notice["id"])

    monkeypatch.setattr(notify, "load_config", lambda: _config(quiet=False))

    assert notices.flush_pending_pushes() == 0
    assert sent_notifications == []


# --- one heartbeat at a time ------------------------------------------------


def test_acquiring_the_heartbeat_lock_is_idempotent_within_a_process():
    try:
        assert singleton.acquire() is True
        assert singleton.acquire() is True, "a process must not contend with itself"
    finally:
        singleton.release()


def test_the_lock_records_the_owning_pid():
    try:
        singleton.acquire()
        assert singleton.owner_pid() == os.getpid()
    finally:
        singleton.release()


def test_a_second_process_cannot_take_the_heartbeat():
    """The whole point: a launchd daemon and a REPL must not both run checks.
    Only a real second process proves this — an in-process check can't."""
    try:
        singleton.acquire()
        result = _subprocess_acquire()
        assert result.stdout.strip() == "denied", result.stderr
    finally:
        singleton.release()


def test_the_lock_is_free_again_after_release():
    singleton.acquire()
    singleton.release()

    result = _subprocess_acquire()

    assert result.stdout.strip() == "acquired", result.stderr


# --- keeping the topic out of the repo --------------------------------------
#
# An ntfy topic is a public URL: anyone who knows it can read your notices and
# post to them. `ada_config.json` is tracked in git, so the topic is read from
# the environment first and the config is only a fallback.


def test_the_topic_is_read_from_the_environment_first(monkeypatch):
    """ada_config.json is tracked; .env is not. A topic committed by accident
    is a password committed by accident."""
    posted = {}

    def fake_urlopen(request, timeout=None):
        posted["url"] = request.full_url
        raise AssertionError("stop here — the URL is what's under test")

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("ADA_NTFY_TOPIC", "from-env")

    with pytest.raises(AssertionError):
        notify._send_ntfy("x", {"ntfy_topic": "from-tracked-config"}, False)

    assert "from-env" in posted["url"]
    assert "from-tracked-config" not in posted["url"]


def test_the_config_is_only_a_fallback(monkeypatch):
    posted = {}

    def fake_urlopen(request, timeout=None):
        posted["url"] = request.full_url
        raise AssertionError("stop here")

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.delenv("ADA_NTFY_TOPIC", raising=False)

    with pytest.raises(AssertionError):
        notify._send_ntfy("x", {"ntfy_topic": "from-config"}, False)

    assert "from-config" in posted["url"]


def test_a_blank_topic_sends_nothing_rather_than_posting_to_a_bare_host(monkeypatch):
    """Posting to https://ntfy.sh/ with no topic is a request to a stranger."""
    monkeypatch.setenv("ADA_NTFY_TOPIC", "   ")

    with pytest.raises(notify.ChannelUnavailable):
        notify._send_ntfy("x", {}, False)


# --- knowing before you needed it -------------------------------------------


def test_an_inert_channel_is_reported_and_does_not_stop_the_others(monkeypatch, capsys):
    monkeypatch.delenv("ADA_NTFY_TOPIC", raising=False)
    monkeypatch.setattr(notify, "_CHANNELS", {"ntfy": notify._send_ntfy, "terminal": notify._send_terminal})
    monkeypatch.setattr(notify, "load_config", lambda: _config(channels=["ntfy", "terminal"]))

    assert notify.deliver(_notice()) is True, "the terminal still took it"

    out = capsys.readouterr().out
    assert "can't send" in out
    assert "ADA_NTFY_TOPIC" in out


def test_an_inert_channel_reads_differently_from_a_broken_one(monkeypatch, capsys):
    """Nothing is wrong with an unconfigured channel — it was never set up, and
    the fix is a line of config rather than an investigation."""
    def broken(_text, _settings, _urgent):
        raise RuntimeError("the phone is off")

    monkeypatch.setattr(notify, "_CHANNELS", {"ntfy": broken})
    monkeypatch.setattr(notify, "load_config", lambda: _config(channels=["ntfy"]))
    notify.deliver(_notice())

    assert "couldn't notify via ntfy" in capsys.readouterr().out


def test_unavailable_channels_answers_the_question_without_sending(monkeypatch):
    """'Is my alerting working?' should be answerable before the alert you
    needed, not after it didn't arrive."""
    monkeypatch.delenv("ADA_NTFY_TOPIC", raising=False)
    monkeypatch.setattr(notify, "load_config", lambda: _config(channels=["terminal", "ntfy"]))

    broken = dict(notify.unavailable_channels())

    assert "terminal" not in broken
    assert "ADA_NTFY_TOPIC" in broken["ntfy"]


def test_a_configured_channel_is_not_reported_as_broken(monkeypatch):
    monkeypatch.setenv("ADA_NTFY_TOPIC", "a-real-topic")
    monkeypatch.setattr(notify, "load_config", lambda: _config(channels=["terminal", "ntfy"]))

    assert notify.unavailable_channels() == []


def test_a_channel_name_that_does_not_exist_is_reported_too(monkeypatch):
    monkeypatch.setattr(notify, "load_config", lambda: _config(channels=["carrier-pigeon"]))

    assert notify.unavailable_channels() == [("carrier-pigeon", "not a channel Ada knows about")]


def test_a_critical_notice_is_marked_urgent_to_the_phone(monkeypatch):
    """Priority is what decides whether a phone breaks through Do Not Disturb,
    which is the whole point of the critical level."""
    headers = {}

    def fake_urlopen(request, timeout=None):
        headers.update(request.headers)
        raise AssertionError("stop here")

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("ADA_NTFY_TOPIC", "t")

    with pytest.raises(AssertionError):
        notify._send_ntfy("stop hit", {}, urgent=True)
    assert headers.get("Priority") == "urgent"

    with pytest.raises(AssertionError):
        notify._send_ntfy("fyi", {}, urgent=False)
    assert headers.get("Priority") == "default"
