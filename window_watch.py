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
from datetime import datetime, timezone

LAT = os.getenv("LAT") or "51.527"
LON = os.getenv("LON") or "-0.021"
WEATHER_MODEL = os.getenv("WEATHER_MODEL") or "icon_d2"
CLOSE_ABOVE = float(os.getenv("CLOSE_ABOVE") or "25")    # still used for forecast_close_hour
THERMAL_ALPHA = float(os.getenv("THERMAL_ALPHA") or "0.18")   # heat bleed-in per hour
INDOOR_BASE = float(os.getenv("INDOOR_BASE") or "19.0")        # overnight cool-down target
SOLAR_GAIN = float(os.getenv("SOLAR_GAIN") or "3.0")           # south-facing solar load added during daylight (°C)
HYSTERESIS = float(os.getenv("HYSTERESIS") or "0.5")           # dead band to prevent flapping
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


def get_forecast():
    """Return list of (hour_int, temp_float) for today in Europe/London time."""
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={LAT}&longitude={LON}"
        f"&hourly=temperature_2m&temperature_unit=celsius&models={WEATHER_MODEL}"
        f"&forecast_days=1&timezone=Europe%2FLondon"
    )
    with urllib.request.urlopen(url, timeout=20) as r:
        data = json.load(r)
    times = data["hourly"]["time"]   # e.g. "2026-06-21T14:00"
    temps = data["hourly"]["temperature_2m"]
    return [(int(t.split("T")[1][:2]), float(temp)) for t, temp in zip(times, temps)]


def simulate_indoor_day(forecast):
    """Run the thermal lag model across all 24 forecast hours.

    Returns a list of (hour, indoor_est) pairs — the full day simulation.
    Starting temp is the overnight minimum outdoor (hours 0-5), which is more
    realistic than a fixed base on warm nights where the room can't cool further.
    """
    overnight = [t for h, t in forecast if h <= 5]
    start = min(overnight) if overnight else INDOOR_BASE
    indoor = min(start, INDOOR_BASE)  # never start warmer than the configured base
    result = []
    for h, outdoor in forecast:
        effective = outdoor + (SOLAR_GAIN if 7 <= h <= 19 else 0)
        indoor = THERMAL_ALPHA * effective + (1 - THERMAL_ALPHA) * indoor
        result.append((h, round(indoor, 1)))
    return result


def estimate_indoor(forecast):
    """Return estimated indoor temp at the current local hour."""
    current_hour = datetime.now(timezone.utc).hour + 1  # UTC+1 approximates BST
    indoor = INDOOR_BASE
    for h, est in simulate_indoor_day(forecast):
        indoor = est
        if h >= current_hour:
            break
    return indoor


def decide(outdoor, indoor_est, last):
    if outdoor >= indoor_est + HYSTERESIS:
        return "close"
    if outdoor <= indoor_est - HYSTERESIS:
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


def update_dashboard(outdoor, status, indoor_est_c=None, forecast_max=None, forecast_peak_hour=None, forecast_close_hour=None, forecast_open_hour=None):
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


def daily_summary(outdoor):
    try:
        forecast = get_forecast()
    except Exception as e:
        print(f"[warn] Forecast fetch failed: {e}", file=sys.stderr)
        return

    max_temp = max(t for _, t in forecast)
    max_hour = next(h for h, t in forecast if t == max_temp)
    indoor_sim = simulate_indoor_day(forecast)
    close_hour = next(
        (h for (h, t_out), (_, t_in) in zip(forecast, indoor_sim)
         if h >= 6 and t_out >= t_in + HYSTERESIS),
        None,
    )
    open_hour = next(
        (h for (h, t_out), (_, t_in) in zip(forecast, indoor_sim)
         if h > (max_hour or 0) and t_out <= t_in - HYSTERESIS),
        None,
    )

    print(f"daily summary: max={max_temp:.1f}°C at {fmt_hour(max_hour)}  close={close_hour}  open={open_hour}")

    if close_hour is not None:
        title = f"Close before {fmt_hour_approx(close_hour)}"
        if open_hour is not None:
            title += f" · open after {fmt_hour_approx(open_hour)}"
        body = (
            f"Peak {max_temp:.0f}°C around {fmt_hour(max_hour)}. "
            f"Shut windows before {fmt_hour_approx(close_hour)}"
            + (f" and open up again after {fmt_hour_approx(open_hour)}." if open_hour else " — may stay hot into the evening.")
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

    outdoor = get_outdoor()

    # Fetch today's forecast; derive indoor estimate and all forward-looking stats
    forecast_max = forecast_peak_hour = forecast_close_hour = forecast_open_hour = None
    indoor_est = INDOOR_BASE
    try:
        forecast = get_forecast()
        forecast_max = max(t for _, t in forecast)
        forecast_peak_hour = next(h for h, t in forecast if t == forecast_max)
        indoor_sim = simulate_indoor_day(forecast)
        indoor_est = estimate_indoor(forecast)
        # Use simulated indoor at each forecast hour for consistent thermal comparisons
        forecast_close_hour = next(
            (h for (h, t_out), (_, t_in) in zip(forecast, indoor_sim)
             if h >= 6 and t_out >= t_in + HYSTERESIS),
            None,
        )
        forecast_open_hour = next(
            (h for (h, t_out), (_, t_in) in zip(forecast, indoor_sim)
             if h > (forecast_peak_hour or 0) and t_out <= t_in - HYSTERESIS),
            None,
        )
    except Exception as e:
        print(f"[warn] Forecast fetch failed: {e}", file=sys.stderr)

    if os.getenv("DAILY_SUMMARY") == "true":
        daily_summary(outdoor)
        update_dashboard(outdoor, load_state() or "open", indoor_est, forecast_max, forecast_peak_hour, forecast_close_hour, forecast_open_hour)
        return

    last = load_state()
    status = decide(outdoor, indoor_est, last)
    print(f"outdoor={outdoor:.1f}  indoor_est={indoor_est}  last={last}  -> {status}")

    if status != last:
        if forecast_max is not None and forecast_peak_hour is not None:
            peak_ctx = f"peaks at {forecast_max:.0f}°C around {fmt_hour(forecast_peak_hour)}"
        else:
            peak_ctx = None

        if last is None:
            body = f"Outside {outdoor:.1f}°C, ~{indoor_est:.0f}°C estimated inside. "
            if peak_ctx:
                body += f"Today {peak_ctx}. "
            body += f"Windows should be: {status}."
            notify("Window Watch running", body, tags="house,white_check_mark")

        elif status == "close":
            body = (
                f"Outside {outdoor:.1f}°C — warmer than estimated {indoor_est:.0f}°C inside. "
                + (f"Today {peak_ctx}. " if peak_ctx else "")
                + "Shut windows, doors and blinds."
            )
            notify("Close up now", body, tags="house,sunny", priority="high")

        elif status == "open":
            body = (
                f"Outside dropped to {outdoor:.1f}°C — cooler than estimated {indoor_est:.0f}°C inside. "
                "Open up and flush the heat out."
            )
            notify("Open up", body, tags="house,leaves")

    save_state(status)
    update_dashboard(outdoor, status, indoor_est, forecast_max, forecast_peak_hour, forecast_close_hour, forecast_open_hour)


if __name__ == "__main__":
    main()
