#!/usr/bin/env python3
"""Energy logging: SolaX battery/inverter every few minutes, Vaillant heat pump weekly.

Kept apart from window_watch.py on purpose. The open/close advisory is the primary job
and must never fail because a battery or heat-pump API is down, so nothing here is
imported by the advisory path and every entry point swallows its own errors.

What it writes (all under /data on Fly, alongside history.csv):

  solax.csv              one row per new SolaX reading (~every 5 min), from the SolaX
                         Developer OpenAPI (OAuth client credentials). Columns use SolaX's
                         own field names, first non-null across the inverter and battery
                         records; `raw` keeps both records whole on the first row of each
                         UTC hour only (empty otherwise, for size). Sign conventions of
                         gridPower and chargeDischargePower are NOT yet verified against this
                         install — check against a known charge window before trusting them.
                         First live read (00:13 BST, SOC 99%, grid 0 W) showed
                         chargeDischargePower -396 W, i.e. negative = discharging.
                         totalImportEnergy is cumulative grid import (kWh): diff it across
                         Cosy bands for cost.
  vaillant_hourly.csv    hourly energy buckets in Wh, keyed (start_utc, mode, energy_type),
                         upserted so overlapping pulls are idempotent.
                         HEATING:HEAT_GENERATED is delivered space heat — the HTC input.
  vaillant_settings.jsonl  one line per pull: schedule, setback, heating curve, mode. The
                         API only exposes *current* settings, so this is the only record
                         of when they change.
  energy_status.json     last success/failure per source, so staleness is visible.

Vaillant's API quota is tight (exhausted on 2026-10-02 by ~170 calls in a few minutes),
and HOUR data is capped at 3 days per request, each request fanning out to six calls
(two modes x three energy types). A weekly pull of 9 days is ~20 calls. Backfills must
pace themselves — see --pace.

Usage:
  python energy.py solax                  # one SolaX poll
  python energy.py vaillant [--days 9]    # pull the last N days of hourly data
  python energy.py vaillant --start 2025-10-01 --end 2026-05-01 --pace 120
                                          # slow backfill, 2 min between chunks; saves each
                                          # chunk as it lands, writes no settings snapshot
"""
import argparse
import asyncio
import csv
import datetime as dt
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DATA_DIR = os.getenv("ENERGY_DATA_DIR", "/data")
SOLAX_FILE = os.path.join(DATA_DIR, "solax.csv")
VAILLANT_FILE = os.path.join(DATA_DIR, "vaillant_hourly.csv")
VAILLANT_SETTINGS_FILE = os.path.join(DATA_DIR, "vaillant_settings.jsonl")
STATUS_FILE = os.path.join(DATA_DIR, "energy_status.json")

SOLAX_CLIENT_ID = os.getenv("SOLAX_CLIENT_ID")
SOLAX_CLIENT_SECRET = os.getenv("SOLAX_CLIENT_SECRET")
SOLAX_BASE = os.getenv("SOLAX_BASE") or "https://openapi-eu.solaxcloud.com"   # UK is served by EU
SOLAX_BUSINESS_TYPE = 1                     # 1 = residential (4 = C&I)
SOLAX_DEVICE_TYPES = {"inverter": 1, "battery": 2}
SOLAX_OK_TOKEN, SOLAX_OK_API = 0, 10000     # success codes differ between token and data calls
SOLAX_AUTH_ERRORS = {10400, 10401, 10402}

VAILLANT_USER = os.getenv("VAILLANT_USER")
VAILLANT_PASS = os.getenv("VAILLANT_PASS")
VAILLANT_COUNTRY = os.getenv("VAILLANT_COUNTRY") or "unitedkingdom"
VAILLANT_HOUR_CHUNK_DAYS = 3                # API maximum for HOUR resolution

# Names as this X1-Hybrid-LV + battery actually return them (checked live 2026-10-03; they
# differ from the reference client's FIELD_UNITS, e.g. totalImportEnergy not totalImported).
SOLAX_FIELDS = ["batterySOC", "chargeDischargePower", "gridPower", "totalActivePower",
                "acPower1", "totalImportEnergy", "totalExportEnergy", "todayImportEnergy",
                "totalDeviceCharge", "totalDeviceDischarge", "batteryTemperature", "deviceStatus"]
