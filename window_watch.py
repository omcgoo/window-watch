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
from zoneinfo import ZoneInfo

LAT = os.getenv("LAT") or "51.527"
LON = os.getenv("LON") or "-0.021"
WEATHER_MODEL = os.getenv("WEATHER_MODEL") or "icon_d2"
CLOSE_ABOVE = float(os.getenv("CLOSE_ABOVE") or "25")    # still used for forecast_close_hour
INDOOR_BASE = float(os.getenv("INDOOR_BASE") or "19.0")        # overnight cool-down target (fallback start when no sensor)
HYSTERESIS = float(os.getenv("HYSTERESIS") or "1.0")           # reopen dead band — only reopen once genuinely cooler
CLOSE_LEAD = float(os.getenv("CLOSE_LEAD") or "0.5")           # anticipation margin — close this many °C before the crossover while outdoor is still climbing
MAX_INDOOR_RATE = float(os.getenv("MAX_INDOOR_RATE") or "4.0")  # °C/hr; a faster lurch between reads is a sensor glitch, not the room
SOLAR_RISE = float(os.getenv("SOLAR_RISE") or "0.3")           # °C rise since the last reading that counts as indoor genuinely climbing
# Sol-air seed: how much the sun-baked south wall lifts the air right outside it, per
# W/m² of sun (so at 1000 W/m² ≈ +4°C). Once outdoor + k·solar tops indoor, venting the
# sunny side adds heat, so windows must shut too — not just the blinds. Not in the
# thermal model and we've no south-side air sensor to learn it, so it's a tunable seed.
SOLAR_WALL_K = float(os.getenv("SOLAR_WALL_K") or "0.004")
# The gain path is the south facade, but Open-Meteo's shortwave_radiation is on a
# *horizontal* plane — directionless. south_facade_solar() projects it onto the facade
# using the sun's actual position, so evening west sun stops reading as heat on the
# south glass. FACADE_AZ = compass bearing the glass faces; SOLAR_DIFFUSE = the share
# of horizontal irradiance the facade sees regardless of sun direction (sky glow).
FACADE_AZ = float(os.getenv("FACADE_AZ") or "180")
SOLAR_DIFFUSE = float(os.getenv("SOLAR_DIFFUSE") or "0.25")

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
# Actual window state, reported from the dashboard. Overrides the inferred regime (the
# app's open/close recommendation) for calibration. A report is retired when the advice
# next flips (the natural moment you'd change the windows), with WINDOW_TTL_HOURS as a
# pure backstop for stable spells. This is the "learn from real states, fall back to
# inferred" path — forgetting to re-confirm can't mislabel for long.
WINDOW_FILE = os.getenv("WINDOW_FILE", "window_report.json")
WINDOW_TTL_HOURS = float(os.getenv("WINDOW_TTL_HOURS") or "18")
# Blinds are the third control — external south blinds block most solar before it hits
# the glass. Logged like windows so calibration can separate shaded from unshaded days
# instead of blending them into one muddy solar coefficient: when down, only BLIND_FACTOR
# of the solar reaches the interior (external blinds cut ~85%). A tunable seed for now;
# once enough shaded/unshaded data accrues we can learn the real figure.
BLIND_FILE = os.getenv("BLIND_FILE", "blind_report.json")
BLIND_TTL_HOURS = float(os.getenv("BLIND_TTL_HOURS") or "18")
BLIND_FACTOR = float(os.getenv("BLIND_FACTOR") or "0.15")
NTFY_TOPIC = os.getenv("NTFY_TOPIC")
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")
STATE_FILE = os.getenv("STATE_FILE", "state.json")
DASHBOARD_GIST_ID = "45ba603447bbc3ec258e479f6dee6a20"
DASHBOARD_URL = "https://omcgoo.github.io/window-watch/"
SHELLY_AUTH_KEY = os.getenv("SHELLY_AUTH_KEY")
SHELLY_DEVICE_ID = os.getenv("SHELLY_DEVICE_ID")
SHELLY_SERVER = os.getenv("SHELLY_SERVER")
# For one-tap "confirm actual window state" buttons on the push notifications.
FLY_BASE = os.getenv("FLY_BASE", "https://window-watch-ollie.fly.dev")
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN", "")


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
    # Project each hour's horizontal irradiance onto the south facade — the surface the
    # thermal model's b coefficient is actually about. Times are local (Europe/London).
    tz = ZoneInfo("Europe/London")
    return [
        (int(t.split("T")[1][:2]), float(tp),
         south_facade_solar(float(sw or 0.0),
                            datetime.strptime(t, "%Y-%m-%dT%H:%M").replace(tzinfo=tz)),
         float(rhv if rhv is not None else 50.0))
        for t, tp, sw, rhv in zip(times, temps, solar, rh)
    ]


