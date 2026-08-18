"""Talking like a person rather than a document.

The agent, the tools and the system prompt are shared by all three front-ends,
but two of them are read out loud by a synthetic voice. What reads well on a
screen — bullet lists, bold, a cited URL — is unlistenable, and the failure is
silent: the reply is correct, it just sounds wrong. These pin the three places
that difference is handled.
"""

import pytest

from ada import channel, system_prompt, tts
from ada.voice_turn import _complete_sentences


@pytest.fixture(autouse=True)
def text_channel(monkeypatch):
    """Default every test back to the screen, so one that switches to voice
    can't leak into the next."""
    monkeypatch.setattr(channel, "_spoken", False)


# --- knowing which channel it's on -------------------------------------------


def test_the_screen_gets_no_speech_instructions():
    """In the text REPL markdown and links are exactly right — telling her to
    drop them would make that front-end worse."""
    assert "spoken" not in system_prompt.stable_prompt().lower()


def test_a_voice_front_end_asks_for_speakable_replies():
    channel.use_voice()
    prompt = system_prompt.stable_prompt().lower()

    assert "spoken" in prompt
    assert "markdown" in prompt


def test_the_speech_rules_come_after_the_rules_they_override():
    """The base prompt says to cite the URLs used. Read aloud that's a spelled
    -out link, so the spoken block countermands it — and only wins if it's
    last."""
    channel.use_voice()
    prompt = system_prompt.stable_prompt()

    assert prompt.index("cite the URLs") < prompt.index("Don't say URLs out loud")


def test_the_channel_does_not_change_under_the_prompt_cache():
    """It's decided once at startup, which is what lets it sit above the cache
    breakpoint. A prompt that flipped mid-session would re-bill every call."""
    channel.use_voice()

    assert system_prompt.stable_prompt() == system_prompt.stable_prompt()


# --- not pausing in the middle of a phrase -----------------------------------


@pytest.mark.parametrize(
    "text,spoken_whole",
    [
        ("Your flight's at 3 p.m. tomorrow.", "Your flight's at 3 p.m. tomorrow."),
        ("Dr. Patel called about Friday.", "Dr. Patel called about Friday."),
        ("Milk, eggs, etc. are on the list.", "Milk, eggs, etc. are on the list."),
        ("J. R. Hartley wrote back.", "J. R. Hartley wrote back."),
    ],
)
def test_an_abbreviation_is_not_a_pause(text, spoken_whole):
    """Each chunk is a separate call to the voice, so a split here is an
    audible gap in the middle of a phrase — the tell that it's a machine."""
    sentences, remainder = _complete_sentences(text + " Anything else?")

    assert sentences == [spoken_whole]
    assert remainder.strip() == "Anything else?"


def test_real_sentences_still_split():
    """Over-merging costs nothing but a longer chunk; under-merging would mean
    waiting for the whole reply before any of it is spoken."""
    sentences, remainder = _complete_sentences("Standup at 9. Dentist at 3. Flight at")

    assert sentences == ["Standup at 9.", "Dentist at 3."]
    assert remainder.strip() == "Flight at"


def test_an_unfinished_sentence_is_held_back():
    """Text arrives a token at a time; speaking half a sentence is worse than
    waiting for the rest of it."""
    assert _complete_sentences("I checked and") == ([], "I checked and")


def test_questions_and_exclamations_end_a_sentence():
    sentences, _ = _complete_sentences("Shall I book it? It's free. And quick!  Well?  No")

    assert sentences == ["Shall I book it?", "It's free.", "And quick!", "Well?"]


def test_a_finished_sentence_waits_for_the_next_one_to_start():
    """A full stop alone isn't proof the sentence ended — "3 p.m." has one. So
    the boundary needs the next sentence to have started, which means the last
    sentence of a reply stays buffered. run_spoken_turn flushes the remainder
    once the stream ends, so it still gets spoken; the cost is one token of
    latency, and the alternative is pausing mid-phrase on every abbreviation."""
    assert _complete_sentences("All booked. ") == ([], "All booked. ")


# --- what actually reaches the voice -----------------------------------------


@pytest.mark.parametrize(
    "written,heard",
    [
        ("Here are the **three** things:", "Here are the three things:"),
        ("## Tomorrow", "Tomorrow"),
        ("- Standup\n- Dentist", "Standup Dentist"),
        ("1. Standup", "Standup"),
        ("Run `pytest` first.", "Run pytest first."),
        ("Per [the Guardian](https://x.com/y), it rained.", "Per the Guardian, it rained."),
        ("See https://example.com/a/b?q=1 for more.", "See the link for more."),
    ],
)
def test_screen_only_markup_never_reaches_the_voice(written, heard):
    """Read literally, "**" becomes "asterisk asterisk" and a URL is spelled
    out for ten seconds. The prompt asks her not to write these; this is the
    net under that, at the last point before the audio."""
    assert tts._for_speech(written) == heard


def test_underscores_survive():
    """Stripping these as emphasis would say "sethomedevice" — in this app an
    underscore is far more often a tool name than markup."""
    assert tts._for_speech("Use control_home_device for that.") == (
        "Use control_home_device for that."
    )


def test_ordinary_prose_is_left_exactly_alone():
    plain = "I moved it to 4pm — Priya can't do 3, and the room's booked anyway."

    assert tts._for_speech(plain) == plain


def test_nothing_to_say_is_not_spoken(monkeypatch):
    """A chunk that was pure markup mustn't become an empty API call."""
    calls = []
    monkeypatch.setattr(tts.urllib.request, "urlopen", lambda *a, **k: calls.append(1))

    tts.speak("```", should_stop=lambda: False)

    assert calls == []
