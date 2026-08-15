"""Open-mic wake-word voice front end: no push-to-talk, Ada listens
continuously (locally, via voice-activity detection — nothing is sent
anywhere until she actually hears speech) and responds when addressed by
name. Shares the exact same agent core and turn-running logic as the
push-to-talk voice REPL (`ada/voice_turn.py`) — only how a turn gets kicked
off differs.

No voice-based barge-in here: while Ada is speaking, `speaking` gates the
mic off entirely (see `ada/audio.py`'s `listen_for_utterance`) rather than
trying to interrupt on hearing "Ada" again, since without real echo
cancellation her own voice could false-trigger that. Interrupt with Ctrl-C.
"""

import re
import threading

from ada import cost, session
from ada import interrupt as ada_interrupt
from ada import status as ada_status
from ada.audio import listen_for_utterance
from ada.face import server as face_server
from ada.heartbeat import loop as heartbeat_loop
from ada.heartbeat.greeting import startup_summary
from ada.stt import STTError, transcribe
from ada.tts import TTSError, speak
from ada.voice_turn import run_spoken_turn

# "Ada" reliably comes back misheard against real transcription — confirmed
# "Ida." when said in isolation, and "Haida" mid-sentence in real use. These
# are the near-homophones to also accept as the wake word.
_WAKE_ALIASES = ("ada", "ida", "aida", "eda", "aita", "haida")
_WAKE_PATTERN = re.compile(
    rf"^\s*(?:hey|ok(?:ay)?)?[,\s]*(?:{'|'.join(_WAKE_ALIASES)})\b[,.!?\s]*", re.IGNORECASE
)


def _strip_wake_word(text: str) -> str | None:
    """Returns the command after the wake word, '' if only the wake word was
    said, or None if the wake word wasn't heard at all."""
    stripped = text.strip()
    match = _WAKE_PATTERN.match(stripped)
    if not match:
        return None
    return stripped[match.end() :].strip()


def _print_cost_summary() -> None:
    summary = cost.summary()
    if summary:
        print(summary)


def run() -> None:
    print('Ada (wake word) is up. Just say "Ada" followed by what you need. Ctrl-C to quit.\n')

    # Installed once, up front: sounddevice's blocking mic/speaker calls don't
    # reliably let a raised KeyboardInterrupt propagate, so Ctrl-C is handled
    # by polling ada_interrupt.requested() at points that do return control to
    # Python regularly, rather than depending on the exception alone.
    ada_interrupt.install()

    stop_event = threading.Event()
    heartbeat_loop.start(stop_event)
    face_server.start()

    speaking = threading.Event()

    summary = startup_summary()
    if summary and not ada_interrupt.requested():
        print(summary + "\n")
        try:
            speaking.set()
            speak(summary, should_stop=ada_interrupt.requested)
        except TTSError as e:
            print(f"[voice error: {e}]\n")
        finally:
            speaking.clear()

    messages = session.load()
    pending_command = False  # True right after "Ada" alone was said, waiting for the follow-up

    try:
        while not ada_interrupt.requested():
            if pending_command:
                ada_status.set_status("listening...")
            else:
                print('listening for "Ada"...')
                ada_status.set_status('listening for "Ada"...')

            wav = listen_for_utterance(should_ignore=speaking.is_set)
            if ada_interrupt.requested():
                break

            ada_status.set_status("transcribing...")
            try:
                text = transcribe(wav)
            except STTError as e:
                print(f"[{e}]")
                continue

            if ada_interrupt.requested() or not text.strip():
                continue

            if pending_command:
                command = text.strip()
                pending_command = False
            else:
                command = _strip_wake_word(text)
                if command is None:
                    continue  # not addressed to Ada — discard, keep listening

            if not command:
                print('you said: "Ada"')
                print("ada> Yes?\n")
                try:
                    speaking.set()
                    speak("Yes?", should_stop=ada_interrupt.requested)
                except TTSError as e:
                    print(f"[voice error: {e}]")
                finally:
                    speaking.clear()
                pending_command = True
                continue

            print(f'you said: "{command}"')
            messages.append({"role": "user", "content": command})
            interrupted, reply = run_spoken_turn(messages, interrupt_check=lambda: False, speaking=speaking)
            session.record(messages)

            # If Ada just asked a question, listen for the answer directly next
            # time around — no need to say the wake word again to reply to her.
            pending_command = (not interrupted) and reply.rstrip().endswith("?")
    except (EOFError, KeyboardInterrupt):
        # Backstop, not the primary mechanism: if a KeyboardInterrupt ever does
        # get raised cleanly (some path other than the confirmed-broken
        # sounddevice calls), this still catches it instead of crashing.
        pass

    stop_event.set()
    print("\nbye!")
    _print_cost_summary()