SOLAX_HEADER = ["timestamp", "data_time"] + SOLAX_FIELDS + ["raw"]
SOLAX_TIME_KEYS = ("dataTime", "uploadTime", "updateTime", "dataTimeUtc")
VAILLANT_HEADER = ["start_utc", "mode", "energy_type", "wh"]


def _utcnow():
    return dt.datetime.now(dt.timezone.utc)


def _status(source, ok, detail=""):
    """Record last success/failure per source. Never raises — status is best-effort."""
    try:
        try:
            with open(STATUS_FILE) as f:
                st = json.load(f)
        except (OSError, ValueError):
            st = {}
        entry = st.setdefault(source, {})
        now = _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        entry["last_ok" if ok else "last_fail"] = now
        if not ok:
            entry["last_error"] = str(detail)[:300]
        elif detail:
            entry["last_detail"] = str(detail)[:300]
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f, indent=1)
        os.replace(tmp, STATUS_FILE)
    except Exception as e:
        print(f"[warn] energy status write failed: {e}", file=sys.stderr)


# ---- SolaX ------------------------------------------------------------------------------

# Process-lifetime caches: the access token (valid ~hours) and the device serials, which
# are discovered once rather than configured, so no serial number lives in config.
_solax_token = {"value": None, "expires": 0.0}
_solax_devices = {}                         # {"inverter": [sn, ...], "battery": [sn, ...]}


def _solax_token_get(force=False):
    if not force and _solax_token["value"] and time.time() < _solax_token["expires"] - 300:
        return _solax_token["value"]
    data = urllib.parse.urlencode({"client_id": SOLAX_CLIENT_ID, "client_secret": SOLAX_CLIENT_SECRET,
                                   "grant_type": "client_credentials"}).encode()
    req = urllib.request.Request(f"{SOLAX_BASE}/openapi/auth/oauth/token", data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as r:
        body = json.load(r)
    if body.get("code") != SOLAX_OK_TOKEN or not (body.get("result") or {}).get("access_token"):
        raise RuntimeError(f"SolaX token: {body.get('code')} {body.get('message')}")
    res = body["result"]
    _solax_token.update(value=res["access_token"],
                        expires=time.time() + int(res.get("expires_in") or 3600))
    return _solax_token["value"]


def _solax_get(path, params, _retried=False):
    url = f"{SOLAX_BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"bearer {_solax_token_get()}",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        body = json.load(r)
    code = body.get("code")
    if code in SOLAX_AUTH_ERRORS and not _retried:
        _solax_token_get(force=True)
        return _solax_get(path, params, _retried=True)
    if code != SOLAX_OK_API:
        raise RuntimeError(f"SolaX {path}: {code} {body.get('message')}")
    return body.get("result")


def _solax_discover():
    """Find this account's inverter and battery serials (first call only)."""
    if _solax_devices:
        return _solax_devices
    for kind, dtype in SOLAX_DEVICE_TYPES.items():
        res = _solax_get("/openapi/v2/device/page_device_info",
                         {"businessType": SOLAX_BUSINESS_TYPE, "deviceType": dtype, "pageNo": 1})
        records = res.get("records", []) if isinstance(res, dict) else (res or [])
        _solax_devices[kind] = [r["deviceSn"] for r in records if r.get("deviceSn")]
    if not any(_solax_devices.values()):
        _solax_devices.clear()
        raise RuntimeError("SolaX: no devices visible to this app — is it authorised for the plant?")
    return _solax_devices


def _solax_fetch():
    """Realtime records for every inverter and battery: {"inverter": [...], "battery": [...]}."""
    out = {}
    for kind, sns in _solax_discover().items():
        if sns:
            res = _solax_get("/openapi/v2/device/realtime_data",
                             {"snList": ",".join(sns), "deviceType": SOLAX_DEVICE_TYPES[kind],
                              "businessType": SOLAX_BUSINESS_TYPE})
            out[kind] = res if isinstance(res, list) else [res]
    return out


def _first(records, key):
    """First non-null value of `key` across inverter then battery records."""
    for kind in ("inverter", "battery"):
        for rec in records.get(kind) or []:
            if isinstance(rec, dict) and rec.get(key) not in (None, ""):
                return rec[key]
    return ""


def _last_row(path):
    """Last data row of a CSV as a dict, or None. Reads only the tail of the file."""
    try:
        with open(path, "rb") as f:
            header = f.readline().decode()
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 8192))
            tail = f.read().decode(errors="ignore").splitlines()
    except OSError:
        return None
    rows = [l for l in tail if l.strip() and l != header.strip()]
    if not rows:
        return None
    return next(csv.DictReader(io.StringIO(header + rows[-1] + "\n")), None)


