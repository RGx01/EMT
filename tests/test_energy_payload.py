"""
`/api/energy?resolution=block|day` — the payload C §3 specifies, and the three
properties measurement showed it has to have.

§3: the response is **priced blocks and their segments, not chart geometry**.
Series, stacks, heatmap cells and billing totals are projections a client makes
from the same payload; baking them server-side is what produced two renderers
that disagreed.

The three properties, each measured on a 4.5.14 prod database before being built:

  1. **Sub-meter rows are first-class.** They carry device energy and have ZERO
     segments — 6,845 ev_charger and 7,792 house_battery blocks, none. A client
     projecting devices from segments alone gets an empty chart, silently.
  2. **Provenance is three orthogonal facts**, not one `settled` flag. 48,050
     imported blocks are settleable with no `imp_kwh_api`; 29 hold a cached bill
     that has not been applied.
  3. **A device inherits its parent's bill provenance.** 1,206 device blocks
     under a settled parent carry `rate_source='schedule'` while their rate
     matches the settled parent exactly — £42.14 of billed cost that a naive read
     would label schedule-derived.
"""
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from block_store import BlockStore              # noqa: E402

FROM, TO = "2026-09-01T00:00:00", "2026-09-02T00:00:00"


class _Base(unittest.TestCase):
    def setUp(self):
        self.st = BlockStore(tempfile.mktemp(suffix=".db"))
        self.st._conn.execute("PRAGMA foreign_keys=OFF")
        self.addCleanup(self.st.close)
        self.st._conn.execute(
            "INSERT INTO meters (config_period_id, meter_id, is_sub_meter) "
            "VALUES (1,'ev_charger',1)")
        self.st._conn.commit()

    def blk(self, meter_id="electricity_main", start=FROM, **cols):
        base = {"block_start": start, "block_end": start, "config_period_id": 1,
                "meter_id": meter_id, "imp_kwh": 2.0, "imp_cost": 0.5, "imp_rate": 0.25}
        base.update(cols)
        self.st._conn.execute("INSERT INTO blocks (%s) VALUES (%s)"
                              % (",".join(base), ",".join("?" * len(base))),
                              tuple(base.values()))
        self.st._conn.commit()

    def blocks(self):
        return {r["meter_id"]: r for r in self.st.get_energy_blocks(FROM, TO)}


class SubMeterRowsAreFirstClass(_Base):

    def test_devices_appear_even_with_no_segments(self):
        self.blk()
        self.blk("ev_charger", imp_kwh=1.5, imp_kwh_grid=1.2)
        b = self.blocks()
        self.assertIn("ev_charger", b)
        self.assertEqual(self.st.get_energy_segments(FROM, TO), [],
                         "the fixture has no segments — the point of the test")

    def test_a_device_is_flagged_as_one(self):
        self.blk()
        self.blk("ev_charger")
        b = self.blocks()
        self.assertEqual(b["electricity_main"]["is_sub_meter"], 0)
        self.assertEqual(b["ev_charger"]["is_sub_meter"], 1)

    def test_the_grid_portion_is_carried(self):
        """`imp_kwh_grid` came OFF the drop list (import spec §5.2): a battery or
        V2X device draws from storage as well as the grid and only the grid part
        is chargeable. It cannot be reconstructed — devices have no segments."""
        self.blk()
        self.blk("ev_charger", imp_kwh=2.0, imp_kwh_grid=1.2)
        self.assertAlmostEqual(self.blocks()["ev_charger"]["imp_kwh_grid"], 1.2)


class ProvenanceIsThreeFacts(_Base):

    def test_imported_history_is_authoritative_without_api_kwh(self):
        self.blk(source="imported_csv")
        r = self.blocks()["electricity_main"]
        self.assertTrue(r["kwh_authoritative"])
        self.assertFalse(r["bill_cached"])
        self.assertFalse(r["bill_applied"])

    def test_a_live_cad_block_is_none_of_them(self):
        self.blk()
        r = self.blocks()["electricity_main"]
        self.assertFalse(r["kwh_authoritative"])

    def test_a_cached_but_unapplied_bill_is_visible(self):
        """29 such blocks on the reference database — a real, durable state that
        a single `settled` boolean cannot express."""
        self.blk(imp_kwh_api=1.9)
        self.st.upsert_measured_cost(FROM, mpan="1", cost_incl=0.5)
        r = self.blocks()["electricity_main"]
        self.assertTrue(r["kwh_authoritative"])
        self.assertTrue(r["bill_cached"])
        self.assertFalse(r["bill_applied"], "the bill is held, not written")

    def test_each_channel_has_its_own_api_kwh_flag(self):
        """Export settles separately, and later, than import — one flag cannot say
        both (the Heatmaps tab's API-coverage view is split by channel)."""
        self.blk(imp_kwh_api=1.9)
        r = self.blocks()["electricity_main"]
        self.assertTrue(r["kwh_authoritative"])
        self.assertFalse(r["exp_kwh_authoritative"])

    def test_imported_history_is_authoritative_on_both_channels(self):
        self.blk(source="imported_api")
        r = self.blocks()["electricity_main"]
        self.assertTrue(r["kwh_authoritative"] and r["exp_kwh_authoritative"])

    def test_an_applied_bill_is_all_three(self):
        self.blk(imp_kwh_api=1.9, rate_source="measured")
        self.st.upsert_measured_cost(FROM, mpan="1", cost_incl=0.5)
        r = self.blocks()["electricity_main"]
        self.assertTrue(r["kwh_authoritative"] and r["bill_cached"] and r["bill_applied"])


