#!/usr/bin/env python3
"""Weekly heat-loss estimate (W/K): how many watts the flat loses per degree inside-outside.

This is the number the insulation decision needs. Run the same way before and after the
work, it measures what the work achieved.

Daily energy balance over the last 28 days, using only days the heating actually ran:

    Q_heat = H·(T_in − T_out) − g·S − G + C·dT_in/dt

  Q_heat   mean heat delivered to the radiators (W): Vaillant HEATING / HEAT_GENERATED
  T_in     mean of the indoor sensors (lounge Shelly, hallway, bedroom), °C
  T_out    outdoor sensor (falls back to the logged Open-Meteo value), °C
  S        mean horizontal sun, W/m² (free solar gain through the glass)
  G        internal gains: people, appliances, hot-water cylinder losses (fitted constant)
  C        heat capacity, from the day-to-day change in T_in (the flat's slow thermal mass)

H is the answer, with a 90% interval from a day-resampled bootstrap. Pure Python: the
Fly image carries no numpy.

The loft sensor adds an indicator: overnight on heating days, the share of the indoor-to-
outdoor temperature drop that happens across the ceiling (T_in − T_loft)/(T_in − T_out).
A rising share after loft insulation means the ceiling is now doing more of the work.

Writes /data/heatloss.json, which keeps the history of weekly estimates. Mondays at 08:15,
after the 03:30 Vaillant pull, it sends a summary once there are enough heating days.

Usage:  python heatloss.py [--days 28] [--no-notify]
"""
import argparse
import csv
import datetime as dt
import json
import os
import random
import sys
from zoneinfo import ZoneInfo

import energy as en

