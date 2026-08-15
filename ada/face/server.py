"""Runs the face's Flask app in a background thread, same pattern as the
heartbeat loop. A dashboard failing to bind is a non-event for Ada's actual
job (talking to you), so it's caught and reported, never fatal.
"""

import threading

PORT = 4173


def start() -> threading.Thread | None:
    from ada.face.app import app

    def serve() -> None:
        try:
            app.run(host="127.0.0.1", port=PORT, threaded=True, use_reloader=False)
        except OSError as e:
            print(f"\n[face: couldn't start dashboard on port {PORT}: {e}]", flush=True)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    print(f"Face running at http://127.0.0.1:{PORT}\n")
    return thread
