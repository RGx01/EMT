"""
BL-79 (1) — a settled kWh on a held-rate block refits the block's segments (8 Oct 2026).

When the settlement re-run finds a corrected, reconciled or measured rate, it holds that rate and
re-costs the block to the settled kWh — but re-derived nothing else, so the block's segments stayed
at the kWh it was finalised with. On real databases 11–12 measured blocks per install (4 with EV)
and a corrected one had segments that no longer summed to imp_kwh. The bill's EV/Home rows and
every EV surface (ev_split) read the segments, so they no longer added up to the block.

Now the segments are fitted to the settled kWh at the rates they carry: the house takes the
change; only when the house is gone does the EV shrink, and the EV columns follow it.

Discriminating: test_its_segments_follow_the_settled_kwh, test_the_house_takes_a_rise and
test_the_ev_shrinks_only_when_the_house_is_gone fail on the parent. No-re-price, the predicted
band, no settlement and the helper's own cases are guards.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import test_rebuild_meter_type as H              # noqa: E402  (stubs, real engine, flat tariff)
import pricing_segments as ps                    # noqa: E402
engine = H.engine

MAIN = H.MAIN                                    # 7.058 kWh at the close


class _Held(H._Harness):

    def _segs(self):
        return engine._store.get_block_segments(H.START, "electricity_main")

    def _hold(self, word, segs=None, kwh_ev=None):
        """Mark the closed block's rate as held by `word`; optionally give it an EV split."""
        c = engine._store._conn
        with c:
            c.execute("UPDATE blocks SET rate_source=? WHERE block_start=? "
                      "AND meter_id='electricity_main'", (word, H.START))
            if kwh_ev is not None:
                c.execute("UPDATE blocks SET imp_kwh_ev=?, imp_cost_ev=ROUND(?*imp_rate, 6), "
                          "imp_rate_ev=imp_rate WHERE block_start=? AND meter_id='electricity_main'",
                          (kwh_ev, kwh_ev, H.START))
        if segs is not None:
            engine._store.set_block_segments(H.START, "electricity_main", segs)

    def _total(self, attr=None):
        return round(sum(s["kwh"] for s in self._segs()
                         if attr is None or s["attribution"] == attr), 6)

    def _row(self):
        return engine._store._conn.execute(
            "SELECT imp_kwh, imp_rate, imp_kwh_ev, imp_cost_ev FROM blocks WHERE block_start=? "
            "AND meter_id='electricity_main'", (H.START,)).fetchone()


class ACorrectedBlock(_Held):

    def test_its_segments_follow_the_settled_kwh(self):
        self._finalise()
        self._hold("corrected")
        self._settle(dcc=7.5)
        self.assertAlmostEqual(self._row()["imp_kwh"], 7.5, places=6)
        self.assertAlmostEqual(self._total(), 7.5, places=6)

    def test_no_segment_is_re_priced(self):
        """GUARD: a held rate stays held."""
        self._finalise()
        before = {s["inc_rate"] for s in self._segs()}
        self._hold("corrected")
        self._settle(dcc=7.5)
        self.assertEqual({s["inc_rate"] for s in self._segs()}, before)


class AMeasuredBlockWithAnEv(_Held):
    """The bill split the half-hour 3.0 EV + 4.058 house; a later settlement revises the kWh."""

    def _ev_block(self):
        self._finalise()
        r = H.FLAT
        self._hold("settled", kwh_ev=3.0, segs=[
            ps.Segment(3.0, r, round(r / 1.05, 6), "off_peak", "ev"),
            ps.Segment(round(MAIN - 3.0, 6), r, round(r / 1.05, 6), "off_peak", "house")])

    def test_the_house_takes_a_rise(self):
        self._ev_block()
        self._settle(dcc=7.5)
        self.assertAlmostEqual(self._total("ev"), 3.0, places=6)
        self.assertAlmostEqual(self._total("house"), 4.5, places=6)

    def test_the_ev_shrinks_only_when_the_house_is_gone(self):
        self._ev_block()
        self._settle(dcc=2.0)
        self.assertAlmostEqual(self._total("ev"), 2.0, places=6)
        self.assertAlmostEqual(self._total("house"), 0.0, places=6)
        self.assertAlmostEqual(self._row()["imp_kwh_ev"], 2.0, places=6)    # the columns follow
        self.assertAlmostEqual(self._row()["imp_cost_ev"], round(2.0 * H.FLAT, 6), places=6)


