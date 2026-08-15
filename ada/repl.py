import threading

from ada import agent, cost, session
from ada import status as ada_status
from ada.face import server as face_server
from ada.heartbeat import loop as heartbeat_loop
from ada.heartbeat.greeting import startup_summary
from ada.llm import LLMError


def _confirm(message: str) -> bool:
    try:
        answer = input(f"\n[confirm] {message} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        # No one there to answer — fail safe: decline, don't hang or crash.
        return False
    return answer in ("y", "yes")


def _print_cost_summary() -> None:
    summary = cost.summary()
    if summary:
        print(summary)


def run() -> None:
    print("Ada is up. Type a message, 'reset' to start a fresh conversation, or 'exit' to quit.\n")

    stop_event = threading.Event()
    heartbeat_loop.start(stop_event)
    face_server.start()

    summary = startup_summary()
    if summary:
        print(summary + "\n")

    messages = session.load()
    resumed = session.resumed_summary(messages)
    if resumed:
        print(resumed + "\n")

    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            stop_event.set()
            print("\nbye!")
            _print_cost_summary()
            return

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            stop_event.set()
            print("bye!")
            _print_cost_summary()
            return
        if user_input.lower() == "reset":
            messages.clear()
            session.clear()
            print("[fresh conversation — what I remember about you is untouched]\n")
            continue

        start_len = len(messages)
        messages.append({"role": "user", "content": user_input})
        print("ada> ", end="", flush=True)
        ada_status.set_status("thinking...")

        def on_text(chunk: str) -> None:
            print(chunk, end="", flush=True)

        def on_status(status_text: str) -> None:
            print(f"\n  [{status_text}]", flush=True)
            ada_status.set_status(status_text)

        try:
            agent.run_turn(messages, on_text, on_status, _confirm)
            print("\n")
        except LLMError as e:
            print(f"\n[{e}]\n")
            # Roll back the whole failed turn, so a retry doesn't resend a
            # half-finished exchange the model never actually answered.
            del messages[start_len:]
        finally:
            session.record(messages)
            ada_status.set_status("idle")
