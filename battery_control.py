#!/usr/bin/env python3
"""Weekly 100% balance charge for the SolaX battery, via the Developer OpenAPI.

Why: the inverter's Max Charge SOC is capped at 95% to skip the slow 98-99% top-up
(~4 h/day at ~31% loss, measured Oct 2026). LFP cells balance near full, though, so once
a week the battery is pushed to 100% in Sunday's 13:00-16:00 Cosy cheap window.

The only thing this file ever does to the inverter is a *temporary* VPP override:
  start  -> POST inverter_vpp_mode/soc_target_control_mode  {targetSoc, chargeDischargPower}
  stop   -> POST inverter_vpp_mode/exit_vpp_mode
It deliberately never calls batch_set_spontaneity_self_use: that rewrites the whole
self-use config and accepts only two charge periods, and this inverter charges in all
three Cosy windows. Base settings stay as the user set them in the SolaX app.

Untested on an X1-Hybrid-LV when written (the reference client hard-blocks VPP writes),
hence the kill switch and the watchdog:
  BATTERY_CONTROL=off     nothing is sent (default). stop() still works, so a stuck
                          override can always be cleared.
  BATTERY_CONTROL=dryrun  guards and state run, payloads are printed, nothing is sent.
  BATTERY_CONTROL=on      live.
Every 10 min the watchdog checks the inverter is back in Normal (deviceStatus 102, from
solax.csv) after the window. If it's still in VPP Mode (130) it resends the exit, up to
3 times, then raises a high-priority alert with the manual fix.

Usage (on the Fly machine, from /app):
  python battery_control.py status
  python battery_control.py start [--target 100] [--force]   # --force skips the time window only
  python battery_control.py stop
  python battery_control.py watchdog
"""
import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
import urllib.request
from zoneinfo import ZoneInfo

import energy as en

MODE = (os.getenv("BATTERY_CONTROL") or "off").strip().lower()
STATE_FILE = os.path.join(en.DATA_DIR, "battery_control.json")
LOCAL = ZoneInfo("Europe/London")

START_PATH = "/openapi/v2/device/inverter_vpp_mode/soc_target_control_mode"
EXIT_PATH = "/openapi/v2/device/inverter_vpp_mode/exit_vpp_mode"
RESULT_PATH = "/openapi/apiRequestLog/listByCondition"

TARGET_SOC = 100
CHARGE_W = 3000                    # the fast-charge rate this battery takes from the grid
WINDOW = (dt.time(13, 0), dt.time(15, 30))   # latest sensible start inside the 13-16 cheap slot
WINDOW_END = dt.time(16, 0)        # peak starts; the override must be gone by then
FRESH = dt.timedelta(minutes=15)   # SolaX logs every 5 min; older than this = flying blind
STATUS_NORMAL, STATUS_VPP = 102, 130
CMD_OK, CMD_FAIL = {3, 4}, {1, 2, 5, 6}   # 3 issued, 4 executing; offline/failed/timed out
MAX_EXIT_ATTEMPTS = 3
RETRY_GAP = dt.timedelta(minutes=10)


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def _load():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_FILE)


def _notify(title, body, priority="default"):
    """Best-effort push via the advisory's ntfy helper; never let it break control."""
    try:
        import window_watch
        window_watch.notify(title, body, tags="battery", priority=priority)
    except Exception as e:
        print(f"[warn] battery_control notify failed: {e}", file=sys.stderr)


def _latest():
    """Newest SolaX reading as (age, deviceStatus, soc), or None if there isn't one."""
    row = en._last_row(en.SOLAX_FILE)
    if not row or not row.get("timestamp"):
        return None
    try:
        status = int(float(row["deviceStatus"])) if row.get("deviceStatus") not in (None, "") else None
        soc = float(row["batterySOC"]) if row.get("batterySOC") not in (None, "") else None
        return _now() - _parse(row["timestamp"]), status, soc
    except (ValueError, KeyError):
        return None


# ---- SolaX writes -----------------------------------------------------------------------

