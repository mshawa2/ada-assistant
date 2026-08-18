import threading

from ada import channel, cost, session
from ada import interrupt as ada_interrupt
from ada import status as ada_status
from ada.audio import enter_pressed, record_until_enter
from ada.face import server as face_server
from ada.heartbeat import loop as heartbeat_loop
from ada.heartbeat.greeting import startup_summary
from ada.stt import STTError, transcribe
from ada.tts import TTSError, speak
from ada.voice_turn import run_spoken_turn


def _print_cost_summary() -> None:
    summary = cost.summary()
    if summary:
        print(summary)


def run() -> None:
    # See ada/channel.py — replies here are spoken, not read.
    channel.use_voice()

    print("Ada (voice) is up. Press Enter to start talking, Enter again to stop. Ctrl-C to quit.\n")

    # See ada/interrupt.py — sounddevice's blocking calls don't reliably let a
    # raised KeyboardInterrupt propagate, so TTS playback (in run_spoken_turn)
    # polls this flag instead of depending on the exception alone.
    ada_interrupt.install()

    stop_event = threading.Event()
    heartbeat_loop.start(stop_event)
    face_server.start()

    speaking = threading.Event()

    summary = startup_summary()
    if summary:
        print(summary + "\n")
        try:
            speak(summary, should_stop=ada_interrupt.requested)
        except TTSError as e:
            print(f"[voice error: {e}]\n")

    messages = session.load()
    skip_prompt = False

    try:
        while True:
            if not skip_prompt:
                input("press Enter to talk> ")
            skip_prompt = False

            print("listening... (press Enter to stop)")
            ada_status.set_status("listening...")
            wav = record_until_enter()

            print("thinking...")
            ada_status.set_status("transcribing...")
            try:
                text = transcribe(wav)
            except STTError as e:
                print(f"[{e}]\n")
                ada_status.set_status("idle")
                continue

            if not text.strip():
                print("[didn't catch anything]\n")
                ada_status.set_status("idle")
                continue

            print(f'you said: "{text}"\n')

            ada_status.say("you", text)
            messages.append({"role": "user", "content": text})
            interrupted, _reply = run_spoken_turn(messages, enter_pressed, speaking)
            session.record(messages)
            skip_prompt = interrupted
    except (EOFError, KeyboardInterrupt):
        # Catches Ctrl-C (or piped stdin ending) no matter which stage of a
        # turn it lands in — waiting at the prompt, recording, transcribing,
        # thinking, or speaking — not just the narrower spots the old code covered.
        stop_event.set()
        print("\nbye!")
        _print_cost_summary()
