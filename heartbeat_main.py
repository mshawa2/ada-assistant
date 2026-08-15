"""Run only the heartbeat — no conversation loop, no terminal needed.

This is what a launchd agent (or any always-on host) runs. Everything else in
the project starts a REPL and beats the heartbeat as a side effect, which
means proactivity only exists while you happen to have a terminal open. Here
the heartbeat is the whole program.

It's safe to run this and a REPL at the same time: `ada/heartbeat/singleton.py`
means whoever gets the lock does the checking and the other just reads. In
practice the daemon wins, since it started first.

    python heartbeat_main.py

See launchd/README.md to install it as a background agent.
"""

import signal
import threading

from ada.heartbeat import loop as heartbeat_loop
from ada.heartbeat import singleton


def run() -> None:
    stop_event = threading.Event()

    def shutdown(_signum, _frame):
        stop_event.set()

    # launchd stops an agent with SIGTERM; Ctrl-C sends SIGINT when run by
    # hand. Both should end the same clean way rather than killing the process
    # mid-check and leaving a stale PID in the lock file.
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    print("Ada's heartbeat is running. Ctrl-C to stop.", flush=True)
    heartbeat_loop.start(stop_event)

    try:
        stop_event.wait()
    finally:
        singleton.release()
        print("heartbeat stopped.", flush=True)


if __name__ == "__main__":
    run()
