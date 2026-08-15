"""Sending an iMessage — almost entirely a test of the things that stop it.

No test here sends anything: `subprocess.run` is replaced in every case, so
osascript is never invoked. What's exercised is the validation, the gate, the
rate limit, and the guarantee that message text can never become AppleScript.
"""

import subprocess

import pytest

from ada import agent, limits, safety, settings, store
from ada.tools import imessage
from tests.conftest import FakeBlock, FakeMessage, fake_stream_turn


@pytest.fixture
def sent(monkeypatch):
    """Capture what would have been handed to osascript."""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(imessage.subprocess, "run", fake_run)
    return calls


# --- the recipient has to be a real handle ----------------------------------


@pytest.mark.parametrize("handle", ["+14155550123", "415-555-0123", "(415) 555 0123", "sam@example.com"])
def test_real_handles_are_accepted(handle, sent):
    imessage.send_message(handle, "hello")

    assert len(sent) == 1


@pytest.mark.parametrize("handle", ["Mom", "my landlord", "the group chat", "sam", ""])
def test_a_name_is_refused_rather_than_guessed_at(handle, sent):
    """No Contacts access means resolving a name is a guess, and guessing
    wrong here texts a stranger."""
    with pytest.raises(ValueError):
        imessage.send_message(handle, "hello")

    assert sent == [], "nothing may reach Messages when the recipient is unclear"


def test_the_refusal_tells_the_model_what_to_do_instead(sent):
    with pytest.raises(ValueError, match="read_recent_messages"):
        imessage.send_message("Mom", "hi")


@pytest.mark.parametrize("handle", ["+1415555012, +1415555013", "a@b.com; c@d.com", "a@b.com\nc@d.com"])
def test_multiple_recipients_are_refused(handle, sent):
    """A loop that texts several people is the failure this must not make easy."""
    with pytest.raises(ValueError, match="one person at a time"):
        imessage.send_message(handle, "hello")

    assert sent == []


# --- the message itself -----------------------------------------------------


def test_an_empty_message_is_refused(sent):
    with pytest.raises(ValueError, match="empty message"):
        imessage.send_message("+14155550123", "   ")

    assert sent == []


def test_an_absurdly_long_message_is_refused(sent):
    with pytest.raises(ValueError, match="limit is"):
        imessage.send_message("+14155550123", "x" * (imessage.MAX_LENGTH + 1))

    assert sent == []


def test_the_text_travels_as_an_argument_never_as_script_source(sent):
    """Ada can read incoming messages, so text written by someone else can
    reach this tool. It must be data, not code."""
    payload = 'hi" & (do shell script "rm -rf ~") & "'
    imessage.send_message("+14155550123", payload)

    argv = sent[0]
    assert argv[0] == "osascript"
    assert payload in argv, "the text is passed as its own argv item"
    assert "rm -rf" not in argv[2], "the payload must never appear in the script body"
    assert argv[2] == imessage._SEND_SCRIPT, "the script is a fixed constant"


def test_the_recipient_also_travels_as_an_argument(sent):
    imessage.send_message("+14155550123", "hello")

    assert sent[0][3] == "+14155550123"


def test_a_successful_send_reports_what_went_where(sent):
    result = imessage.send_message("+14155550123", "on my way")

    assert "+14155550123" in result
    assert "on my way" in result


# --- failures are explained, not swallowed ----------------------------------


def test_a_permission_failure_names_the_settings_pane(monkeypatch):
    monkeypatch.setattr(
        imessage.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "Not authorized to send Apple events"),
    )

    with pytest.raises(ValueError, match="Privacy & Security > Automation"):
        imessage.send_message("+14155550123", "hi")


def test_an_unknown_recipient_failure_is_explained(monkeypatch):
    monkeypatch.setattr(
        imessage.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "Messages got an error (-1728)"),
    )

    with pytest.raises(ValueError, match="reachable on iMessage"):
        imessage.send_message("+14155550123", "hi")


def test_a_timeout_admits_it_does_not_know_whether_it_sent(monkeypatch):
    """The one honest answer here is 'I can't tell' — claiming either outcome
    would be a guess about something that already happened or didn't."""

    def timeout(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 30)

    monkeypatch.setattr(imessage.subprocess, "run", timeout)

    with pytest.raises(ValueError, match="may or may not have been sent"):
        imessage.send_message("+14155550123", "hi")


# --- the gate ---------------------------------------------------------------


def test_sending_is_gated_even_with_no_config_file():
    """A missing or broken config must never be what ungates a send."""
    assert safety.requires_confirmation("send_message")
    assert settings.DEFAULTS["autonomy"]["tools"]["send_message"] == settings.CONFIRM


def test_the_confirmation_shows_the_recipient_and_the_full_text():
    """The generic key=value dump isn't good enough when the whole decision is
    who this reaches and what it says."""
    description = agent._describe_action("send_message", {"to": "+14155550123", "text": "I'll be late"})

    assert "SEND A MESSAGE" in description
    assert "cannot be unsent" in description
    assert "+14155550123" in description
    assert "I'll be late" in description


def _send_turn(monkeypatch, confirm, on_status=lambda _s: None):
    monkeypatch.setattr(
        agent,
        "stream_turn",
        fake_stream_turn(
            FakeMessage(
                [FakeBlock("tool_use", id="t1", name="send_message", input={"to": "+14155550123", "text": "hi"})],
                "tool_use",
            ),
            FakeMessage([FakeBlock("text", text="ok")], "end_turn"),
        ),
    )
    agent.run_turn(
        [{"role": "user", "content": "text them"}],
        on_text=lambda _c: None,
        on_status=on_status,
        confirm=confirm,
    )


def test_a_declined_send_never_reaches_messages(monkeypatch, sent):
    _send_turn(monkeypatch, confirm=lambda _d: False)

    assert sent == [], "declining must mean nothing left the machine"


def test_an_approved_send_goes_through(monkeypatch, sent):
    _send_turn(monkeypatch, confirm=lambda _d: True)

    assert len(sent) == 1


# --- the ceiling ------------------------------------------------------------


def test_sending_has_a_rate_limit_by_default():
    assert settings.get("autonomy.limits.send_message.per_hour")


def test_the_limit_stops_a_runaway_loop_before_it_asks_again(monkeypatch, sent):
    """Being over the ceiling should stop the prompt appearing at all, not
    hand the user one more thing to click through."""
    store.write_json(settings._CONFIG_FILE, {"autonomy": {"limits": {"send_message": {"per_minute": 2}}}})
    settings._cache["stamp"] = None

    prompts = []
    forever = FakeMessage(
        [FakeBlock("tool_use", id="t1", name="send_message", input={"to": "+14155550123", "text": "hi"})],
        "tool_use",
    )
    monkeypatch.setattr(agent, "stream_turn", fake_stream_turn(forever))

    agent.run_turn(
        [{"role": "user", "content": "spam them"}],
        on_text=lambda _c: None,
        on_status=lambda _s: None,
        confirm=lambda d: prompts.append(d) or True,
    )

    assert len(sent) == 2, "the ceiling is enforced no matter how many rounds the model runs"
    assert len(prompts) == 2, "no confirmation is shown once the ceiling is hit"


def test_a_declined_send_does_not_count_against_the_ceiling(monkeypatch, sent):
    store.write_json(settings._CONFIG_FILE, {"autonomy": {"limits": {"send_message": {"per_minute": 2}}}})
    settings._cache["stamp"] = None

    _send_turn(monkeypatch, confirm=lambda _d: False)

    assert limits.remaining("send_message")["per_minute"] == 2, "a refusal costs nothing"
