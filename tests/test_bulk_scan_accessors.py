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
    st = BlockStore(os.path.join(tempfile.mkdtemp(), "blocks.db"))
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



class Surfaces(unittest.TestCase):

    def test_reads_read_write_writes(self):
        from store_types import ReadStore, WriteStore
        self.assertTrue(hasattr(ReadStore, "get_blocks_off_dispatch_split"))
        # the carve's own write (carve_ev_split) went with engine step 4c: it settles each
        # half-hour, through write_predicted / apply_measured_to_main
        self.assertTrue(hasattr(WriteStore, "write_predicted"))
        self.assertFalse(hasattr(ReadStore, "write_predicted"),
                         "a write on the read surface has no thread guard")


if __name__ == "__main__":
    unittest.main()
