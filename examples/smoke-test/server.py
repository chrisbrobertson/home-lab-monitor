#!/usr/bin/env python3
"""Minimal HTTP service for slot workflow smoke test.
Responds to GET /health with JSON. Exits cleanly on SIGTERM.
"""
import json
import os
import signal
import socket
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("PORT", 8080))
SLOT_ID = os.environ.get("HLAB_SLOT_ID", "unknown")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health"):
            body = json.dumps({
                "status": "ok",
                "service": "hlab-smoke-test",
                "slot_id": SLOT_ID,
                "host": socket.gethostname(),
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, fmt, *args):
        pass  # silent


srv = HTTPServer(("0.0.0.0", PORT), Handler)
signal.signal(signal.SIGTERM, lambda *_: srv.shutdown())
print(f"smoke-test listening on :{PORT}  slot={SLOT_ID}", flush=True)
srv.serve_forever()
