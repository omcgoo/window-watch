#!/usr/bin/env python3
"""
Window Watch — buzz your phone when it's time to close (or reopen) the windows.

Logic: fixed outdoor temperature thresholds.
- Outdoor >= CLOSE_ABOVE  -> "close"  (shut windows/doors/blinds to hold the cool)
- Outdoor <= OPEN_BELOW   -> "open"   (outside is cool enough, let air through)
- in between              -> hold the previous state (hysteresis, stops flapping)

Sends an ntfy.sh push only when the state *changes*.
Updates a public Gist with current status for the dashboard widget.

Config via environment variables:
  LAT, LON          location (defaults to Bow, E3)
  WEATHER_MODEL     Open-Meteo model, default icon_d2 (2km resolution)
  CLOSE_ABOVE       °C at which to close up, default 22
  OPEN_BELOW        °C at which to reopen, default 20
  NTFY_TOPIC        your private ntfy topic (REQUIRED)
  NTFY_SERVER       default https://ntfy.sh
  GITHUB_TOKEN      if set, updates the dashboard Gist
  STATE_FILE        path to persist last state, default ./state.json
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

LAT = os.getenv("LAT") or "51.527"
LON = os.getenv("LON") or "-0.021"
WEATHER_MODEL = os.getenv("WEATHER_MODEL") or "icon_d2"
CLOSE_ABOVE = float(os.getenv("CLOSE_ABOVE") or "22")
OPEN_BELOW = float(os.getenv("OPEN_BELOW") or "20")
NTFY_TOPIC = os.getenv("NTFY_TOPIC")
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")
STATE_FILE = os.getenv("STATE_FILE", "state.json")
DASHBOARD_GIST_ID = "bd24c63bd7e129c86942db6ed67f9008"


def get_outdoor():
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={LAT}&longitude={LON}"
        f"&current=temperature_2m&temperature_unit=celsius&models={WEATHER_MODEL}"
    )
    with urllib.request.urlopen(url, timeout=20) as r:
        data = json.load(r)
    return float(data["current"]["temperature_2m"])


def decide(outdoor, last):
    if outdoor >= CLOSE_ABOVE:
        return "close"
    if outdoor <= OPEN_BELOW:
        return "open"
    return last or "open"


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f).get("status")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_state(status):
    with open(STATE_FILE, "w") as f:
        json.dump({"status": status}, f)


def notify(title, body, tags, priority="default"):
    if not NTFY_TOPIC:
        print("[error] NTFY_TOPIC not set — cannot send push", file=sys.stderr)
        return
    url = f"{NTFY_SERVER}/{urllib.parse.quote(NTFY_TOPIC)}"
    req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST")
    req.add_header("Title", title)
    req.add_header("Tags", tags)
    req.add_header("Priority", priority)
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


def update_dashboard(outdoor, status):
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        return
    payload = json.dumps({
        "files": {
            "window-watch-status.json": {
                "content": json.dumps({
                    "status": status,
                    "outdoor_c": outdoor,
                    "close_above_c": CLOSE_ABOVE,
                    "open_below_c": OPEN_BELOW,
                    "updated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }, indent=2)
            }
        }
    })
    req = urllib.request.Request(
        f"https://api.github.com/gists/{DASHBOARD_GIST_ID}",
        data=payload.encode(),
        method="PATCH",
    )
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
        print("Dashboard updated.")
    except Exception as e:
        print(f"[warn] Dashboard update failed: {e}", file=sys.stderr)


def main():
    if not NTFY_TOPIC:
        sys.exit("Set NTFY_TOPIC (your private ntfy topic name).")

    outdoor = get_outdoor()
    last = load_state()
    status = decide(outdoor, last)
    print(f"outdoor={outdoor:.1f}  close_above={CLOSE_ABOVE}  open_below={OPEN_BELOW}  last={last}  -> {status}")

    if status != last:
        if last is None:
            notify(
                "Window Watch running",
                f"Started. Outside {outdoor:.1f}°C (close above {CLOSE_ABOVE:.0f}°C, open below {OPEN_BELOW:.0f}°C). "
                f"Windows should be: {status}.",
                tags="house,white_check_mark",
            )
        elif status == "close":
            notify(
                "Close up now",
                f"Outside is {outdoor:.1f}°C — above your {CLOSE_ABOVE:.0f}°C threshold. "
                "Shut windows, doors and sun-side blinds.",
                tags="house,sunny",
                priority="high",
            )
        elif status == "open":
            notify(
                "Open up",
                f"Outside is {outdoor:.1f}°C — below your {OPEN_BELOW:.0f}°C threshold. "
                "Open windows to flush the heat out.",
                tags="house,leaves",
            )

    save_state(status)
    update_dashboard(outdoor, status)


if __name__ == "__main__":
    main()