def sun_position(dt):
    """Sun (elevation°, azimuth° clockwise from north) at LAT/LON.

    NOAA's simplified solar-position algorithm — accurate to a fraction of a degree,
    which is far more than the facade projection needs. `dt` must be timezone-aware.
    """
    dt = dt.astimezone(timezone.utc)
    lat = math.radians(float(LAT))
    lon = float(LON)
    frac_hour = dt.hour + dt.minute / 60.0
    gamma = 2 * math.pi / 365.0 * (dt.timetuple().tm_yday - 1 + (frac_hour - 12) / 24.0)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
                       - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
    decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
            - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
            - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
    tst = frac_hour * 60.0 + eqtime + 4.0 * lon          # true solar time, minutes
    ha = math.radians(tst / 4.0 - 180.0)                 # hour angle
    cos_zen = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(ha)
    elev = 90.0 - math.degrees(math.acos(max(-1.0, min(1.0, cos_zen))))
    az = math.degrees(math.atan2(math.sin(ha),
                                 math.cos(ha) * math.sin(lat) - math.tan(decl) * math.cos(lat))) + 180.0
    return elev, az % 360.0


def south_facade_solar(ghi, dt=None):
    """Horizontal irradiance → effective irradiance on the (vertical) south facade.

    Diffuse floor: the facade always sees ~SOLAR_DIFFUSE of the sky's glow, whatever
    the sun's direction. Direct part: scaled by cos(incidence)/sin(elevation) — how
    square-on the beam hits vertical glass vs how obliquely it made the horizontal
    reading — clamped so low-sun geometry can't blow up. Net effect: square-on
    morning/midday sun counts more than the horizontal number says; evening west sun
    (behind the facade's shoulder) drops to the diffuse floor and the solar advisory
    goes quiet even though the sky is still bright.
    """
    if not ghi or ghi <= 0:
        return 0.0
    elev, az = sun_position(dt or datetime.now(timezone.utc))
    if elev <= 2.0:
        return round(ghi * SOLAR_DIFFUSE, 1)
    cos_inc = math.cos(math.radians(elev)) * math.cos(math.radians(az - FACADE_AZ))
    direct = max(0.0, min(1.6, cos_inc / max(math.sin(math.radians(elev)), 0.15)))
    return round(ghi * (SOLAR_DIFFUSE + (1.0 - SOLAR_DIFFUSE) * direct), 1)


def load_calibration():
    """Blend each regime's fitted params with its profile seed by sample count.

    Rather than a hard switch once enough data exists, every fitted coefficient is
    shrunk toward its seed:  param = (n·fit + K·seed) / (n + K)  (K = CAL_PRIOR_WEIGHT).
    With no history it's the pure seed; the more data accrues the closer it gets to the
    raw fit — so accuracy climbs continuously and without a cap. Temperature (a, b) is
    weighted by its own sample count; humidity (c) by the humidity sample count, since
    rows logged while the sensor was offline carry no real RH.
    """
    cal = {k: {"a": v["a"], "b": v["b"], "c": v["c"], "n": 0, "rmse": None}
           for k, v in CAL_DEFAULTS.items()}
    cal["_coverage"] = {"confirmed": 0, "total": 0}
    cal["_blinds"] = {"ok": False, "eff": None, "n_up": 0, "n_down": 0}
    try:
        with open(CALIBRATION_FILE) as f:
            fitted = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return cal
    cal["_coverage"] = fitted.get("_coverage") or cal["_coverage"]
    cal["_blinds"] = fitted.get("_blinds") or cal["_blinds"]
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
        # carry the fit diagnostics through for the accuracy readout
        cal[regime]["n"] = n
        cal[regime]["rmse"] = f_r.get("rmse")
    return cal


