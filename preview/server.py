#!/usr/bin/env python3
"""Preview server for the black hole instrument.

Serves the project folder and one small API. The page never encodes anything
itself: every control change goes through the real bridge (blackhole_instrument.step
and frame_bytes) and comes back as the exact escape sequences a terminal would
receive, so what you see is the bridge, the wire format and the shader decode
working together.

  python3 preview/server.py            # http://127.0.0.1:8765/preview/
"""

import http.server
import json
import os
import sys
import time
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import blackhole_instrument as bt  # noqa: E402

PORT = int(os.environ.get("BLACKHOLE_PREVIEW_PORT", "8765"))
STATE = {}


def num(query, name):
    try:
        return float(query[name][0])
    except (KeyError, ValueError, IndexError):
        return None


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")     # shader edits show on reload
        super().end_headers()

    def reply(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        global STATE
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        if url.path == "/api/set":
            burn, heat = num(query, "burn"), num(query, "heat")
            if burn is not None:
                STATE["burn_manual"] = bt.clamp(burn)
            if heat is not None:
                STATE["heat_manual"] = bt.clamp(heat)
            STATE.setdefault("burn_manual", bt.BURN_NEUTRAL)
            flare = None
            kind = (query.get("flare") or [""])[0]
            if kind in bt.FLARE_KINDS:
                mag = int(num(query, "mag") or 3)
                flare = (bt.FLARE_KINDS[kind], max(1, min(3, mag)))
            STATE, frame = bt.step(STATE, time.time(), fill=num(query, "fill"), flare=flare)
            self.reply({"seq": bt.frame_bytes(frame).decode("latin-1"), "frame": frame})
        elif url.path == "/api/off":
            STATE = {}
            self.reply({"seq": bt.reset_bytes().decode("latin-1"), "frame": None})
        elif url.path in ("/", "/preview"):
            self.send_response(302)
            self.send_header("Location", "/preview/")
            self.end_headers()
        else:
            super().do_GET()

    def do_POST(self):
        # /api/snapshot?name=idle  (body: PNG) -> docs/frames/idle.png, for README images
        url = urllib.parse.urlparse(self.path)
        name = (urllib.parse.parse_qs(url.query).get("name") or [""])[0]
        if url.path != "/api/snapshot" or not name or not all(c.isalnum() or c == "-" for c in name):
            self.send_error(404)
            return
        size = int(self.headers.get("Content-Length") or 0)
        if not 0 < size < 20_000_000:
            self.send_error(413)
            return
        frames = os.path.join(ROOT, "docs", "frames")
        os.makedirs(frames, exist_ok=True)
        with open(os.path.join(frames, name + ".png"), "wb") as f:
            f.write(self.rfile.read(size))
        self.reply({"saved": name})

    def log_message(self, fmt, *args):
        if "/api/" not in (args[0] if args else ""):
            super().log_message(fmt, *args)


if __name__ == "__main__":
    with http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler) as httpd:
        print("Black hole instrument preview: http://127.0.0.1:%d/preview/" % PORT, flush=True)
        httpd.serve_forever()