class DeviceProvenanceIsInherited(_Base):

    def test_a_device_under_a_settled_parent_reads_as_billed(self):
        """The finding this exists for: the device's own `rate_source` says
        'schedule' because PASS 2 wrote the row, while its RATE is the bill's."""
        self.blk(rate_source="measured", imp_rate=0.0549)
        self.blk("ev_charger", rate_source="schedule", imp_rate=0.0549)
        b = self.blocks()
        self.assertEqual(b["ev_charger"]["rate_source"], "schedule",
                         "the column still says schedule — that is the problem")
        self.assertTrue(b["ev_charger"]["bill_applied"],
                        "but the payload must say the number came from the bill")

    def test_a_device_under_an_unsettled_parent_is_not_billed(self):
        self.blk(rate_source="schedule")
        self.blk("ev_charger", rate_source="schedule")
        self.assertFalse(self.blocks()["ev_charger"]["bill_applied"])

    def test_the_main_row_uses_its_own(self):
        """COALESCE picks the parent join, which is NULL for the main row itself
        — so the main meter must fall back to its own column, not to False."""
        self.blk(rate_source="measured")
        self.assertTrue(self.blocks()["electricity_main"]["bill_applied"])

    def test_a_device_with_no_parent_row_falls_back_to_its_own(self):
        self.blk("ev_charger", rate_source="measured")
        self.assertTrue(self.blocks()["ev_charger"]["bill_applied"])


class Segments(_Base):

    def test_band_and_attribution_are_both_carried(self):
        """Together they are the 2x2 cell — which is why a client never infers a
        band from a rate (C §3, corrected 25 Sep)."""
        import pricing_segments as ps
        self.blk()
        self.st.set_block_segments(FROM, "electricity_main",
                                   [ps.Segment(1.0, 0.0549, 0.0523, "off_peak", "ev"),
                                    ps.Segment(1.0, 0.3231, 0.3077, "day", "house")])
        segs = self.st.get_energy_segments(FROM, TO)
        self.assertEqual({(s["attribution"], s["band"]) for s in segs},
                         {("ev", "off_peak"), ("house", "day")})

    def test_they_are_ordered_within_a_block(self):
        import pricing_segments as ps
        self.blk()
        self.st.set_block_segments(FROM, "electricity_main",
                                   [ps.Segment(1.0, 0.05, None, "off_peak", "ev"),
                                    ps.Segment(2.0, 0.32, None, "day", "house")])
        seqs = [s["seq"] for s in self.st.get_energy_segments(FROM, TO)]
        self.assertEqual(seqs, sorted(seqs))


class DayRollups(_Base):

    def test_one_row_per_day_per_meter(self):
        self.blk(start="2026-09-01T00:00:00")
        self.blk(start="2026-09-01T00:30:00")
        self.blk("ev_charger", start="2026-09-01T00:00:00")
        rows = self.st.get_day_rollups(FROM, TO, "UTC")
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["meter_id"] for r in rows}, {"electricity_main", "ev_charger"})

    def test_it_sums_and_counts(self):
        self.blk(start="2026-09-01T00:00:00", imp_kwh=2.0, imp_cost=0.5)
        self.blk(start="2026-09-01T00:30:00", imp_kwh=3.0, imp_cost=0.75)
        main = [r for r in self.st.get_day_rollups(FROM, TO, "UTC")
                if r["meter_id"] == "electricity_main"][0]
        self.assertEqual(main["blocks"], 2)
        self.assertAlmostEqual(main["imp_kwh"], 5.0)
        self.assertAlmostEqual(main["imp_cost"], 1.25)

    def test_interpolated_blocks_are_counted_not_hidden(self):
        self.blk(start="2026-09-01T00:00:00", interpolated=1)
        self.blk(start="2026-09-01T00:30:00", interpolated=0)
        main = [r for r in self.st.get_day_rollups(FROM, TO, "UTC")
                if r["meter_id"] == "electricity_main"][0]
        self.assertEqual(main["interpolated_blocks"], 1)


