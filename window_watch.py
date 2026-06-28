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
import math
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
MAX_INDOOR_RATE = float(os.getenv("MAX_INDOOR_RATE") or "4.0")  # °C/hr; a faster lurch between reads is a sensor glitch, not the room
SOLAR_MIN = float(os.getenv("SOLAR_MIN") or "250")             # W/m² shortwave that counts as real solar load on the glass
SOLAR_RISE = float(os.getenv("SOLAR_RISE") or "0.3")           # °C rise since the last reading that counts as indoor genuinely climbing

# Thermal/humidity model parameters, per regime. Each hour:
#   indoor   += a * (outdoor - indoor) + b * solar_wm2          (temperature)
#   e_indoor += c * (e_outdoor - e_indoor)                      (moisture / vapour pressure)
# 'a' is conductance (how fast the room follows outside temp); 'b' is solar coupling;
# 'c' is how fast indoor moisture relaxes toward outdoor moisture (air exchange).
# Open windows = fast follow + strong solar + fast moisture exchange; closed = the
# building envelope insulates, blinds cut solar, and a tighter shell holds its own air.
#
# BUILDING_PROFILE picks the starting point: "add your building, learn going forward".
# These presets are only seeds — calibrate_from_history() blends in a fit to your flat's
# own recorded behaviour, weighted by how much data backs it. The key difference is the
# *closed* regime: an insulated flat barely warms and holds its air once shut; an
# uninsulated one (solid wall, single glazing) keeps leaking heat and air in even closed.
# A future app would surface this as a one-time onboarding choice.
BUILDING_PROFILES = {
    "uninsulated": {   # older solid-wall / single glazing — heat & air still seep in when shut
        "label":  "Older / uninsulated (solid wall, single glazing)",
        "open":   {"a": 0.20, "b": 0.00080, "c": 0.50, "n": 0, "n_h": 0},
        "closed": {"a": 0.10, "b": 0.00040, "c": 0.15, "n": 0, "n_h": 0},
    },
    "insulated": {     # modern envelope, double glazing — closing really holds cool & air
        "label":  "Modern / insulated (cavity or EWI, double glazing)",
        "open":   {"a": 0.18, "b": 0.00065, "c": 0.45, "n": 0, "n_h": 0},
        "closed": {"a": 0.05, "b": 0.00022, "c": 0.08, "n": 0, "n_h": 0},
    },
}
BUILDING_PROFILE = os.getenv("BUILDING_PROFILE", "uninsulated")
_profile = BUILDING_PROFILES.get(BUILDING_PROFILE, BUILDING_PROFILES["uninsulated"])
CAL_DEFAULTS = {"open": dict(_profile["open"]), "closed": dict(_profile["closed"])}
# The seed acts as a prior worth this many observations. Each learned coefficient is
# blended toward its seed as  param = (n·fit + K·seed) / (n + K)  — pure seed with no
# data, asymptotically the raw fit as history grows. So accuracy improves continuously
# with more data and there's no hard cut-over: at n = K the fit and seed weigh equally.
CAL_PRIOR_WEIGHT = float(os.getenv("CAL_PRIOR_WEIGHT") or "24")
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
    """Return list of (hour_int, temp_c, solar_wm2, rh_pct) for today (Europe/London)."""
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={LAT}&longitude={LON}"
        f"&hourly=temperature_2m,shortwave_radiation,relative_humidity_2m&temperature_unit=celsius&models={WEATHER_MODEL}"
        f"&forecast_days=1&timezone=Europe%2FLondon"
    )
    with urllib.request.urlopen(url, timeout=20) as r:
        data = json.load(r)
    h = data["hourly"]
    times = h["time"]   # e.g. "2026-06-21T14:00"
    temps = h["temperature_2m"]
    solar = h.get("shortwave_radiation") or [0.0] * len(temps)
    rh = h.get("relative_humidity_2m") or [50.0] * len(temps)
    return [
        (int(t.split("T")[1][:2]), float(tp), float(sw or 0.0), float(rhv if rhv is not None else 50.0))
        for t, tp, sw, rhv in zip(times, temps, solar, rh)
    ]


