"""
/api/energy?include=billing — the Billing tab's data (billing_view.py), C 30 Sep 2026.

The pre-rendered Billing page computes, per period, a bill summary and, per day, a
chart's series and side panel, for all of history at once. The API computes the same
for one range, by calling the same functions (energy_charts.period_summary under
billing_context, and day_chart_data — split out of generate_daily_import_export_charts
and build_day_chart_html for this). So what can differ is only the INPUTS, and each is
pinned here against the page itself:

  * a range's blocks instead of all of history — day charts and bill cards equal the
    page's;
  * BL-24's ex-VAT capability gate — asked of the WHOLE store, not the range: a period
    with no captured exc figure still gets the bill method when another period has one,
    as it does on the page;
  * JSON — the summary's float rate keys and date keys, made lists, lose nothing.

On a real database `.build/billing_payload_parity.py` checks every day and every
period (30 Sep 2026: 1,212 days, 98 periods, all identical). The markup half of the
split is held byte-identical by that same run (md5 of the whole 36 MB page unchanged)
and by the seven existing build_day_chart_html test files.

These are new-facet tests: they fail on the parent tree because the facet and
billing_view do not exist there.
"""
import json
import os
import random
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import billing_view as bv                        # noqa: E402
import energy_charts as ec                       # noqa: E402
from block_store import BlockStore               # noqa: E402

TZ = "Europe/London"
EXC_DAYS = 5          # the first five days carry a captured ex-VAT cost, the rest none


def _store(bill_rounding=True):
    """20 days from 21 Sep 2026 (local), billing day 3 — so two billing periods — a
    main meter with export and a standing charge, and an EV sub-meter charging at night."""
    st = BlockStore(tempfile.mktemp(suffix=".db"))
    st._conn.execute("PRAGMA foreign_keys=OFF")
    st._conn.execute("INSERT INTO config_periods (id, effective_from, billing_day, block_minutes, "
                     "timezone) VALUES (1, '2026-01-01T00:00:00', 3, 30, ?)", (TZ,))
    st._conn.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter) "
                     "VALUES (1, 'electricity_main', 0)")
    st._conn.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter, parent_meter_id, "
                     "device_label, meter_type) VALUES (1, 'ev_charger', 1, 'electricity_main', "
                     "'EV Charger', 'ev')")
    rnd = random.Random(5)
    t0 = datetime(2026, 9, 20, 23, 0)
    for i in range(48 * 20):
        t = t0 + timedelta(minutes=30 * i)
        bs = t.isoformat()
        rate = 0.07 if t.hour < 5 else 0.30
        k = round(rnd.uniform(0.1, 1.5), 3)
        exc = round(k * rate / 1.05, 6) if i < 48 * EXC_DAYS else None
        st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, imp_cost, "
            "imp_rate, imp_cost_exc, exp_kwh, exp_cost, exp_rate, standing_charge) "
            "VALUES (?,?,'electricity_main',1,?,?,?,?,?,?,?,?)",
            (bs, bs, k, round(k * rate, 6), rate, exc, 0.2, 0.03, 0.15, 0.61))
        if t.hour < 5:
            e = round(k * 0.6, 3)
            st._conn.execute(
                "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
                "imp_kwh_grid, imp_cost, imp_rate) VALUES (?,?,'ev_charger',1,?,?,?,?)",
                (bs, bs, e, e, round(e * rate, 6), rate))
    st._conn.commit()
    st.save_settings({**(st.get_settings() or {}), "bill_rounding_summary": bill_rounding})
    return st


def _page(st, cfg):
    return ec.generate_daily_import_export_charts(
        st.get_blocks_lightweight(), timezone_name=TZ, block_minutes=30, currency="£",
        cfg=cfg, store=st)


# The two billing periods the fixture spans, as UTC bounds (BST until 25 Oct).
SEP = ("2026-09-02T23:00:00", "2026-10-02T23:00:00")    # 3 Sep → 3 Oct local
OCT = ("2026-10-02T23:00:00", "2026-11-03T00:00:00")    # 3 Oct → 3 Nov local


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.st = _store()
        cls.cfg = cls.st.config_from_db(1)
        cls.page = _page(cls.st, cls.cfg)

    @classmethod
    def tearDownClass(cls):
        cls.st.close()


