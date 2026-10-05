"""
#31: the hybrid EV fold — a day equals its half-hours (5 Oct 2026).

`_apply_hybrid_ev_to_summary_rows` caps what it moves out of Direct at what Direct holds.
Folded per DAY, one slot's surplus covered another's shortfall, so the Usage Stats day view
moved more EV (and showed less Direct) than its own half-hours summed: 36 of 60 days on a
production-like database. The half-hour cap is the physical one — a car cannot take more grid
in a half-hour than that half-hour had — so a day is now folded slot by slot and summed.

One day, two slots, a configured EV sub-meter with no recorded draw (all EV synthetic):
  A  Direct 4.0 kWh, dispatch EV 1.0  -> moves 1.0
  B  Direct 0.5 kWh, dispatch EV 3.0  -> capped at 0.5 (a battery covered the rest)
The half-hours move 1.5 between them; the parent's day view moved 4.0 (capped only at the
day's 4.5 of Direct). The equality test fails on the parent; the totals test is a guard.
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import usage_view as uv                           # noqa: E402
from block_store import BlockStore                # noqa: E402

CONFIG = {"meters": {
    "electricity_main": {"meta": {"billing_day": 1, "block_minutes": 30, "timezone": "UTC",
                                  "currency_symbol": "£", "currency_code": "GBP"},
                         "channels": {"import": {}, "export": {}}},
    "ev_charger": {"meta": {"sub_meter": True, "meter_type": "ev_charger",
                            "parent_meter": "electricity_main"},
                   "channels": {"import": {}}},
}}
DAY_FROM, DAY_TO = "2026-08-01T00:00:00", "2026-08-02T00:00:00"
SLOTS = {  # block_start: (main kWh, main cost, Direct kWh, dispatch EV kWh, its cost)
    "2026-08-01T10:00:00": (4.0, 1.20, 4.0, 1.0, 0.07),
    "2026-08-01T10:30:00": (1.0, 0.30, 0.5, 3.0, 0.21),
}


def _store():
    st = BlockStore(":memory:")
    st.insert_config_period(CONFIG)
    cp = st.get_current_config_period_id()
    with st._conn:
        for bs, (k, c, rem, ek, ec) in SLOTS.items():
            st._conn.execute(
                "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
                "imp_kwh_remainder, imp_rate, imp_cost, exp_kwh, exp_cost, standing_charge, "
                "imp_kwh_ev, imp_cost_ev) VALUES (?, ?, 'electricity_main', ?, ?, ?, 0.3, ?, 0, 0, "
                "0, ?, ?)", (bs, bs, cp, k, rem, c, ek, ec))
    return st


class ADayAndItsSlots(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.st = _store()
        cls.cfg = cls.st.config_from_db(cls.st.get_current_config_period_id())
        assert uv._configured_ev_meter_id(cls.cfg) == "ev_charger", "fixture has no EV meter"
        cls.day = uv.day_stats(cls.st, cls.cfg, "UTC", DAY_FROM, DAY_TO)["rows"][0]["meters"]
        cls.slots = [r["meters"] for r in
                     uv.slot_stats(cls.st, cls.cfg, "UTC", DAY_FROM, DAY_TO)["rows"]]

    def _sum(self, meter, field):
        return round(sum((m.get(meter) or {}).get(field, 0.0) for m in self.slots), 4)

    def test_the_half_hours_cap_each_slot(self):
        """GUARD: the HH fold itself is unchanged — 1.0 + 0.5."""
        self.assertAlmostEqual(self._sum("ev_charger", "imp_kwh"), 1.5, places=4)

    def test_the_day_equals_its_half_hours(self):
        for meter in ("ev_charger", "electricity_main"):
            for field in ("imp_kwh", "imp_cost"):
                with self.subTest(meter=meter, field=field):
                    self.assertAlmostEqual(self.day[meter][field], self._sum(meter, field),
                                           places=3)

    def test_the_day_total_does_not_move(self):
        """GUARD: the fold only moves between Direct and EV."""
        self.assertAlmostEqual(self.day["electricity_main"]["imp_kwh"]
                               + self.day["ev_charger"]["imp_kwh"], 4.5, places=4)


if __name__ == "__main__":
    unittest.main()
