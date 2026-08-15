"""Speech-to-text via Deepgram. This is the only place that touches Deepgram —
`transcribe` is the seam: give it audio, get back text."""

import json
import urllib.error
import urllib.request

from ada.config import DEEPGRAM_API_KEY

_ENDPOINT = "https://api.deepgram.com/v1/listen?model=nova-2&smart_format=true"


class STTError(Exception):
    """Raised when Deepgram can't be reached or fails."""


def transcribe(wav_bytes: bytes) -> str:
    request = urllib.request.Request(
        _ENDPOINT,
        data=wav_bytes,
        headers={"Authorization": f"Token {DEEPGRAM_API_KEY}", "Content-Type": "audio/wav"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as e:
        raise STTError(f"Deepgram returned an error: HTTP {e.code}.") from e
    except urllib.error.URLError as e:
        raise STTError(f"Couldn't reach Deepgram: {e.reason}") from e
    except OSError as e:
        # Catches things like a raw ssl.SSLError from a flaky connection,
        # which can surface before urllib ever wraps it into a URLError.
        raise STTError(f"Network problem talking to Deepgram: {e}") from e

    try:
        return payload["results"]["channels"][0]["alternatives"][0]["transcript"]
    except (KeyError, IndexError) as e:
        raise STTError("Deepgram's response was empty or malformed.") from e
