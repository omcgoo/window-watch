#!/usr/bin/env python3
"""
Persistent scheduler for Window Watch.
Runs every 30 minutes for state-change checks; sends morning brief at 08:10.
TZ=Europe/London in the environment ensures the 08:10 fires at BST/GMT correctly.
State is persisted to STATE_FILE (default /data/state.json on Fly).

Also serves a tiny HTTP API on port 8080:
  POST /refresh  (Authorization: Bearer <REFRESH_TOKEN>)
    Triggers an immediate check and returns 200 when done.
"""
import os
import sys
import time
import threading
import schedule
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

os.environ.setdefault("STATE_FILE", "/data/state.json")
os.environ.setdefault("CALIBRATION_FILE", "/data/calibration.json")
os.environ.setdefault("WINDOW_FILE", "/data/window_report.json")

import window_watch as ww

REFRESH_TOKEN = os.getenv("REFRESH_TOKEN", "")

# Prevent concurrent runs (scheduled + manual refresh at the same moment)
_lock = threading.Lock()


def check():
    os.environ.pop("DAILY_SUMMARY", None)
    with _lock:
        try:
            ww.main()
        except Exception as e:
            print(f"[error] check failed: {e}", file=sys.stderr)


def morning_brief():
    os.environ["DAILY_SUMMARY"] = "true"
    with _lock:
        try:
            ww.main()
        except Exception as e:
            print(f"[error] morning brief failed: {e}", file=sys.stderr)
        finally:
            os.environ.pop("DAILY_SUMMARY", None)


class RefreshHandler(BaseHTTPRequestHandler):
    def _cors(self):
        # Dashboard is served cross-origin (GitHub Pages -> fly.dev), so it sends a
        # CORS preflight before the POST. Allow it, plus the Authorization header.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path not in ("/refresh", "/window"):
            self.send_response(404)
            self.end_headers()
            return

        auth = self.headers.get("Authorization", "")
        if REFRESH_TOKEN and auth != f"Bearer {REFRESH_TOKEN}":
            self.send_response(401)
            self._cors()
            self.end_headers()
            return

        if parsed.path == "/window":
            # Record the user's reported actual window state, e.g. /window?state=open|shut.
            state = (parse_qs(parsed.query).get("state") or [""])[0]
            ok = ww.record_window_state(state)
            self.send_response(200 if ok else 400)
            self._cors()
            self.end_headers()
            return

        self.send_response(200)
        self._cors()
        self.end_headers()
        # Run in background so HTTP response returns immediately
        threading.Thread(target=check, daemon=True).start()

    def log_message(self, fmt, *args):
        print(f"[http] {fmt % args}")


def serve():
    server = HTTPServer(("0.0.0.0", 8080), RefreshHandler)
    print("HTTP server listening on :8080")
    server.serve_forever()


# Refit the model once on startup so the accuracy figure is fresh after a deploy
# (otherwise it'd only refresh at the next 08:10 brief). Cheap and idempotent.
try:
    ww.calibrate_from_history()
except Exception as e:
    print(f"[warn] startup calibration failed: {e}", file=sys.stderr)

# Run once immediately on startup so the dashboard is fresh
check()

schedule.every(30).minutes.do(check)
schedule.every().day.at("08:10").do(morning_brief)

threading.Thread(target=serve, daemon=True).start()

print("Window Watch runner started — checking every 30 min, brief at 08:10.")
while True:
    schedule.run_pending()
    time.sleep(15)
