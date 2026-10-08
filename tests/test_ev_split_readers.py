"""
Step 5 — the EV split is read from the EV segments (ev_split), not the imp_*_ev columns (8 Oct 2026).

The bill prices a half-hour's EV from its EV-attributed segments (BL-27). The Overview card,
Usage Stats and Insights read `imp_kwh_ev` / `imp_cost_ev` instead, and `imp_kwh_ev IS NOT NULL`
was their seam test: the dispatch EV where it is set, the physical EV meter where it is not. The
EV-split writers commit the columns and the segments separately (ROADMAP BL-79), so the two can part;
when they do, those surfaces left the bill. Now every one reads ev_split: the EV segments, or for a
half-hour with no segments at all its columns (LEGACY, until the importer writes the segments).

One day, two half-hours, an EV meter, each with a stale column:
  01:00  house-only segments, a stale imp_kwh_ev 2.0 -> no dispatch EV; the meter's 4.0 counts
  02:00  EV segment 3.5, a stale imp_kwh_ev 1.0     -> the dispatch EV 3.5; the meter's 3.8 doesn't
So the EV is 7.5 kWh, £2.25, on every surface. A second day holds one unsegmented half-hour whose
columns say 2.5 kWh: the LEGACY fallback keeps them.

Discriminating: the Overview and Usage Stats cases fail on the parent. Insights' EV is a guard (it
already priced from the segments on a fully segmented range), as is the legacy day. The day charts'
readers are in test_ev_split_chart_readers.py (this file's test_server import stubs energy_charts).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import test_server as TS                         # noqa: E402  (stubs; server)
import usage_view as uv                          # noqa: E402

RATE = 0.30
DAY, DAY2 = ("2025-06-01T00:00:00", "2025-06-02T00:00:00"), ("2025-06-02T00:00:00", "2025-06-03T00:00:00")
PRE, POST, LEGACY = "2025-06-01T01:00:00", "2025-06-01T02:00:00", "2025-06-02T03:00:00"
CONFIG = {"meters": {
    "electricity_main": {"meta": {"billing_day": 1, "block_minutes": 30, "timezone": "UTC",
                                  "currency_symbol": "£", "currency_code": "GBP"},
                         "channels": {"import": {}, "export": {}}},
    "ev_charger": {"meta": {"sub_meter": True, "meter_type": "ev_charger",
                            "parent_meter": "electricity_main", "device": "EV"},
                   "channels": {"import": {}}},
}}
# block_start: (physical EV kWh, stale imp_kwh_ev, segments as (kWh, attribution) or None)
SLOTS = {
    PRE:    (4.0, 2.0, [(10.0, "house")]),
    POST:   (3.8, 1.0, [(3.5, "ev"), (6.5, "house")]),
    LEGACY: (2.6, 2.5, None),
}
WANT = (7.5, round(7.5 * RATE, 6))               # 4.0 recorded + 3.5 dispatch


def _store():
    st = TS.BlockStore(":memory:")
    st.insert_config_period(CONFIG)
    cp = st.get_current_config_period_id()
    with st._conn:
        for bs, (phys, stale, segs) in SLOTS.items():
            st._conn.execute(
                "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
                "imp_kwh_grid, imp_rate, imp_cost, exp_kwh, exp_cost, standing_charge, imp_kwh_ev, "
                "imp_cost_ev, imp_rate_ev) VALUES (?, ?, 'electricity_main', ?, 10.0, 10.0, ?, ?, "
                "0, 0, 0, ?, ?, ?)", (bs, bs, cp, RATE, 10.0 * RATE, stale, round(stale * RATE, 6), RATE))
            st._conn.execute(
                "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
                "imp_kwh_grid, imp_rate, imp_cost, exp_kwh, exp_cost, standing_charge) "
                "VALUES (?, ?, 'ev_charger', ?, ?, ?, ?, ?, 0, 0, 0)",
                (bs, bs, cp, phys, phys, RATE, round(phys * RATE, 6)))
            for seq, (k, attr) in enumerate(segs or []):
                st._conn.execute(
                    "INSERT INTO block_segments (block_start, meter_id, channel, seq, kwh, inc_rate, "
                    "exc_rate, band, attribution) VALUES (?, 'electricity_main', 'import', ?, ?, ?, "
                    "?, 'off_peak', ?)", (bs, seq, k, RATE, round(RATE / 1.05, 6), attr))
    return st


class EverySurfaceReadsTheSegments(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.st = _store()
        cls.cfg = cls.st.config_from_db(cls.st.get_current_config_period_id())
        assert uv._configured_ev_meter_id(cls.cfg) == "ev_charger", "fixture has no EV meter"

    def _overview(self, days):
        ev = self.st.get_day_live_rollups(days[0], days[1], "UTC", "ev_charger")["ev"]
        return sum(v["kwh"] for v in ev.values()), sum(v["cost"] for v in ev.values())

    def _usage_day(self, days):
        m = uv.day_stats(self.st, self.cfg, "UTC", days[0], days[1])["rows"][0]["meters"]["ev_charger"]
        return m["imp_kwh"], m["imp_cost"]

    def _usage_slots(self, days):
        rows = uv.slot_stats(self.st, self.cfg, "UTC", days[0], days[1])["rows"]
        ms = [r["meters"].get("ev_charger") or {} for r in rows]
        return sum(m.get("imp_kwh", 0.0) for m in ms), sum(m.get("imp_cost", 0.0) for m in ms)

    def _check(self, got, want):
        self.assertAlmostEqual(got[0], want[0], places=4)
        self.assertAlmostEqual(got[1], want[1], places=4)

    def test_the_overview_card(self):
        self._check(self._overview(DAY), WANT)

    def test_usage_stats_day(self):
        self._check(self._usage_day(DAY), WANT)

    def test_usage_stats_half_hours(self):
        self._check(self._usage_slots(DAY), WANT)

    def test_insights(self):
        """GUARD: on a fully segmented range Insights already priced the EV from the segments."""
        ev = TS.server._aggregate_usage(self.st, self.cfg, DAY[0], DAY[1], "UTC")["sub_meters"]["ev_dispatch"]
        self._check((ev["imp_kwh"], ev["imp_cost"]), WANT)

    def test_a_block_with_no_segments_keeps_its_columns(self):
        """GUARD (LEGACY): until the importer writes its segments, an unsegmented half-hour's
        split is its columns — 2.5 kWh, superseding the meter's 2.6."""
        want = (2.5, round(2.5 * RATE, 6))
        for surface, got in (("Overview", self._overview(DAY2)), ("Usage Stats", self._usage_day(DAY2))):
            with self.subTest(surface=surface):
                self._check(got, want)


