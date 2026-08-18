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

# Sentences are handed to TTS one at a time, so a split is an audible pause.
# Splitting on every ". " puts one in the middle of "at 3 p.m. tomorrow" or
# "Dr. Patel called" — the giveaway that a voice is synthetic. These are the
# words that end in a full stop without ending the sentence.
_ABBREVIATIONS = frozenset(
    """mr mrs ms dr prof rev sr jr st vs etc al approx dept est fig vol no
    a.m p.m e.g i.e u.s u.k""".split()
)

# A boundary is punctuation, then whitespace, then something that looks like
# the start of a new sentence. Requiring the capital rules out "e.g. this"
# without needing to know the abbreviation.
_BOUNDARY = re.compile(r"""(?<=[.!?])["')\]]*\s+(?=["'(\[]*[A-Z0-9])""")
_TRAILING_WORD = re.compile(r"""([A-Za-z][A-Za-z.]*)\.["')\]]*\s*$""")


def _ends_a_sentence(text: str) -> bool:
    """Whether `text` really finishes on a full stop, or just looks like it."""
    match = _TRAILING_WORD.search(text)
    if not match:
        return True  # ends on '?', '!' or a digit — a real boundary
    word = match.group(1).lower()
    # A single letter is an initial: "J. R. Hartley" is a name, not three
    # sentences. Erring towards merging is the safe direction — the cost is a
    # slightly longer chunk of speech, where the cost of erring the other way
    # is a hard pause in the middle of a phrase.
    return word not in _ABBREVIATIONS and len(word.rstrip(".")) > 1


def _complete_sentences(buffer: str) -> tuple[list[str], str]:
    """Splits off whatever whole sentences `buffer` holds, returning them and
    the unfinished remainder to keep buffering."""
    sentences, start = [], 0
    for match in _BOUNDARY.finditer(buffer):
        candidate = buffer[start : match.start()]
        if not _ends_a_sentence(candidate):
            continue
        if candidate.strip():
            sentences.append(candidate.strip())
        start = match.end()
    return sentences, buffer[start:]


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
        sentences, buffer = _complete_sentences(buffer)
        for sentence in sentences:
            speech_queue.put(sentence)
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
