import csv
import datetime as dt
import json
import os

from tests.helpers import EnvTestCase

UTC = dt.timezone.utc
BST = dt.timezone(dt.timedelta(hours=1))


def sunday(h, m):
    return dt.datetime(2026, 10, 18, h, m, tzinfo=BST).astimezone(UTC)


class BatteryControlTest(EnvTestCase):
    mode = "on"

    def setUp(self):
        self.env = {"SOLAX_CLIENT_ID": "id", "SOLAX_CLIENT_SECRET": "secret", "BATTERY_CONTROL": self.mode}
        super().setUp()
        self.en = self.load("energy")
        self.bc = self.load("battery_control")
        self.clock = [sunday(13, 5)]
        self.bc._now = lambda: self.clock[0]
        self.bc.time.sleep = lambda s: None
        self.notes = []
        self.bc._notify = lambda title, body, priority="default": self.notes.append((title, priority))
        self.cmd_status = 4
        self.http.route("oauth/token", lambda r, b: {"code": 0, "result": {"access_token": "t", "expires_in": 3600}})
        self.http.route("page_device_info", lambda r, b: {"code": 10000, "result": {"records": [{"deviceSn": "INV1"}]}})
        self.http.route("soc_target_control_mode", self._start)
        self.http.route("exit_vpp_mode", lambda r, b: {"code": 10000, "result": {"requestId": "REQ2"}})
        self.http.route("listByCondition", lambda r, b: {"code": 0, "result": [{"requestId": b["requestId"],
                                                                                 "status": self.cmd_status}]})
        self.reading()

    def _start(self, req, body):
        self.assertEqual(body, {"snList": ["INV1"], "targetSoc": 100, "chargeDischargPower": 3000, "businessType": 1})
        return {"code": 10000, "result": "REQ1"}

    def reading(self, status=102, soc=80, age_min=2):
        now = self.clock[0]
        with open(self.en.SOLAX_FILE, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=self.en.SOLAX_HEADER)
            w.writeheader()
            for minutes, s in [(age_min + 40, soc), (age_min + 20, 99), (age_min + 5, 99), (age_min, soc)]:
                w.writerow({"timestamp": (now - dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "batterySOC": s, "deviceStatus": status, "raw": ""})

    def state(self):
        with open(self.bc.STATE_FILE) as f:
            return json.load(f)

    def sent(self, name):
        return self.http.calls.count(name)


class GuardsTest(BatteryControlTest):
    def test_outside_window_skips(self):
        self.clock[0] = sunday(12, 30)
        self.reading()
        self.assertEqual(self.bc.start(), "skipped")
        self.assertEqual(self.http.calls, [])

    def test_stale_reading_skips(self):
        self.reading(age_min=30)
        self.assertEqual(self.bc.start(), "skipped")

    def test_not_normal_skips(self):
        self.reading(status=130)
        self.assertEqual(self.bc.start(), "skipped")

    def test_already_full_skips(self):
        self.reading(soc=100)
        self.assertEqual(self.bc.start(), "skipped")

    def test_force_skips_only_the_time_window(self):
        self.clock[0] = sunday(21, 0)
        self.reading()
        self.assertEqual(self.bc.start(force=True), "started")


class LifecycleTest(BatteryControlTest):
    def test_rejected_start_leaves_no_state_and_tells_user(self):
        self.cmd_status = 5
        self.assertEqual(self.bc.start(), "failed")
        self.assertFalse(os.path.exists(self.bc.STATE_FILE))
        self.assertTrue(self.notes[0][0].startswith("Battery charge didn't"))

    def test_start_watchdog_exit_done(self):
        self.assertEqual(self.bc.start(), "started")
        self.assertEqual(self.state()["phase"], "charging")
        self.clock[0] = sunday(15, 0)
        self.assertIsNone(self.bc.watchdog())                       # mid-window: leave it alone
        self.clock[0] = sunday(16, 5)
        self.assertEqual(self.bc.watchdog(), "exiting")             # 15:55 missed: exit now
        self.assertEqual(self.state()["exit_attempts"], 1)
        self.reading(status=102, soc=97)
        self.clock[0] = sunday(16, 15)
        self.assertEqual(self.bc.watchdog(), "done")
        self.assertEqual(self.state()["peak_soc"], 99)

    def test_custom_end_time_is_honoured(self):
        self.clock[0] = sunday(20, 0)
        self.reading()
        end = sunday(21, 0)
        self.assertEqual(self.bc.start(force=True, until=end, reason="free electricity"), "started")
        self.assertEqual(self.state()["end_utc"], end.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.clock[0] = sunday(20, 30)
        self.assertIsNone(self.bc.watchdog())          # past 16:00 but before its own end
        self.clock[0] = sunday(21, 0)
        self.assertEqual(self.bc.watchdog(), "exiting")

    def test_stuck_override_three_exits_then_one_high_alert(self):
        self.bc.start()
        self.clock[0] = sunday(15, 55)
        self.bc.stop()
        for m in range(5, 85, 5):
            self.clock[0] = sunday(15, 55) + dt.timedelta(minutes=m)
            self.reading(status=130, soc=100)
            self.bc.watchdog()
        self.assertEqual(self.state()["exit_attempts"], 3)
        alerts = [n for n in self.notes if n[0].startswith("Battery stuck")]
        self.assertEqual(alerts, [("Battery stuck in remote-control mode", "high")])


class DryRunTest(BatteryControlTest):
    mode = "dryrun"

    def test_sends_nothing_writes_no_state(self):
        self.assertEqual(self.bc.start(), "dryrun")
        self.assertEqual(self.sent("soc_target_control_mode"), 0)
        self.assertFalse(os.path.exists(self.bc.STATE_FILE))


class OffTest(BatteryControlTest):
    mode = "off"

    def test_start_does_nothing(self):
        self.assertEqual(self.bc.start(), "skipped")
        self.assertEqual(self.http.calls, [])

    def test_stop_still_clears_an_override(self):
        with open(self.bc.STATE_FILE, "w") as f:
            json.dump({"phase": "charging", "started_utc": "2026-10-18T12:05:00Z"}, f)
        self.assertEqual(self.bc.stop(), "exiting")
        self.assertEqual(self.sent("exit_vpp_mode"), 1)
