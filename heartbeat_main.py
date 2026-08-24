"""Run the heartbeat and the dashboard — no conversation loop, no terminal.

This is what a launchd agent (or any always-on host) runs. Everything else in
the project starts a REPL and beats the heartbeat as a side effect, which
means proactivity only exists while you happen to have a terminal open. Here
the heartbeat is the whole program.

It serves the face too, and that isn't an afterthought: `ada/status.py` is
*process memory*, so a dashboard in its own process would report `idle` and an
empty tool list forever while this one ran the jobs. The dashboard belongs to
the process that's actually doing something. Everything durable — jobs,
approvals, schedules, notices, spend — is on disk and readable from anywhere;
only the live half needs to be in here.

It's safe to run this and a REPL at the same time: `ada/heartbeat/singleton.py`
means whoever gets the lock does the checking and the other just reads. In
practice the daemon wins, since it started first. The REPL's own face then
can't bind the port and says so — its conversation still reaches the dashboard,
because the panel falls back to the transcript on disk.

    python heartbeat_main.py

See launchd/README.md to install it as a background agent.
"""

import signal
import threading

from ada.face import server as face_server
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
    face_server.start()

    try:
        stop_event.wait()
    finally:
        singleton.release()
        print("heartbeat stopped.", flush=True)


if __name__ == "__main__":
    run()
