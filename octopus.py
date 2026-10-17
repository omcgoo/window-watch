#!/usr/bin/env python3
"""Octopus Energy: smart-meter consumption, actual tariff rates, and free-electricity sessions.

Like energy.py this sits outside the advisory path: every entry point swallows its own
errors and no-ops without OCTOPUS_API_KEY.

What it writes (under /data, alongside the other logs):

  octopus_consumption.csv  half-hourly kWh per meter, keyed (interval_start, mpan, serial),
                           upserted. `direction` is import/export. This is the billing meter:
                           the ground truth the SolaX import counter and the inverter-loss
                           estimate get checked against.
  octopus_rates.csv        unit rates (p/kWh inc VAT) for every tariff agreement on the
                           account, keyed (tariff_code, valid_from). Public endpoint.
  octopus_agreements.json  which tariff each meter point was on, when. Pricing must go
                           through this: rate rows from different tariffs overlap in time.
  octopus_sessions.json    free-electricity sessions seen, and what was done about each.

Free-electricity sessions come from Octopus's GraphQL API
(customerFlexibilityCampaignEvents, campaignSlug "free_electricity"), which needs a Kraken
token obtained from the same API key. Only consumption *above your usual* for each
half-hour is free, so the useful response is to add load: the battery charges (via
battery_control, only when OCTOPUS_SESSION_CHARGE=on and BATTERY_CONTROL=on) and you get a
nudge to run the washing machine or dishwasher.

Env:
  OCTOPUS_API_KEY        required (Octopus dashboard → Personal details → API access)
  OCTOPUS_ACCOUNT        optional; discovered from the key when unset
  OCTOPUS_SESSION_CHARGE off (default) | on

Usage:
  python octopus.py pull [--days 7]        # consumption + rates
  python octopus.py backfill               # all consumption history the API holds
  python octopus.py sessions               # refresh sessions and act on them
  python octopus.py report [--days 30]     # import kWh and cost by Cosy band
"""
import argparse
import base64
import csv
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

import energy as en

API_KEY = os.getenv("OCTOPUS_API_KEY")
ACCOUNT = os.getenv("OCTOPUS_ACCOUNT")
SESSION_CHARGE = (os.getenv("OCTOPUS_SESSION_CHARGE") or "off").strip().lower() == "on"
REST = "https://api.octopus.energy/v1"
GRAPHQL = "https://api.octopus.energy/v1/graphql/"
LOCAL = ZoneInfo("Europe/London")

CONSUMPTION_FILE = os.path.join(en.DATA_DIR, "octopus_consumption.csv")
RATES_FILE = os.path.join(en.DATA_DIR, "octopus_rates.csv")
SESSIONS_FILE = os.path.join(en.DATA_DIR, "octopus_sessions.json")
AGREEMENTS_FILE = os.path.join(en.DATA_DIR, "octopus_agreements.json")
CONSUMPTION_HEADER = ["interval_start", "mpan", "serial", "direction", "kwh"]
RATES_HEADER = ["tariff_code", "valid_from", "valid_to", "p_inc_vat"]

SESSION_WARN = dt.timedelta(minutes=30)     # "free power soon" nudge this far ahead

_account = {}                               # discovered meter points, cached per process
_kraken = {"token": None}


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s):
    """Octopus timestamps come as '...Z' or with an offset; normalise to aware UTC."""
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(dt.timezone.utc)


# ---- HTTP -----------------------------------------------------------------------------

def _rest(path_or_url, params=None, auth=True):
    url = path_or_url if path_or_url.startswith("http") else f"{REST}{path_or_url}"
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    headers = {"Accept": "application/json"}
    if auth:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{API_KEY}:".encode()).decode()
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
        return json.load(r)


CHUNK = dt.timedelta(days=30)     # 1,440 half-hours or ~210 Cosy rate rows: always one page
PAGE_SIZE = 2000


def _chunked(path, since, until, extra=None, auth=True):
    """Yield result rows for [since, until) in CHUNK-sized windows, each fetched as a single
    page. Don't follow Octopus's `next` links: on 17 Oct 2026 a two-page consumption pull
    (page_size 25000) returned 34,717 rows of which 6,963 were repeats, and an equal number
    of real half-hours were silently skipped (the meter looked ~80% complete). A window
    small enough for one page avoids pagination altogether; the count is checked so a
    truncated page fails loudly instead of leaving holes."""
    a = since
    while a < until:
        b = min(a + CHUNK, until)
        page = _rest(path, {"period_from": _iso(a), "period_to": _iso(b), "page_size": PAGE_SIZE,
                            "order_by": "period", **(extra or {})}, auth)
        results = page.get("results", [])
        if page.get("next") or (page.get("count") is not None and page["count"] != len(results)):
            raise RuntimeError(f"Octopus {path}: page truncated ({len(results)} of {page.get('count')}) "
                               f"for {_iso(a)}..{_iso(b)}; shrink CHUNK")
        yield from results
        a = b


