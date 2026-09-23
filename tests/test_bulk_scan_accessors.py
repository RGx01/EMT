"""
The two bulk candidate scans, and the EV carve write — A acceptance 5.

`reprice_history_conformance` and `run_iog_split_carve` each ran a multi-clause
SELECT against the raw connection, and the carve then wrote its result back
through a raw `with conn:` transaction that a `conn = _store._conn` alias had
made invisible to the ratchet (see test_raw_connection_ratchet.py).

The carve's two guards are the interesting part and are pinned individually:
`imp_kwh_ev IS NULL` in the WHERE, so a block carved between selection and write
is not carved twice; and `COALESCE` on the bands, so a predicted carve fills a
blank rather than overwriting an answer settlement already gave.
"""
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from block_store import BlockStore          # noqa: E402


def _mk_store():
    st = BlockStore(tempfile.mktemp(suffix=".db"))
    st._conn.execute("PRAGMA foreign_keys=OFF")
    return st


def _block(st, start, **cols):
    base = {"block_start": start, "block_end": start, "config_period_id": 1,
            "meter_id": "electricity_main"}
    base.update(cols)
    ks = ",".join(base)
    st._conn.execute("INSERT INTO blocks (%s) VALUES (%s)"
                     % (ks, ",".join("?" * len(base))), tuple(base.values()))
    st._conn.commit()


def _dispatch(st, slot, kwh=3.0, kind="completed"):
    st.record_dispatch_history(slot_start=slot, kind=kind, provider="octopus",
                               source=None, energy_kwh=kwh,
                               raw_start=None, raw_end=None)


class PricingRangeScan(unittest.TestCase):

    def setUp(self):
        self.st = _mk_store()

    def test_range_is_half_open_and_ordered(self):
        for d in ("01", "02", "03"):
            _block(self.st, "2026-09-%sT00:00:00" % d, imp_kwh=1.0)
        out = self.st.get_blocks_pricing_for_range(
            "electricity_main", "2026-09-01T00:00:00", "2026-09-03T00:00:00", 100)
        self.assertEqual([r["block_start"] for r in out],
                         ["2026-09-01T00:00:00", "2026-09-02T00:00:00"])

    def test_limit_applies(self):
        for d in ("01", "02", "03"):
            _block(self.st, "2026-09-%sT00:00:00" % d, imp_kwh=1.0)
        out = self.st.get_blocks_pricing_for_range(
            "electricity_main", "2026-09-01T00:00:00", "2026-09-09T00:00:00", 2)
        self.assertEqual(len(out), 2)

    def test_rows_subscript_by_name(self):
        """The consumer hands each row to `_reprice_history_block`, which
        subscripts by column name -- the same access a sqlite3.Row supports, so
        the suite's hand-built Row fixtures stay interchangeable with these."""
        _block(self.st, "2026-09-01T00:00:00", imp_kwh=2.0, imp_rate=0.25)
        (r,) = self.st.get_blocks_pricing_for_range(
            "electricity_main", "2026-09-01T00:00:00", "2026-09-02T00:00:00", 10)
        self.assertAlmostEqual(r["imp_kwh"], 2.0)
        self.assertAlmostEqual(r["imp_rate"], 0.25)
        self.assertIn("imp_kwh_api", r)


