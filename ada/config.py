import os
import sys

from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

if not ANTHROPIC_API_KEY:
    sys.exit(
        "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key."
    )

# Which model to use is a preference, not a secret, so it lives in
# ada_config.json (`model.conversation`) rather than here. This module is only
# for things that come from the environment: keys, and the voice id that's
# tied to one.

# Only needed for voice mode — checked lazily by require_voice_keys() rather
# than at import time, so the text REPL never needs these.
DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "yj30vwTGJxSHezdAGsv9")  # Jessa


def require_voice_keys() -> None:
    missing = [
        name
        for name, value in (("DEEPGRAM_API_KEY", DEEPGRAM_API_KEY), ("ELEVENLABS_API_KEY", ELEVENLABS_API_KEY))
        if not value
    ]
    if missing:
        sys.exit(f"Missing from .env: {', '.join(missing)}. Add them to use voice mode.")
