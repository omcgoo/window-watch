#!/usr/bin/env python3
"""
Window Watch — buzz your phone when it's time to open or close the windows.

Core logic: is it hotter inside than outside?
- outdoor > indoor_est + HYSTERESIS  -> "close"  (outside adding heat, shut up)
- outdoor < indoor_est - HYSTERESIS  -> "open"   (outside cooler, flush the heat out)
- in between                         -> hold the previous state

indoor_est is computed each run using a first-order thermal lag model fed by today's
forecast. THERMAL_ALPHA controls heat bleed-in per hour; SOLAR_GAIN adds daytime load
for south-facing rooms. When a Shelly (or other indoor sensor) is wired up, replace
estimate_indoor() with a real reading — the rest of the logic is identical.

Sends an ntfy.sh push only when the state *changes*.
At 8:10am BST (DAILY_SUMMARY=true) sends a morning forecast brief.
Updates a public Gist with current status for the dashboard/widget.

Config via environment variables:
  LAT, LON          location (defaults to Bow, E3)
  WEATHER_MODEL     Open-Meteo model, default icon_d2 (2km resolution)
  CLOSE_ABOVE       °C used for forecast close-hour prediction, default 25
  THERMAL_ALPHA     heat conductance per hour [0–1], default 0.18
  INDOOR_BASE       overnight cool-down target °C, default 19
  SOLAR_GAIN        daytime solar load added for south-facing rooms, default 3
  HYSTERESIS        dead band °C to prevent flapping, default 1.5
  NTFY_TOPIC        your private ntfy topic (REQUIRED)
  NTFY_SERVER       default https://ntfy.sh
  GITHUB_TOKEN      if set, updates the dashboard Gist
  DAILY_SUMMARY     if "true", sends morning forecast instead of state-change check
  STATE_FILE        path to persist last state, default ./state.json
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

LAT = os.getenv("LAT") or "51.527"
LON = os.getenv("LON") or "-0.021"
WEATHER_MODEL = os.getenv("WEATHER_MODEL") or "icon_d2"
CLOSE_ABOVE = float(os.getenv("CLOSE_ABOVE") or "25")    # still used for forecast_close_hour
INDOOR_BASE = float(os.getenv("INDOOR_BASE") or "19.0")        # overnight cool-down target (fallback start when no sensor)
HYSTERESIS = float(os.getenv("HYSTERESIS") or "1.0")           # reopen dead band — only reopen once genuinely cooler
CLOSE_LEAD = float(os.getenv("CLOSE_LEAD") or "0.5")           # anticipation margin — close this many °C before the crossover while outdoor is still climbing

# Thermal model parameters, per regime. Each hour:
#   indoor += a * (outdoor - indoor) + b * solar_wm2
# 'a' is conductance (how fast the room follows outside); 'b' is solar coupling.
# Open windows = fast follow + strong solar; closed = the building envelope insulates
# and blinds cut solar — how *well* it insulates depends on the building.
#
# BUILDING_PROFILE picks the starting point: "add your building, learn going forward".
# These presets are only seeds — calibrate_from_history() overrides each regime with a
# fit to your flat's own recorded behaviour once enough data accumulates. The key
# difference is the *closed* regime: an insulated flat barely warms once shut; an
# uninsulated one (solid wall, single glazing) keeps leaking heat in even closed.
# A future app would surface this as a one-time onboarding choice.
BUILDING_PROFILES = {
    "uninsulated": {   # older solid-wall / single glazing — heat still seeps in when shut
        "label":  "Older / uninsulated (solid wall, single glazing)",
        "open":   {"a": 0.20, "b": 0.00080, "n": 0},
        "closed": {"a": 0.10, "b": 0.00040, "n": 0},
    },
    "insulated": {     # modern envelope, double glazing — closing really holds the cool
        "label":  "Modern / insulated (cavity or EWI, double glazing)",
        "open":   {"a": 0.18, "b": 0.00065, "n": 0},
        "closed": {"a": 0.05, "b": 0.00022, "n": 0},
    },
}
BUILDING_PROFILE = os.getenv("BUILDING_PROFILE", "uninsulated")
_profile = BUILDING_PROFILES.get(BUILDING_PROFILE, BUILDING_PROFILES["uninsulated"])
CAL_DEFAULTS = {"open": dict(_profile["open"]), "closed": dict(_profile["closed"])}
MIN_CAL_SAMPLES = 24   # per regime before a fit is trusted over the profile seed
CALIBRATION_FILE = os.getenv("CALIBRATION_FILE", "calibration.json")
NTFY_TOPIC = os.getenv("NTFY_TOPIC")
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")
STATE_FILE = os.getenv("STATE_FILE", "state.json")
DASHBOARD_GIST_ID = "bd24c63bd7e129c86942db6ed67f9008"
DASHBOARD_URL = "https://voop-ollie.github.io/window-watch/"
SHELLY_AUTH_KEY = os.getenv("SHELLY_AUTH_KEY")
SHELLY_DEVICE_ID = os.getenv("SHELLY_DEVICE_ID")
SHELLY_SERVER = os.getenv("SHELLY_SERVER")


def get_outdoor():
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={LAT}&longitude={LON}"
        "&current=temperature_2m,relative_humidity_2m,apparent_temperature"
        ",wind_speed_10m,wind_gusts_10m,shortwave_radiation,precipitation,cloud_cover"
        f"&temperature_unit=celsius&wind_speed_unit=kmh&models={WEATHER_MODEL}"
    )
    with urllib.request.urlopen(url, timeout=20) as r:
        data = json.load(r)
    c = data["current"]
    return {
        "temp":       float(c["temperature_2m"]),
        "feels_like": float(c["apparent_temperature"]),
        "humidity":   int(c["relative_humidity_2m"]),
        "wind_kmh":   float(c["wind_speed_10m"]),
        "gusts_kmh":  float(c["wind_gusts_10m"]),
        "solar_wm2":  float(c["shortwave_radiation"]),
        "precip_mm":  float(c["precipitation"]),
        "cloud_pct":  int(c["cloud_cover"]),
    }


def get_forecast():
    """Return list of (hour_int, temp_c, solar_wm2) for today in Europe/London time."""
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={LAT}&longitude={LON}"
        f"&hourly=temperature_2m,shortwave_radiation&temperature_unit=celsius&models={WEATHER_MODEL}"
        f"&forecast_days=1&timezone=Europe%2FLondon"
    )
    with urllib.request.urlopen(url, timeout=20) as r:
        data = json.load(r)
    h = data["hourly"]
    times = h["time"]   # e.g. "2026-06-21T14:00"
    temps = h["temperature_2m"]
    solar = h.get("shortwave_radiation") or [0.0] * len(temps)
    return [
        (int(t.split("T")[1][:2]), float(tp), float(sw or 0.0))
        for t, tp, sw in zip(times, temps, solar)
    ]


def load_calibration():
    """Load fitted thermal params from the volume, falling back to defaults per regime.

    A regime only uses its fitted (a, b) once it has at least MIN_CAL_SAMPLES of
    real history behind it — until then it rides the hand-picked defaults.
    """
    cal = {k: dict(v) for k, v in CAL_DEFAULTS.items()}
    try:
        with open(CALIBRATION_FILE) as f:
            fitted = json.load(f)
        for regime in cal:
            f_r = fitted.get(regime)
            if f_r and f_r.get("n", 0) >= MIN_CAL_SAMPLES:
                cal[regime] = {"a": f_r["a"], "b": f_r["b"], "n": f_r["n"]}
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass
    return cal


def thermal_step(indoor, outdoor, solar, closed, cal):
    """Advance indoor temperature one hour under the given regime."""
    p = cal["closed" if closed else "open"]
    return indoor + p["a"] * (outdoor - indoor) + p["b"] * (solar or 0.0)


def simulate_indoor_day(forecast, cal):
    """Full-day indoor sim from the overnight low — fallback when no live sensor.

    Regime-aware: once outdoor passes indoor we assume the windows are shut (the
    app's own advice), so the insulated 'closed' params take over.
    """
    overnight = [t for h, t, _s in forecast if h <= 5]
    start = min(overnight) if overnight else INDOOR_BASE
    indoor = min(start, INDOOR_BASE)
    result = []
    for h, outdoor, solar in forecast:
        closed = outdoor >= indoor
        indoor = thermal_step(indoor, outdoor, solar if 7 <= h <= 19 else 0.0, closed, cal)
        result.append((h, round(indoor, 1)))
    return result


def estimate_indoor(forecast, cal):
    """Return estimated indoor temp at the current local hour (sensor fallback)."""
    current_hour = datetime.now(timezone.utc).hour + 1  # UTC+1 approximates BST
    indoor = INDOOR_BASE
    for h, est in simulate_indoor_day(forecast, cal):
        indoor = est
        if h >= current_hour:
            break
    return indoor


def project_indoor(forecast, indoor_now, from_hour, cal):
    """Project indoor temp forward from a *real* reading at from_hour.

    Anchored to the live sensor, and regime-aware: each hour we assume the windows
    are shut whenever it's warmer outside than in (i.e. you followed the close
    advice), so the afternoon tracks the insulated 'closed' model — and the
    evening reopen prediction reflects a flat that actually held its cool.
    Hours before from_hour are returned as None (no projection backwards).
    """
    indoor = indoor_now
    result = []
    for h, outdoor, solar in forecast:
        if h < from_hour:
            result.append((h, None))
            continue
        if h > from_hour:
            closed = outdoor >= indoor
            indoor = thermal_step(indoor, outdoor, solar if 7 <= h <= 19 else 0.0, closed, cal)
        result.append((h, round(indoor, 1)))
    return result


def forecast_windows(forecast, cal, indoor_now=None):
    """Return (close_hour, open_hour, max_temp, peak_hour) for today.

    The close moment is the *crossover* — when outdoor first rises to meet indoor
    (no hysteresis: being late traps warm air). The open moment is the first hour
    after the peak where outdoor falls a full HYSTERESIS below indoor (patient, so
    we don't reopen on a brief dip). When a live indoor reading is supplied the
    indoor curve is projected forward from it; otherwise the model-only sim is used.
    """
    max_temp = max(t for _, t, _s in forecast)
    peak_hour = next(h for h, t, _s in forecast if t == max_temp)
    if indoor_now is not None:
        current_hour = datetime.now(timezone.utc).hour + 1  # UTC+1 approximates BST
        curve = project_indoor(forecast, indoor_now, current_hour, cal)
        start = current_hour
    else:
        curve = simulate_indoor_day(forecast, cal)
        start = 6
    close_hour = next(
        (h for (h, t_out, _s), (_, t_in) in zip(forecast, curve)
         if t_in is not None and h >= start and t_out >= t_in),
        None,
    )
    open_hour = next(
        (h for (h, t_out, _s), (_, t_in) in zip(forecast, curve)
         if t_in is not None and h > (peak_hour or 0) and t_out <= t_in - HYSTERESIS),
        None,
    )
    return close_hour, open_hour, max_temp, peak_hour


def calibrate_from_history():
    """Fit per-regime thermal params (a, b) from the recorded history CSV.

    For each pair of consecutive same-regime samples (regime taken from the status
    recommended at the time — i.e. assuming you followed the advice), least-squares
    fit the hourly step:  Δindoor = a·(outdoor − indoor)·Δt + b·solar·Δt
    The result is written to CALIBRATION_FILE; regimes with < MIN_CAL_SAMPLES of
    data are skipped and keep their defaults (see load_calibration). Over time, as
    closed-window days accumulate, the closed model learns how well the flat holds.
    """
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        return
    base = f"https://api.github.com/gists/{DASHBOARD_GIST_ID}"
    req = urllib.request.Request(base)
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github+json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            gist = json.load(r)
        csv = gist["files"].get("window-watch-history.csv", {}).get("content", "")
    except Exception as e:
        print(f"[warn] Calibration fetch failed: {e}", file=sys.stderr)
        return

    rows = []
    for line in csv.strip().splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 13:
            continue
        try:
            ts = datetime.strptime(parts[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            outdoor, solar = float(parts[1]), float(parts[6] or 0)
            indoor, status = float(parts[9]), parts[12]
        except (ValueError, IndexError):
            continue
        rows.append((ts, outdoor, solar, indoor, status))

    acc = {r: dict(S11=0.0, S12=0.0, S22=0.0, Sy1=0.0, Sy2=0.0, n=0)
           for r in ("open", "closed")}
    for (t0, o0, s0, i0, st0), (t1, _o1, _s1, i1, _st1) in zip(rows, rows[1:]):
        dt = (t1 - t0).total_seconds() / 3600.0
        if dt <= 0 or dt > 1.5:          # skip overnight gaps and duplicate runs
            continue
        regime = "closed" if st0 == "close" else "open"
        x1, x2, y = (o0 - i0) * dt, s0 * dt, i1 - i0
        a = acc[regime]
        a["S11"] += x1 * x1; a["S12"] += x1 * x2; a["S22"] += x2 * x2
        a["Sy1"] += x1 * y;  a["Sy2"] += x2 * y;  a["n"] += 1

    fitted = {}
    for regime, a in acc.items():
        det = a["S11"] * a["S22"] - a["S12"] * a["S12"]
        if a["n"] < MIN_CAL_SAMPLES or abs(det) < 1e-9:
            continue
        coef_a = (a["Sy1"] * a["S22"] - a["Sy2"] * a["S12"]) / det
        coef_b = (a["S11"] * a["Sy2"] - a["S12"] * a["Sy1"]) / det
        coef_a = max(0.01, min(0.6, coef_a))     # clamp to physically sane ranges
        coef_b = max(0.0, min(0.005, coef_b))
        fitted[regime] = {"a": round(coef_a, 5), "b": round(coef_b, 7), "n": a["n"]}

    if not fitted:
        print("Calibration: not enough data yet — keeping defaults.")
        return
    try:
        with open(CALIBRATION_FILE, "w") as f:
            json.dump(fitted, f, indent=2)
        print(f"Calibration updated: {fitted}")
    except Exception as e:
        print(f"[warn] Calibration save failed: {e}", file=sys.stderr)


def get_indoor_shelly():
    """Return live indoor readings from Shelly Cloud API, or None on failure."""
    if not (SHELLY_AUTH_KEY and SHELLY_DEVICE_ID and SHELLY_SERVER):
        return None
    url = f"https://{SHELLY_SERVER}/device/status"
    data = urllib.parse.urlencode({"auth_key": SHELLY_AUTH_KEY, "id": SHELLY_DEVICE_ID}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.load(r)
        if not resp.get("isok"):
            return None
        s = resp["data"]["device_status"]
        return {
            "temp":     float(s["temperature:0"]["tC"]),
            "humidity": float(s["humidity:0"]["rh"]),
            "battery":  int(s["devicepower:0"]["battery"]["percent"]),
        }
    except Exception as e:
        print(f"[warn] Shelly fetch failed: {e}", file=sys.stderr)
        return None


def decide(outdoor, indoor, last, rising=False):
    """Asymmetric decision: close eagerly, reopen patiently.

    Closing late is the expensive mistake — once outdoor passes indoor, every
    minute open pours heat in. So we close *at* the crossover, and a touch early
    (CLOSE_LEAD) while outdoor is still climbing toward it. Reopening only happens
    once outdoor is a full HYSTERESIS below indoor, so a brief dip won't flap us.
    """
    close_thresh = indoor - (CLOSE_LEAD if rising else 0.0)
    if outdoor >= close_thresh:
        return "close"
    if outdoor <= indoor - HYSTERESIS:
        return "open"
    return last or "open"


def load_state():
    """Return the full persisted state dict ({} if missing/corrupt).

    Holds: status, day, close_hour, open_hour, closed_today, reopened_today.
    The hour fields let us freeze today's close/open times once they've passed
    instead of recomputing a drifting 'next crossover from now' each run.
    """
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def local_today():
    """Today's date in Europe/London (UTC+1 approximates BST, matching the rest)."""
    return (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%d")


def notify(title, body, tags, priority="default"):
    if not NTFY_TOPIC:
        print("[error] NTFY_TOPIC not set — cannot send push", file=sys.stderr)
        return
    url = f"{NTFY_SERVER}/{urllib.parse.quote(NTFY_TOPIC)}"
    req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST")
    req.add_header("Title", title)
    req.add_header("Tags", tags)
    req.add_header("Priority", priority)
    req.add_header("Click", DASHBOARD_URL)
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


def update_dashboard(outdoor, status, indoor_est_c=None, forecast_max=None, forecast_peak_hour=None, forecast_close_hour=None, forecast_open_hour=None, forecast_hourly=None):
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        return
    payload = json.dumps({
        "files": {
            "window-watch-status.json": {
                "content": json.dumps({
                    "status": status,
                    "outdoor_c": outdoor,
                    "indoor_est_c": indoor_est_c,
                    "forecast_max_c": forecast_max,
                    "forecast_peak_hour": forecast_peak_hour,
                    "forecast_close_hour": forecast_close_hour,
                    "forecast_open_hour": forecast_open_hour,
                    "forecast_hourly": forecast_hourly,
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


HISTORY_HEADER = (
    "timestamp,outdoor_c,feels_like_c,outdoor_humidity_pct,"
    "wind_kmh,gusts_kmh,solar_wm2,cloud_pct,precip_mm,"
    "indoor_c,indoor_humidity_pct,battery_pct,status\n"
)

def log_history(outdoor_data, indoor_c, indoor_humidity, battery_pct, status):
    """Append a CSV row to the history file in the dashboard Gist."""
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        return
    base = f"https://api.github.com/gists/{DASHBOARD_GIST_ID}"
    req = urllib.request.Request(base)
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github+json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            gist = json.load(r)
        existing = gist["files"].get("window-watch-history.csv", {}).get("content", HISTORY_HEADER)
        if not existing.startswith("timestamp"):
            existing = HISTORY_HEADER
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = (
            f"{ts},"
            f"{outdoor_data['temp']},"
            f"{outdoor_data['feels_like']},"
            f"{outdoor_data['humidity']},"
            f"{outdoor_data['wind_kmh']},"
            f"{outdoor_data['gusts_kmh']},"
            f"{outdoor_data['solar_wm2']},"
            f"{outdoor_data['cloud_pct']},"
            f"{outdoor_data['precip_mm']},"
            f"{indoor_c},"
            f"{indoor_humidity if indoor_humidity is not None else ''},"
            f"{battery_pct if battery_pct is not None else ''},"
            f"{status}\n"
        )
        import time
        for attempt in range(3):
            if attempt:
                time.sleep(2 ** attempt)
                with urllib.request.urlopen(req, timeout=20) as r:
                    gist = json.load(r)
                existing = gist["files"].get("window-watch-history.csv", {}).get("content", HISTORY_HEADER)
            payload = json.dumps({"files": {"window-watch-history.csv": {"content": existing + row}}})
            patch = urllib.request.Request(base, data=payload.encode(), method="PATCH")
            patch.add_header("Authorization", f"token {token}")
            patch.add_header("Accept", "application/vnd.github+json")
            patch.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(patch, timeout=20) as r:
                    r.read()
                print("History logged.")
                break
            except urllib.error.HTTPError as e:
                if e.code == 409 and attempt < 2:
                    continue
                raise
    except Exception as e:
        print(f"[warn] History log failed: {e}", file=sys.stderr)


def fmt_hour(h):
    if h == 0:   return "midnight"
    if h < 12:   return f"{h}am"
    if h == 12:  return "noon"
    return f"{h - 12}pm"


def fmt_hour_approx(h):
    """Round to nearest 2-hour slot — for forecast display where precision implies false accuracy."""
    r = round(h / 2) * 2
    if r == 0 or r == 24: return "midnight"
    if r < 12:  return f"{r}am"
    if r == 12: return "noon"
    return f"{r - 12}pm"


def daily_summary(outdoor, cal, indoor_now=None):
    try:
        forecast = get_forecast()
    except Exception as e:
        print(f"[warn] Forecast fetch failed: {e}", file=sys.stderr)
        return

    close_hour, open_hour, max_temp, max_hour = forecast_windows(forecast, cal, indoor_now)

    print(f"daily summary: max={max_temp:.1f}°C at {fmt_hour(max_hour)}  close={close_hour}  open={open_hour}  indoor_now={indoor_now}")

    if close_hour is not None:
        title = f"Close before {fmt_hour(close_hour)}"
        if open_hour is not None:
            title += f" · open around {fmt_hour(open_hour)}"
        body = (
            f"Peak {max_temp:.0f}°C around {fmt_hour(max_hour)}. "
            f"Shut windows before {fmt_hour(close_hour)}"
            + (f" and open up again around {fmt_hour(open_hour)}." if open_hour else " — may stay hot into the evening.")
        )
        notify(title, body, tags="house,sunny", priority="high")
    elif max_temp >= INDOOR_BASE + 3:
        notify(
            "Warm but manageable today",
            f"Max {max_temp:.0f}°C around {fmt_hour(max_hour)} — "
            f"won't hit the {CLOSE_ABOVE:.0f}°C threshold. Windows can stay open.",
            tags="house,thermometer",
        )
    else:
        notify(
            "Cool day — windows fine all day",
            f"Max only {max_temp:.0f}°C today. No need to close up.",
            tags="house,leaves",
        )


def main():
    if not NTFY_TOPIC:
        sys.exit("Set NTFY_TOPIC (your private ntfy topic name).")

    outdoor_data = get_outdoor()
    outdoor = outdoor_data["temp"]

    shelly = get_indoor_shelly()
    indoor_humidity = shelly["humidity"] if shelly else None
    indoor_battery  = shelly["battery"]  if shelly else None

    is_brief = os.getenv("DAILY_SUMMARY") == "true"
    if is_brief:
        calibrate_from_history()   # refit the thermal model from yesterday's data (once daily)
    cal = load_calibration()

    # Fetch today's forecast; derive indoor estimate and all forward-looking stats
    forecast_max = forecast_peak_hour = forecast_close_hour = forecast_open_hour = None
    forecast_hourly = None
    rising = False
    indoor_est = INDOOR_BASE
    try:
        forecast = get_forecast()
        indoor_sim = simulate_indoor_day(forecast, cal)
        indoor_est = (shelly["temp"] if shelly else None) or estimate_indoor(forecast, cal)
        forecast_hourly = [[h, t_out, t_in] for (h, t_out, _s), (_, t_in) in zip(forecast, indoor_sim)]
        # Predictions anchored to the live reading when we have one, else model-only
        forecast_close_hour, forecast_open_hour, forecast_max, forecast_peak_hour = forecast_windows(
            forecast, cal, shelly["temp"] if shelly else None
        )
        # Is outdoor still climbing? (drives anticipatory close)
        current_hour = datetime.now(timezone.utc).hour + 1
        temp_now = next((t for h, t, _s in forecast if h == current_hour), outdoor)
        temp_next = next((t for h, t, _s in forecast if h == current_hour + 1), temp_now)
        rising = temp_next >= temp_now
    except Exception as e:
        print(f"[warn] Forecast fetch failed: {e}", file=sys.stderr)

    # ---- State, decision, and frozen close/open times ----------------------
    # forecast_close_hour / forecast_open_hour above are the *fresh* predictions.
    # We only let them move the displayed times while the event is still ahead —
    # once you've actually closed, today's close time is frozen for reference, and
    # likewise the reopen time freezes the moment you reopen. This stops the close
    # time drifting to "now" as the day wears on.
    state = load_state()
    today = local_today()
    if state.get("day") != today:
        state = {"status": state.get("status"), "day": today,
                 "close_hour": None, "open_hour": None,
                 "closed_today": False, "reopened_today": False}

    last = state.get("status")
    status = decide(outdoor, indoor_est, last, rising)

    closed_today = state.get("closed_today", False)
    reopened_today = state.get("reopened_today", False)
    just_reopened = status == "open" and closed_today and not reopened_today

    if status == "open" and not closed_today and forecast_close_hour is not None:
        state["close_hour"] = forecast_close_hour          # refine while still pre-close
    if not reopened_today and not just_reopened and forecast_open_hour is not None:
        state["open_hour"] = forecast_open_hour            # refine while reopen still ahead

    if status == "close":
        state["closed_today"] = True
    if just_reopened:
        state["reopened_today"] = True

    display_close = state.get("close_hour")
    display_open = state.get("open_hour")

    print(f"outdoor={outdoor:.1f}  indoor_est={indoor_est}  last={last}  rising={rising}  -> {status}  (close={display_close} open={display_open})")

    if is_brief:
        daily_summary(outdoor, cal, shelly["temp"] if shelly else None)
        update_dashboard(outdoor, last or "open", indoor_est, forecast_max, forecast_peak_hour, display_close, display_open, forecast_hourly)
        save_state(state)
        return

    if status != last:
        if forecast_max is not None and forecast_peak_hour is not None:
            peak_ctx = f"peaks at {forecast_max:.0f}°C around {fmt_hour(forecast_peak_hour)}"
        else:
            peak_ctx = None

        # Use precise reading when Shelly is live; flag as estimate otherwise
        if shelly:
            indoor_label = f"{indoor_est:.1f}°C inside"
        else:
            indoor_label = f"~{indoor_est:.0f}°C estimated inside"

        if last is None:
            body = f"Outside {outdoor:.1f}°C, {indoor_label}. "
            if peak_ctx:
                body += f"Today {peak_ctx}. "
            body += f"Windows should be: {status}."
            notify("Window Watch running", body, tags="house,white_check_mark")

        elif status == "close":
            # Anticipatory if outdoor hasn't *quite* crossed indoor yet but is climbing
            if outdoor < indoor_est:
                lead = (
                    f"Outside {outdoor:.1f}°C, about to overtake {indoor_label} and still climbing. "
                    "Shut windows now to trap the cool while you still can."
                )
            else:
                lead = (
                    f"Outside {outdoor:.1f}°C — now warmer than {indoor_label}. "
                    "Shut windows, doors and blinds."
                )
            body = lead + (f" Today {peak_ctx}." if peak_ctx else "")
            notify("Close up now", body, tags="house,sunny", priority="high")

        elif status == "open":
            body = (
                f"Outside dropped to {outdoor:.1f}°C — cooler than {indoor_label}. "
                "Open up and flush the heat out."
            )
            notify("Open up", body, tags="house,leaves")

    state["status"] = status
    save_state(state)
    update_dashboard(outdoor, status, indoor_est, forecast_max, forecast_peak_hour, display_close, display_open, forecast_hourly)
    log_history(outdoor_data, indoor_est, indoor_humidity, indoor_battery, status)


if __name__ == "__main__":
    main()