class LocalDays(_Base):
    """REGRESSION (30 Sep 2026): rollups grouped by UTC date, and a UTC day's
    total cannot be split back into local ones — every BST day was wrong."""

    def test_a_bst_evening_block_lands_on_the_next_local_day(self):
        self.blk(start="2026-09-01T22:30:00", imp_kwh=1.0)   # 23:30 BST, 1 Sep
        self.blk(start="2026-09-01T23:00:00", imp_kwh=2.0)   # 00:00 BST, 2 Sep
        rows = {r["day"]: r for r in self.st.get_day_rollups(FROM, TO, "Europe/London")
                if r["meter_id"] == "electricity_main"}
        self.assertAlmostEqual(rows["2026-09-01"]["imp_kwh"], 1.0)
        self.assertAlmostEqual(rows["2026-09-02"]["imp_kwh"], 2.0)

    def test_the_same_blocks_in_utc_share_a_day(self):
        """GUARD: the UTC answer is unchanged for a UTC site. (It fails on the
        pre-fix tree only because that signature took no tz_name.)"""
        self.blk(start="2026-09-01T22:30:00", imp_kwh=1.0)
        self.blk(start="2026-09-01T23:00:00", imp_kwh=2.0)
        rows = [r for r in self.st.get_day_rollups(FROM, TO, "UTC")
                if r["meter_id"] == "electricity_main"]
        self.assertEqual([r["day"] for r in rows], ["2026-09-01"])

    def test_the_clocks_back_day_has_fifty_half_hours(self):
        """25 Oct 2026: BST ends; the local day is 25 hours long."""
        from datetime import datetime, timedelta
        t = datetime(2026, 10, 24, 23, 0)                    # 00:00 BST, 25 Oct
        for i in range(52):
            self.blk(start=(t + timedelta(minutes=30 * i)).isoformat())
        rows = {r["day"]: r for r in self.st.get_day_rollups(
                    "2026-10-24T00:00:00", "2026-10-27T00:00:00", "Europe/London")
                if r["meter_id"] == "electricity_main"}
        self.assertEqual(rows["2026-10-25"]["blocks"], 50)
        self.assertEqual((rows["2026-10-25"]["start_utc"], rows["2026-10-25"]["end_utc"]),
                         ("2026-10-24T23:00:00", "2026-10-26T00:00:00"))

    def test_the_standing_charge_is_the_days_value_not_a_sum(self):
        """REGRESSION: every block carries the full daily charge (48 of 48 on a real
        database), so SUM reported it 48x. v4's rule: the earliest non-zero block."""
        for i in range(48):
            self.blk(start="2026-09-01T%02d:%02d:00" % (i // 2, 30 * (i % 2)),
                     standing_charge=0.6)
        main = [r for r in self.st.get_day_rollups(FROM, TO, "UTC")
                if r["meter_id"] == "electricity_main"][0]
        self.assertAlmostEqual(main["standing_charge"], 0.6)

    def test_a_leading_zero_does_not_shadow_the_charge(self):
        """v4: an early gap-filled block with 0 must not become the day's value."""
        self.blk(start="2026-09-01T00:00:00", standing_charge=0.0)
        self.blk(start="2026-09-01T00:30:00", standing_charge=0.6)
        self.blk(start="2026-09-01T01:00:00", standing_charge=0.55)
        main = [r for r in self.st.get_day_rollups(FROM, TO, "UTC")
                if r["meter_id"] == "electricity_main"][0]
        self.assertAlmostEqual(main["standing_charge"], 0.6)

    def test_the_range_clamps_a_partial_first_day(self):
        self.blk(start="2026-09-01T12:00:00")
        rows = self.st.get_day_rollups("2026-09-01T12:00:00", "2026-09-02T00:00:00",
                                       "Europe/London")
        self.assertEqual(rows[0]["start_utc"], "2026-09-01T12:00:00")


class Surfaces(unittest.TestCase):

    def test_all_three_are_reads(self):
        from store_types import ReadStore
        for name in ("get_energy_blocks", "get_energy_segments", "get_day_rollups"):
            self.assertTrue(hasattr(ReadStore, name), name)


if __name__ == "__main__":
    unittest.main()
