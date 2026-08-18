"""Text-to-speech via ElevenLabs. This is the only place that touches
ElevenLabs — `speak` is the seam: give it text, hear it played aloud.

Streams both the synthesis (ElevenLabs sends audio as it's generated) and the
playback (we write chunks to the output device as they arrive), so speech
starts well before the whole sentence has been synthesized.
"""

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable

import sounddevice as sd

from ada.config import ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID

_SAMPLE_RATE = 24000
_CHUNK_SIZE = 4096


# The prompt tells Ada not to write markdown when she's being spoken (see
# ada/system_prompt.py), and mostly she doesn't. This is the net under that:
# one stray "**" is read out as "asterisk asterisk", and a URL is spelled
# character by character for ten seconds. Cleaning at the audio boundary means
# the terminal and the browser still get the rich text — only the voice is
# stripped.
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_BARE_URL = re.compile(r"<?https?://\S+>?")
_CODE_FENCE = re.compile(r"```[^\n]*")
_LINE_PREFIX = re.compile(r"^[ \t]*(?:#{1,6}[ \t]*|[-*+][ \t]+|\d+[.)][ \t]+)", re.M)
# Asterisks and backticks only. Underscores are left alone on purpose: in this
# app a stray "_" is far more likely to be a tool name like set_home_device
# than emphasis, and stripping those would say "sethomedevice".
_LEFTOVER_MARKUP = re.compile(r"[*`]+")
_WHITESPACE = re.compile(r"\s+")


def _for_speech(text: str) -> str:
    """`text` with anything that only makes sense on a screen taken out."""
    text = _CODE_FENCE.sub(" ", text)
    text = _LINK.sub(r"\1", text)
    text = _BARE_URL.sub("the link", text)
    text = _LINE_PREFIX.sub("", text)
    text = _LEFTOVER_MARKUP.sub("", text)
    return _WHITESPACE.sub(" ", text).strip()


class TTSError(Exception):
    """Raised when ElevenLabs can't be reached or fails."""


def speak(text: str, should_stop: Callable[[], bool]) -> None:
    """Synthesizes and plays `text`, stopping early if should_stop() becomes True."""
    text = _for_speech(text)
    if not text:
        return

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}/stream?output_format=pcm_24000"
    request = urllib.request.Request(
        url,
        data=json.dumps({"text": text, "model_id": "eleven_turbo_v2_5"}).encode(),
        headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            with sd.RawOutputStream(samplerate=_SAMPLE_RATE, channels=1, dtype="int16") as stream:
                while not should_stop():
                    chunk = response.read(_CHUNK_SIZE)
                    if not chunk:
                        return
                    stream.write(chunk)
    except urllib.error.HTTPError as e:
        raise TTSError(f"ElevenLabs returned an error: HTTP {e.code}.") from e
    except urllib.error.URLError as e:
        raise TTSError(f"Couldn't reach ElevenLabs: {e.reason}") from e
    except OSError as e:
        # Catches things like a raw ssl.SSLError from a flaky connection,
        # which can surface before urllib ever wraps it into a URLError.
        raise TTSError(f"Network problem talking to ElevenLabs: {e}") from e
