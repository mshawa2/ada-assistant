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

from ada import cost, session, settings
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


def _followup_window() -> float:
    """Seconds Ada keeps listening after she speaks, so a follow-up doesn't
    need her name again. 0 means the wake word is always required.

    A value that isn't a number fails to 0 rather than to the default: the
    stricter behaviour is the safe direction for anything deciding when a
    microphone is live.
    """
    try:
        return max(0.0, float(settings.get("voice.followup_seconds", 30)))
    except (TypeError, ValueError):
        return 0.0


def _print_cost_summary() -> None:
    summary = cost.summary()
    if summary:
        print(summary)


def run() -> None:
    window = _followup_window()
    print('Ada (wake word) is up. Just say "Ada" followed by what you need.')
    if window:
        # Worth saying, because it's the difference between talking to her and
        # addressing her: the wake word only starts a conversation now.
        print(f"After she answers, keep talking — no need to say her name for {window:g}s. Ctrl-C to quit.\n")
    else:
        print("Ctrl-C to quit.\n")

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

    # How the next thing she hears is treated. False means she needs her name
    # first. True means she's still in the conversation and takes what she hears
    # as a command — with `open_mic_seconds` bounding how long that lasts, or
    # None to wait indefinitely because she asked a question and is owed an
    # answer.
    open_mic = False
    open_mic_seconds: float | None = None

    try:
        while not ada_interrupt.requested():
            if open_mic:
                ada_status.set_status("listening...")
            else:
                print('listening for "Ada"...')
                ada_status.set_status('listening for "Ada"...')

            wav = listen_for_utterance(
                should_ignore=speaking.is_set,
                give_up_after=open_mic_seconds if open_mic else None,
            )
            if ada_interrupt.requested():
                break

            if not wav:
                # The follow-up window closed with nothing said. Back to needing
                # her name, without announcing it — the conversation just ended
                # the way conversations do.
                open_mic = False
                continue

            ada_status.set_status("transcribing...")
            try:
                text = transcribe(wav)
            except STTError as e:
                print(f"[{e}]")
                continue

            if ada_interrupt.requested() or not text.strip():
                continue

            if open_mic:
                # Mid-conversation, but people say her name out of habit anyway
                # — strip it if it's there rather than sending "Ada, ..." along
                # as part of the request.
                command = _strip_wake_word(text)
                if command is None:
                    command = text.strip()
                open_mic = False
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
                open_mic, open_mic_seconds = True, None
                continue

            print(f'you said: "{command}"')
            ada_status.say("you", command)
            messages.append({"role": "user", "content": command})
            interrupted, reply = run_spoken_turn(messages, interrupt_check=lambda: False, speaking=speaking)
            session.record(messages)

            # Stay in the conversation rather than making them say her name
            # again for every sentence. A question waits indefinitely — she
            # asked it, so she waits for the answer. Anything else gets a
            # bounded window, because a mic that never closes is a different
            # product in a room with other people in it.
            if interrupted:
                open_mic = False
            elif reply.rstrip().endswith("?"):
                open_mic, open_mic_seconds = True, None
            else:
                open_mic_seconds = _followup_window()
                open_mic = open_mic_seconds > 0
    except (EOFError, KeyboardInterrupt):
        # Backstop, not the primary mechanism: if a KeyboardInterrupt ever does
        # get raised cleanly (some path other than the confirmed-broken
        # sounddevice calls), this still catches it instead of crashing.
        pass

    stop_event.set()
    print("\nbye!")
    _print_cost_summary()