def model_accuracy(cal):
    """Roll the per-regime one-step errors into a single headline.

    Returns (rmse_°C, total_samples): how close the learned model gets the next
    reading on average, and how many hourly transitions back it. None rmse until
    there's a fit. Samples-weighted so the busier regime dominates the figure.
    """
    total_n, sse = 0, 0.0
    for regime in ("open", "closed"):
        n = cal[regime].get("n", 0)
        rmse = cal[regime].get("rmse")
        if n and rmse is not None:
            sse += (rmse ** 2) * n
            total_n += n
    if total_n == 0:
        return None, 0
    return round((sse / total_n) ** 0.5, 2), total_n


def thermal_summary(cal, confirmed=0):
    """Translate the learned coefficients into plain heating/cooling rates.

    Conductance a = the fraction of the indoor↔outdoor gap the flat closes each hour —
    the same whether heat is flowing in or out — reported as %/hr and a half-life (the
    time to halve the gap, ln2/a). Solar b = the heat-only gain through the glass at a
    strong-sun reference (~800 W/m²).

    'overall_half_h' is the sample-weighted responsiveness across both regimes — an
    honest, split-independent "how sluggish is this flat" that's defensible even before
    real window states are logged. The per-regime open/shut split is only trustworthy
    once enough confirmed states de-pollute the buckets AND it's physically sane (open
    faster than shut); 'split_ok' gates showing it.
    """
    def rate(regime):
        a = cal[regime]["a"]
        return (round(a * 100), round(0.693 / a, 1)) if a > 0 else (0, None)
    vent_pct, vent_half = rate("open")
    shut_pct, shut_half = rate("closed")
    no, nc = cal["open"].get("n", 0), cal["closed"].get("n", 0)
    ao, ac = cal["open"]["a"], cal["closed"]["a"]
    overall_a = (no * ao + nc * ac) / (no + nc) if (no + nc) else (ao + ac) / 2
    return {
        "vent_pct": vent_pct, "vent_half_h": vent_half,
        "shut_pct": shut_pct, "shut_half_h": shut_half,
        "hold_ratio": (round(shut_half / vent_half, 1)
                       if vent_half and shut_half else None),
        "solar_c_hr": round(cal["open"]["b"] * 800, 1),   # direct gain at ~800 W/m²
        "overall_half_h": round(0.693 / overall_a, 1) if overall_a > 0 else None,
        "split_ok": confirmed >= 120 and vent_pct > shut_pct,
    }


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
    current_hour = local_now().hour
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
        current_hour = local_now().hour
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
        current_hour = local_now().hour
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
        # window_actual (col 13) is the reported real state when known; blank on older
        # rows or unreported periods. Overrides the inferred regime for calibration.
        win = parts[13].strip() if len(parts) > 13 else ""
        blind = parts[14].strip() if len(parts) > 14 else ""
        # The CSV stores raw horizontal irradiance; project it onto the south facade at
        # this row's timestamp so b is fitted against what the glass actually received.
        # (Same transform live readings get — the CSV itself stays raw.)
        solar = south_facade_solar(solar, ts)
        # Blinds down (shaded) → only BLIND_FACTOR of the sun reaches the interior, so the
        # solar term sees effective solar. Keeps shaded days from dragging the coefficient
        # toward zero and lets shaded/unshaded days both inform one clean b.
        solar_eff = solar * BLIND_FACTOR if blind == "down" else solar
        rows.append((ts, outdoor, solar_eff, indoor, status, out_rh, in_rh, win, blind, solar))

    acc = {r: dict(S11=0.0, S12=0.0, S22=0.0, Sy1=0.0, Sy2=0.0, n=0,
                   Hxx=0.0, Hxy=0.0, n_h=0, pairs=[])
           for r in ("open", "closed")}
    # Blinds inference: sunny pairs whose blinds state is *confirmed*, kept as
    # (regime, x_gap, x_rawsolar, Δ) so we can later strip conductance with the fitted a
    # and fit the solar response separately for shaded vs exposed → the real blinds effect.
    blind_buckets = {"up": [], "down": []}
    total_pairs = confirmed_pairs = 0
    for (t0, o0, s0, i0, st0, orh0, irh0, win0, blind0, raws0), (t1, _o1, _s1, i1, _st1, _orh1, irh1, _w1, _b1, _rs1) in zip(rows, rows[1:]):
        dt = (t1 - t0).total_seconds() / 3600.0
        if dt <= 0 or dt > 1.5:          # skip overnight gaps and duplicate runs
            continue
        if win0 == "part":
            # Mixed state (sunny side shut, shaded open) — neither regime's physics.
            # Excluded entirely so it can't pollute either fit; that's its whole job.
            continue
        total_pairs += 1
        confirmed = win0 in ("open", "closed")   # regime came from a real report, not inference
        if confirmed:
            confirmed_pairs += 1
        # Prefer the reported actual state; fall back to the inferred recommendation.
        regime = win0 if confirmed else ("closed" if st0 == "close" else "open")
        a = acc[regime]
        # temperature: Δindoor = a·(outdoor−indoor)·dt + b·solar·dt
        x1, x2, y = (o0 - i0) * dt, s0 * dt, i1 - i0
        a["S11"] += x1 * x1; a["S12"] += x1 * x2; a["S22"] += x2 * x2
        a["Sy1"] += x1 * y;  a["Sy2"] += x2 * y;  a["n"] += 1
        a["pairs"].append((x1, x2, y))   # kept to score the fit's one-step error afterwards
        if blind0 in ("up", "down") and raws0 > 150:   # need real sun + a confirmed blind state
            blind_buckets[blind0].append((regime, x1, raws0 * dt, y))
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
            ca = max(0.01, min(0.6, coef_a))    # clamp to sane ranges
            cb = max(0.0, min(0.005, coef_b))
            entry["a"] = round(ca, 5)
            entry["b"] = round(cb, 7)
            entry["n"] = a["n"]
            # One-step prediction error of the (clamped) fit: how close it gets the
            # next reading, in °C. This is the accuracy metric surfaced to the user.
            sse = sum((y - (ca * x1 + cb * x2)) ** 2 for x1, x2, y in a["pairs"])
            entry["rmse"] = round((sse / a["n"]) ** 0.5, 3)
        if a["n_h"] >= 2 and a["Hxx"] > 1e-9:
            coef_c = a["Hxy"] / a["Hxx"]
            entry["c"] = round(max(0.0, min(1.0, coef_c)), 4)     # 0..1 relax fraction/hr
            entry["n_h"] = a["n_h"]
        if entry:
            fitted[regime] = entry

    if not fitted:
        print("Calibration: not enough data yet — keeping defaults.")
        return
    # How much of the fit rests on real reported window states vs inferred defaults.
    fitted["_coverage"] = {"confirmed": confirmed_pairs, "total": total_pairs}

    # Blinds effectiveness — inferred, not seeded. Strip conductance with each pair's
    # fitted a, then least-squares the residual solar response for shaded vs exposed:
    #   resid = Δ − a·gap ≈ b·(solar·dt).  eff = 1 − b_down/b_up.
    # Needs *contrast* — enough confirmed sunny pairs in BOTH states — else stays dormant.
    def fit_solar_b(bucket):
        sxx = sxy = 0.0
        for regime, x_gap, x_sun, y in bucket:
            a = fitted.get(regime, {}).get("a")
            if a is None:
                continue
            resid = y - a * x_gap
            sxx += x_sun * x_sun
            sxy += resid * x_sun
        return (sxy / sxx if sxx > 1e-9 else None), len(bucket)
    b_up, n_up = fit_solar_b(blind_buckets["up"])
    b_down, n_down = fit_solar_b(blind_buckets["down"])
    blinds = {"n_up": n_up, "n_down": n_down, "ok": False, "eff": None}
    if n_up >= 30 and n_down >= 30 and b_up and b_up > 0 and b_down is not None:
        eff = 1.0 - max(0.0, b_down) / b_up
        blinds.update(eff=round(max(0.0, min(1.0, eff)), 2),
                      b_up=round(b_up, 7), b_down=round(max(0.0, b_down), 7), ok=True)
    fitted["_blinds"] = blinds
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
    delta = abs(new_temp - last_temp)
    if delta < 2.0:                  # small moves are never glitches, however close the reads
        return False
    dt = (datetime.now(timezone.utc) - prev).total_seconds() / 3600.0
    if dt <= 0 or dt > 2.0:          # too stale to judge a rate
        return False
    return delta / dt > MAX_INDOOR_RATE


