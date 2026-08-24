"""Commands, typed or spoken.

The matching rules carry most of the weight here. In a room where Ada is
listening on an open mic, the cost of a false positive — wiping a conversation
because someone said "let's start over on the kitchen" — is much higher than
the cost of a miss, which just goes to the model and gets answered properly.
"""

import pytest

from ada import approvals, commands, jobs, session
from ada.confirm import Action
from ada.heartbeat.kill_switch import is_paused, set_paused


# --- matching ---------------------------------------------------------------


def test_a_plain_command_is_recognised():
    assert commands.handle("reset").reset is True
    assert commands.handle("exit").quit is True


def test_speech_arrives_punctuated_and_capitalised():
    """"Reset." and "reset" are the same instruction. A command layer that can
    only see the second one works when typed and not when spoken."""
    for spoken in ("Reset.", "Reset!", "  reset  ", "RESET?"):
        assert commands.handle(spoken) is not None, spoken


def test_the_phrasings_people_actually_use_are_listed():
    """Someone says "start over", not "reset"."""
    for spoken in ("start over", "start fresh", "new conversation"):
        assert commands.handle(spoken).reset is True

    for spoken in ("goodbye", "bye", "good night", "that's all"):
        assert commands.handle(spoken).quit is True


def test_a_command_inside_a_sentence_is_not_a_command():
    """The rule the whole module rests on. "Let's reset the schedule for
    Monday" must not wipe the conversation."""
    for sentence in (
        "let's reset the schedule for monday",
        "can you help me with the boiler",
        "say goodbye to sam for me",
        "what's the status of my flight",
        "pause the music",
    ):
        assert commands.handle(sentence) is None, sentence


def test_anything_else_is_just_a_conversation():
    assert commands.handle("what's the weather") is None
    assert commands.handle("") is None
    assert commands.handle(None) is None


def test_normalising_keeps_the_words_and_drops_the_rest():
    assert commands.normalize("  Start   Over!  ") == "start over"
    assert commands.normalize("Bye.") == "bye"


# --- what they do -----------------------------------------------------------


def test_reset_clears_the_stored_transcript_but_not_durable_memory():
    session.record([{"role": "user", "content": "hello"}])
    assert session.load()

    result = commands.handle("reset")

    assert session.load() == []
    assert result.reset is True, "the caller still has to clear its own live list"
    assert "untouched" in result.text, "it should say what it did *not* clear"


def test_exit_asks_the_caller_to_leave_rather_than_leaving():
    """This module can clear what's on disk; only the front end can leave the
    loop it's sitting in."""
    result = commands.handle("quit")

    assert result.quit is True
    assert result.text


def test_pause_and_resume_drive_the_real_kill_switch():
    try:
        commands.handle("be quiet")
        assert is_paused() is True

        commands.handle("carry on")
        assert is_paused() is False
    finally:
        set_paused(False)


def test_status_reports_ada_s_own_state_without_spending_anything():
    jobs.create("something queued")
    parked = jobs.create("something blocked")
    jobs.claim()
    jobs.claim()
    approvals.park(parked["id"], Action(tool="send_message", input={}, description="Text Sam"))

    text = commands.handle("status").text

    assert "2 jobs open" in text
    assert "1 job waiting on your approval" in text
    assert "spent today" in text


def test_status_says_so_when_there_is_nothing_on():
    text = commands.handle("status").text

    assert "Nothing queued" in text
    assert "running" in text


def test_help_lists_every_command_by_its_plainest_name():
    text = commands.handle("help").text

    for command in commands.COMMANDS:
        assert command.phrases[0] in text
        assert command.summary in text


# --- the contract the front ends rely on ------------------------------------


def test_every_command_returns_a_result_with_something_to_say():
    """All three front ends print it, and two of them speak it aloud."""
    try:
        for command in commands.COMMANDS:
            result = command.run()
            assert isinstance(result, commands.Result)
            assert result.text.strip(), command.name
    finally:
        set_paused(False)


def test_no_phrase_is_claimed_by_two_commands():
    seen = set()
    for command in commands.COMMANDS:
        for phrase in command.phrases:
            assert phrase not in seen, f"{phrase} is claimed twice"
            seen.add(phrase)


def test_every_phrase_survives_its_own_normaliser():
    """A phrase that normalises to something other than itself can never be
    matched, because lookup happens on the normalised form."""
    for command in commands.COMMANDS:
        for phrase in command.phrases:
            assert commands.normalize(phrase) == phrase, f"{command.name}: {phrase!r}"


def test_commands_do_not_shadow_tools_the_model_needs():
    """A command that could have been a tool is a capability the model can no
    longer reason about or combine."""
    from ada.tools import specs

    tool_names = {spec["name"] for spec in specs()}
    for command in commands.COMMANDS:
        assert command.name not in tool_names, f"{command.name} exists as both"


# --- reaching them by voice -------------------------------------------------
#
# The voice loops need audio hardware to drive, so what's tested here is the
# seam they depend on: that what the wake-word front end hands to `handle` is
# the command and not the whole utterance.


def test_a_command_said_after_her_name_is_still_a_command():
    from ada.voice_wake_repl import _strip_wake_word

    for spoken in ("Ada, reset", "Hey Ada, start over", "ada status"):
        stripped = _strip_wake_word(spoken)
        assert stripped is not None, spoken
        assert commands.handle(stripped) is not None, spoken


def test_her_name_alone_is_not_a_command():
    """It's the two-stage "Yes?" case, and it must not fall through to here."""
    from ada.voice_wake_repl import _strip_wake_word

    assert commands.handle(_strip_wake_word("Ada") or "") is None
