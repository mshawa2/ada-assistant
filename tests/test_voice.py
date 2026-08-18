"""The follow-up window.

Once Ada has answered, she keeps listening for a while so a conversation
doesn't need her name in front of every sentence. That makes a microphone stay
live on a timer, which is worth pinning down precisely: the window has to close
on its own when nothing is said, must never cut someone off once they've
started talking, and must not tick down while Ada is the one speaking.

The mic and the voice-activity detector are both faked here. Nothing in these
tests touches real audio hardware.
"""

import io
import wave

import pytest

from ada import audio, settings, store, voice_wake_repl

# Frame content is what the fake detector reads to decide speech vs silence, so
# the two just need to differ.
_FRAME_BYTES = audio._VAD_FRAME_SAMPLES * audio.SAMPLE_WIDTH
_SPEECH = b"\x01" * _FRAME_BYTES
_SILENCE = b"\x00" * _FRAME_BYTES

# Enough frames to outlast any window under test. Reaching it means the call
# never returned, which should fail loudly rather than hang the suite.
_RUNAWAY = 2_000


class _FakeStream:
    """Stands in for sounddevice's mic stream, reading from a script."""

    def __init__(self, frames):
        self.frames = list(frames)
        self.reads = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, _samples):
        self.reads += 1
        if self.reads > _RUNAWAY:
            raise AssertionError("listen_for_utterance never returned")
        frame = self.frames.pop(0) if self.frames else _SILENCE
        return frame, False


class _FakeVad:
    def __init__(self, _aggressiveness):
        pass

    def is_speech(self, frame, _rate):
        return frame[:1] == b"\x01"


@pytest.fixture
def mic(monkeypatch):
    """Scripts the microphone. Returns the stream so tests can count frames."""

    def _script(pattern):
        # '#' is a frame of speech, '.' a frame of silence. Anything past the
        # end of the pattern is silence, so a script only spells out what matters.
        stream = _FakeStream(_SPEECH if c == "#" else _SILENCE for c in pattern)
        monkeypatch.setattr(audio.sd, "RawInputStream", lambda **kw: stream)
        monkeypatch.setattr(audio.webrtcvad, "Vad", _FakeVad)
        return stream

    return _script


def _speech_seconds(wav: bytes) -> float:
    with wave.open(io.BytesIO(wav)) as wf:
        return wf.getnframes() / wf.getframerate()


# --- the window closing on its own -------------------------------------------


def test_a_window_with_nothing_said_into_it_closes(mic):
    """The whole point of a window rather than an always-open mic."""
    stream = mic("." * 200)

    assert audio.listen_for_utterance(should_ignore=lambda: False, give_up_after=1.0) == b""
    # ~30ms per frame, so a one-second window is about 34 frames — the check is
    # that it gave up on time, not that it eventually gave up.
    assert stream.reads == pytest.approx(1.0 / (audio._VAD_FRAME_MS / 1000), abs=2)


def test_without_a_window_it_waits_however_long_it_takes(mic):
    """The wake-word path passes no deadline and must keep waiting."""
    mic("." * 300 + "#" * 20 + "." * 40)

    assert audio.listen_for_utterance(should_ignore=lambda: False, give_up_after=None) != b""


# --- what the window must never do -------------------------------------------


def test_speech_that_starts_in_time_is_never_cut_off(mic):
    """The deadline bounds the wait for someone to start, not how long they get
    to talk. Cutting off mid-sentence would be worse than having no window."""
    # Starts talking ~90ms in, then talks well past the 0.3s deadline.
    mic("." * 3 + "#" * 100 + "." * 40)

    wav = audio.listen_for_utterance(should_ignore=lambda: False, give_up_after=0.3)

    assert _speech_seconds(wav) > 0.3 * 3, "the utterance was truncated at the deadline"


def test_the_window_does_not_tick_down_while_ada_is_talking(mic):
    """Otherwise the window expires while she's still answering the very thing
    it was opened for, and the follow-up lands on a closed mic."""
    mic("." * 100 + "#" * 20 + "." * 40)
    ignored = iter([True] * 100)

    wav = audio.listen_for_utterance(
        should_ignore=lambda: next(ignored, False), give_up_after=0.5
    )

    assert wav != b"", "her own speaking time was charged against the window"


# --- resolving the setting ----------------------------------------------------


def test_the_window_defaults_to_thirty_seconds():
    assert voice_wake_repl._followup_window() == 30


def test_zero_turns_the_window_off():
    store.write_json(settings._CONFIG_FILE, {"voice": {"followup_seconds": 0}})
    settings._cache["stamp"] = None

    assert voice_wake_repl._followup_window() == 0


@pytest.mark.parametrize("junk", ["soon", None, [], -5])
def test_a_window_that_cannot_be_read_as_a_number_closes(junk):
    """Config can be wrong, and the safe direction for anything deciding when a
    microphone is live is the stricter one: fall back to needing her name."""
    store.write_json(settings._CONFIG_FILE, {"voice": {"followup_seconds": junk}})
    settings._cache["stamp"] = None

    assert voice_wake_repl._followup_window() == 0
