"""balatrobot's JSON-RPC API on Jackdaw, with the JackPotts fork's extra gamestate fields.

Usage (in a Python 3.12+ venv with jackdaw installed):
    python sim/jackdaw_adapter/serve.py [--port 12371]

The bot then runs unchanged: ``balatro-bot --port 12371 --start ...``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

from jackdaw.bridge import RPCError, SimBackend

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fork_view import augment  # noqa: E402


def handle(sim: SimBackend, body: bytes) -> dict:
    try:
        req = json.loads(body)
    except json.JSONDecodeError:
        return {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}, "id": None}
    try:
        result = sim.handle(req.get("method", ""), req.get("params"))
    except RPCError as e:
        return {"jsonrpc": "2.0", "id": req.get("id"),
                "error": {"code": e.code, "message": e.message, "data": e.data}}
    game = sim.game
    return {"jsonrpc": "2.0", "id": req.get("id"),
            "result": augment(result, game.to_dict() if game is not None else None)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=12371)
    args = parser.parse_args()
    sim = SimBackend()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            out = json.dumps(handle(sim, self.rfile.read(int(self.headers.get("Content-Length", 0))))).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    print(f"jackdaw adapter on http://{args.host}:{args.port}", flush=True)
    HTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
