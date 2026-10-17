import base64
import csv
import datetime as dt
import json
import urllib.error
import urllib.parse

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
        self.meter = consumption(NOW - dt.timedelta(hours=4), 6)     # the meter's true half-hours
        self.http.route("/meters/SMART/consumption/", self._consumption)
        self.rate_windows = []
        self.http.route("standard-unit-rates", self._rates)

    def _rest(self, obj):
        def handler(req, body):
            self.auth_headers.append(req.headers.get("Authorization"))
            return obj
        return handler

    @staticmethod
    def _window(req):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(req.full_url).query))
        f = lambda s: dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return f(q["period_from"]), f(q["period_to"])

    def _consumption(self, req, body):
        self.auth_headers.append(req.headers.get("Authorization"))
        a, b = self._window(req)
        self.assertLessEqual(b - a, dt.timedelta(days=30))              # one page per window, always
        rows = [r for r in self.meter if a <= dt.datetime.fromisoformat(r["interval_start"]) < b]
        return {"count": len(rows), "next": None, "results": rows}

    def _rates(self, req, body):
        self.assertIsNone(req.headers.get("Authorization"))          # public endpoint
        self.rate_windows.append((req.full_url.split("/electricity-tariffs/")[1].split("/")[0], *self._window(req)))
        if "COSY" not in req.full_url:
            return {"count": 0, "next": None, "results": []}
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
        self.assertEqual(len(rows), 6)
        self.assertEqual({r["serial"] for r in rows}, {"SMART"})       # the old meter's 404 is ignored
        self.assertEqual(rows[0]["direction"], "import")
        expected = "Basic " + base64.b64encode(b"sk_live_test:").decode()
        self.assertTrue(all(h == expected for h in self.auth_headers))

    def test_truncated_page_fails_loudly(self):
        self.http.routes.insert(0, ("/meters/SMART/consumption/",
                                    lambda r, b: {"count": 3, "next": "https://x?page=2", "results": self.meter[:1]}))
        self.assertIsNone(self.oc.pull(days=1))
        with open(self.en.STATUS_FILE) as f:
            self.assertIn("page truncated", json.load(f)["octopus"]["last_error"])

    def test_rates_fetched_for_every_stint_on_a_repeated_tariff(self):
        """Regression: the same tariff twice (Variable 2023-25 and again 2025-26) used to
        dedupe on the code and skip the second stint's rates."""
        acct = json.loads(json.dumps(ACCOUNT))
        acct["properties"][0]["electricity_meter_points"][0]["agreements"] = [
            {"tariff_code": "E-1R-VAR-22-11-01-C", "valid_from": "2025-01-01T00:00:00Z", "valid_to": "2025-04-14T00:00:00Z"},
            {"tariff_code": "E-1R-INTELLI-FIX-12M-25-04-10-C", "valid_from": "2025-04-14T00:00:00Z", "valid_to": "2025-04-17T00:00:00Z"},
            {"tariff_code": "E-1R-VAR-22-11-01-C", "valid_from": "2025-04-17T00:00:00Z", "valid_to": "2026-01-31T00:00:00Z"},
            {"tariff_code": "E-1R-COSY-22-12-08-C", "valid_from": "2026-01-31T00:00:00Z", "valid_to": None}]
        self.http.routes.insert(0, ("/accounts/A-1234/", self._rest(acct)))
        self.oc.pull(days=730)
        var = sorted((a, b) for code, a, b in self.rate_windows if code == "E-1R-VAR-22-11-01-C")
        self.assertEqual(var[0][0], dt.datetime(2025, 1, 1, tzinfo=UTC))        # first stint's start
        self.assertEqual(var[-1][1], dt.datetime(2026, 1, 31, tzinfo=UTC))      # second stint reached
        self.assertTrue(any(a <= dt.datetime(2025, 10, 1, tzinfo=UTC) < b for a, b in var))

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
        self.assertEqual(rep["standard"], {"kwh": 3.0, "gbp": round(3.0 * 0.2536, 2)})
        self.assertEqual(rep["unpriced_half_hours"], 0)
        self.assertEqual(rep["coverage"], round(6 / 48, 3))

    def test_prices_by_the_agreement_in_force_not_the_latest_rate(self):
        """Regression: overlapping rate rows from different tariffs. Apr 2025-Jan 2026 was
        priced at an Intelligent 7p rate when the account was actually on Variable."""
        mpan = "1900000000001"
        with open(self.oc.AGREEMENTS_FILE, "w") as f:
            json.dump({mpan: [
                {"tariff_code": "E-1R-VAR-22-11-01-C", "valid_from": "2025-04-17T00:00:00Z", "valid_to": "2026-01-31T00:00:00Z"},
                {"tariff_code": "E-1R-INTELLI-FIX-12M-25-04-10-C", "valid_from": "2025-04-14T00:00:00Z", "valid_to": "2025-04-17T00:00:00Z"},
                {"tariff_code": "E-1R-COSY-22-12-08-C", "valid_from": "2026-01-31T00:00:00Z", "valid_to": None}]}, f)
        with open(self.oc.RATES_FILE, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(self.oc.RATES_HEADER)
            w.writerow(["E-1R-VAR-22-11-01-C", "2025-01-01T00:00:00Z", "", "26.8"])            # open-ended
            w.writerow(["E-1R-INTELLI-FIX-12M-25-04-10-C", "2025-04-10T00:00:00Z", "", "7.0"])  # starts later, open-ended
            w.writerow(["E-1R-COSY-22-12-08-C", "2026-10-20T03:00:00Z", "2026-10-20T06:00:00Z", "12.44"])
        pr = self.oc.Pricer()
        t = dt.datetime(2025, 9, 1, 12, tzinfo=UTC)
        self.assertEqual(pr.price(mpan, t), (26.8, "E-1R-VAR-22-11-01-C"))
        self.assertEqual(pr.price(mpan, dt.datetime(2025, 4, 15, 12, tzinfo=UTC))[0], 7.0)
        self.assertEqual(pr.price(mpan, dt.datetime(2026, 10, 20, 4, tzinfo=UTC))[0], 12.44)
        self.assertEqual(pr.price(mpan, dt.datetime(2026, 10, 20, 8, tzinfo=UTC))[0], None)   # no Cosy rate row then

    def test_agreements_saved_on_pull(self):
        self.oc.pull(days=1)
        with open(self.oc.AGREEMENTS_FILE) as f:
            self.assertEqual(list(json.load(f)), ["1900000000001"])

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
