"""Test setup.

The two `os.environ` lines below run before anything under `ada` is imported,
and that ordering is load-bearing: `ada.store` reads ADA_DATA_DIR at import
time and every store module derives its path from it, so setting it later
would leave the tests pointed at the user's real reminders and memory.
"""

import os
import shutil
import tempfile
from pathlib import Path

TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="ada-tests-"))
os.environ["ADA_DATA_DIR"] = str(TEST_DATA_DIR)
# Only so importing ada.config doesn't sys.exit on a machine without a .env.
# Nothing here makes a real API call — the model seam is always faked.
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-never-used")

import pytest  # noqa: E402

from ada import notify, settings  # noqa: E402

# A window of zero width is never active, so quiet hours are off unless a test
# asks for them. Without this, notification tests would pass or fail depending
# on what time of day the suite happened to run.
_NEVER_QUIET = {"start": "22:00", "end": "22:00"}

TEST_NOTIFY_CONFIG = {
    "quiet_hours": _NEVER_QUIET,
    "notifications": {"channels": ["terminal"], "push_levels": ["urgent", "critical"]},
}


@pytest.fixture(autouse=True)
def clean_data_dir():
    """Every test starts against an empty data directory."""
    for child in TEST_DATA_DIR.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    yield


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    """Point the settings file at the temp directory, not the real repo.

    `ada_config.json` lives at the repo root rather than under `data/`, so
    without this a test that writes a setting would edit the developer's own
    configuration — and the legacy-migration path would read their real
    heartbeat_config.json. Everything here is redirected into the throwaway
    directory, and the parse cache is cleared so no state leaks between tests.
    """
    monkeypatch.setattr(settings, "_CONFIG_FILE", TEST_DATA_DIR / "ada_config.json")
    monkeypatch.setattr(settings, "_LEGACY_HEARTBEAT", TEST_DATA_DIR / "heartbeat_config.json")
    monkeypatch.setattr(settings, "_LEGACY_SAFETY", TEST_DATA_DIR / "safety_config.json")
    settings._cache["stamp"] = None
    settings._cache["value"] = None
    yield
    settings._cache["stamp"] = None
    settings._cache["value"] = None


@pytest.fixture(autouse=True)
def sent_notifications(monkeypatch):
    """Record notifications instead of sending them, and pin the config.

    Autouse and unconditional: every channel is replaced, so no test can put a
    real banner on the developer's screen or a real push on their phone. The
    level and quiet-hours logic above the channels still runs for real — only
    the final hop out of the process is swapped.
    """
    sent = []

    def record(text, _settings, urgent):
        sent.append({"text": text, "urgent": urgent})
        return True

    monkeypatch.setattr(notify, "_CHANNELS", {"terminal": record, "macos": record, "ntfy": record})
    monkeypatch.setattr(notify, "load_config", lambda: TEST_NOTIFY_CONFIG)
    return sent


class FakeBlock:
    """Stands in for an SDK content block — same duck type agent.py reads."""

    def __init__(self, type, **fields):
        self.type = type
        for key, value in fields.items():
            setattr(self, key, value)


class FakeMessage:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason


def fake_stream_turn(*responses):
    """A stand-in for `llm.stream_turn` that replays `responses` in order.

    Matches the real contract exactly: a generator that yields text chunks and
    returns the final message via StopIteration.value. The last response
    repeats if the agent asks for more turns than were scripted.
    """
    calls = []

    def stream_turn(messages, tools):
        message = responses[min(len(calls), len(responses) - 1)]
        calls.append({"messages": [dict(m) for m in messages], "tools": tools})

        def generator():
            for block in message.content:
                if block.type == "text":
                    yield block.text
            return message

        return generator()

    stream_turn.calls = calls
    return stream_turn
