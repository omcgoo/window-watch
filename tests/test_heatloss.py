import csv
import datetime as dt
import json
import random
from zoneinfo import ZoneInfo

from tests.helpers import EnvTestCase

UTC = dt.timezone.utc
LOCAL = ZoneInfo("Europe/London")
H, G_SUN, G_INT, C = 100.0, 0.5, 300.0, 8000.0     # the "true" building the fit must recover
END = dt.datetime(2026, 12, 21, 3, 0, tzinfo=UTC)
HISTORY_HEADER = ["timestamp", "outdoor_c", "feels_like_c", "outdoor_humidity_pct", "wind_kmh", "gusts_kmh",
                  "solar_wm2", "cloud_pct", "precip_mm", "indoor_c", "indoor_humidity_pct", "battery_pct",
                  "status", "window_actual", "blinds_actual", "zones", "outdoor_source", "outdoor_forecast_c"]


class HeatLossTest(EnvTestCase):
    def setUp(self):
        super().setUp()
        self.en = self.load("energy")
        self.hl = self.load("heatloss")
        self.hl._now = lambda: END

    def build(self, n_days, noise=0.0, heating=True):
        rng = random.Random(7)
        vh = []
        with open(self.path("history.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(HISTORY_HEADER)
            for i in range(n_days):
                day = (END - dt.timedelta(days=n_days - i)).astimezone(LOCAL).date()
                t_out = 2 + 10 * rng.random()
                t_in = 19 + rng.random()
                sun = 20 + 100 * rng.random()
                drift = rng.uniform(-1.0, 1.0)                 # T_in change over the day, K
                rows = []
                for k in range(48):
                    local = dt.datetime.combine(day, dt.time(0, 0), LOCAL) + dt.timedelta(minutes=30 * k)
                    tin_k = t_in - drift / 2 + drift * k / 47
                    zones = {"outside": {"t": t_out}, "hallway_downstairs": {"t": tin_k}, "bedroom": {"t": tin_k},
                             "loft": {"t": tin_k - 0.4 * (tin_k - t_out)}}
                    rows.append((local, tin_k))
                    w.writerow([local.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), t_out, "", "", "", "", sun, "",
                                "", tin_k, "", "", "open", "", "", json.dumps(zones), "sensor", ""])
                dtin_per_h = (rows[-1][1] - rows[0][1]) / 24.0
                q = H * (t_in - t_out) - G_SUN * sun - G_INT + C * dtin_per_h
                q *= 1 + noise * rng.uniform(-1, 1)
                for hour in range(24):
                    start = dt.datetime.combine(day, dt.time(hour), LOCAL).astimezone(UTC)
                    vh.append((start.strftime("%Y-%m-%dT%H:%M:%SZ"), "HEATING", "HEAT_GENERATED",
                               q if heating else 0.0))
        self.en._upsert_vaillant({(a, b, c): d for a, b, c, d in vh})

    def test_recovers_known_heat_loss(self):
        self.build(21)
        r = self.hl.estimate(days=28)
        self.assertEqual(r["status"], "ok")
        self.assertAlmostEqual(r["H_w_per_k"], H, delta=0.5)
        self.assertAlmostEqual(r["ceiling_share_night"], 0.4, places=2)

    def test_interval_brackets_truth_with_noise(self):
        self.build(24, noise=0.05)
        r = self.hl.estimate(days=28)
        lo, hi = r["ci90"]
        self.assertLess(lo, H)
        self.assertGreater(hi, H)
        self.assertLess(hi - lo, 60)

    def test_waits_for_enough_heating_days(self):
        self.build(6)
        r = self.hl.estimate(days=28)
        self.assertEqual((r["status"], r["heating_days"]), ("waiting", 6))

    def test_days_without_heating_are_ignored(self):
        self.build(14, heating=False)
        self.assertEqual(self.hl.estimate(days=28)["heating_days"], 0)

    def test_weekly_appends_history_and_notifies_once_ok(self):
        notes = []
        self.hl._notify = lambda title, body: notes.append(body)
        self.build(21)
        self.hl.weekly()
        self.hl.weekly()
        with open(self.hl.OUT_FILE) as f:
            store = json.load(f)
        self.assertEqual(len(store["history"]), 2)
        self.assertEqual(len(notes), 2)
        self.assertIn("100 W/K", notes[0])
        self.assertIn("last week 100", notes[1])
