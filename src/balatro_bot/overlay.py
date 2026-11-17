"""The stream overlay: a sidebar page for an OBS Browser Source showing what the model
weighs before each move, its outlook, and the stream's record, read from the player's
``now.json``.

    .venv\\Scripts\\python.exe -m balatro_bot.overlay [--dir bot_log\\model_stream]

``balatro_bot.stream`` serves it on OVERLAY_PORT while a stream runs. In OBS, add a
Browser Source at http://localhost:12341/ sized 480x1080.
"""

from __future__ import annotations

import argparse
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

OVERLAY_PORT = 12341
PAGE = Path(__file__).parent / "assets" / "overlay.html"
FILES = {"/now.json": "now.json", "/status.json": "status.json"}


class _Handler(BaseHTTPRequestHandler):
    folder: Path

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/", "/index.html"):
                body, kind = PAGE.read_bytes(), "text/html; charset=utf-8"
            elif path in FILES:
                body, kind = (self.folder / FILES[path]).read_bytes(), "application/json"
            else:
                self.send_error(404)
                return
        except FileNotFoundError:
            self.send_error(404)
            return
        except OSError:  # caught mid-replace; the page asks again shortly
            self.send_error(503)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args) -> None:
        pass


def serve(folder: Path, port: int = OVERLAY_PORT) -> ThreadingHTTPServer:
    """The overlay served from ``folder`` on a background thread; ``shutdown()`` ends it.
    Raises OSError when the port is taken."""
    handler = type("Handler", (_Handler,), {"folder": Path(folder)})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> int:
    from balatro_bot.supervisor import LOG_DIR

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dir", type=Path, default=LOG_DIR / "model_stream")
    ap.add_argument("--port", type=int, default=OVERLAY_PORT)
    args = ap.parse_args()
    server = serve(args.dir, args.port)
    print(f"overlay on http://localhost:{args.port}/ from {args.dir}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
