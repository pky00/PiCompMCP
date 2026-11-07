# python -m picomp: per-monitor DPI awareness first (before pynput, mss or uvicorn
# are imported, so every pixel they report is physical), then the server on
# 127.0.0.1:8765. Ctrl+C runs the lifespan shutdown: the replay stops, the board
# gets R, a recording in progress is saved.
from picomp import win

try:
    win.setup_dpi()
except RuntimeError as e:
    raise SystemExit(str(e))

import logging
import secrets
import threading
import time
import webbrowser

import uvicorn

from picomp.controller import Controller
from picomp.web import create_app

HOST, PORT = "127.0.0.1", 8765


def open_browser_when_started(server, url):
    # uvicorn runs the lifespan startup before it binds the socket.
    while not server.started:
        if server.should_exit:
            return
        time.sleep(0.05)
    webbrowser.open(url)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = secrets.token_urlsafe(32)
    app = create_app(Controller(), token, PORT)
    url = f"http://{HOST}:{PORT}/auth?t={token}"
    print(f"PiComp GUI: {url}", flush=True)
    # No access log: the GUI polls /api/status twice a second.
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, access_log=False))
    threading.Thread(target=open_browser_when_started, args=(server, url), name="browser", daemon=True).start()
    try:
        server.run()
    except KeyboardInterrupt:
        pass  # uvicorn re-raises the Ctrl+C it handled once its graceful shutdown is done
    print("PiComp stopped", flush=True)


if __name__ == "__main__":
    main()