def load_calibration():
    """Blend each regime's fitted params with its profile seed by sample count.

    Rather than a hard switch once enough data exists, every fitted coefficient is
    shrunk toward its seed:  param = (n·fit + K·seed) / (n + K)  (K = CAL_PRIOR_WEIGHT).
    With no history it's the pure seed; the more data accrues the closer it gets to the
    raw fit — so accuracy climbs continuously and without a cap. Temperature (a, b) is
    weighted by its own sample count; humidity (c) by the humidity sample count, since
    rows logged while the sensor was offline carry no real RH.
    """
    cal = {k: {"a": v["a"], "b": v["b"], "c": v["c"]} for k, v in CAL_DEFAULTS.items()}
    try:
        with open(CALIBRATION_FILE) as f:
            fitted = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return cal
    K = CAL_PRIOR_WEIGHT
    for regime, seed in CAL_DEFAULTS.items():
        f_r = fitted.get(regime) or {}
        n = f_r.get("n", 0)
        if n > 0:
            for p in ("a", "b"):
                if p in f_r:
                    cal[regime][p] = (n * f_r[p] + K * seed[p]) / (n + K)
        n_h = f_r.get("n_h", 0)
        if n_h > 0 and "c" in f_r:
            cal[regime]["c"] = (n_h * f_r["c"] + K * seed["c"]) / (n_h + K)
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
    overnight = [t for h, t, _s, _rh in forecast if h <= 5]
    start = min(overnight) if overnight else INDOOR_BASE
    indoor = min(start, INDOOR_BASE)
    result = []
    for h, outdoor, solar, _rh in forecast:
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


def saturation_vp(t_c):
    """Saturation vapour pressure (hPa) — Magnus formula."""
    return 6.112 * math.exp(17.62 * t_c / (243.12 + t_c))


def estimate_indoor_humidity(indoor_temp, outdoor_temp, outdoor_rh):
    """Fallback indoor RH for when the Shelly is offline.

    There's no humidity model, so we lean on physics: a flat's water-vapour
    content broadly tracks outdoors through air exchange, while RH is just that
    vapour pressure expressed against the (warmer) indoor temperature. So we take
    the outdoor vapour pressure and re-evaluate it at the estimated indoor temp.
    Indoor moisture sources (cooking, people, drying laundry) aren't captured, so
    this is approximate — good enough to keep the wet-bulb gauge alive, no more.
    """
    e_out = (outdoor_rh / 100.0) * saturation_vp(outdoor_temp)
    rh_in = e_out / saturation_vp(indoor_temp) * 100.0
    return max(1.0, min(99.0, rh_in))


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
    for h, outdoor, solar, _rh in forecast:
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
    max_temp = max(t for _, t, _s, _rh in forecast)
    peak_hour = next(h for h, t, _s, _rh in forecast if t == max_temp)
    if indoor_now is not None:
        current_hour = datetime.now(timezone.utc).hour + 1  # UTC+1 approximates BST
        curve = project_indoor(forecast, indoor_now, current_hour, cal)
        start = current_hour
    else:
        curve = simulate_indoor_day(forecast, cal)
        start = 6
    close_hour = next(
        (h for (h, t_out, _s, _rh), (_, t_in) in zip(forecast, curve)
         if t_in is not None and h >= start and t_out >= t_in),
        None,
    )
    open_hour = next(
        (h for (h, t_out, _s, _rh), (_, t_in) in zip(forecast, curve)
         if t_in is not None and h > (peak_hour or 0) and t_out <= t_in - HYSTERESIS),
        None,
    )
    return close_hour, open_hour, max_temp, peak_hour