class DaysEqualThePage(_Base):

    def test_every_days_chart_is_the_one_the_page_embeds(self):
        embedded = {m.group(1).replace("_", "-"): json.loads(m.group(2)) for m in re.finditer(
            r'<script type="application/json" id="data_chart_(\d{4}_\d\d_\d\d)">(.*?)</script>',
            self.page)}
        served = {}
        for rng in (SEP, OCT):
            for row in bv.day_billing(self.st, self.cfg, TZ, *rng)["days"]:
                served[row["day"]] = json.loads(json.dumps(row["chart"]))
        self.assertEqual(len(embedded), 20)
        self.assertEqual(served, embedded)

    def test_the_side_panel_formats_to_the_pages_markup(self):
        row = bv.day_billing(self.st, self.cfg, TZ, *SEP)["days"][0]
        orig = ec.day_chart_data
        try:
            ec.day_chart_data = lambda *a, **k: {"chart": row["chart"], "panel": row["panel"]}
            html = ec.build_day_chart_html(row["day"], [], {}, currency="£")
        finally:
            ec.day_chart_data = orig
        panel = html[html.index('<div class="chart-summary">'):html.index('<div id="chart_')]
        self.assertIn(panel.strip(), self.page)
        # the breakdown is there: one EV line, labelled "EV" (H6), not the device name
        self.assertIn("\u21b3 EV</span>", panel)

    def test_each_day_carries_its_panel_lines(self):
        import bill_card
        row = bv.day_billing(self.st, self.cfg, TZ, *SEP)["days"][0]
        self.assertEqual(row["panel_lines"], bill_card.panel_lines(row["panel"], "£"))

    def test_days_come_newest_first_and_only_those_in_range(self):
        days = [r["day"] for r in bv.day_billing(self.st, self.cfg, TZ, *OCT)["days"]]
        self.assertEqual(days, sorted(days, reverse=True))
        self.assertTrue(all("2026-10-03" <= d < "2026-11-03" for d in days))


class PeriodsEqualThePage(_Base):

    def _card(self, rng):
        raw, _, _ = bv.period_summary_raw(self.st, self.cfg, TZ, *rng)
        return ec.render_billing_summary(raw, currency="£")

    def test_both_billing_periods_render_the_pages_bill_card(self):
        for rng in (SEP, OCT):
            self.assertIn(self._card(rng), self.page)

    def test_the_served_summary_is_that_summary(self):
        raw, _, _ = bv.period_summary_raw(self.st, self.cfg, TZ, *OCT)
        out = bv.period_billing(self.st, self.cfg, TZ, *OCT, {"kind": "billing"})
        self.assertEqual(out["summary"], bv.summary_json(raw))
        import bill_card
        self.assertEqual(out["card"], bill_card.card(raw, currency="£", site_name=None))
        self.assertIs(out["is_config_change"], False)
        self.assertEqual(out["days"][0], "2026-10-10")


class TheExcGateIsStoreWide(_Base):
    """BL-24 shows the bill method only where EMT holds a captured ex-VAT figure. The
    page asks that of ALL history. Asked of one period, October (which has none —
    only the first five days do) would lose its bill method and differ from the page."""

    def test_a_period_without_exc_still_gets_the_bill_method(self):
        out = bv.period_billing(self.st, self.cfg, TZ, *OCT, {"kind": "billing"})
        self.assertTrue(out["bill_rounding"])
        self.assertIn("bill_method", out["summary"])

    def test_the_fixture_discriminates(self):
        """GUARD on the fixture: October's own blocks hold no exc figure, so a gate
        computed over the range would switch the bill method off there."""
        blocks = self.st.get_blocks_lightweight(*OCT)
        self.assertFalse(ec.billing_context(self.cfg, self.st, blocks)["bill_rounding"])

    def test_no_exc_anywhere_means_no_bill_method(self):
        st = _store()
        self.addCleanup(st.close)
        st._conn.execute("UPDATE blocks SET imp_cost_exc = NULL")
        st._conn.commit()
        out = bv.period_billing(st, st.config_from_db(1), TZ, *SEP, {"kind": "billing"})
        self.assertFalse(out["bill_rounding"])
        self.assertNotIn("bill_method", out["summary"])


class SummaryJson(_Base):

    def test_rate_maps_are_sorted_lists_and_nothing_is_lost(self):
        raw, _, _ = bv.period_summary_raw(self.st, self.cfg, TZ, *SEP)
        js = bv.summary_json(raw)
        self.assertEqual(json.loads(json.dumps(js)), js)
        rates = [r["rate"] for r in js["main_import_raw"]]
        self.assertEqual(rates, sorted(raw["main_import_raw"]))
        self.assertEqual([r["kwh"] for r in js["main_import_raw"]],
                         [raw["main_import_raw"][r]["kwh"] for r in rates])
        self.assertEqual(len(js["standing"]), len(raw["standing"]))
        self.assertEqual(js["standing"][0]["day"], min(raw["standing"]).isoformat())
        for k, v in raw["meters"].items():
            self.assertEqual([r["rate"] for r in js["meters"][k]], sorted(v))


class TheRoute(unittest.TestCase):

    def _get(self, qs):
        from tests.test_server import make_client
        r = make_client().get("/api/energy" + qs)
        return r.status_code, json.loads(r.data)

    def test_billing_is_a_known_facet_at_period_and_day(self):
        code, d = self._get("?resolution=period&kind=month&include=billing")
        self.assertEqual(code, 200, d)
        self.assertIn("summary", d["billing"])
        code, d = self._get("?resolution=day&kind=month&include=billing")
        self.assertEqual(code, 200, d)
        self.assertIn("days", d["billing"])
        self.assertIn("rate_context", d["billing"])

    def test_not_at_block(self):
        self.assertEqual(self._get("?resolution=block&kind=month&include=billing")[0], 400)

    def test_an_unknown_facet_is_still_refused(self):
        self.assertEqual(self._get("?resolution=period&include=bills")[0], 400)


if __name__ == "__main__":
    unittest.main()
