import json
import urllib.error

from tests.helpers import EnvTestCase

GOOD = {"temp": 10.0, "feels_like": 8.0, "humidity": 90, "wind_kmh": 12.0, "gusts_kmh": 25.0,
        "solar_wm2": 150.0, "precip_mm": 0.0, "cloud_pct": 60}
OPEN_METEO = {"current": {"temperature_2m": 9.9, "apparent_temperature": 8, "relative_humidity_2m": 80,
                          "wind_speed_10m": 5, "wind_gusts_10m": 9, "shortwave_radiation": 0,
                          "precipitation": 0, "cloud_cover": 90}}


def http_503(*a):
    return urllib.error.HTTPError("https://api.open-meteo.com", 503, "Service Unavailable", None, None)


class OutdoorFallbackTest(EnvTestCase):
    def setUp(self):
        super().setUp()
        w = self.w = self.load("window_watch")
        self.logged, self.dash = [], []
        self.zones = {"outside": {"t": 11.2, "rh": 82.0, "batt": 60}}
        w.get_indoor_shelly = lambda: {"temp": 20.5, "humidity": 55, "battery": 64}
        w.get_zones = lambda: dict(self.zones)
        w.get_forecast = lambda: (_ for _ in ()).throw(RuntimeError("forecast down"))
        w.update_dashboard = lambda outdoor, status, indoor, *a, **k: self.dash.append((outdoor, status, indoor))
        w.notify = lambda *a, **k: None
        w.log_history = lambda *a, **k: self.logged.append(dict(a[0]))
        w.time.sleep = lambda s: None
        self.real_get_outdoor = w.get_outdoor

    def state(self):
        with open(self.path("state.json")) as f:
            return json.load(f)

    def run_main(self, outdoor):
        self.w.get_outdoor = outdoor
        self.w.main()

    def test_live_indoor_reading_used_when_forecast_fails(self):
        self.run_main(lambda: dict(GOOD))
        self.assertEqual(self.dash[-1][2], 20.5)

    def test_fresh_reading_is_logged_and_cached(self):
        self.run_main(lambda: dict(GOOD))
        self.assertEqual(self.logged[-1]["temp"], 11.2)                 # sensor overrides the logged temp
        self.assertEqual(self.state()["last_outdoor"]["data"]["temp"], 10.0)  # cache keeps Open-Meteo's

    def test_outage_with_fresh_cache_advises_but_does_not_log(self):
        self.run_main(lambda: dict(GOOD))
        self.logged.clear()
        self.run_main(lambda: (_ for _ in ()).throw(http_503()))
        self.assertEqual(self.logged, [])
        self.assertEqual(self.dash[-1][0], 11.2)
        self.assertEqual(self.state()["last_outdoor"]["data"]["solar_wm2"], 150.0)

    def test_stale_cache_falls_back_to_sensor_alone(self):
        stale = {"data": GOOD, "utc": "2020-01-01T00:00:00Z"}
        d = self.w.outdoor_fallback(stale, self.zones["outside"])
        self.assertEqual((d["temp"], d["humidity"], d["solar_wm2"]), (11.2, 82, 0.0))

    def test_no_cache_and_no_sensor_raises(self):
        with self.assertRaises(RuntimeError):
            self.w.outdoor_fallback(None, None)

    def test_cache_survives_day_reset(self):
        with open(self.path("state.json"), "w") as f:
            json.dump({"day": "2000-01-01", "last_outdoor": {"data": GOOD, "utc": "2099-01-01T00:00:00Z"}}, f)
        self.run_main(lambda: (_ for _ in ()).throw(http_503()))
        self.assertEqual(self.state()["last_outdoor"]["utc"], "2099-01-01T00:00:00Z")

    def test_get_outdoor_retries_once(self):
        n = [0]

        def flaky(req, body):
            n[0] += 1
            return http_503() if n[0] == 1 else OPEN_METEO
        self.http.route("open-meteo", flaky)
        self.assertEqual(self.real_get_outdoor()["temp"], 9.9)
        self.assertEqual(n[0], 2)

    def test_get_outdoor_gives_up_after_two_attempts(self):
        self.http.route("open-meteo", lambda r, b: http_503())
        with self.assertRaises(urllib.error.HTTPError):
            self.real_get_outdoor()
        self.assertEqual(len(self.http.calls), 2)