def solax_poll():
    """Append one row if SolaX has a new reading since the last row. Returns the row or None."""
    if not (SOLAX_CLIENT_ID and SOLAX_CLIENT_SECRET):
        return None
    try:
        recs = _solax_fetch()
        fields = {k: _first(recs, k) for k in SOLAX_FIELDS}
        # Dedupe on the device's own timestamp when it has one, else on the logged fields:
        # the cloud only refreshes every few minutes, and repeats would fake a flat line.
        data_time = next((str(t) for t in (_first(recs, k) for k in SOLAX_TIME_KEYS) if t), "")
        last = _last_row(SOLAX_FILE)
        if last and ((data_time and last.get("data_time") == data_time)
                     or (not data_time and all(str(last.get(k)) == str(v) for k, v in fields.items()))):
            return None
        now = _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        # The full payload is ~3 KB (the inverter returns ~55 fields), so every-5-min raw
        # would grow the file ~300 MB/yr and bloat every weekly backup copy. Keep it on the
        # first row of each UTC hour only: a full sample survives for any field we later
        # want, at ~1/12 the size.
        first_of_hour = not last or (last.get("timestamp") or "")[:13] != now[:13]
        raw = json.dumps(recs, separators=(",", ":"), sort_keys=True) if first_of_hour else ""
        row = {"timestamp": now, "data_time": data_time, **fields, "raw": raw}
        new = not os.path.exists(SOLAX_FILE) or os.path.getsize(SOLAX_FILE) == 0
        with open(SOLAX_FILE, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=SOLAX_HEADER)
            if new:
                w.writeheader()
            w.writerow(row)
        _status("solax", True)
        return row
    except Exception as e:
        print(f"[warn] SolaX poll failed: {e}", file=sys.stderr)
        _status("solax", False, e)
        return None


# ---- Vaillant ---------------------------------------------------------------------------

def _settings_snapshot(system):
    """Current controller settings as plain JSON. getattr throughout: field names vary
    across myPyllant versions and a missing one must not lose the energy data."""
    def slots(tp, day):
        return [[s.start_time, s.end_time, getattr(s, "setpoint", None)]
                for s in (getattr(tp, day, None) or [])]
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    snap = {"utc": _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "outdoor_temperature": getattr(system, "outdoor_temperature", None),
            "zones": [], "circuits": []}
    for z in system.zones:
        h = getattr(z, "heating", None)
        tp = getattr(h, "time_program_heating", None)
        snap["zones"].append({
            "name": getattr(z, "name", None),
            "room_temperature": getattr(z, "current_room_temperature", None),
            "desired_setpoint": getattr(z, "desired_room_temperature_setpoint", None),
            "operation_mode": str(getattr(h, "operation_mode_heating", None)),
            "set_back_temperature": getattr(h, "set_back_temperature", None),
            "manual_setpoint": getattr(h, "manual_mode_setpoint_heating", None),
            "time_program": {d: slots(tp, d) for d in days} if tp else None,
        })
    for c in system.circuits:
        snap["circuits"].append({
            "heating_curve": getattr(c, "heating_curve", None),
            "heat_demand_limited_by_outside_temperature":
                getattr(c, "heat_demand_limited_by_outside_temperature", None),
            "min_flow_temperature_setpoint": getattr(c, "min_flow_temperature_setpoint", None),
            "max_flow_temperature_setpoint": getattr(c, "max_flow_temperature_setpoint", None),
        })
    return snap