class AHeldPredictionKeepsItsBands(_Held):
    """BL-79 (part 2, 8 Oct 2026): the held PREDICTION path (a schedule block settling) re-ran the
    seam, and where the seam emitted nothing (no dispatch here) _persist_block_segments rebuilt the
    segments from the block dict's imp_*_ev copies — get_block_dict_by_start attaches no segments.
    A two-band block came back as one house segment at the blended rate. Now the stored segments
    are kept and fitted to the settled kWh. Discriminating: the first two fail on the parent."""
    OFF, DAY = 0.07, 0.30

    def _two_bands(self):
        self._finalise()
        c = engine._store._conn
        cost = round(3.0 * self.OFF + (MAIN - 3.0) * self.DAY, 6)
        with c:
            c.execute("UPDATE blocks SET imp_rate=?, imp_cost=? WHERE block_start=? "
                      "AND meter_id='electricity_main'", (round(cost / MAIN, 6), cost, H.START))
        engine._store.set_block_segments(H.START, "electricity_main", [
            ps.Segment(3.0, self.OFF, None, "off_peak", "house"),
            ps.Segment(round(MAIN - 3.0, 6), self.DAY, None, "day", "house")])

    def test_its_bands_survive_the_settlement(self):
        self._two_bands()
        self._settle(dcc=MAIN)
        self.assertEqual([(s["band"], s["inc_rate"]) for s in self._segs()],
                         [("off_peak", self.OFF), ("day", self.DAY)])

    def test_they_are_fitted_to_a_revised_kwh(self):
        self._two_bands()
        self._settle(dcc=7.5)
        self.assertEqual([s["band"] for s in self._segs()], ["off_peak", "day"])
        self.assertAlmostEqual(self._total(), 7.5, places=6)

    def test_a_block_with_no_segments_is_still_given_them(self):
        """GUARD: with nothing stored, the rebuild from the columns still runs."""
        self._finalise()
        with engine._store._conn as c:
            c.execute("DELETE FROM block_segments WHERE block_start=?", (H.START,))
        self._settle(dcc=7.5)
        self.assertAlmostEqual(self._total(), 7.5, places=6)


class TheGuards(_Held):

    def test_a_predicted_band_was_already_refitted(self):
        """GUARD: the held PREDICTION (4.5.15) re-derived its segments already."""
        self._finalise()
        self._settle(dcc=7.5)
        self.assertAlmostEqual(self._total(), 7.5, places=6)

    def test_no_settled_kwh_changes_nothing(self):
        """GUARD: with no settled import the chosen kWh is the stored one; nothing moves."""
        self._finalise()
        self._hold("corrected")
        before = self._segs()
        blk = engine._store.get_block_dict_by_start(H.START)
        engine._rerun_pass2_for_settled_block(blk, billing_source="api", standing_resolver=None)
        engine.append_block_replace(blk)
        self.assertEqual(self._segs(), before)


class TheFit(unittest.TestCase):
    """GUARD: pricing_segments.refit_to_kwh's shapes (new code)."""
    S = ps.Segment

    def _fit(self, segs, kwh):
        return ps.refit_to_kwh(segs, kwh, rate=0.30, exc_rate=None, band="day")

    def test_a_house_less_block_gains_a_house_segment_at_the_held_rate(self):
        out = self._fit([self.S(2.0, 0.07, None, "off_peak", "ev")], 3.0)
        self.assertEqual([(s.kwh, s.inc_rate, s.attribution) for s in out],
                         [(2.0, 0.07, "ev"), (1.0, 0.30, "house")])

    def test_two_house_bands_keep_their_mix(self):
        out = self._fit([self.S(1.0, 0.07, None, "off_peak", "house"),
                         self.S(1.0, 0.30, None, "day", "house")], 3.0)
        self.assertEqual([s.kwh for s in out], [1.5, 1.5])

    def test_it_sums_to_the_kwh(self):
        out = self._fit([self.S(1.0 / 3, 0.07, None, "off_peak", "house"),
                         self.S(2.0 / 3, 0.30, None, "day", "house")], 1.234567)
        self.assertAlmostEqual(sum(s.kwh for s in out), 1.234567, places=6)


if __name__ == "__main__":
    unittest.main()
