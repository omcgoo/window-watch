import base64
import csv
import datetime as dt
import json
import urllib.error

from tests.helpers import EnvTestCase

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 20, 12, 0, tzinfo=UTC)

ACCOUNT = {"number": "A-1234", "properties": [{"electricity_meter_points": [
    {"mpan": "1900000000001", "is_export": False,
     "meters": [{"serial_number": "OLD"}, {"serial_number": "SMART"}],
     "agreements": [{"tariff_code": "E-1R-COSY-22-12-08-C", "valid_from": "2026-01-01T00:00:00Z", "valid_to": None},
                    {"tariff_code": "E-1R-VAR-22-11-01-C", "valid_from": "2024-01-01T00:00:00Z",
                     "valid_to": "2026-01-01T00:00:00Z"}]}]}]}


def consumption(start, n):
    return [{"interval_start": (start + dt.timedelta(minutes=30 * i)).isoformat(),
             "interval_end": (start + dt.timedelta(minutes=30 * (i + 1))).isoformat(),
             "consumption": 0.5} for i in range(n)]


class OctopusTest(EnvTestCase):
    env = {"OCTOPUS_API_KEY": "sk_live_test", "OCTOPUS_SESSION_CHARGE": "off"}

    def setUp(self):
        super().setUp()
        self.en = self.load("energy")
        self.oc = self.load("octopus")
        self.oc._now = lambda: NOW
        self.notes, self.auth_headers = [], []
        self.oc._notify = lambda title, body, priority="default": self.notes.append((title, priority))
        self.sessions = []
        self.http.route("/graphql/", self._graphql)
        self.http.route("/accounts/A-1234/", self._rest(ACCOUNT))
        self.http.route("/meters/OLD/consumption/", lambda r, b: urllib.error.HTTPError(r.full_url, 404, "nf", None, None))
        page2 = "https://api.octopus.energy/v1/electricity-meter-points/1900000000001/meters/SMART/consumption/?page=2"
        self.http.route("consumption/?page=2", self._rest({"results": consumption(NOW - dt.timedelta(hours=2), 2), "next": None}))
        self.http.route("/meters/SMART/consumption/", self._rest({"results": consumption(NOW - dt.timedelta(hours=4), 4),
                                                                  "next": page2}))
        self.http.route("standard-unit-rates", self._rates)

    def _rest(self, obj):
        def handler(req, body):
            self.auth_headers.append(req.headers.get("Authorization"))
            return obj
        return handler

    def _rates(self, req, body):
        self.assertIn("/products/COSY-22-12-08/electricity-tariffs/E-1R-COSY-22-12-08-C/", req.full_url)
        self.assertIsNone(req.headers.get("Authorization"))          # public endpoint
        return {"results": [{"value_inc_vat": 12.44, "valid_from": "2026-10-20T03:00:00Z", "valid_to": "2026-10-20T06:00:00Z"},
                            {"value_inc_vat": 25.36, "valid_from": "2026-10-20T06:00:00Z", "valid_to": "2026-10-20T12:00:00Z"}],
                "next": None}

    def _graphql(self, req, body):
        q = body["query"]
        if "obtainKrakenToken" in q:
            self.assertEqual(body["variables"], {"key": "sk_live_test"})
            return {"data": {"obtainKrakenToken": {"token": "KTOKEN"}}}
        self.assertEqual(req.headers.get("Authorization"), "KTOKEN")
        if "viewer" in q:
            return {"data": {"viewer": {"accounts": [{"number": "A-1234"}]}}}
        if "customerFlexibilityCampaignEvents" in q:
            self.assertEqual(body["variables"], {"account": "A-1234", "mpan": "1900000000001"})
            return {"data": {"customerFlexibilityCampaignEvents": {"edges": [{"node": s} for s in self.sessions]}}}
        raise AssertionError(q)

    def csv_rows(self, path):
        with open(path, newline="") as f:
            return list(csv.DictReader(f))


class PullTest(OctopusTest):
    def test_discovers_account_and_pulls_all_pages(self):
        self.assertIsNotNone(self.oc.pull(days=1))
        rows = self.csv_rows(self.oc.CONSUMPTION_FILE)
        self.assertEqual(len(rows), 6)                                # 4 + 2 over two pages
        self.assertEqual({r["serial"] for r in rows}, {"SMART"})       # the old meter's 404 is ignored
        self.assertEqual(rows[0]["direction"], "import")
        expected = "Basic " + base64.b64encode(b"sk_live_test:").decode()
        self.assertTrue(all(h == expected for h in self.auth_headers))

    def test_rates_only_for_agreements_in_range(self):
        self.oc.pull(days=1)
        rates = self.csv_rows(self.oc.RATES_FILE)
        self.assertEqual([r["p_inc_vat"] for r in rates], ["12.44", "25.36"])
        self.assertEqual({r["tariff_code"] for r in rates}, {"E-1R-COSY-22-12-08-C"})

    def test_upsert_is_idempotent(self):
        self.oc.pull(days=1)
        self.oc.pull(days=1)
        self.assertEqual(len(self.csv_rows(self.oc.CONSUMPTION_FILE)), 6)

    def test_report_costs_by_band(self):
        self.oc.pull(days=1)
        rep = self.oc.report(days=1)
        # 6 half-hours of 0.5 kWh, 08:00-11:00 UTC = 09:00-12:00 BST: all standard rate,
        # priced at the 25.36p that the rates file says applied 06:00-12:00 UTC.
        self.assertEqual(rep, {"standard": {"kwh": 3.0, "gbp": round(3.0 * 0.2536, 2)}})

    def test_no_key_does_nothing(self):
        self.oc.API_KEY = None
        self.assertIsNone(self.oc.pull())
        self.assertEqual(self.http.calls, [])


class SessionsTest(OctopusTest):
    def session(self, start, hours=1, code="FREE_1"):
        s = NOW + start
        self.sessions = [{"code": code, "name": "Free Electricity", "startAt": s.isoformat().replace("+00:00", "Z"),
                          "endAt": (s + dt.timedelta(hours=hours)).isoformat().replace("+00:00", "Z")}]

    def test_new_session_announced_once(self):
        self.session(dt.timedelta(days=1))
        self.assertEqual(self.oc.sessions(), 1)
        self.assertEqual(self.oc.sessions(), 1)
        self.assertEqual([n[0] for n in self.notes], ["Free electricity session"])

    def test_warns_half_an_hour_before(self):
        self.session(dt.timedelta(minutes=20))
        self.oc.sessions()
        self.assertEqual([n for n in self.notes if n[0] == "Free electricity in 30 min"],
                         [("Free electricity in 30 min", "high")])

    def test_no_battery_charge_unless_enabled(self):
        self.session(dt.timedelta(minutes=-10))
        calls = []
        import battery_control
        battery_control.start = lambda **k: calls.append(k) or "started"
        self.oc.sessions()
        self.assertEqual(calls, [])

    def test_charges_battery_through_session_when_enabled(self):
        self.session(dt.timedelta(minutes=-10))
        self.oc.SESSION_CHARGE = True
        calls = []
        import battery_control
        battery_control.start = lambda **k: calls.append(k) or "started"
        self.oc.sessions()
        self.oc.sessions_tick()                                   # no second start
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["until"], NOW + dt.timedelta(minutes=50))
        self.assertTrue(calls[0]["force"])

    def test_tick_does_not_hit_the_api(self):
        self.session(dt.timedelta(days=1))
        self.oc.sessions()
        self.http.calls.clear()
        self.oc.sessions_tick()
        self.assertEqual(self.http.calls, [])
