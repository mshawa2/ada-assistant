"""Microphone capture and non-blocking keypress detection.

Push-to-talk here is Enter-to-start / Enter-to-stop rather than a literal
held key — that needs no OS-level Accessibility permission and is just as
unambiguous about when Ada is and isn't listening.
"""

import collections
import io
import select
import sys
import wave
from collections.abc import Callable

import sounddevice as sd
import webrtcvad

from ada import interrupt as ada_interrupt

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2  # bytes, 16-bit PCM

_VAD_FRAME_MS = 30
_VAD_FRAME_SAMPLES = int(SAMPLE_RATE * _VAD_FRAME_MS / 1000)  # 480
_SILENCE_FRAMES_TO_END = int(800 / _VAD_FRAME_MS)  # ~800ms of trailing silence ends the utterance
_PREBUFFER_FRAMES = int(300 / _VAD_FRAME_MS)  # ~300ms kept before speech is detected, so the first syllable isn't clipped

# Tuned down from webrtcvad's default aggressiveness (2) after real-world testing showed
# normal-volume speech often went undetected, requiring the user to repeat themselves or
# speak unusually loud. 1 is more permissive about classifying a frame as speech. On its
# own that would also let more brief noise blips (clicks, breaths) false-trigger — so
# triggering now requires several *consecutive* speech frames, not just one, which filters
# those out without needing the stricter (and less sensitive) aggressiveness setting back.
_VAD_AGGRESSIVENESS = 1
_MIN_SPEECH_FRAMES_TO_TRIGGER = 3  # ~90ms of sustained speech before an utterance starts capturing


def record_until_enter() -> bytes:
    """Records mic audio until the user presses Enter; returns WAV bytes."""
    frames: list[bytes] = []

    def callback(indata, frame_count, time_info, status):
        frames.append(bytes(indata))

    with sd.RawInputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16", callback=callback):
        input()

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"".join(frames))
    return buf.getvalue()


def listen_for_utterance(
    should_ignore: Callable[[], bool],
    give_up_after: float | None = None,
) -> bytes:
    """Blocks until it captures one spoken utterance — speech, then a pause
    — using local voice-activity detection. No audio is sent anywhere until
    this returns; nothing at all is processed while `should_ignore()` is
    True (e.g. Ada is speaking), so she never reacts to her own voice.

    `give_up_after` bounds only the wait *before* anyone starts talking, and
    returns b"" if nobody does. Once speech has started the utterance always
    runs to its natural end — a clock that could cut someone off mid-sentence
    would be worse than no clock. Time spent ignoring Ada's own voice doesn't
    count against it either, or a window would expire while she was still
    answering the thing it was opened for.
    """
    vad = webrtcvad.Vad(_VAD_AGGRESSIVENESS)
    prebuffer: collections.deque[bytes] = collections.deque(maxlen=_PREBUFFER_FRAMES)
    voiced_frames: list[bytes] = []
    triggered = False
    silence_run = 0
    speech_run = 0
    frames_waited = 0

    with sd.RawInputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16") as stream:
        while True:
            if ada_interrupt.requested():
                return b""
            data, _ = stream.read(_VAD_FRAME_SAMPLES)
            frame = bytes(data)

            if should_ignore():
                prebuffer.clear()
                triggered = False
                voiced_frames = []
                silence_run = 0
                speech_run = 0
                continue

            if not triggered and give_up_after is not None:
                frames_waited += 1
                if frames_waited * _VAD_FRAME_MS / 1000 >= give_up_after:
                    return b""

            is_speech = vad.is_speech(frame, SAMPLE_RATE)

            if not triggered:
                prebuffer.append(frame)
                if is_speech:
                    speech_run += 1
                    if speech_run >= _MIN_SPEECH_FRAMES_TO_TRIGGER:
                        triggered = True
                        voiced_frames = list(prebuffer)
                        silence_run = 0
                else:
                    speech_run = 0
            else:
                voiced_frames.append(frame)
                if is_speech:
                    silence_run = 0
                else:
                    silence_run += 1
                    if silence_run >= _SILENCE_FRAMES_TO_END:
                        break

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"".join(voiced_frames))
    return buf.getvalue()


def enter_pressed() -> bool:
    """Non-blocking check for whether a line is waiting on stdin (Enter was hit)."""
    ready, _, _ = select.select([sys.stdin], [], [], 0)
    if ready:
        sys.stdin.readline()
        return True
    return False