def wet_bulb(t_c, rh):
    """Wet-bulb temperature (°C) — Stull (2011) approximation. Mirrors the JS."""
    rh = max(1.0, min(100.0, rh))
    return (t_c * math.atan(0.151977 * math.sqrt(rh + 8.313659))
            + math.atan(t_c + rh) - math.atan(rh - 1.676331)
            + 0.00391838 * rh ** 1.5 * math.atan(0.023101 * rh)
            - 4.686035)


def forecast_wetbulb_max(forecast, cal, indoor_now=None, indoor_rh_now=None):
    """Project today's peak indoor wet-bulb (°C) and the hour it occurs.

    Runs the prediction engine forward on two coupled tracks, regime-aware (windows
    assumed shut once it's warmer outside than in):
      • temperature — project_indoor / simulate_indoor_day, anchored to the live sensor
      • moisture    — indoor vapour pressure relaxes toward outdoor at the learned rate
                      cal[regime]['c'], anchored to the live RH reading when we have one
    Each hour we convert the projected vapour pressure back to RH at the projected temp
    and take the wet-bulb; the daily max answers "how balmy will it get inside today".
    With no live RH anchor we seed indoor moisture at the outdoor value. When anchored,
    only hours from now forward count (the peak is still ahead in the morning brief).
    """
    if indoor_now is not None:
        current_hour = datetime.now(timezone.utc).hour + 1  # UTC+1 approximates BST
        curve = project_indoor(forecast, indoor_now, current_hour, cal)
    else:
        curve = simulate_indoor_day(forecast, cal)

    e_in = ((indoor_rh_now / 100.0) * saturation_vp(indoor_now)
            if indoor_now is not None and indoor_rh_now is not None else None)
    best_wb, best_h, first = None, None, True
    for (h, t_out, _s, rh_out), (_, t_in) in zip(forecast, curve):
        if t_in is None:
            continue
        e_out = (rh_out / 100.0) * saturation_vp(t_out)
        if first:
            if e_in is None:
                e_in = e_out                        # no anchor → assume equilibrated
            first = False
        else:
            closed = t_out >= t_in
            e_in += cal["closed" if closed else "open"]["c"] * (e_out - e_in)  # dt = 1h
        rh_in = max(1.0, min(100.0, e_in / saturation_vp(t_in) * 100.0))
        wb = wet_bulb(t_in, rh_in)
        if best_wb is None or wb > best_wb:
            best_wb, best_h = wb, h
    if best_wb is None:
        return None, None
    return round(best_wb, 1), best_h


