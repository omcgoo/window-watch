import csv
import datetime as dt
import json

from tests.helpers import EnvTestCase

TOKEN = {"code": 0, "result": {"access_token": "tok", "expires_in": 3600}}


class SolaxPollTest(EnvTestCase):
    env = {"SOLAX_CLIENT_ID": "id", "SOLAX_CLIENT_SECRET": "secret"}

    def setUp(self):
        super().setUp()
        self.en = self.load("energy")
        self.data_time = "2026-10-03T00:05:00.000+00:00"
        self.expire_next = False
        self.http.route("oauth/token", lambda r, b: TOKEN)
        self.http.route("page_device_info", lambda r, b: {
            "code": 10000,
            "result": {"records": [{"deviceSn": "INV1" if "deviceType=1" in r.full_url else "BAT1"}]}})
        self.http.route("realtime_data", self._realtime)

    def _realtime(self, req, body):
        if self.expire_next:
            self.expire_next = False
            return {"code": 10401, "message": "token expired"}
        if "deviceType=1" in req.full_url:
            return {"code": 10000, "result": [{"deviceSn": "INV1", "gridPower": -15.0, "acPower1": 300,
                                               "totalImportEnergy": 1000.5, "deviceStatus": 102,
                                               "dataTime": self.data_time}]}
        return {"code": 10000, "result": [{"deviceSn": "BAT1", "batterySOC": 97,
                                           "chargeDischargePower": -310, "dataTime": self.data_time}]}

    def rows(self):
        with open(self.en.SOLAX_FILE, newline="") as f:
            return list(csv.DictReader(f))

    def test_logs_fields_from_inverter_and_battery(self):
        row = self.en.solax_poll()
        self.assertEqual(row["batterySOC"], 97)
        self.assertEqual(row["gridPower"], -15.0)
        self.assertEqual(row["deviceStatus"], 102)
        self.assertEqual(self.http.calls, ["token", "page_device_info", "page_device_info",
                                           "realtime_data", "realtime_data"])

    def test_skips_duplicate_cloud_reading(self):
        self.en.solax_poll()
        self.assertIsNone(self.en.solax_poll())
        self.assertEqual(len(self.rows()), 1)

    def test_renews_expired_token(self):
        self.en.solax_poll()
        self.data_time = "2026-10-03T00:10:00.000+00:00"
        self.expire_next = True
        self.assertIsNotNone(self.en.solax_poll())
        self.assertEqual(self.en._solax_token["value"], "tok")
        self.assertEqual(self.http.calls.count("token"), 2)

    def test_raw_payload_only_on_first_row_of_each_hour(self):
        clock = [dt.datetime(2026, 10, 3, 0, 5, tzinfo=dt.timezone.utc)]
        self.en._utcnow = lambda: clock[0]
        for minute, stamp in [(5, "00:05"), (10, "00:10"), (55, "00:55"), (65, "01:05"), (70, "01:10")]:
            clock[0] = dt.datetime(2026, 10, 3, tzinfo=dt.timezone.utc) + dt.timedelta(minutes=minute)
            self.data_time = f"2026-10-03T{stamp}:00.000+00:00"
            self.en.solax_poll()
        raw = [bool(r["raw"]) for r in self.rows()]
        self.assertEqual(raw, [True, False, False, True, False])

    def test_failure_is_recorded_not_raised(self):
        self.http.routes.insert(0, ("realtime_data", lambda r, b: OSError("network down")))
        self.assertIsNone(self.en.solax_poll())
        with open(self.en.STATUS_FILE) as f:
            self.assertIn("network down", json.load(f)["solax"]["last_error"])

    def test_unconfigured_does_nothing(self):
        self.en.SOLAX_CLIENT_ID = None
        self.assertIsNone(self.en.solax_poll())
        self.assertEqual(self.http.calls, [])


class VaillantUpsertTest(EnvTestCase):
    def test_later_pull_overwrites_same_bucket(self):
        en = self.load("energy")
        en._upsert_vaillant({("2026-10-01T00:00:00Z", "HEATING", "HEAT_GENERATED"): 100.0})
        total = en._upsert_vaillant({("2026-10-01T00:00:00Z", "HEATING", "HEAT_GENERATED"): 150.0,
                                     ("2026-10-01T01:00:00Z", "HEATING", "HEAT_GENERATED"): 90.0})
        self.assertEqual(total, 2)
        with open(en.VAILLANT_FILE, newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([r["wh"] for r in rows], ["150.0", "90.0"])