async def _vaillant_fetch(start, end, pace, on_chunk):
    """Fetch hourly buckets for [start, end) in 3-day chunks, handing each chunk's rows to
    on_chunk as it lands, so a failure part-way through keeps everything fetched so far.
    Returns (bucket count, settings snapshots)."""
    from myPyllant.api import MyPyllantAPI
    from myPyllant.enums import DeviceDataBucketResolution

    n, snaps = 0, []
    async with MyPyllantAPI(VAILLANT_USER, VAILLANT_PASS, "vaillant", VAILLANT_COUNTRY) as api:
        async def fresh_token():
            # Vaillant's access token lives only ~5 min and myPyllant never renews it on its
            # own, so a paced backfill died with 401 "Invalid JWT" on 2026-10-02. Renew
            # ahead of expiry before every chunk.
            exp = getattr(api, "oauth_session_expires", None)
            if exp and exp - _utcnow() < dt.timedelta(minutes=2):
                await api.refresh_token()

        async for system in api.get_systems():
            snaps.append(_settings_snapshot(system))
            for dev in system.devices:
                if dev.type != "primary_heat_generator":
                    continue
                a = start
                while a < end:
                    b = min(a + dt.timedelta(days=VAILLANT_HOUR_CHUNK_DAYS), end)
                    await fresh_token()
                    chunk = {}
                    async for dd in api.get_data_by_device(dev, DeviceDataBucketResolution.HOUR, a, b):
                        for bucket in dd.data:
                            if bucket.value is None:
                                continue
                            key = (bucket.start_date.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                   dd.operation_mode, dd.energy_type)
                            chunk[key] = round(bucket.value, 3)
                    on_chunk(chunk)
                    n += len(chunk)
                    a = b
                    if pace and a < end:
                        await asyncio.sleep(pace)
    return n, snaps


def _upsert_vaillant(new_rows):
    """Merge into vaillant_hourly.csv. A later pull overwrites an earlier value for the
    same bucket (the current hour is partial when first pulled). Atomic temp+rename."""
    merged = {}
    try:
        with open(VAILLANT_FILE, newline="") as f:
            for r in csv.DictReader(f):
                merged[(r["start_utc"], r["mode"], r["energy_type"])] = r["wh"]
    except OSError:
        pass
    merged.update(new_rows)
    tmp = VAILLANT_FILE + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(VAILLANT_HEADER)
        for k in sorted(merged):
            w.writerow([*k, merged[k]])
    os.replace(tmp, VAILLANT_FILE)
    return len(merged)


def vaillant_pull(days=9, pace=0, start=None, end=None):
    """Pull hourly heat-pump energy, by default the last `days` plus a settings snapshot.

    Default 9 days = three full 3-day chunks, so a weekly run overlaps the previous one
    by two days and a single missed week loses nothing (Vaillant retains ~2 years).

    With explicit start/end (a backfill) no settings snapshot is written: the API only
    exposes *current* settings, so filing one against a past range would be false."""
    if not (VAILLANT_USER and VAILLANT_PASS):
        return None
    backfill = start is not None or end is not None
    end = end or _utcnow().replace(minute=0, second=0, microsecond=0)
    start = start or end - dt.timedelta(days=days)
    source = "vaillant_backfill" if backfill else "vaillant"
    total = [0]

    def save(chunk):
        total[0] = _upsert_vaillant(chunk)
        if backfill:
            print(f"vaillant backfill: +{len(chunk)} buckets, file now {total[0]} rows", flush=True)

    try:
        n, snaps = asyncio.run(_vaillant_fetch(start, end, pace, save))
        if not backfill:
            with open(VAILLANT_SETTINGS_FILE, "a") as f:
                for s in snaps:
                    f.write(json.dumps(s, separators=(",", ":")) + "\n")
        detail = f"{n} buckets {start:%Y-%m-%d}..{end:%Y-%m-%d}, file now {total[0]} rows"
        print(f"vaillant pull: {detail}")
        _status(source, True, detail)
        return n
    except Exception as e:
        print(f"[warn] Vaillant pull failed: {e}", file=sys.stderr)
        _status(source, False, e)
        return None


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("source", choices=["solax", "vaillant"])
    p.add_argument("--days", type=int, default=9, help="vaillant: days of hourly data to pull")
    p.add_argument("--pace", type=float, default=0,
                   help="vaillant: seconds to wait between 3-day chunks (use for backfills)")
    p.add_argument("--start", help="vaillant backfill: start date YYYY-MM-DD (UTC)")
    p.add_argument("--end", help="vaillant backfill: end date YYYY-MM-DD (UTC), exclusive")
    a = p.parse_args()
    if a.source == "solax":
        r = solax_poll()
        print(json.dumps(r, indent=1) if r else "no new SolaX row (unconfigured, unchanged, or failed)")
    else:
        day = lambda s: dt.datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc) if s else None
        sys.exit(0 if vaillant_pull(a.days, a.pace, day(a.start), day(a.end)) is not None else 1)
