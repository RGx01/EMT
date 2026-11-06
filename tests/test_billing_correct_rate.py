"""
Corrections on the Billing tab (data management design §1 (3), PR 6; 21 Oct 2026).

Right-click (or long-press) a half-hour on a day chart: *Correct this rate…* for that half-hour
only, or the day's standing charge. The corrections routes take `{day, slot}` (a rate) or `{day}`
(the standing charge) in place of the date range and times, and the slot is the chart's — the local
clock position, so the clocks-back day's repeated hour is the later block, as the bar is.

Also: the preview listed each device twice under `meter_id: "all"` (once as a "main" row at its raw
kWh, again as a device at its grid kWh) — now once, flagged `device`.

Discriminating — every test here fails on the parent: the routes refused `{day, …}` without
from_date / to_date, the preview carried no `device` flag and listed devices twice, and the module
had no menu. The CAD-only gate is the page's (`uses_api`), checked in the template.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import test_server as TS                       # noqa: E402  (stubs + make_client)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RATE, NEW = 0.25, 0.30
# UTC block starts: 19 Oct 2026 is BST (local midnight 18 Oct 23:00 UTC); 25 Oct 2026 the clocks
# go back at 02:00 BST, so local 01:00 is 00:00 UTC (BST) and again 01:00 UTC (GMT).
STARTS = ["2026-10-18T22:30:00", "2026-10-18T23:00:00", "2026-10-19T01:00:00",
          "2026-10-19T01:30:00", "2026-10-19T02:00:00", "2026-10-19T22:30:00",
          "2026-10-19T23:00:00", "2026-10-25T00:00:00", "2026-10-25T01:00:00"]


def _end(bs):
    from datetime import datetime, timedelta
    return (datetime.fromisoformat(bs) + timedelta(minutes=30)).isoformat()


def _store():
    st = TS.BlockStore(TS._memory_uri())
    st.insert_config_period({"meters": {
        "electricity_main": {"meta": {"billing_day": 1, "block_minutes": 30,
                                      "timezone": "Europe/London",
                                      "currency_symbol": "£", "currency_code": "GBP"}},
        "house_battery": {"meta": {"sub_meter": True, "parent_meter": "electricity_main",
                                   "meter_type": "battery"}},
    }})
    cp = st.get_current_config_period_id()
    with st._conn:
        for bs in STARTS:
            st._conn.execute(
                "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, interpolated, "
                "imp_kwh, imp_rate, imp_cost, exp_kwh, exp_cost, standing_charge) "
                "VALUES (?, ?, 'electricity_main', ?, 0, 1.0, ?, ?, 0, 0, 0.5)",
                (bs, _end(bs), cp, RATE, RATE))
            st._conn.execute(
                "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, interpolated, "
                "imp_kwh, imp_kwh_grid, imp_rate, imp_cost, exp_kwh, exp_cost, standing_charge) "
                "VALUES (?, ?, 'house_battery', ?, 0, 0.6, 0.4, ?, ?, 0, 0, 0)",
                (bs, _end(bs), cp, RATE, round(0.4 * RATE, 6)))
    return st


def _rates(st, meter="electricity_main"):
    return {r[0]: r[1] for r in st._conn.execute(
        "SELECT block_start, imp_rate FROM blocks WHERE meter_id = ?", (meter,))}


class OneHalfHour(unittest.TestCase):

    def setUp(self):
        self.st = _store()
        self.client = TS.make_client(store=self.st)

    def _post(self, route, **body):
        r = self.client.post("/api/corrections/" + route, json=dict(
            {"type": "rate", "channel": "import", "meter_id": "all"}, **body))
        self.assertEqual(r.status_code, 200, r.data)
        return r.get_json()

    def test_the_preview_is_the_half_hour(self):
        """19 Oct slot 5 is 02:30 BST — 01:30 UTC — and nothing either side of it."""
        j = self._post("preview", day="2026-10-19", slot=5, value=NEW)
        self.assertEqual({b["block_start"] for b in j["blocks"]}, {"2026-10-19T01:30:00"})

    def test_each_device_is_listed_once_at_its_grid_kwh(self):
        j = self._post("preview", day="2026-10-19", slot=5, value=NEW)
        rows = sorted((b["meter_id"], b["device"], b["kwh"]) for b in j["blocks"])
        self.assertEqual(rows, [("electricity_main", False, 1.0), ("house_battery", True, 0.4)])

    def test_apply_corrects_only_the_half_hour(self):
        self._post("apply", day="2026-10-19", slot=5, value=NEW, recalc_cost=True)
        for meter in ("electricity_main", "house_battery"):
            changed = {bs for bs, r in _rates(self.st, meter).items() if r == NEW}
            self.assertEqual(changed, {"2026-10-19T01:30:00"}, meter)
        cost = self.st._conn.execute(
            "SELECT imp_cost FROM blocks WHERE block_start = '2026-10-19T01:30:00' "
            "AND meter_id = 'house_battery'").fetchone()[0]
        self.assertAlmostEqual(cost, 0.4 * NEW, places=6)       # the grid kWh, not the raw 0.6

    def test_the_repeated_hour_is_the_later_block(self):
        """25 Oct slot 2 is local 01:00, twice: the chart's bar is the later (GMT) block."""
        self._post("apply", day="2026-10-25", slot=2, value=NEW, recalc_cost=True)
        r = _rates(self.st)
        self.assertEqual(r["2026-10-25T01:00:00"], NEW)
        self.assertEqual(r["2026-10-25T00:00:00"], RATE)

    def test_a_skipped_time_matches_nothing(self):
        """29 Mar 2026 slot 2 (local 01:00) never happened; it must not land on 01:00 UTC's block."""
        from web.server import _corrections_half_hour
        lo, hi = _corrections_half_hour({"day": "2026-03-29", "slot": 2}, "Europe/London", 30)
        self.assertEqual(lo, hi)