def _solax_post(path, body, _retried=False):
    req = urllib.request.Request(
        f"{en.SOLAX_BASE}{path}", data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"bearer {en._solax_token_get()}",
                 "Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        resp = json.load(r)
    code = resp.get("code")
    if code in en.SOLAX_AUTH_ERRORS and not _retried:
        en._solax_token_get(force=True)
        return _solax_post(path, body, _retried=True)
    return resp


def _find(obj, keys):
    """First value under any of `keys`, searching nested dicts/lists. The OpenAPI's result
    shapes for control calls aren't documented, so look rather than assume."""
    if isinstance(obj, dict):
        for k in keys:
            if obj.get(k) not in (None, ""):
                return obj[k]
        obj = list(obj.values())
    if isinstance(obj, list):
        for v in obj:
            found = _find(v, keys)
            if found is not None:
                return found
    return None


def _command(path, body, wait_s=120, poll_s=15):
    """Send one control command and wait for SolaX's verdict on it.
    Returns (status, request_id): status is 1-6 when SolaX reported one, None if it didn't."""
    resp = _solax_post(path, body)
    if resp.get("code") not in (en.SOLAX_OK_API, en.SOLAX_OK_TOKEN):
        raise RuntimeError(f"SolaX {path}: {resp.get('code')} {resp.get('message')}")
    req_id = _find(resp, ("requestId", "request_id")) or (
        resp.get("result") if isinstance(resp.get("result"), (str, int)) else None)
    print(f"battery_control: {path} accepted, requestId={req_id}, response={json.dumps(resp)[:300]}")
    if req_id is None:
        return None, None
    status, deadline = None, time.time() + wait_s
    while time.time() < deadline:
        time.sleep(poll_s)
        try:
            res = _solax_post(RESULT_PATH, {"requestId": str(req_id)})
            s = _find(res.get("result"), ("status", "commandStatus", "executeStatus", "executionStatus"))
            status = int(s) if s is not None else None
        except Exception as e:
            print(f"[warn] battery_control result poll: {e}", file=sys.stderr)
        if status in CMD_OK | CMD_FAIL and status != 3:   # 3 = issued; wait for 4 or a failure
            break
    return status, req_id


def _sn():
    sns = en._solax_discover().get("inverter") or []
    if not sns:
        raise RuntimeError("no inverter visible to the SolaX app")
    return sns[0]


# ---- The three jobs ---------------------------------------------------------------------

def start(target=TARGET_SOC, force=False):
    """Begin the balance charge. Every guard failing is a clean no-op, logged with why."""
    try:
        if MODE == "off":
            return _skip("start", "BATTERY_CONTROL=off")
        local = _now().astimezone(LOCAL)
        if not force and not (WINDOW[0] <= local.time() < WINDOW[1]):
            return _skip("start", f"outside {WINDOW[0]:%H:%M}-{WINDOW[1]:%H:%M} ({local:%H:%M})")
        state = _load()
        if state.get("phase") in ("charging", "exiting"):
            return _skip("start", f"already {state['phase']} since {state.get('started_utc')}")
        latest = _latest()
        if not latest or latest[0] > FRESH:
            return _skip("start", "no SolaX reading in the last 15 min")
        age, status, soc = latest
        if status != STATUS_NORMAL:
            return _skip("start", f"inverter status {status}, not Normal ({STATUS_NORMAL})")
        if soc is not None and soc >= target:
            return _skip("start", f"battery already at {soc:.0f}%")

        body = {"snList": [_sn()], "targetSoc": int(target), "chargeDischargPower": CHARGE_W,
                "businessType": en.SOLAX_BUSINESS_TYPE}
        if MODE != "on":
            print(f"battery_control DRY-RUN start: POST {START_PATH} {json.dumps(body)}")
            en._status("battery_control", True, f"dry-run start at SOC {soc}: {json.dumps(body)}")
            return "dryrun"
        cmd, req_id = _command(START_PATH, body)
        if cmd in CMD_FAIL:
            en._status("battery_control", False, f"start rejected, command status {cmd}")
            _notify("Battery balance charge didn't start",
                    f"SolaX rejected the weekly 100% charge (status {cmd}). Nothing changed on the inverter.")
            return "failed"
        _save({"phase": "charging", "started_utc": _iso(_now()), "start_soc": soc,
               "request_id": req_id, "start_cmd_status": cmd, "exit_attempts": 0})
        en._status("battery_control", True, f"start sent at SOC {soc}, command status {cmd}")
        _notify("Battery balance charge started",
                f"Charging to {target}% until 16:00 (currently {soc:.0f}%).", priority="low")
        return "started"
    except Exception as e:
        print(f"[warn] battery_control start failed: {e}", file=sys.stderr)
        en._status("battery_control", False, f"start: {e}")
        return "error"


def stop(force=False):
    """End the override. Allowed even with BATTERY_CONTROL=off, so it can always be undone."""
    try:
        state = _load()
        if not force and state.get("phase") not in ("charging", "exiting"):
            return _skip("stop", "no balance charge active")
        if MODE == "dryrun":
            print(f"battery_control DRY-RUN stop: POST {EXIT_PATH}")
            return "dryrun"
        body = {"snList": [_sn()], "businessType": en.SOLAX_BUSINESS_TYPE}
        cmd, req_id = _command(EXIT_PATH, body)
        state.update(phase="exiting", last_exit_utc=_iso(_now()), last_exit_status=cmd,
                     exit_attempts=state.get("exit_attempts", 0) + 1)
        state.setdefault("started_utc", _iso(_now()))
        _save(state)
        en._status("battery_control", cmd not in CMD_FAIL, f"exit sent, command status {cmd}")
        return "exiting"
    except Exception as e:
        print(f"[warn] battery_control stop failed: {e}", file=sys.stderr)
        en._status("battery_control", False, f"stop: {e}")
        return "error"


def watchdog():
    """Make sure the override ends: stop it if 15:55 was missed, confirm Normal after exit,
    retry the exit, and alert a human if it won't let go."""
    try:
        state = _load()
        phase = state.get("phase")
        if phase not in ("charging", "exiting"):
            return None
        now = _now()
        started = _parse(state["started_utc"])
        past_window = (now.astimezone(LOCAL).time() >= WINDOW_END or
                       now.astimezone(LOCAL).date() != started.astimezone(LOCAL).date())
        if phase == "charging":
            return stop() if past_window else None

        latest = _latest()
        if latest and latest[0] <= FRESH and latest[1] == STATUS_NORMAL:
            peak, mins = _summary(started)
            state.update(phase="done", done_utc=_iso(now), peak_soc=peak, minutes_at_99=mins)
            _save(state)
            en._status("battery_control", True, f"done: peak {peak}%, {mins} min at >=99%")
            _notify("Battery balance charge done",
                    f"Peaked at {peak}%, {mins} min at 99% or above. Back to normal.", priority="low")
            return "done"

        last_exit = _parse(state.get("last_exit_utc") or state["started_utc"])
        if now - last_exit < RETRY_GAP:
            return "waiting"
        if state.get("exit_attempts", 0) < MAX_EXIT_ATTEMPTS:
            return stop()
        if not state.get("alerted"):
            state["alerted"] = _iso(now)
            _save(state)
            seen = f"status {latest[1]}" if latest else "no recent reading"
            en._status("battery_control", False, f"stuck after {MAX_EXIT_ATTEMPTS} exits ({seen})")
            _notify("Battery stuck in remote-control mode",
                    "The weekly charge didn't release after 3 tries "
                    f"({seen}). In the SolaX app, set the work mode back to Self Use. "
                    "Until then the battery may not discharge.", priority="high")
        return "alerted"
    except Exception as e:
        print(f"[warn] battery_control watchdog failed: {e}", file=sys.stderr)
        en._status("battery_control", False, f"watchdog: {e}")
        return "error"


def _summary(since):
    """Peak SOC and minutes at >=99% in solax.csv since the charge started."""
    peak, mins = None, 0
    try:
        with open(en.SOLAX_FILE, newline="") as f:
            for r in csv.DictReader(f):
                if not r.get("timestamp") or _parse(r["timestamp"]) < since:
                    continue
                try:
                    soc = float(r["batterySOC"])
                except (TypeError, ValueError):
                    continue
                peak = soc if peak is None else max(peak, soc)
                mins += 5 if soc >= 99 else 0
    except OSError:
        pass
    return (int(peak) if peak is not None else None), mins


def _skip(job, why):
    print(f"battery_control {job}: skipped — {why}")
    return "skipped"


def status():
    latest = _latest()
    return {"mode": MODE, "state": _load(),
            "latest": None if not latest else {"age_min": round(latest[0].total_seconds() / 60, 1),
                                               "deviceStatus": latest[1], "soc": latest[2]}}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", choices=["start", "stop", "watchdog", "status"])
    p.add_argument("--target", type=int, default=TARGET_SOC)
    p.add_argument("--force", action="store_true",
                   help="start: skip the time-window guard; stop: send exit even with no state")
    a = p.parse_args()
    if a.action == "start":
        print(start(a.target, a.force))
    elif a.action == "stop":
        print(stop(a.force))
    elif a.action == "watchdog":
        print(watchdog())
    print(json.dumps(status(), indent=1))
