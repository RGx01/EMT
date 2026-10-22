"""
The two bulk candidate scans, and the EV carve write — A acceptance 5.

`run_iog_split_carve` (and `reprice_history_conformance`, deleted 22 Oct 2026 with its
`get_blocks_pricing_for_range`) each ran a multi-clause SELECT against the raw connection, and the carve then wrote its result back
through a raw `with conn:` transaction that a `conn = _store._conn` alias had
made invisible to the ratchet (see test_raw_connection_ratchet.py).

The carve's two guards are the interesting part and are pinned individually:
"no EV segment yet", checked in the write's own transaction, so a block carved
between selection and write is not carved twice; and the house band already
recorded on the block's segments is kept, so a predicted carve fills a blank
rather than overwriting an answer settlement already gave. (Until step 5c both
lived in the imp_*_ev columns: `imp_kwh_ev IS NULL` and `COALESCE` on the bands.)
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


def _carve_segs(ev_kwh=2.0, house_kwh=3.0, rate=0.25, ev_band="off_peak",
                home_band="day"):
    return [(ev_kwh, rate, None, ev_band, "ev"), (house_kwh, rate, None, home_band, "house")]


class CarveWrite(unittest.TestCase):

    def setUp(self):
        self.st = _mk_store()
        _block(self.st, "2026-09-01T00:00:00", imp_kwh=5.0, imp_rate=0.25)

    def _segs(self):
        return {s["attribution"]: s for s in
                self.st.get_block_segments("2026-09-01T00:00:00", "electricity_main")}

    def test_it_writes_and_reports_one_row(self):
        n = self.st.carve_ev_split("2026-09-01T00:00:00", segments=_carve_segs())
        self.assertEqual(n, 1)
        r = self._segs()
        self.assertAlmostEqual(r["ev"]["kwh"], 2.0)
        self.assertEqual(r["ev"]["band"], "off_peak")

    def test_a_second_carve_is_refused(self):
        """The "no EV segment yet" guard is checked in the write's own
        transaction, so selection and write cannot come apart -- a block carved
        in between is not carved twice."""
        self.st.carve_ev_split("2026-09-01T00:00:00", segments=_carve_segs())
        n = self.st.carve_ev_split("2026-09-01T00:00:00", segments=_carve_segs(
            9.9, 9.9, 9.9, "peak", "night"))
        self.assertEqual(n, 0, "a second carve must change nothing")
        self.assertAlmostEqual(self._segs()["ev"]["kwh"], 2.0)

    def test_an_existing_band_is_not_overwritten(self):
        """A band already recorded was decided by something with better
        information. A prediction fills a blank; it does not argue. Under the
        guard the block has no EV segment, so the recorded answer is the house
        segments' band (step 5c; it was COALESCE on the band columns)."""
        self.st.set_block_segments("2026-09-01T00:00:00", "electricity_main",
                                   [(5.0, 0.25, None, "peak", "house")])
        self.st.carve_ev_split("2026-09-01T00:00:00", segments=_carve_segs())
        r = self._segs()
        self.assertEqual(r["house"]["band"], "peak", "existing band was clobbered")
        self.assertEqual(r["ev"]["band"], "off_peak", "blank band should fill")

    def test_it_is_meter_scoped(self):
        n = self.st.carve_ev_split("2026-09-01T00:00:00", meter_id="sub_meter_ev",
                                   segments=_carve_segs())
        self.assertEqual(n, 0)


class Surfaces(unittest.TestCase):

    def test_reads_read_write_writes(self):
        from store_types import ReadStore, WriteStore
        self.assertTrue(hasattr(ReadStore, "get_blocks_off_dispatch_split"))
        self.assertTrue(hasattr(WriteStore, "carve_ev_split"))
        self.assertFalse(hasattr(ReadStore, "carve_ev_split"),
                         "a write on the read surface has no thread guard")


if __name__ == "__main__":
    unittest.main()
