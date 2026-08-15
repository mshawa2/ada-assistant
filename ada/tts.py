"""Text-to-speech via ElevenLabs. This is the only place that touches
ElevenLabs — `speak` is the seam: give it text, hear it played aloud.

Streams both the synthesis (ElevenLabs sends audio as it's generated) and the
playback (we write chunks to the output device as they arrive), so speech
starts well before the whole sentence has been synthesized.
"""

import json
import urllib.error
import urllib.request
from collections.abc import Callable

import sounddevice as sd

from ada.config import ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID

_SAMPLE_RATE = 24000
_CHUNK_SIZE = 4096


class TTSError(Exception):
    """Raised when ElevenLabs can't be reached or fails."""


def speak(text: str, should_stop: Callable[[], bool]) -> None:
    """Synthesizes and plays `text`, stopping early if should_stop() becomes True."""
    text = text.strip()
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