class TheDaysStandingCharge(unittest.TestCase):

    def test_one_local_day(self):
        st = _store()
        client = TS.make_client(store=st)
        r = client.post("/api/corrections/apply", json={
            "type": "standing", "day": "2026-10-19", "value": 0.6, "meter_id": "all"})
        self.assertEqual(r.status_code, 200, r.data)
        got = {r[0] for r in st._conn.execute(
            "SELECT block_start FROM blocks WHERE standing_charge = 0.6")}
        self.assertEqual(got, {"2026-10-18T23:00:00", "2026-10-19T01:00:00", "2026-10-19T01:30:00",
                               "2026-10-19T02:00:00", "2026-10-19T22:30:00"})


class TheBillingModule(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "web", "templates", "billing_module.html")) as f:
            cls.mod = f.read()
        with open(os.path.join(ROOT, "web", "templates", "charts.html")) as f:
            cls.charts = f.read()
        with open(os.path.join(ROOT, "web", "templates", "lovelace_billing.html")) as f:
            cls.card = f.read()

    def test_cad_only(self):
        self.assertIn("state.corr = !j.uses_api", self.mod)
        self.assertIn("if (state.corr) bindCorrect(canvas, day)", self.mod)

    def test_the_charts_page_offers_it_and_the_card_does_not(self):
        self.assertIn("EMTBilling.init(document.getElementById('chart-daily-content'), {corrections: true, remember: true})",
                      self.charts)
        self.assertNotIn("corrections", re.sub(r"<!--.*?-->", "", self.card, flags=re.S))

    def test_right_click_and_long_press(self):
        self.assertIn("'contextmenu'", self.mod)
        self.assertIn("'touchstart'", self.mod)

    def test_the_summary_panel_offers_the_standing_charge(self):
        self.assertIn("e.target.closest('.chart-summary')", self.mod)

    def test_the_modal_asks_for_the_half_hour(self):
        self.assertIn("b.slot = q.slot", self.mod)
        self.assertIn("{type: q.type, day: q.day", self.mod)


if __name__ == "__main__":
    unittest.main()