def solar_rise_threshold(indoor, outdoor, cal):
    """Shortwave (W/m²) needed to start warming the flat with windows open.

    From the open-regime balance  a·(outdoor−indoor) + b·solar = 0  →
    solar = (a/b)·(indoor−outdoor). The bigger the indoor↔outdoor gap, the more sun it
    takes to overcome ventilation cooling; on a cold day no realistic sun will. Returns
    None when it's already warmer outside (then any sun only adds to the close case).
    """
    gap = indoor - outdoor
    p = cal["open"]
    if gap <= 0 or p["b"] <= 0:
        return None
    return round((p["a"] / p["b"]) * gap)


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


def record_window_state(state):
    """Persist a dashboard-reported actual window state ('open', 'shut' or 'part').

    'part' = the strong-sun mixed state (sunny side shut, a shaded window open). It
    exists to keep labels honest — those hours are neither regime's physics, so
    calibration excludes them rather than polluting either fit.

    Stamps it with the recommendation in force at report time ('rec'), so the report
    can be retired the moment the app's advice next flips — that's when you'd naturally
    change the windows anyway, so a forgotten re-confirm reverts to the inferred default
    at exactly the right point.
    """
    if state not in ("open", "shut", "part"):
        return False
    try:
        with open(WINDOW_FILE, "w") as f:
            json.dump({"state": state,
                       "utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "rec": load_state().get("status")}, f)
        print(f"Window state recorded: {state}")
        return True
    except Exception as e:
        print(f"[warn] window state save failed: {e}", file=sys.stderr)
        return False


def current_window_regime(current_rec=None):
    """The reported regime ('open'/'closed'/'part') while trustworthy, else None.

    A report is retired when the recommendation flips from what it was at report time
    (the natural moment windows change), or after WINDOW_TTL_HOURS as a backstop. Beyond
    that, callers fall back to the inferred default.
    """
    try:
        with open(WINDOW_FILE) as f:
            rep = json.load(f)
        ts = datetime.strptime(rep["utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError):
        return None
    if (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0 > WINDOW_TTL_HOURS:
        return None
    rec0 = rep.get("rec")
    if current_rec is not None and rec0 is not None and current_rec != rec0:
        return None   # advice flipped since you reported — you'd have changed the windows
    st = rep.get("state")
    return "closed" if st == "shut" else ("part" if st == "part" else "open")


def record_blind_state(state):
    """Persist a reported blinds state ('down' = shaded, 'up' = exposed) with a timestamp."""
    if state not in ("down", "up"):
        return False
    try:
        with open(BLIND_FILE, "w") as f:
            json.dump({"state": state,
                       "utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}, f)
        print(f"Blind state recorded: {state}")
        return True
    except Exception as e:
        print(f"[warn] blind state save failed: {e}", file=sys.stderr)
        return False


def current_blind_regime():
    """Reported blinds state ('down'/'up') if fresh (within BLIND_TTL_HOURS), else None."""
    try:
        with open(BLIND_FILE) as f:
            rep = json.load(f)
        ts = datetime.strptime(rep["utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError):
        return None
    if (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0 > BLIND_TTL_HOURS:
        return None
    return rep.get("state")


def local_now():
    """Now in Europe/London — DST-correct (BST in summer, GMT in winter)."""
    return datetime.now(ZoneInfo("Europe/London"))


def local_today():
    """Today's date in Europe/London."""
    return local_now().strftime("%Y-%m-%d")


def window_action(state):
    """An ntfy action button that logs the confirmed window state in one tap.

    The phone's ntfy app POSTs straight to the /window endpoint — no need to open the
    app — so you can confirm what you actually did right from the alert you're reacting
    to. Returns None if there's no token to authorise it.
    """
    if not REFRESH_TOKEN:
        return None
    label = {"shut": "Confirm shut", "part": "Sunny side shut"}.get(state, "Confirm open")
    return (f"action=http, label={label}, url={FLY_BASE}/window?state={state}, "
            f"method=POST, headers.Authorization=Bearer {REFRESH_TOKEN}, clear=true")


def blind_action(state):
    """ntfy action button to log blinds ('down'/'up') in one tap — pairs with the solar
    alert that tells you to shade. Feeds the blinds-vs-solar learning."""
    if not REFRESH_TOKEN:
        return None
    label = "Blinds down" if state == "down" else "Blinds up"
    return (f"action=http, label={label}, url={FLY_BASE}/blinds?state={state}, "
            f"method=POST, headers.Authorization=Bearer {REFRESH_TOKEN}, clear=true")


def notify(title, body, tags, priority="default", actions=None):
    if not NTFY_TOPIC:
        print("[error] NTFY_TOPIC not set — cannot send push", file=sys.stderr)
        return
    url = f"{NTFY_SERVER}/{urllib.parse.quote(NTFY_TOPIC)}"
    req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST")
    req.add_header("Title", title)
    req.add_header("Tags", tags)
    req.add_header("Priority", priority)
    req.add_header("Click", DASHBOARD_URL)
    if actions:
        req.add_header("Actions", actions)
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


def update_dashboard(outdoor, status, indoor_est_c=None, forecast_max=None, forecast_peak_hour=None, forecast_close_hour=None, forecast_open_hour=None, forecast_hourly=None, indoor_humidity_pct=None, indoor_estimated=False, wetbulb_max_c=None, wetbulb_peak_hour=None, solar_now=None, solar_threshold=None, solar_window_threshold=None, model_rmse=None, model_samples=None, thermal=None, window_actual=None, model_confirmed=None, blinds_actual=None, blinds_effect=None):
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
                    "solar_now": solar_now,
                    "solar_threshold": solar_threshold,
                    "solar_window_threshold": solar_window_threshold,
                    "model_rmse": model_rmse,
                    "model_samples": model_samples,
                    "model_confirmed": model_confirmed,
                    "thermal": thermal,
                    "window_actual": window_actual,
                    "blinds_actual": blinds_actual,
                    "blinds_effect": blinds_effect,
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
    "indoor_c,indoor_humidity_pct,battery_pct,status,window_actual,blinds_actual\n"
)

def log_history(outdoor_data, indoor_c, indoor_humidity, battery_pct, status, window_actual=None, blinds_actual=None):
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
            f"{status},"
            f"{window_actual or ''},"
            f"{blinds_actual or ''}\n"
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
    indoor_est = INDOOR_BASE
    try:
        forecast = get_forecast()
        current_hour = local_now().hour
        indoor_est = (shelly["temp"] if shelly else None) or estimate_indoor(forecast, cal)
        # Chart's indoor forecast is projected forward from the *real* current reading
        # (anchored), not the whole-day sim that resets to the overnight low each night —
        # this flat never resets, so once it's heat-soaked the sim's shape is meaningless.
        # project_indoor returns None before current_hour; the chart only draws it forward.
        indoor_proj = project_indoor(forecast, indoor_est, current_hour, cal)
        forecast_hourly = [[h, t_out, t_in] for (h, t_out, _s, _rh), (_, t_in) in zip(forecast, indoor_proj)]
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
        temp_now = next((t for h, t, _s, _rh in forecast if h == current_hour), outdoor)
        temp_next = next((t for h, t, _s, _rh in forecast if h == current_hour + 1), temp_now)
        rising = temp_next >= temp_now
    except Exception as e:
        print(f"[warn] Forecast fetch failed: {e}", file=sys.stderr)

    # Live sun on the south facade right now — the current horizontal reading projected
    # through the sun's actual position, matching the units the model/thresholds use.
    solar_now = south_facade_solar(outdoor_data["solar_wm2"])

    # Sensor fallback for humidity: when the Shelly is offline indoor_est is
    # already a modelled temperature — pair it with a modelled RH so the
    # wet-bulb gauge keeps working (flagged as an estimate downstream). The
    # history CSV still records the real reading only (None here), so the
    # estimate goes to the dashboard alone, never into calibration.
    indoor_estimated = shelly is None
    indoor_humidity_display = indoor_humidity
    if indoor_estimated:
        indoor_humidity_display = estimate_indoor_humidity(indoor_est, outdoor, outdoor_data["humidity"])

    # How well the learned model predicts the next reading, + how much data backs it.
    model_rmse, model_samples = model_accuracy(cal)
    model_confirmed = cal.get("_coverage", {}).get("confirmed", 0)   # rows backed by a real report
    thermal = thermal_summary(cal, model_confirmed)   # heating/cooling rates for display
    blinds_effect = cal.get("_blinds")   # inferred blinds solar-blocking (dormant until enough contrast)

    # Two solar thresholds: the sun that starts warming the room through the glass
    # (close blinds), and the higher sun that warms the air against the south wall
    # enough to top indoors (close the sunny-side windows too).
    solar_threshold = solar_rise_threshold(indoor_est, outdoor, cal)
    solar_window_threshold = (round((indoor_est - outdoor) / SOLAR_WALL_K)
                              if indoor_est - outdoor > 0 and SOLAR_WALL_K > 0 else None)

    # ---- Decision, and frozen close/open times -----------------------------
    # forecast_close_hour / forecast_open_hour above are the *fresh* predictions.
    # We only let them move the displayed times while the event is still ahead —
    # once you've actually closed, today's close time is frozen for reference, and
    # likewise the reopen time freezes the moment you reopen. This stops the close
    # time drifting to "now" as the day wears on. (state was loaded up top.)
    last = state.get("status")
    status = decide(outdoor, indoor_est, last, rising)

    # Reported actual window state, retired once the recommendation flips from it.
    window_regime = current_window_regime(status)
    blind_regime = current_blind_regime()   # 'down'/'up'/None (reported blinds state if fresh)

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
        update_dashboard(outdoor, last or "open", indoor_est, forecast_max, forecast_peak_hour, display_close, display_open, forecast_hourly, indoor_humidity_display, indoor_estimated, wetbulb_max, wetbulb_peak_hour, solar_now, solar_threshold, solar_window_threshold, model_rmse, model_samples, thermal, window_regime, model_confirmed, blind_regime, blinds_effect)
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
            # One action per line, matching the dashboard callouts. Blinds join in
            # whenever the sun is actually on the south face.
            sun_up = solar_threshold is not None and solar_now >= solar_threshold
            if outdoor < indoor_est:
                win_line = (f"🪟 Windows shut — outside {outdoor:.1f}°C is about to overtake "
                            f"{indoor_label} and still climbing; trap the cool while you can")
            else:
                win_line = (f"🪟 Windows shut — outside {outdoor:.1f}°C is now warmer than "
                            f"{indoor_label}")
            lines = [win_line]
            if sun_up:
                lines.append("🕶 Blinds down — the sun's on the south glass too")
            if peak_ctx:
                lines.append(f"Today {peak_ctx}")
            acts = [a for a in ([window_action("shut")] +
                                ([blind_action("down")] if sun_up else [])) if a]
            notify("Close up now", "\n".join(lines), tags="house,sunny", priority="high",
                   actions="; ".join(acts) if acts else None)

        elif status == "open":
            # Evening flush: windows open, and blinds back up once the sun's off the glass.
            sun_done = solar_threshold is None or solar_now < solar_threshold
            lines = [f"🪟 Windows open — outside dropped to {outdoor:.1f}°C, cooler than "
                     f"{indoor_label}; flush the heat out"]
            if sun_done:
                lines.append("🕶 Blinds up — the sun's off the glass, let the evening light in")
            acts = [a for a in ([window_action("open")] +
                                ([blind_action("up")] if sun_done else [])) if a]
            notify("Open up", "\n".join(lines), tags="house,leaves",
                   actions="; ".join(acts) if acts else None)

    # Solar-gain alert: indoor is climbing from sun through the glass even though it's
    # still cooler outside (so the crossover hasn't fired). Two tiers: shade first (kills
    # the gain through the glass), and on stronger sun shut the sunny-side windows too —
    # by then the sun-baked wall has warmed the air there, so venting it adds heat.
    # Once per day to avoid nagging.
    prior_indoor = state.get("last_indoor")
    indoor_rising = (indoor_real is not None and prior_indoor is not None
                     and indoor_real - prior_indoor >= SOLAR_RISE)
    if (status != "close" and indoor_rising and solar_threshold is not None
            and solar_now >= solar_threshold and not state.get("solar_warned_today")):
        windows_too = solar_window_threshold is not None and solar_now >= solar_window_threshold
        # One action per line, mirroring the dashboard's two callouts.
        lines = ["🕶 Blinds down — the sun's warming the room through the glass"]
        if windows_too:
            lines.append("🪟 Windows shut, sunny side — the south wall is heating the air "
                         "just outside; venting that side adds heat. Keep a shaded window open for air")
        else:
            lines.append(f"🪟 Windows open — it's still cooler out ({outdoor:.1f}°C), let the air in")
        lines.append(f"Inside {indoor_real:.1f}°C and climbing · south-face sun {round(solar_now)} W/m²")
        # Strong sun advises the mixed state (sunny side shut, shaded open) — confirming
        # it logs 'part', which calibration deliberately sets aside rather than
        # mislabelling as open or shut. Moderate sun: windows genuinely stay open.
        acts = [a for a in (window_action("part" if windows_too else "open"),
                            blind_action("down")) if a]
        notify("Sun's heating the flat", "\n".join(lines), tags="sunny,warning", priority="high",
               actions="; ".join(acts) if acts else None)
        state["solar_warned_today"] = True

    # Remember this cycle's real reading for next time's glitch/rise checks.
    if indoor_real is not None:
        state["last_indoor"] = indoor_real
        state["last_indoor_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    state["status"] = status
    save_state(state)
    update_dashboard(outdoor, status, indoor_est, forecast_max, forecast_peak_hour, display_close, display_open, forecast_hourly, indoor_humidity_display, indoor_estimated, wetbulb_max, wetbulb_peak_hour, solar_now, solar_threshold, solar_window_threshold, model_rmse, model_samples, thermal, window_regime, model_confirmed, blind_regime, blinds_effect)
    log_history(outdoor_data, indoor_real, indoor_humidity, indoor_battery, status, window_regime, blind_regime)


if __name__ == "__main__":
    main()
