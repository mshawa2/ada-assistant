import threading

from ada import agent, commands, confirm, cost, session
from ada import status as ada_status
from ada.face import server as face_server
from ada.heartbeat import loop as heartbeat_loop
from ada.heartbeat.greeting import startup_summary
from ada.llm import LLMError


def _confirm(action: confirm.Action) -> bool:
    # Answerable from the terminal or the browser, whichever comes first.
    # See ada/confirm.py — it still fails safe with nobody there.
    return confirm.ask(action.description)


def _print_cost_summary() -> None:
    summary = cost.summary()
    if summary:
        print(summary)


def run() -> None:
    print("Ada is up. Type a message, or 'help' for the things I'll do straight away.\n")

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

        # The same layer the voice front ends use — see ada/commands.py. What
        # it can't do from here is clear the live list or leave this loop, so
        # it says so and this does it.
        done = commands.handle(user_input)
        if done is not None:
            # Brackets mark a line that isn't conversation — but wrapped round
            # a six-line list they read as a mistake, so multi-line output
            # stands on its own.
            print(f"{done.text}\n" if "\n" in done.text else f"[{done.text}]\n")
            if done.reset:
                messages.clear()
            if done.quit:
                stop_event.set()
                _print_cost_summary()
                return
            continue

        start_len = len(messages)
        messages.append({"role": "user", "content": user_input})
        ada_status.say("you", user_input)
        print("ada> ", end="", flush=True)
        ada_status.set_status("thinking...")

        ada_status.begin_reply()

        def on_text(chunk: str) -> None:
            print(chunk, end="", flush=True)
            ada_status.stream(chunk)

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
            ada_status.end_reply()
            session.record(messages)
            ada_status.set_status("idle")