class TheChannelForm(unittest.TestCase):
    """ev_split.from_channel, which the day charts and the chart EV slot map read."""

    def test_house_only_segments_have_no_split(self):
        import ev_split
        self.assertIsNone(ev_split.from_channel({
            "kwh_ev": 1.0, "cost_ev": 0.3, "rate_ev": 0.3,
            "segments": [{"kwh": 10.0, "inc_rate": 0.3, "attribution": "house"}]}))

    def test_the_ev_segments_are_the_split(self):
        import ev_split
        k, c, r = ev_split.from_channel({
            "kwh_ev": 1.0, "cost_ev": 0.3, "rate_ev": 0.9,
            "segments": [{"kwh": 3.5, "inc_rate": 0.3, "attribution": "ev"},
                         {"kwh": 6.5, "inc_rate": 0.3, "attribution": "house"}]})
        self.assertAlmostEqual(k, 3.5); self.assertAlmostEqual(c, 1.05); self.assertAlmostEqual(r, 0.3)

    def test_no_segments_keeps_the_columns(self):
        """GUARD (LEGACY)."""
        import ev_split
        self.assertEqual(ev_split.from_channel({"kwh_ev": 1.0, "cost_ev": 0.3, "rate_ev": 0.3}),
                         (1.0, 0.3, 0.3))


if __name__ == "__main__":
    unittest.main()
