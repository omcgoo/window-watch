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

os.environ.setdefault("STATE_FILE", "/data/state.json")

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
    def do_POST(self):
        if self.path != "/refresh":
            self.send_response(404)
            self.end_headers()
            return

        auth = self.headers.get("Authorization", "")
        if REFRESH_TOKEN and auth != f"Bearer {REFRESH_TOKEN}":
            self.send_response(401)
            self.end_headers()
            return

        self.send_response(200)
        self.end_headers()
        # Run in background so HTTP response returns immediately
        threading.Thread(target=check, daemon=True).start()

    def log_message(self, fmt, *args):
        print(f"[http] {fmt % args}")


def serve():
    server = HTTPServer(("0.0.0.0", 8080), RefreshHandler)
    print("HTTP server listening on :8080")
    server.serve_forever()


# Run once immediately on startup so the dashboard is fresh
check()

schedule.every(30).minutes.do(check)
schedule.every().day.at("08:10").do(morning_brief)

threading.Thread(target=serve, daemon=True).start()

print("Window Watch runner started — checking every 30 min, brief at 08:10.")
while True:
    schedule.run_pending()
    time.sleep(15)
