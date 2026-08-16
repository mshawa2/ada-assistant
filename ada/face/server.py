"""Runs the face's Flask app in a background thread, same pattern as the
heartbeat loop. A dashboard failing to bind is a non-event for Ada's actual
job (talking to you), so it's caught and reported, never fatal.
"""

import logging
import threading

from werkzeug.serving import make_server

PORT = 4173


def start() -> threading.Thread | None:
    from ada.face.app import app

    # The face polls five endpoints, the liveliest several times a second, and
    # Werkzeug logs every one to the same stdout you type to Ada on — the
    # prompt ends up buried under a waterfall of GET /api/status. Ada's own
    # output is the only thing that belongs in that stream. Real failures still
    # surface: exceptions inside a view are logged by Flask, not by Werkzeug,
    # and a port clash raises OSError below.
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    def serve() -> None:
        try:
            # `make_server` rather than `app.run`, which greets stdout with a
            # four-line banner mid-prompt on its way up. None of the machinery
            # that banner is advertising (reloader, debugger) is wanted in a
            # daemon thread anyway.
            make_server("127.0.0.1", PORT, app, threaded=True).serve_forever()
        except OSError as e:
            print(f"\n[face: couldn't start dashboard on port {PORT}: {e}]", flush=True)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    print(f"Face running at http://127.0.0.1:{PORT}\n")
    return thread