LOCAL = ZoneInfo("Europe/London")
HISTORY_FILE = os.getenv("HISTORY_FILE", "/data/history.csv")
OUT_FILE = os.path.join(en.DATA_DIR, "heatloss.json")
INDOOR_ZONES = ("hallway_downstairs", "bedroom")   # averaged with the lounge Shelly
MIN_DAYS = 10            # four unknowns need a decent spread of days
MIN_HEAT_KWH = 1.0       # a day with less heating than this says little about H
MIN_ROWS = 36            # of the 48 half-hourly history rows, for a usable daily mean


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _parse(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(dt.timezone.utc)


def _f(x):
    try:
        return float(x) if x not in (None, "") else None
    except ValueError:
        return None


# ---- Data -------------------------------------------------------------------------------

def daily_heat(since):
    """{local date: kWh of space heating delivered} from vaillant_hourly.csv."""
    out = {}
    try:
        with open(en.VAILLANT_FILE, newline="") as f:
            for r in csv.DictReader(f):
                if r["mode"] != "HEATING" or r["energy_type"] != "HEAT_GENERATED":
                    continue
                t = _parse(r["start_utc"])
                if t < since:
                    continue
                d = t.astimezone(LOCAL).date()
                out[d] = out.get(d, 0.0) + float(r["wh"]) / 1000
    except OSError:
        pass
    return out


def daily_climate(since):
    """{local date: dict of daily means and the night-time ceiling share} from history.csv.
    Uses the csv module: the zones column is quoted JSON full of commas."""
    days = {}
    try:
        with open(HISTORY_FILE, newline="") as f:
            for r in csv.DictReader(f):
                try:
                    t = _parse(r["timestamp"])
                except (KeyError, ValueError):
                    continue
                if t < since:
                    continue
                try:
                    zones = json.loads(r.get("zones") or "{}")
                except ValueError:
                    zones = {}
                temps = [_f(r.get("indoor_c"))] + [(zones.get(z) or {}).get("t") for z in INDOOR_ZONES]
                temps = [x for x in temps if x is not None]
                t_out = (zones.get("outside") or {}).get("t")
                t_out = t_out if t_out is not None else _f(r.get("outdoor_c"))
                if not temps or t_out is None:
                    continue
                local = t.astimezone(LOCAL)
                d = days.setdefault(local.date(), {"tin": [], "tout": [], "sun": [], "share": [], "first": None, "last": None})
                t_in = sum(temps) / len(temps)
                d["tin"].append(t_in)
                d["tout"].append(t_out)
                d["sun"].append(_f(r.get("solar_wm2")) or 0.0)
                d["first"] = d["first"] if d["first"] is not None else t_in
                d["last"] = t_in
                loft = (zones.get("loft") or {}).get("t")
                if loft is not None and local.hour < 6 and t_in - t_out > 3:
                    d["share"].append((t_in - loft) / (t_in - t_out))
    except OSError:
        pass
    out = {}
    for day, d in days.items():
        if len(d["tin"]) < MIN_ROWS:
            continue
        mean = lambda xs: sum(xs) / len(xs)
        out[day] = {"tin": mean(d["tin"]), "tout": mean(d["tout"]), "sun": mean(d["sun"]),
                    "dtin_per_h": (d["last"] - d["first"]) / 24.0,
                    "share": mean(d["share"]) if d["share"] else None}
    return out


# ---- Least squares (pure Python) --------------------------------------------------------

def _solve(rows, ys):
    """Ordinary least squares via the normal equations and Gaussian elimination."""
    k = len(rows[0])
    a = [[sum(r[i] * r[j] for r in rows) for j in range(k)] + [sum(r[i] * y for r, y in zip(rows, ys))]
         for i in range(k)]
    for c in range(k):
        p = max(range(c, k), key=lambda i: abs(a[i][c]))
        if abs(a[p][c]) < 1e-12:
            raise ValueError("singular: not enough variation in the data")
        a[c], a[p] = a[p], a[c]
        for i in range(k):
            if i != c:
                f = a[i][c] / a[c][c]
                a[i] = [x - f * y for x, y in zip(a[i], a[c])]
    return [a[i][k] / a[i][i] for i in range(k)]


def fit(samples, boot=300, seed=1):
    """samples: [(Q_W, dT, sun, dtin_per_h)]. Returns H, its 90% interval, and C, G, g."""
    def design(s):
        return [[dT, -sun, -1.0, dtin] for _, dT, sun, dtin in s], [q for q, *_ in s]

    x, y = design(samples)
    h, g, gi, c = _solve(x, y)
    rng = random.Random(seed)
    hs = []
    for _ in range(boot):
        res = [samples[rng.randrange(len(samples))] for _ in samples]
        try:
            hs.append(_solve(*design(res))[0])
        except ValueError:
            continue
    hs.sort()
    lo = hs[int(0.05 * len(hs))] if hs else None
    hi = hs[int(0.95 * len(hs)) - 1] if hs else None
    return {"H_w_per_k": h, "ci90": [lo, hi], "C_wh_per_k": c, "internal_gains_w": gi, "solar_w_per_wm2": g}


# ---- Weekly job -------------------------------------------------------------------------

def estimate(days=28):
    since = _now() - dt.timedelta(days=days + 1)
    heat, climate = daily_heat(since), daily_climate(since)
    usable = sorted(d for d in heat if heat[d] >= MIN_HEAT_KWH and d in climate)
    result = {"utc": _now().strftime("%Y-%m-%dT%H:%M:%SZ"), "window_days": days,
              "heating_days": len(usable), "needed_days": MIN_DAYS}
    shares = [climate[d]["share"] for d in usable if climate[d]["share"] is not None]
    result["ceiling_share_night"] = round(sum(shares) / len(shares), 3) if shares else None
    if len(usable) < MIN_DAYS:
        result["status"] = "waiting"
        return result
    samples = [(heat[d] * 1000 / 24, climate[d]["tin"] - climate[d]["tout"], climate[d]["sun"],
                climate[d]["dtin_per_h"]) for d in usable]
    try:
        f = fit(samples)
    except ValueError as e:
        result.update(status="unfittable", error=str(e))
        return result
    result.update(status="ok", **{k: (round(v, 1) if isinstance(v, float) else
                                      [round(x, 1) if x is not None else None for x in v] if isinstance(v, list) else v)
                                  for k, v in f.items()},
                  mean_dT=round(sum(s[1] for s in samples) / len(samples), 1),
                  first_day=str(usable[0]), last_day=str(usable[-1]))
    return result


def _notify(title, body):
    try:
        import window_watch
        window_watch.notify(title, body, tags="house,thermometer", priority="low")
    except Exception as e:
        print(f"[warn] heatloss notify failed: {e}", file=sys.stderr)


def weekly(days=28, notify=True):
    """Compute, append to heatloss.json, and send the Monday summary when there's a result."""
    try:
        r = estimate(days)
        try:
            with open(OUT_FILE) as f:
                store = json.load(f)
        except (OSError, ValueError):
            store = {"history": []}
        store["latest"] = r
        store["history"].append(r)
        tmp = OUT_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(store, f, indent=1)
        os.replace(tmp, OUT_FILE)
        if r["status"] == "ok":
            lo, hi = r["ci90"]
            prev = [h for h in store["history"][:-1] if h.get("status") == "ok"]
            trend = f" (last week {prev[-1]['H_w_per_k']:.0f})" if prev else ""
            share = (f" Overnight, {r['ceiling_share_night']:.0%} of the indoor-outdoor drop is across the ceiling."
                     if r.get("ceiling_share_night") is not None else "")
            body = (f"{r['H_w_per_k']:.0f} W/K (90% range {lo:.0f}–{hi:.0f}){trend}, from {r['heating_days']} heating days "
                    f"at an average {r['mean_dT']:.1f}°C inside-outside gap.{share}")
            if notify:
                _notify("Heat loss this month", body)
            en._status("heatloss", True, body)
        else:
            en._status("heatloss", True, f"{r['status']}: {r['heating_days']}/{r['needed_days']} heating days")
        return r
    except Exception as e:
        print(f"[warn] heatloss failed: {e}", file=sys.stderr)
        en._status("heatloss", False, e)
        return None


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=28)
    ap.add_argument("--no-notify", action="store_true")
    a = ap.parse_args()
    print(json.dumps(weekly(a.days, notify=not a.no_notify), indent=1, default=str))
