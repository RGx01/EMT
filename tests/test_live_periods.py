"""
/api/periods?kind=live&include=net_cost,rows — Live Power's three cards (C, 1 Oct 2026).

`/api/billing` computed Today / This Bill / This Year with six queries per window and per-block
Python. This is the same three windows from /api/periods: the totals are `net_cost` (the bill's
per-day rule, the Billing tab's — what /api/billing's compute_period_net gave), the rows
`bill_card.live_rows` over `BlockStore.get_day_live_rollups` — one per-day pass.

On real databases (dev: no devices, dispatch EV; production-like: EV charger + battery, the
hybrid fold) every window's total, label and rows were identical to /api/billing's.

"This Bill" is `billing.current_billing_period`: the generated period holding today, else —
issue #221 — the current one built from the billing day, never the last historical period.

New-facet tests: they fail on the parent tree.
"""
import importlib.util
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "web"))

import billing                                  # noqa: E402
from block_store import BlockStore              # noqa: E402

TZ = "Europe/London"


class ThisBill(unittest.TestCase):
    """#221 — the fallback when no generated period holds today."""

    def _p(self, now, bd=15):
        return billing.current_billing_period([], ZoneInfo(TZ), now, bd)

    def test_on_or_after_the_billing_day_it_is_this_months(self):
        self.assertEqual(self._p(datetime(2026, 10, 20)), (datetime(2026, 10, 15), datetime(2026, 11, 15)))

    def test_before_the_billing_day_it_is_last_months(self):
        self.assertEqual(self._p(datetime(2026, 10, 3)), (datetime(2026, 9, 15), datetime(2026, 10, 15)))

    def test_january_reaches_back_to_december(self):
        self.assertEqual(self._p(datetime(2027, 1, 5)), (datetime(2026, 12, 15), datetime(2027, 1, 15)))

    def test_a_31st_billing_day_clamps_to_the_month(self):
        self.assertEqual(self._p(datetime(2026, 2, 28), bd=31), (datetime(2026, 2, 28), datetime(2026, 3, 31)))

    def test_a_generated_period_holding_today_wins(self):
        with mock.patch.object(billing, "get_billing_periods_from_config_periods",
                               lambda cps, tz=None: [(datetime(2026, 9, 3), datetime(2026, 10, 3)),
                                                     (datetime(2026, 10, 3), datetime(2026, 11, 3))]):
            p = billing.current_billing_period(["x"], ZoneInfo(TZ), datetime(2026, 10, 20), 15)
        self.assertEqual(p, (datetime(2026, 10, 3), datetime(2026, 11, 3)))


CFG = {"meters": {
    "electricity_main": {"meta": {"timezone": TZ, "currency_symbol": "£", "billing_day": 3}},
    "ev_charger": {"meta": {"sub_meter": True, "device": "Zappi", "meter_type": "ev_charger"}},
    "house_battery": {"meta": {"sub_meter": True, "device": "Battery"}}}}
NOW = datetime(2026, 10, 1, 12, 0)        # local; today's first block at 23:00Z on 30 Sep


def _store():
    """Today, local: main 2 kWh @ 0.25 per block for 4 blocks (export 1 kWh @ 0.15 on one),
    standing 0.48 on every block; the battery draws 0.5 grid kWh of each; the EV meter reads
    0.8 kWh, but on the first two blocks the bill split says EV was 1.0 kWh / £0.05."""
    st = BlockStore(tempfile.mktemp(suffix=".db"))
    st._conn.execute("PRAGMA foreign_keys=OFF")
    st._conn.execute("INSERT INTO config_periods (id, effective_from, billing_day, block_minutes, "
                     "timezone) VALUES (1, '2026-01-01T00:00:00', 3, 30, ?)", (TZ,))
    for mid, sub in (("electricity_main", 0), ("ev_charger", 1), ("house_battery", 1)):
        st._conn.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter) VALUES (1, ?, ?)",
                         (mid, sub))
    t = datetime(2026, 9, 30, 23, 0)
    for i in range(4):
        bs, be = t.isoformat(), (t + timedelta(minutes=30)).isoformat()
        ins = ("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
               "imp_kwh_grid, imp_cost, exp_kwh, exp_cost, standing_charge, "
               "is_provisional, interpolated) VALUES (?,?,?,1,?,?,?,?,?,?,0,0)")
        st._conn.execute(ins, (bs, be, "electricity_main", 2.0, None, 0.5, 1.0 if i == 0 else 0,
                               0.15 if i == 0 else 0, 0.48))
        if i < 2:                          # the bill split: the main's EV segment (step 5c)
            st._conn.execute("INSERT INTO block_segments (block_start, meter_id, channel, seq, kwh, "
                             "inc_rate, exc_rate, band, attribution) "
                             "VALUES (?, 'electricity_main', 'import', 0, 1.0, 0.05, NULL, "
                             "'off_peak', 'ev')", (bs,))
        st._conn.execute(ins, (bs, be, "ev_charger", 0.8, 0.8, 0.2, 0, 0, 0))
        st._conn.execute(ins, (bs, be, "house_battery", 0.6, 0.5, 0.125, 0, 0, 0))
        t += timedelta(minutes=30)
    st._conn.commit()
    return st


class TheLiveCards(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("server_live", os.path.join(_ROOT, "web", "server.py"))
        cls.srv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.srv)
        cls.st = _store()

    def _get(self, q):
        class _DT(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW.replace(tzinfo=tz) if tz else NOW
        with mock.patch.object(self.srv.store_access, "store", lambda: self.st), \
             mock.patch.object(self.srv, "load_config", lambda: CFG), \
             mock.patch.object(self.srv, "_config_timezone", lambda: TZ), \
             mock.patch("datetime.datetime", _DT):
            return self.srv.app.test_client().get("/api/periods?" + q)

    def _live(self):
        r = self._get("kind=live&include=net_cost,rows")
        self.assertEqual(r.status_code, 200, r.data[:300])
        return {p["key"]: p for p in r.get_json()["periods"]}

    def test_the_three_windows(self):
        p = self._live()
        self.assertEqual([p[k]["label"] for k in ("today", "bill", "year")],
                         ["01 Oct 2026", "03 Sep → 02 Oct 2026", "1 Jan → 01 Oct 2026"])
        self.assertEqual((p["bill"]["start"], p["bill"]["end"]), ("2026-09-03", "2026-10-03"))

    def test_the_total_is_the_bills_day_rule(self):
        # one day: 4 × £0.50 import + £0.48 standing − £0.15 export
        self.assertEqual(self._live()["today"]["net_cost"], 2.33)

    def test_the_rows(self):
        rows = self._live()["today"]["rows"]
        self.assertEqual(rows, [
            {"label": "Total Import (8.000 kWh)", "cost": 2.0, "bold": True},
            # 8 − battery 2.0 − EV hybrid (1.0 + 1.0 billed, 0.8 + 0.8 metered) 3.6
            {"label": "Direct import (2.400 kWh)", "cost": 1.0, "bold": False},
            {"label": "↳ Battery (2.000 kWh)", "cost": 0.5, "bold": False},
            {"label": "↳ Zappi (3.600 kWh)", "cost": 0.5, "bold": False},   # 0.05+0.05+0.2+0.2
            {"label": "Grid Export (1.000 kWh)", "cost": -0.15, "bold": False},
            {"label": "Standing Charge", "cost": 0.48, "bold": False}])

    def test_rows_are_live_only(self):
        self.assertEqual(self._get("kind=billing&include=rows").status_code, 400)


if __name__ == "__main__":
    unittest.main()
