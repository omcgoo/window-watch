#!/usr/bin/env python3
"""
Persistent scheduler for Window Watch.
Runs every 30 minutes for state-change checks; sends morning brief at 08:10.
TZ=Europe/London in the environment ensures the 08:10 fires at BST/GMT correctly.
State is persisted to STATE_FILE (default /data/state.json on Fly).
"""
import os
import time
import schedule

os.environ.setdefault("STATE_FILE", "/data/state.json")

import window_watch as ww


def check():
    os.environ.pop("DAILY_SUMMARY", None)
    try:
        ww.main()
    except Exception as e:
        import sys
        print(f"[error] check failed: {e}", file=sys.stderr)


def morning_brief():
    os.environ["DAILY_SUMMARY"] = "true"
    try:
        ww.main()
    finally:
        os.environ.pop("DAILY_SUMMARY", None)


# Run once immediately on startup so the dashboard is fresh
check()

schedule.every(30).minutes.do(check)
schedule.every().day.at("08:10").do(morning_brief)

print("Window Watch runner started — checking every 30 min, brief at 08:10.")
while True:
    schedule.run_pending()
    time.sleep(15)
