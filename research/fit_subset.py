#!/usr/bin/env python3
"""Fit the thermal model over arbitrary SUBSETS of history, and compare them.

Why this one is committed when the convention has been throwaway scratch scripts: this
is a measurement instrument, not an exploration. Its output has to be reproducible on a
later date against a longer history, and a deleted script can't do that. The numbers it
prints are meant to be pasted into a dated research/ note along with the exact command.

The question it exists for: on 2026-08-14T06:49Z the `outdoor_c` column stopped being
Open-Meteo's estimate and became a physical wall sensor. `calibrate_from_history()`
pools both eras into one fit. This splits them. It generalises to any column — it will
serve the hallway_upstairs -> south_window sensor move, and pre/post-insulation splits.

Design constraints, each of which is load-bearing:

  * Uses csv.DictReader, NOT line.split(","). The `zones` column (index 15) is
    CSV-quoted JSON full of commas and `outdoor_source` sits *behind* it at index 16.
    Naive splitting cannot reach the provenance column — which is exactly why
    calibrate_from_history(), which does split naively, could never have done this.

  * Writes nothing. There is no open(..., "w") in this file and there must never be.
    calibrate_from_history() both writes CALIBRATION_FILE and reads its blinds factor
    back off disk, which makes production refits path-dependent; a research tool that
    touched that file would silently bend the live model. --json goes to stdout.

  * Pins the facade bearing rather than re-learning it per era. Production's bearing
    scan only fires at sunny_n >= 100; a small era would silently not fire while a large
    one did, making `a` and `b` incomparable across exactly the comparison being made.
    Same reasoning for the blinds learner — hence --blind-factor is required, not
    defaulted, so the value used lands in the output and thence in the note.

  * Pairs are built from the FULL time-ordered row list and only then assigned to
    groups. Splitting rows by group first would let a lone dropout row vanish from
    between two same-group rows, bridging a real 60-minute gap into a bogus pair that
    sails through the dt <= 1.5 guard and fabricates a temperature step.

Usage:
    python3 research/fit_subset.py --history PATH --blind-factor 0.59
    python3 research/fit_subset.py --history PATH --blind-factor 0.59 --group-by none
    python3 research/fit_subset.py --history PATH --blind-factor 0.59 --placebo shuffle-days
"""
import argparse
import csv
import hashlib
import json
import os
import random
import statistics as st
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# --- Argument parsing happens BEFORE importing window_watch -----------------------------
# window_watch binds LAT/LON/FACADE_AZ/SOLAR_DIFFUSE/CALIBRATION_FILE etc. via os.getenv
# at module scope (lines 53-161), so anything we want pinned has to be in the environment
# before the import statement runs.
parser = argparse.ArgumentParser(description="Fit thermal coefficients over subsets of history.")
parser.add_argument("--history", required=True,
                    help="Path to history.csv (no default on purpose — production's lives "
                         "on the Fly volume; forcing the path stops a stray local file "
                         "being picked up silently)")
parser.add_argument("--blind-factor", required=True, type=float,
                    help="Solar pass-through with blinds down, e.g. 0.59 for 41%% blocked. "
                         "Required, never defaulted: it must be pinned identically across "
                         "groups and recorded in the output.")
parser.add_argument("--group-by", default="outdoor_source",
                    help="History column to split on, or 'none' for a single whole-history "
                         "fit (default: outdoor_source)")
parser.add_argument("--facade-az", type=float, default=160.0,
                    help="Facade bearing, pinned for every group (default: 160, production)")
parser.add_argument("--solar-diffuse", type=float, default=0.25)
parser.add_argument("--since", default=None, help="ISO timestamp; extra filter, never the group definition")
parser.add_argument("--until", default=None, help="ISO timestamp; extra filter, never the group definition")
parser.add_argument("--bootstrap", type=int, default=400, help="Day-clustered bootstrap replicates (default 400)")
parser.add_argument("--ci", type=float, default=90.0, help="Confidence interval percent (default 90)")
parser.add_argument("--seed", type=int, default=20260911)
parser.add_argument("--placebo", choices=["shuffle-days"], default=None,
                    help="Reassign group labels at random BY DAY, preserving each group's "
                         "day count. A real effect should vanish; if it doesn't, the "
                         "machinery is manufacturing the difference.")
parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON to stdout")
args = parser.parse_args()

os.environ["FACADE_AZ"] = str(args.facade_az)
os.environ["SOLAR_DIFFUSE"] = str(args.solar_diffuse)
# Point these at paths that cannot exist, so an accidental production read/write in any
# future refactor of window_watch fails loudly here instead of touching the live model.
os.environ["CALIBRATION_FILE"] = "/nonexistent/fit_subset/calibration.json"
os.environ["HISTORY_FILE"] = "/nonexistent/fit_subset/history.csv"

sys.path.insert(0, REPO)
import window_watch as ww  # noqa: E402

# Belt-and-braces over the env-first path: pin the globals the imported helpers actually
# read. _facade_project() takes the bearing explicitly but reads SOLAR_DIFFUSE from the
# module; sun_position() reads LAT/LON. LAT/LON are left at window_watch's defaults
# because fly.toml [env] does not override them — module default IS production.
ww.SOLAR_DIFFUSE = args.solar_diffuse

# Deliberately NOT imported: blind_factor() and load_calibration(), both of which read
# disk. The blinds factor enters only as the --blind-factor float.
CLAMP_A = (0.01, 0.6)
CLAMP_B = (0.0, 0.005)
REGIMES = ("open", "closed", "part")

# Power thresholds below which this script refuses to dress a number up as a result.
MIN_DAYS_FOR_CI = 8
UNDERPOWERED_PAIRS = 300
UNDERPOWERED_DAYS = 14


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def git_head():
    try:
        return subprocess.run(["git", "-C", REPO, "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=5).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def load_rows(path, group_col):
    """Parse history into time-ordered records, replicating production's row filters.

    Production reads positionally (parts[1], parts[9], ...); we read by name. The header
    assertion below is what keeps those two in agreement — if the schema is widened
    without this script being updated, it aborts rather than silently misaligning.
    """
    with open(path, newline="") as f:
        reader = csv.DictReader(f, restkey="_extra")
        expected = ww.HISTORY_HEADER.strip().split(",")
        if reader.fieldnames != expected:
            # A comma-prefix is the one tolerable case: older rows written before a
            # column was appended (see _upgrade_history_header).
            if not (reader.fieldnames and expected[:len(reader.fieldnames)] == reader.fieldnames):
                sys.exit(f"ABORT: header mismatch.\n  file: {reader.fieldnames}\n  code: {expected}")
            print(f"[note] header is a prefix of HISTORY_HEADER ({len(reader.fieldnames)}/"
                  f"{len(expected)} cols) — treating missing trailing columns as blank.")
        if group_col != "none" and group_col not in (reader.fieldnames or []):
            sys.exit(f"ABORT: --group-by column '{group_col}' not in header {reader.fieldnames}")

        rows, skipped, overflow = [], 0, 0
        for rec in reader:
            if rec.get("_extra"):
                overflow += 1
            try:
                ts = datetime.strptime(rec["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                outdoor = float(rec["outdoor_c"])
                solar = float(rec["solar_wm2"] or 0)
                indoor = float(rec["indoor_c"])
                status = rec["status"] or ""
            except (ValueError, TypeError, KeyError):
                skipped += 1
                continue
            def opt(name):
                v = (rec.get(name) or "").strip()
                try:
                    return float(v) if v else None
                except ValueError:
                    return None
            elev, saz = ww.sun_position(ts)
            rows.append({
                "ts": ts, "outdoor": outdoor, "solar": solar, "indoor": indoor,
                "status": status,
                "out_rh": opt("outdoor_humidity_pct"), "in_rh": opt("indoor_humidity_pct"),
                "win": (rec.get("window_actual") or "").strip(),
                "blind": (rec.get("blinds_actual") or "").strip(),
                "elev": elev, "saz": saz,
                "group": (rec.get(group_col) or "").strip().lower() if group_col != "none" else "all",
            })
    rows.sort(key=lambda r: r["ts"])
    if args.since:
        cutoff = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        rows = [r for r in rows if r["ts"] >= cutoff]
    if args.until:
        cutoff = datetime.fromisoformat(args.until).replace(tzinfo=timezone.utc)
        rows = [r for r in rows if r["ts"] <= cutoff]
    return rows, skipped, overflow


def build_pairs(rows, bf, faz):
    """Consecutive-row pairs with the fit's design matrix, replicating window_watch.py:702-755.

    Built over the WHOLE ordered list; group assignment is a label on the pair, taken
    from r0. Only r0's outdoor value enters the temperature fit (x1 = (o0-i0)*dt), so
    requiring both ends to share a label would discard pairs for no measurement gain —
    but the mismatch count is reported, because it is the diagnostic that would reveal
    a boundary being straddled more often than dropouts explain.
    """
    pairs, mismatched = [], 0
    for r0, r1 in zip(rows, rows[1:]):
        dt = (r1["ts"] - r0["ts"]).total_seconds() / 3600.0
        if dt <= 0 or dt > 1.5:          # skip overnight gaps and duplicate runs
            continue
        raws0 = ww._facade_project(r0["solar"], r0["elev"], r0["saz"], faz)
        s0 = raws0 * bf if r0["blind"] == "down" else raws0
        win0, blind0 = r0["win"], r0["blind"]

        if win0 == "part":
            # Mixed state: its own accumulator, and a confirmed blind state is mandatory
            # because without it the solar term can't be attenuated.
            if blind0 not in ("up", "down"):
                continue
            regime, confirmed, counts = "part", False, False
        else:
            # A blank blinds column is not evidence of blinds up — ~40% of history
            # predates that logging, and assuming exposed feeds the fit full sun on
            # shaded hours. Only matters when there's real sun on the glass.
            if raws0 > 150 and blind0 not in ("up", "down"):
                continue
            confirmed = win0 in ("open", "closed")
            regime = win0 if confirmed else ("closed" if r0["status"] == "close" else "open")
            counts = True

        x1 = (r0["outdoor"] - r0["indoor"]) * dt
        x2 = s0 * dt
        y = r1["indoor"] - r0["indoor"]

        hx = hy = None
        if regime != "part" and None not in (r0["out_rh"], r0["in_rh"], r1["in_rh"]):
            e_out = (r0["out_rh"] / 100.0) * ww.saturation_vp(r0["outdoor"])
            e_in0 = (r0["in_rh"] / 100.0) * ww.saturation_vp(r0["indoor"])
            e_in1 = (r1["in_rh"] / 100.0) * ww.saturation_vp(r1["indoor"])
            hx, hy = (e_out - e_in0) * dt, e_in1 - e_in0

        if r0["group"] != r1["group"]:
            mismatched += 1
        pairs.append({"day": r0["ts"].date(), "group": r0["group"], "regime": regime,
                      "x1": x1, "x2": x2, "y": y, "hx": hx, "hy": hy,
                      "confirmed": confirmed, "counts": counts})
    return pairs, mismatched


def fit(pairs):
    """Least-squares a/b per regime plus moisture c. Mirrors window_watch.py:757-778.

    Returns both clamped and unclamped coefficients: production only ever acts on the
    clamped value, but a clamp that binds turns a wild estimate into a tidy-looking one,
    and on a small subset that is a real way to be fooled.
    """
    acc = {r: dict(S11=0.0, S12=0.0, S22=0.0, Sy1=0.0, Sy2=0.0, n=0,
                   Hxx=0.0, Hxy=0.0, n_h=0, pts=[]) for r in REGIMES}
    total = confirmed = 0
    for p in pairs:
        a = acc[p["regime"]]
        x1, x2, y = p["x1"], p["x2"], p["y"]
        a["S11"] += x1 * x1; a["S12"] += x1 * x2; a["S22"] += x2 * x2
        a["Sy1"] += x1 * y;  a["Sy2"] += x2 * y;  a["n"] += 1
        a["pts"].append((x1, x2, y))
        if p["hx"] is not None:
            a["Hxx"] += p["hx"] * p["hx"]; a["Hxy"] += p["hx"] * p["hy"]; a["n_h"] += 1
        if p["counts"]:
            total += 1
            confirmed += 1 if p["confirmed"] else 0
    out = {"_coverage": {"confirmed": confirmed, "total": total}}
    for regime, a in acc.items():
        entry = {"n": a["n"]}
        det = a["S11"] * a["S22"] - a["S12"] * a["S12"]
        if a["n"] >= 2 and abs(det) > 1e-9:
            raw_a = (a["Sy1"] * a["S22"] - a["Sy2"] * a["S12"]) / det
            raw_b = (a["S11"] * a["Sy2"] - a["S12"] * a["Sy1"]) / det
            ca = max(CLAMP_A[0], min(CLAMP_A[1], raw_a))
            cb = max(CLAMP_B[0], min(CLAMP_B[1], raw_b))
            sse = sum((y - (ca * x1 + cb * x2)) ** 2 for x1, x2, y in a["pts"])
            entry.update(a=ca, b=cb, a_raw=raw_a, b_raw=raw_b,
                         a_clamped=(ca != raw_a), b_clamped=(cb != raw_b),
                         rmse=(sse / a["n"]) ** 0.5)
        if a["n_h"] >= 2 and a["Hxx"] > 1e-9:
            entry.update(c=max(0.0, min(1.0, a["Hxy"] / a["Hxx"])), n_h=a["n_h"])
        out[regime] = entry
    return out


def bootstrap_ci(pairs, n_rep, ci_pct, rng):
    """Percentile CI from a bootstrap that resamples CALENDAR DAYS, not pairs.

    Consecutive half-hourly pairs share weather and are nowhere near independent; an
    i.i.d.-over-pairs bootstrap reports intervals several times too tight, which is the
    precise failure this tool exists to avoid. Matches the 31 Jul note's convention.
    """
    by_day = defaultdict(list)
    for p in pairs:
        by_day[p["day"]].append(p)
    days = list(by_day)
    if len(days) < MIN_DAYS_FOR_CI:
        return None, len(days)
    draws = {r: {"a": [], "b": []} for r in REGIMES}
    for _ in range(n_rep):
        sample = []
        for _ in range(len(days)):
            sample.extend(by_day[days[rng.randrange(len(days))]])
        f = fit(sample)
        for r in REGIMES:
            if "a" in f[r]:
                draws[r]["a"].append(f[r]["a"])
                draws[r]["b"].append(f[r]["b"])
    lo_q, hi_q = (100 - ci_pct) / 2, 100 - (100 - ci_pct) / 2
    res = {}
    for r in REGIMES:
        if len(draws[r]["a"]) >= max(20, n_rep // 10):
            res[r] = {
                "a_lo": pct(draws[r]["a"], lo_q), "a_hi": pct(draws[r]["a"], hi_q),
                "b_lo": pct(draws[r]["b"], lo_q), "b_hi": pct(draws[r]["b"], hi_q),
            }
    return res, len(days)


def pct(vals, q):
    s = sorted(vals)
    if not s:
        return float("nan")
    k = (len(s) - 1) * q / 100.0
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def describe(rows):
    """Descriptive stats that need no fit at all.

    Printed ABOVE the coefficients on purpose. The hypothesis under test — that a damped
    outdoor signal inflates fitted `a` — is first a claim about the driving signal, and
    the signal's dispersion is measurable with vastly more power than `a` is. If the
    outdoor SD is not visibly compressed, the mechanism isn't there whatever `a` does.
    """
    if not rows:
        return {}
    out = [r["outdoor"] for r in rows]
    gaps = [r["outdoor"] - r["indoor"] for r in rows]
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["ts"].date()].append(r["outdoor"])
    ranges = [max(v) - min(v) for v in by_day.values() if len(v) >= 30]
    return {
        "n_rows": len(rows), "n_days": len(by_day),
        "outdoor_mean": st.mean(out), "outdoor_sd": st.pstdev(out) if len(out) > 1 else 0.0,
        "daily_range_mean": st.mean(ranges) if ranges else None,
        "daily_range_sd": st.pstdev(ranges) if len(ranges) > 1 else None,
        "n_full_days": len(ranges),
        "gap_mean": st.mean(gaps),
        "solar_mean": st.mean([r["solar"] for r in rows]),
        "first": min(r["ts"] for r in rows).isoformat(),
        "last": max(r["ts"] for r in rows).isoformat(),
    }


BANNER = (
    "+" + "-" * 76 + "+\n"
    "| UNDERPOWERED - differences here are not evidence.                          |\n"
    "| Do not write a conclusion from this run.                                   |\n"
    "+" + "-" * 76 + "+"
)


def main():
    rng = random.Random(args.seed)
    rows, skipped, overflow = load_rows(args.history, args.group_by)
    if not rows:
        sys.exit("ABORT: no parseable rows")

    bf = args.blind_factor
    pairs, mismatched = build_pairs(rows, bf, args.facade_az)

    groups = sorted({r["group"] for r in rows})
    if args.placebo == "shuffle-days":
        # Preserve each group's DAY count, reassign which days belong to which group.
        day_group = {}
        all_days = sorted({r["ts"].date() for r in rows})
        counts = defaultdict(int)
        for r in rows:
            counts[r["group"]] += 0
        day_of = defaultdict(set)
        for r in rows:
            day_of[r["group"]].add(r["ts"].date())
        wanted = {g: len(day_of[g]) for g in groups}
        shuffled = all_days[:]
        rng.shuffle(shuffled)
        i = 0
        for g in groups:
            for d in shuffled[i:i + wanted[g]]:
                day_group[d] = g
            i += wanted[g]
        for p in pairs:
            p["group"] = day_group.get(p["day"], p["group"])
        for r in rows:
            r["group"] = day_group.get(r["ts"].date(), r["group"])

    rows_by_group = defaultdict(list)
    for r in rows:
        rows_by_group[r["group"]].append(r)
    pairs_by_group = defaultdict(list)
    for p in pairs:
        pairs_by_group[p["group"]].append(p)

    # ---- Power check drives the banner, printed top and bottom (scrollback eats one) ---
    underpowered = any(
        len(pairs_by_group[g]) < UNDERPOWERED_PAIRS or
        len({p["day"] for p in pairs_by_group[g]}) < UNDERPOWERED_DAYS
        for g in groups
    )

    result = {"meta": {}, "groups": {}}
    meta = result["meta"] = {
        "history": os.path.abspath(args.history),
        "history_sha256": sha256(args.history),
        "window_watch_sha256": sha256(os.path.join(REPO, "window_watch.py")),
        "git_head": git_head(),
        "rows_parsed": len(rows), "rows_skipped": skipped, "rows_overflow": overflow,
        "pairs": len(pairs), "cross_group_pairs": mismatched,
        "facade_az": args.facade_az, "blind_factor": bf,
        "solar_diffuse": args.solar_diffuse, "lat": ww.LAT, "lon": ww.LON,
        "group_by": args.group_by, "bootstrap": args.bootstrap, "ci": args.ci,
        "seed": args.seed, "placebo": args.placebo, "underpowered": underpowered,
    }

    if not args.json:
        if underpowered:
            print(BANNER)
        print(f"\nhistory      : {meta['history']}")
        print(f"  sha256     : {meta['history_sha256'][:16]}…   rows {len(rows)} "
              f"(skipped {skipped}, overflow {overflow})")
        print(f"fitter       : window_watch.py {meta['window_watch_sha256'][:16]}…  git {meta['git_head']}")
        print(f"pinned       : facade_az={args.facade_az}  blind_factor={bf}  "
              f"solar_diffuse={args.solar_diffuse}  lat={ww.LAT} lon={ww.LON}")
        print(f"pairs        : {len(pairs)}  (cross-group boundary/dropout pairs: {mismatched})")
        if args.placebo:
            print(f"PLACEBO      : {args.placebo} (seed {args.seed}) — group labels are RANDOM")
        print(f"group-by     : {args.group_by}")

    for g in groups:
        grows, gpairs = rows_by_group[g], pairs_by_group[g]
        desc = describe(grows)
        f = fit(gpairs)
        ci, n_days = bootstrap_ci(gpairs, args.bootstrap, args.ci, rng) if gpairs else (None, 0)
        result["groups"][g] = {"describe": desc, "fit": f, "ci": ci, "n_days": n_days,
                               "n_pairs": len(gpairs)}
        if args.json:
            continue

        label = g if g else "(blank — pre-column rows)"
        print(f"\n{'=' * 78}\nGROUP: {label}    rows={desc['n_rows']}  pairs={len(gpairs)}  days={n_days}")
        print(f"  span      : {desc['first']} -> {desc['last']}")
        print("  -- driving signal (no fit involved; far more power than the coefficients) --")
        print(f"  outdoor   : mean {desc['outdoor_mean']:.2f}C   SD {desc['outdoor_sd']:.2f}C")
        if desc["daily_range_mean"] is not None:
            print(f"  daily swing: mean {desc['daily_range_mean']:.2f}C  SD {desc['daily_range_sd'] or 0:.2f}  "
                  f"({desc['n_full_days']} full days)")
        print(f"  (out-in)  : mean {desc['gap_mean']:+.2f}C     solar mean {desc['solar_mean']:.0f} W/m2")
        print("  -- fitted coefficients --")
        cov = f["_coverage"]
        print(f"  coverage  : {cov['confirmed']}/{cov['total']} pairs from reported window state")
        for r in REGIMES:
            e = f[r]
            if "a" not in e:
                print(f"    {r:<7} n={e['n']:<5} (insufficient)")
                continue
            flag = ""
            if e["a_clamped"]:
                flag += f"  [a CLAMPED from {e['a_raw']:.5f}]"
            if e["b_clamped"]:
                flag += f"  [b CLAMPED from {e['b_raw']:.7f}]"
            cis = ""
            if ci and r in ci:
                cis = f"  a90%[{ci[r]['a_lo']:.5f},{ci[r]['a_hi']:.5f}]"
            print(f"    {r:<7} n={e['n']:<5} a={e['a']:.5f}  b={e['b']:.7f}  "
                  f"rmse={e['rmse']:.3f}{cis}{flag}")
            if "c" in e:
                print(f"            c={e['c']:.4f} (n_h={e['n_h']})")
        if ci is None:
            print(f"  CI not computable — {n_days} day clusters (< {MIN_DAYS_FOR_CI}). "
                  f"A percentile interval on this many clusters describes which days were "
                  f"drawn, not the building.")

    # ---- Pairwise deltas ---------------------------------------------------------------
    if len(groups) > 1 and not args.json:
        print(f"\n{'=' * 78}\nDELTAS (later group minus earlier, per regime)")
        print("  NOTE: groups differ in season, weather and regime mix, not only in")
        print("  instrument. Any delta is instrument + weather + mix — read it next to the")
        print("  driving-signal block above, which is where the mechanism would show first.")
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                gi, gj = groups[i], groups[j]
                fi, fj = result["groups"][gi]["fit"], result["groups"][gj]["fit"]
                print(f"\n  {gj or '(blank)'} - {gi or '(blank)'}:")
                for r in REGIMES:
                    if "a" in fi[r] and "a" in fj[r]:
                        print(f"    {r:<7} da={fj[r]['a'] - fi[r]['a']:+.5f}   "
                              f"db={fj[r]['b'] - fi[r]['b']:+.7f}")

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    elif underpowered:
        print()
        print(BANNER)


if __name__ == "__main__":
    main()
