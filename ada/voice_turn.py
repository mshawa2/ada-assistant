"""Shared 'run one spoken turn' logic, used by both the push-to-talk and
wake-word voice front-ends — so the two can never drift into different
agent behavior. Both ultimately just call `agent.run_turn`, same as the text
REPL; this module only adds sentence-chunked TTS playback around it.
"""

import queue
import re
import threading
from collections.abc import Callable

from ada import agent, confirm as confirm_module
from ada import interrupt as ada_interrupt
from ada import status as ada_status
from ada.llm import LLMError
from ada.tts import TTSError, speak

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def confirm(message: str) -> bool:
    """Spoken heads-up, but the answer is typed or clicked — never spoken.

    A misheard "yes" is not a risk worth taking on a safety gate; that was a
    deliberate Tier 6 decision and it still holds now the browser can answer.
    """

    def announce(text: str) -> None:
        speak(f"{text} Confirm in the terminal or the browser.", should_stop=ada_interrupt.requested)

    return confirm_module.ask(message, speak=announce)


def run_spoken_turn(
    messages: list[dict],
    interrupt_check: Callable[[], bool],
    speaking: threading.Event,
) -> tuple[bool, str]:
    """Runs one turn: streams Ada's reply, speaking it sentence-by-sentence as
    it's generated. `interrupt_check` is polled during generation and
    playback; if it ever returns True, remaining speech is skipped and any
    in-progress sentence stops early. `speaking` is set only while audio is
    actually playing, so a caller (e.g. the wake-word listener) can avoid
    capturing Ada's own voice through the mic. Returns (interrupted, the full
    reply text) — the text lets a caller notice Ada asked a question and
    should listen for the answer without requiring the wake word again.
    """
    start_len = len(messages)
    interrupted = threading.Event()
    speech_queue: queue.Queue[str | None] = queue.Queue()
    worker = threading.Thread(target=_speak_worker, args=(speech_queue, interrupted, speaking), daemon=True)
    worker.start()

    buffer = ""
    full_reply = ""

    def on_text(chunk: str) -> None:
        nonlocal buffer, full_reply
        print(chunk, end="", flush=True)
        buffer += chunk
        full_reply += chunk
        ada_status.stream(chunk)
        parts = _SENTENCE_END.split(buffer)
        if len(parts) > 1:
            for sentence in parts[:-1]:
                if sentence.strip():
                    speech_queue.put(sentence.strip())
            buffer = parts[-1]
        if interrupt_check() or ada_interrupt.requested():
            interrupted.set()

    def on_status(status_text: str) -> None:
        print(f"\n  [{status_text}]", flush=True)
        ada_status.set_status(status_text)

    print("ada> ", end="", flush=True)
    ada_status.set_status("thinking...")
    ada_status.begin_reply()
    try:
        agent.run_turn(messages, on_text, on_status, confirm)
        if buffer.strip() and not interrupted.is_set():
            speech_queue.put(buffer.strip())
        print("\n")
    except LLMError as e:
        print(f"\n[{e}]\n")
        del messages[start_len:]

    ada_status.end_reply()
    ada_status.set_status("speaking...")
    speech_queue.put(None)
    while worker.is_alive():
        if interrupt_check() or ada_interrupt.requested():
            interrupted.set()
        worker.join(timeout=0.05)
    ada_status.set_status("idle")

    return interrupted.is_set(), full_reply


def _speak_worker(
    speech_queue: "queue.Queue[str | None]",
    interrupted: threading.Event,
    speaking: threading.Event,
) -> None:
    while True:
        sentence = speech_queue.get()
        if sentence is None:
            return
        if interrupted.is_set():
            continue
        speaking.set()
        try:
            speak(sentence, should_stop=lambda: interrupted.is_set() or ada_interrupt.requested())
        except TTSError as e:
            print(f"\n[voice error: {e}]", flush=True)
        finally:
            speaking.clear()