def _graphql(query, variables=None, _retried=False):
    if not _kraken["token"]:
        tok = _graphql_raw('mutation($key: String!) { obtainKrakenToken(input: {APIKey: $key}) { token } }',
                           {"key": API_KEY}, token=None)
        _kraken["token"] = ((tok.get("data") or {}).get("obtainKrakenToken") or {}).get("token")
        if not _kraken["token"]:
            raise RuntimeError(f"Octopus GraphQL token: {tok.get('errors')}")
    resp = _graphql_raw(query, variables, _kraken["token"])
    errors = resp.get("errors") or []
    if errors and not _retried and any("token" in json.dumps(e).lower() or "jwt" in json.dumps(e).lower()
                                       for e in errors):
        _kraken["token"] = None
        return _graphql(query, variables, _retried=True)
    if errors and not resp.get("data"):
        raise RuntimeError(f"Octopus GraphQL: {errors}")
    return resp.get("data") or {}


def _graphql_raw(query, variables, token):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = token
    req = urllib.request.Request(GRAPHQL, data=json.dumps({"query": query, "variables": variables or {}}).encode(),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


# ---- Account --------------------------------------------------------------------------

def account():
    """{number, points: [{mpan, direction, serials, agreements}]}, discovered once."""
    if _account:
        return _account
    number = ACCOUNT
    if not number:
        data = _graphql("query { viewer { accounts { number } } }")
        accounts = ((data.get("viewer") or {}).get("accounts")) or []
        if not accounts:
            raise RuntimeError("no Octopus account visible to this API key; set OCTOPUS_ACCOUNT")
        number = accounts[0]["number"]
    acc = _rest(f"/accounts/{number}/")
    points = []
    for prop in acc.get("properties", []):
        for mp in prop.get("electricity_meter_points", []):
            points.append({
                "mpan": mp["mpan"],
                "direction": "export" if mp.get("is_export") else "import",
                "serials": [m["serial_number"] for m in mp.get("meters", []) if m.get("serial_number")],
                "agreements": mp.get("agreements", []),
            })
    if not points:
        raise RuntimeError(f"Octopus account {number}: no electricity meter points")
    _account.update(number=number, points=points)
    return _account


def import_mpan():
    return next(p["mpan"] for p in account()["points"] if p["direction"] == "import")


# ---- CSV upsert -----------------------------------------------------------------------

def _upsert(path, header, keylen, new_rows):
    """Merge rows keyed on their first `keylen` columns; atomic temp+rename. Returns size."""
    merged = {}
    try:
        with open(path, newline="") as f:
            for r in csv.reader(f):
                if r and r[0] != header[0]:
                    merged[tuple(r[:keylen])] = r
    except OSError:
        pass
    for r in new_rows:
        merged[tuple(str(x) for x in r[:keylen])] = [str(x) for x in r]
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for k in sorted(merged):
            w.writerow(merged[k])
    os.replace(tmp, path)
    return len(merged)


# ---- Consumption + rates ----------------------------------------------------------------

def _pull_consumption(since):
    rows = []
    for p in account()["points"]:
        for serial in p["serials"]:
            try:
                for r in _chunked(f"/electricity-meter-points/{p['mpan']}/meters/{serial}/consumption/",
                                  since, _now()):
                    rows.append([_iso(_parse(r["interval_start"])), p["mpan"], serial, p["direction"],
                                 r["consumption"]])
            except urllib.error.HTTPError as e:
                # Old/replaced meters on the same MPAN 404; that's expected, not a failure.
                if e.code != 404:
                    raise
    return rows


def _pull_rates(since, until):
    """Rates for every agreement period overlapping [since, until). Keyed by period, not
    tariff code: the same tariff can come round twice (Variable 2023-25, then again
    2025-26), and deduping on the code silently dropped the second stint's rates."""
    rows = []
    for p in account()["points"]:
        for a in p["agreements"]:
            code = a.get("tariff_code")
            if not code:
                continue
            a_from = _parse(a["valid_from"]) if a.get("valid_from") else since
            a_to = _parse(a["valid_to"]) if a.get("valid_to") else until
            lo, hi = max(since, a_from), min(until, a_to)
            if lo >= hi:
                continue
            product = "-".join(code.split("-")[2:-1])     # E-1R-COSY-22-12-08-C -> COSY-22-12-08
            for r in _chunked(f"/products/{product}/electricity-tariffs/{code}/standard-unit-rates/",
                              lo, hi, auth=False):
                rows.append([code, _iso(_parse(r["valid_from"])),
                             _iso(_parse(r["valid_to"])) if r.get("valid_to") else "", r["value_inc_vat"]])
    return rows


def pull(days=None):
    """Recent consumption plus rates through tomorrow. The smart meter's half-hours arrive
    late and patchily (days with only 32-40 of 48 seen in Oct 2026), so each run re-pulls
    14 days and the upsert fills the gaps in. The first run, with no consumption file yet,
    backfills two years instead."""
    if not API_KEY:
        return None
    if days is None:
        days = 14 if os.path.exists(CONSUMPTION_FILE) else 730
    try:
        since = _now() - dt.timedelta(days=days)
        _save_json(AGREEMENTS_FILE, {p["mpan"]: p["agreements"] for p in account()["points"]})
        n_c = _upsert(CONSUMPTION_FILE, CONSUMPTION_HEADER, 3, _pull_consumption(since))
        n_r = _upsert(RATES_FILE, RATES_HEADER, 2, _pull_rates(since, _now() + dt.timedelta(days=2)))
        detail = f"{days}d pulled; consumption file {n_c} rows, rates file {n_r} rows"
        print(f"octopus pull: {detail}")
        en._status("octopus", True, detail)
        return n_c
    except Exception as e:
        print(f"[warn] Octopus pull failed: {e}", file=sys.stderr)
        en._status("octopus", False, e)
        return None


def backfill(years=2):
    return pull(days=int(365 * years))


# ---- Free-electricity sessions ----------------------------------------------------------

SESSIONS_QUERY = """
query($account: String!, $mpan: String!) {
  customerFlexibilityCampaignEvents(accountNumber: $account, campaignSlug: "free_electricity",
                                    supplyPointIdentifier: $mpan, first: 20) {
    edges { node { code name startAt endAt } }
  }
}"""


def _save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def _load_sessions():
    try:
        with open(SESSIONS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_sessions(s):
    tmp = SESSIONS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=1)
    os.replace(tmp, SESSIONS_FILE)


def _fetch_sessions():
    data = _graphql(SESSIONS_QUERY, {"account": account()["number"], "mpan": import_mpan()})
    edges = ((data.get("customerFlexibilityCampaignEvents") or {}).get("edges")) or []
    out = {}
    for e in edges:
        n = e.get("node") or {}
        if n.get("startAt") and n.get("endAt"):
            out[n.get("code") or n["startAt"]] = {"name": n.get("name"), "start": _iso(_parse(n["startAt"])),
                                                  "end": _iso(_parse(n["endAt"]))}
    return out


def _notify(title, body, priority="default"):
    try:
        import window_watch
        window_watch.notify(title, body, tags="zap", priority=priority)
    except Exception as e:
        print(f"[warn] octopus notify failed: {e}", file=sys.stderr)


def _when(s):
    a, b = _parse(s["start"]).astimezone(LOCAL), _parse(s["end"]).astimezone(LOCAL)
    return f"{a:%a %-d %b, %H:%M}–{b:%H:%M}"


def sessions(refresh=True):
    """Refresh the session list (when `refresh`), then act on each: announce new ones, nudge
    shortly before, and (if enabled) charge the battery through the session."""
    if not API_KEY:
        return None
    try:
        known = _load_sessions()
        if refresh:
            for code, s in _fetch_sessions().items():
                if code not in known:
                    known[code] = dict(s, announced=False, warned=False, charge=None)
                else:
                    known[code].update(start=s["start"], end=s["end"])   # Octopus can move them
        now = _now()
        for code, s in sorted(known.items(), key=lambda kv: kv[1]["start"]):
            start, end = _parse(s["start"]), _parse(s["end"])
            if end <= now:
                continue
            if not s.get("announced"):
                s["announced"] = True
                _notify("Free electricity session",
                        f"{_when(s)}. Anything above your usual use in that window is free."
                        + (" The battery will charge then." if SESSION_CHARGE else ""))
            if not s.get("warned") and start - SESSION_WARN <= now < start:
                s["warned"] = True
                _notify("Free electricity in 30 min",
                        f"{_when(s)}: run the washing machine, dishwasher or tumble dryer then.",
                        priority="high")
            if SESSION_CHARGE and start <= now < end and not s.get("charge"):
                import battery_control
                s["charge"] = battery_control.start(target=100, force=True, until=end,
                                                    reason="free electricity session")
        _save_sessions(known)
        upcoming = sum(1 for s in known.values() if _parse(s["end"]) > now)
        en._status("octopus_sessions", True, f"{len(known)} known, {upcoming} upcoming")
        return upcoming
    except Exception as e:
        print(f"[warn] Octopus sessions failed: {e}", file=sys.stderr)
        en._status("octopus_sessions", False, e)
        return None


def sessions_tick():
    """Every-5-min hook: act on known sessions without hitting the API."""
    return sessions(refresh=False)


# ---- Report -----------------------------------------------------------------------------

def band(t):
    """Cosy price band of a UTC instant, by local clock time."""
    h = t.astimezone(LOCAL).hour
    return "cheap" if (4 <= h < 7 or 13 <= h < 16 or h >= 22) else "peak" if 16 <= h < 19 else "standard"


class Pricer:
    """Price a half-hour for a meter point at the rate of the tariff it was actually on.

    Rate rows from different tariffs overlap in time (a variable tariff's open-ended rate
    keeps running alongside the Cosy rows), so picking "the latest rate before t" across
    all tariffs is wrong. It priced Apr 2025-Jan 2026 at a 7p Intelligent rate when the
    account was on Variable. Go agreement first, then that tariff's rate."""

    def __init__(self):
        with open(AGREEMENTS_FILE) as f:
            self.agreements = {
                mpan: [(_parse(a["valid_from"]), _parse(a["valid_to"]) if a.get("valid_to") else None, a["tariff_code"])
                       for a in ags if a.get("valid_from") and a.get("tariff_code")]
                for mpan, ags in json.load(f).items()}
        self.rates = {}
        with open(RATES_FILE, newline="") as f:
            for r in csv.DictReader(f):
                self.rates.setdefault(r["tariff_code"], []).append(
                    (_parse(r["valid_from"]), _parse(r["valid_to"]) if r["valid_to"] else None, float(r["p_inc_vat"])))
        for rows in self.rates.values():
            rows.sort(key=lambda x: x[0], reverse=True)    # newest first: first match wins

    def tariff(self, mpan, t):
        return next((c for a, b, c in self.agreements.get(mpan, []) if a <= t and (b is None or t < b)), None)

    def price(self, mpan, t):
        code = self.tariff(mpan, t)
        return next((p for a, b, p in self.rates.get(code, []) if a <= t and (b is None or t < b)), None), code


def report(days=30):
    """Import kWh and £ by Cosy band from the meter, priced at each half-hour's own tariff.
    `coverage` is the share of half-hours the meter has reported so far."""
    pricer = Pricer()
    since = _now() - dt.timedelta(days=days)
    bands, seen, unpriced = {}, set(), 0
    with open(CONSUMPTION_FILE, newline="") as f:
        for r in csv.DictReader(f):
            t = _parse(r["interval_start"])
            if r["direction"] != "import" or t < since:
                continue
            seen.add(t)
            p, _ = pricer.price(r["mpan"], t)
            unpriced += p is None
            kwh = float(r["kwh"])
            b = bands.setdefault(band(t), [0.0, 0.0])
            b[0] += kwh
            b[1] += kwh * (p or 0) / 100
    out = {k: {"kwh": round(v[0], 1), "gbp": round(v[1], 2)} for k, v in bands.items()}
    out["coverage"] = round(len(seen) / (days * 48), 3)
    out["unpriced_half_hours"] = unpriced
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["pull", "backfill", "sessions", "report"])
    ap.add_argument("--days", type=int, default=None)
    a = ap.parse_args()
    if a.action == "pull":
        print(pull(a.days))
    elif a.action == "backfill":
        print(backfill())
    elif a.action == "sessions":
        print(sessions())
        print(json.dumps(_load_sessions(), indent=1))
    else:
        print(json.dumps(report(a.days or 30), indent=1))
