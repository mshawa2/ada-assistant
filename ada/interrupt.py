"""A SIGINT handler that sets a flag instead of raising KeyboardInterrupt.

Several blocking calls this app makes — specifically sounddevice's mic and
speaker streams, confirmed empirically, not assumed — don't reliably let a
raised KeyboardInterrupt propagate out of them; Ctrl-C can go unnoticed for
a long time or indefinitely while one is in progress. Polling a flag from
inside loops that *do* return control to Python regularly (even if each
individual call blocks for a while) works regardless of whether the
exception-based mechanism does.
"""

import signal
import threading

_event = threading.Event()


def _handler(signum, frame) -> None:
    _event.set()


def install() -> None:
    signal.signal(signal.SIGINT, _handler)


def requested() -> bool:
    return _event.is_set()
