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


@pytest.fixture(autouse=True)
def clean_data_dir():
    """Every test starts against an empty data directory."""
    for child in TEST_DATA_DIR.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    yield


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