def calibrate_from_history():
    """Fit per-regime temperature (a, b) and humidity (c) params from the history CSV.

    For each pair of consecutive same-regime samples (regime taken from the status
    recommended at the time — i.e. assuming you followed the advice), least-squares
    fit the hourly steps:
        Δindoor   = a·(outdoor − indoor)·Δt + b·solar·Δt        (temperature)
        Δe_indoor = c·(e_outdoor − e_indoor)·Δt                 (moisture, vapour pressure)
    where e is vapour pressure (the conserved quantity — RH alone is temperature-
    dependent). The humidity fit only uses pairs where both indoor RH readings and the
    outdoor RH are real (sensor online). Results + sample counts go to CALIBRATION_FILE;
    load_calibration() blends them toward the seed by count, so every extra day of data
    sharpens the model — no threshold, no cap.
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
        # Humidity columns are blank when the sensor was offline — keep as None then,
        # so the moisture fit only ever sees real readings.
        try:
            out_rh = float(parts[3]) if parts[3] else None
        except ValueError:
            out_rh = None
        try:
            in_rh = float(parts[10]) if parts[10] else None
        except ValueError:
            in_rh = None
        rows.append((ts, outdoor, solar, indoor, status, out_rh, in_rh))

    acc = {r: dict(S11=0.0, S12=0.0, S22=0.0, Sy1=0.0, Sy2=0.0, n=0,
                   Hxx=0.0, Hxy=0.0, n_h=0)
           for r in ("open", "closed")}
    for (t0, o0, s0, i0, st0, orh0, irh0), (t1, _o1, _s1, i1, _st1, _orh1, irh1) in zip(rows, rows[1:]):
        dt = (t1 - t0).total_seconds() / 3600.0
        if dt <= 0 or dt > 1.5:          # skip overnight gaps and duplicate runs
            continue
        regime = "closed" if st0 == "close" else "open"
        a = acc[regime]
        # temperature: Δindoor = a·(outdoor−indoor)·dt + b·solar·dt
        x1, x2, y = (o0 - i0) * dt, s0 * dt, i1 - i0
        a["S11"] += x1 * x1; a["S12"] += x1 * x2; a["S22"] += x2 * x2
        a["Sy1"] += x1 * y;  a["Sy2"] += x2 * y;  a["n"] += 1
        # moisture: Δe_in = c·(e_out−e_in)·dt, fit in vapour-pressure space
        if orh0 is not None and irh0 is not None and irh1 is not None:
            e_out = (orh0 / 100.0) * saturation_vp(o0)
            e_in0 = (irh0 / 100.0) * saturation_vp(i0)
            e_in1 = (irh1 / 100.0) * saturation_vp(i1)
            hx, hy = (e_out - e_in0) * dt, e_in1 - e_in0
            a["Hxx"] += hx * hx; a["Hxy"] += hx * hy; a["n_h"] += 1

    fitted = {}
    for regime, a in acc.items():
        entry = {}
        det = a["S11"] * a["S22"] - a["S12"] * a["S12"]
        if a["n"] >= 2 and abs(det) > 1e-9:
            coef_a = (a["Sy1"] * a["S22"] - a["Sy2"] * a["S12"]) / det
            coef_b = (a["S11"] * a["Sy2"] - a["S12"] * a["Sy1"]) / det
            entry["a"] = round(max(0.01, min(0.6, coef_a)), 5)    # clamp to sane ranges
            entry["b"] = round(max(0.0, min(0.005, coef_b)), 7)
            entry["n"] = a["n"]
        if a["n_h"] >= 2 and a["Hxx"] > 1e-9:
            coef_c = a["Hxy"] / a["Hxx"]
            entry["c"] = round(max(0.0, min(1.0, coef_c)), 4)     # 0..1 relax fraction/hr
            entry["n_h"] = a["n_h"]
        if entry:
            fitted[regime] = entry

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


def looks_like_glitch(new_temp, last_temp, last_utc):
    """True if the jump from the last reading implies a physically impossible rate.

    This flat changes slowly (its time constant is hours), so a multi-degree lurch
    between two ~30-min readings is the sensor misreporting, not the room. We ignore
    the reading for that cycle rather than act on a phantom plummet/spike.
    """
    if last_temp is None or last_utc is None:
        return False
    try:
        prev = datetime.strptime(last_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    dt = (datetime.now(timezone.utc) - prev).total_seconds() / 3600.0
    if dt <= 0 or dt > 2.0:          # too stale to judge a rate
        return False
    return abs(new_temp - last_temp) / dt > MAX_INDOOR_RATE


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


def update_dashboard(outdoor, status, indoor_est_c=None, forecast_max=None, forecast_peak_hour=None, forecast_close_hour=None, forecast_open_hour=None, forecast_hourly=None, indoor_humidity_pct=None, indoor_estimated=False, wetbulb_max_c=None, wetbulb_peak_hour=None):
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
                    "indoor_humidity_pct": indoor_humidity_pct,
                    "indoor_estimated": indoor_estimated,
                    "forecast_max_c": forecast_max,
                    "forecast_peak_hour": forecast_peak_hour,
                    "forecast_close_hour": forecast_close_hour,
                    "forecast_open_hour": forecast_open_hour,
                    "forecast_hourly": forecast_hourly,
                    "wetbulb_max_c": wetbulb_max_c,
                    "wetbulb_peak_hour": wetbulb_peak_hour,
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
            f"{indoor_c if indoor_c is not None else ''},"
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


def comfort_word(wb):
    """Plain-English air-comfort band for a wet-bulb °C — mirrors the dashboard."""
    if wb < 22: return "comfortable"
    if wb < 26: return "muggy"
    if wb < 28: return "oppressive"
    if wb < 31: return "dangerous"
    return "extreme"


def daily_summary(outdoor, cal, indoor_now=None, indoor_rh_now=None):
    try:
        forecast = get_forecast()
    except Exception as e:
        print(f"[warn] Forecast fetch failed: {e}", file=sys.stderr)
        return

    close_hour, open_hour, max_temp, max_hour = forecast_windows(forecast, cal, indoor_now)
    wb_max, wb_hour = forecast_wetbulb_max(forecast, cal, indoor_now, indoor_rh_now)

    print(f"daily summary: max={max_temp:.1f}°C at {fmt_hour(max_hour)}  close={close_hour}  open={open_hour}  wetbulb_max={wb_max}  indoor_now={indoor_now}")

    balmy = (f" Air feels {comfort_word(wb_max)} at its muggiest, around {fmt_hour(wb_hour)}."
             if wb_max is not None else "")

    if close_hour is not None:
        title = f"Close before {fmt_hour(close_hour)}"
        if open_hour is not None:
            title += f" · open around {fmt_hour(open_hour)}"
        body = (
            f"Peak {max_temp:.0f}°C around {fmt_hour(max_hour)}. "
            f"Shut windows before {fmt_hour(close_hour)}"
            + (f" and open up again around {fmt_hour(open_hour)}." if open_hour else " — may stay hot into the evening.")
            + balmy
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

    # ---- Load state early; guard against sensor glitches -----------------------
    state = load_state()
    today = local_today()
    if state.get("day") != today:
        state = {"status": state.get("status"), "day": today,
                 "close_hour": None, "open_hour": None,
                 "closed_today": False, "reopened_today": False,
                 "last_indoor": state.get("last_indoor"),         # carry over for glitch/rise checks
                 "last_indoor_utc": state.get("last_indoor_utc"),
                 "solar_warned_today": False}

    # A wild jump from the last good reading is the sensor misfiring — drop it for this
    # cycle and fall back to the model estimate, rather than acting on a phantom value.
    if shelly is not None and looks_like_glitch(shelly["temp"], state.get("last_indoor"), state.get("last_indoor_utc")):
        print(f"[warn] indoor {shelly['temp']}°C vs last {state.get('last_indoor')}°C looks like a glitch — ignoring this cycle", file=sys.stderr)
        shelly = None

    indoor_humidity = shelly["humidity"] if shelly else None
    indoor_battery  = shelly["battery"]  if shelly else None
    indoor_real     = shelly["temp"]     if shelly else None   # real reading only (None on dropout/glitch)

    is_brief = os.getenv("DAILY_SUMMARY") == "true"
    if is_brief:
        calibrate_from_history()   # refit the thermal model from yesterday's data (once daily)
    cal = load_calibration()

    # Fetch today's forecast; derive indoor estimate and all forward-looking stats
    forecast_max = forecast_peak_hour = forecast_close_hour = forecast_open_hour = None
    forecast_hourly = None
    wetbulb_max = wetbulb_peak_hour = None
    rising = False
    solar_now = 0.0
    indoor_est = INDOOR_BASE
    try:
        forecast = get_forecast()
        indoor_sim = simulate_indoor_day(forecast, cal)
        indoor_est = (shelly["temp"] if shelly else None) or estimate_indoor(forecast, cal)
        forecast_hourly = [[h, t_out, t_in] for (h, t_out, _s, _rh), (_, t_in) in zip(forecast, indoor_sim)]
        # Predictions anchored to the live reading when we have one, else model-only
        forecast_close_hour, forecast_open_hour, forecast_max, forecast_peak_hour = forecast_windows(
            forecast, cal, shelly["temp"] if shelly else None
        )
        # Today's projected peak indoor wet-bulb — "how balmy will it get inside"
        wetbulb_max, wetbulb_peak_hour = forecast_wetbulb_max(
            forecast, cal,
            shelly["temp"] if shelly else None,
            shelly["humidity"] if shelly else None,
        )
        # Is outdoor still climbing? (drives anticipatory close)
        current_hour = datetime.now(timezone.utc).hour + 1
        temp_now = next((t for h, t, _s, _rh in forecast if h == current_hour), outdoor)
        temp_next = next((t for h, t, _s, _rh in forecast if h == current_hour + 1), temp_now)
        rising = temp_next >= temp_now
        solar_now = next((s for h, _t, s, _rh in forecast if h == current_hour), 0.0)
    except Exception as e:
        print(f"[warn] Forecast fetch failed: {e}", file=sys.stderr)

    # Sensor fallback for humidity: when the Shelly is offline indoor_est is
    # already a modelled temperature — pair it with a modelled RH so the
    # wet-bulb gauge keeps working (flagged as an estimate downstream). The
    # history CSV still records the real reading only (None here), so the
    # estimate goes to the dashboard alone, never into calibration.
    indoor_estimated = shelly is None
    indoor_humidity_display = indoor_humidity
    if indoor_estimated:
        indoor_humidity_display = estimate_indoor_humidity(indoor_est, outdoor, outdoor_data["humidity"])

    # ---- Decision, and frozen close/open times -----------------------------
    # forecast_close_hour / forecast_open_hour above are the *fresh* predictions.
    # We only let them move the displayed times while the event is still ahead —
    # once you've actually closed, today's close time is frozen for reference, and
    # likewise the reopen time freezes the moment you reopen. This stops the close
    # time drifting to "now" as the day wears on. (state was loaded up top.)
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
        daily_summary(outdoor, cal,
                      shelly["temp"] if shelly else None,
                      shelly["humidity"] if shelly else None)
        update_dashboard(outdoor, last or "open", indoor_est, forecast_max, forecast_peak_hour, display_close, display_open, forecast_hourly, indoor_humidity_display, indoor_estimated, wetbulb_max, wetbulb_peak_hour)
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

    # Solar-gain alert: indoor is climbing from sun through the glass/roof even though
    # it's still cooler outside (so the temperature crossover hasn't fired and we're not
    # already advising close). Shutting windows would trap the cooler air out, so the fix
    # is shading, not closing — message it that way. Once per day to avoid nagging.
    prior_indoor = state.get("last_indoor")
    indoor_rising = (indoor_real is not None and prior_indoor is not None
                     and indoor_real - prior_indoor >= SOLAR_RISE)
    if (status != "close" and indoor_rising and solar_now >= SOLAR_MIN
            and outdoor < indoor_est and not state.get("solar_warned_today")):
        notify(
            "Sun's heating the flat",
            f"Inside up to {indoor_real:.1f}°C and climbing though it's only {outdoor:.1f}°C out — "
            "that's sun through the glass, not warm air. Close blinds or curtains on the sunny side; "
            "a shaded window can stay open for air.",
            tags="sunny,warning", priority="high",
        )
        state["solar_warned_today"] = True

    # Remember this cycle's real reading for next time's glitch/rise checks.
    if indoor_real is not None:
        state["last_indoor"] = indoor_real
        state["last_indoor_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    state["status"] = status
    save_state(state)
    update_dashboard(outdoor, status, indoor_est, forecast_max, forecast_peak_hour, display_close, display_open, forecast_hourly, indoor_humidity_display, indoor_estimated, wetbulb_max, wetbulb_peak_hour)
    log_history(outdoor_data, indoor_real, indoor_humidity, indoor_battery, status)


if __name__ == "__main__":
    main()