class EvCarveCandidates(unittest.TestCase):

    def setUp(self):
        self.st = _mk_store()

    def _candidate(self, start):
        _block(self.st, start, imp_kwh=5.0, imp_rate=0.25)
        _dispatch(self.st, start)

    def test_a_plain_candidate_is_selected(self):
        self._candidate("2026-09-01T00:00:00")
        self.assertEqual(len(self.st.get_blocks_awaiting_ev_carve(10)), 1)

    def test_already_carved_is_excluded(self):
        _block(self.st, "2026-09-01T00:00:00", imp_kwh=5.0, imp_kwh_ev=2.0)
        _dispatch(self.st, "2026-09-01T00:00:00")
        self.assertEqual(self.st.get_blocks_awaiting_ev_carve(10), [])

    def test_no_completed_dispatch_is_excluded(self):
        _block(self.st, "2026-09-01T00:00:00", imp_kwh=5.0)
        _dispatch(self.st, "2026-09-01T00:00:00", kind="planned")
        self.assertEqual(self.st.get_blocks_awaiting_ev_carve(10), [])

    def test_settled_and_corrected_are_excluded(self):
        for i, src in enumerate(("measured", "corrected")):
            s = "2026-09-0%dT00:00:00" % (i + 1)
            _block(self.st, s, imp_kwh=5.0, rate_source=src)
            _dispatch(self.st, s)
        self.assertEqual(self.st.get_blocks_awaiting_ev_carve(10), [])

    def test_a_null_rate_source_is_a_candidate(self):
        """`NOT IN` is UNKNOWN against NULL, so the clause needs its explicit
        `IS NULL` arm -- without it every never-stamped block would be dropped,
        which is most of them."""
        self._candidate("2026-09-01T00:00:00")
        row = self.st._conn.execute(
            "SELECT rate_source FROM blocks WHERE block_start='2026-09-01T00:00:00'"
        ).fetchone()
        self.assertIsNone(row["rate_source"])
        self.assertEqual(len(self.st.get_blocks_awaiting_ev_carve(10)), 1)

    def test_zero_kwh_is_excluded(self):
        _block(self.st, "2026-09-01T00:00:00", imp_kwh=0.0)
        _dispatch(self.st, "2026-09-01T00:00:00")
        self.assertEqual(self.st.get_blocks_awaiting_ev_carve(10), [])


class CarveWrite(unittest.TestCase):

    def setUp(self):
        self.st = _mk_store()
        _block(self.st, "2026-09-01T00:00:00", imp_kwh=5.0, imp_rate=0.25)

    def _row(self):
        return self.st._conn.execute(
            "SELECT imp_kwh_ev, imp_cost_ev, imp_rate_ev, imp_ev_band, imp_home_band "
            "FROM blocks WHERE block_start='2026-09-01T00:00:00'").fetchone()

    def test_it_writes_and_reports_one_row(self):
        n = self.st.carve_ev_split("2026-09-01T00:00:00", 2.0, 0.5, 0.25,
                                   "off_peak", "day")
        self.assertEqual(n, 1)
        r = self._row()
        self.assertAlmostEqual(r["imp_kwh_ev"], 2.0)
        self.assertEqual(r["imp_ev_band"], "off_peak")

    def test_a_second_carve_is_refused(self):
        """The `imp_kwh_ev IS NULL` guard lives in the WHERE, so selection and
        write cannot come apart -- a block carved in between is not carved
        twice."""
        self.st.carve_ev_split("2026-09-01T00:00:00", 2.0, 0.5, 0.25,
                               "off_peak", "day")
        n = self.st.carve_ev_split("2026-09-01T00:00:00", 9.9, 9.9, 9.9,
                                   "peak", "night")
        self.assertEqual(n, 0, "a second carve must change nothing")
        self.assertAlmostEqual(self._row()["imp_kwh_ev"], 2.0)

    def test_an_existing_band_is_not_overwritten(self):
        """COALESCE: a band already recorded was decided by something with
        better information. A prediction fills a blank; it does not argue."""
        self.st._conn.execute(
            "UPDATE blocks SET imp_ev_band='peak' WHERE block_start=?",
            ("2026-09-01T00:00:00",))
        self.st._conn.commit()
        self.st.carve_ev_split("2026-09-01T00:00:00", 2.0, 0.5, 0.25,
                               "off_peak", "day")
        r = self._row()
        self.assertEqual(r["imp_ev_band"], "peak", "existing band was clobbered")
        self.assertEqual(r["imp_home_band"], "day", "blank band should fill")

    def test_it_is_meter_scoped(self):
        n = self.st.carve_ev_split("2026-09-01T00:00:00", 2.0, 0.5, 0.25,
                                   "off_peak", "day", meter_id="sub_meter_ev")
        self.assertEqual(n, 0)


class Surfaces(unittest.TestCase):

    def test_reads_read_write_writes(self):
        from store_types import ReadStore, WriteStore
        for name in ("get_blocks_pricing_for_range", "get_blocks_awaiting_ev_carve"):
            self.assertTrue(hasattr(ReadStore, name), name)
        self.assertTrue(hasattr(WriteStore, "carve_ev_split"))
        self.assertFalse(hasattr(ReadStore, "carve_ev_split"),
                         "a write on the read surface has no thread guard")


if __name__ == "__main__":
    unittest.main()
