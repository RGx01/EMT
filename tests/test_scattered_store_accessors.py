"""
The last five raw-connection sites in engine.py — A acceptance 5.

Five single SELECTs across five unrelated functions. After this, `engine.py`
holds NO raw database connection: every read goes through a `ReadStore` method
and every write through a `WriteStore` one.

One of the five needed no new method — `_iog_slot_is_boost` wanted the sources on
a slot's dispatch rows, which `get_dispatch_history_for_slot` (A2) already
returns.

`get_block_rate_authority` replaced four POSITIONAL reads (`_mrow[0]` … `_mrow[3]`)
off a four-column SELECT. Positional access to a row is a landmine for whoever
next edits the SELECT list, and this is the second time the port has found one.
"""
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from block_store import BlockStore          # noqa: E402

S = "2026-09-01T00:00:00"


def _mk_store():
    st = BlockStore(tempfile.mktemp(suffix=".db"))
    st._conn.execute("PRAGMA foreign_keys=OFF")
    return st


def _block(st, start=S, meter_id="electricity_main", **cols):
    base = {"block_start": start, "block_end": start, "config_period_id": 1,
            "meter_id": meter_id, "imp_kwh": 5.0}
    base.update(cols)
    st._conn.execute("INSERT INTO blocks (%s) VALUES (%s)"
                     % (",".join(base), ",".join("?" * len(base))),
                     tuple(base.values()))
    st._conn.commit()


class SiteTimezone(unittest.TestCase):

    def test_no_config_period_is_utc(self):
        st = BlockStore(tempfile.mktemp(suffix=".db"))
        st._conn.execute("DELETE FROM config_periods")
        st._conn.commit()
        self.assertEqual(st.site_timezone(), "UTC")

    def test_the_newest_period_wins(self):
        """The cap day is a LOCAL noon-to-noon window, so this moves with BST and
        must come from the period in force, not the oldest one on file."""
        st = _mk_store()
        st._conn.execute("DELETE FROM config_periods")
        for ef, tz in (("2023-01-01T00:00:00", "UTC"),
                       ("2026-01-01T00:00:00", "Europe/London")):
            st._conn.execute(
                "INSERT INTO config_periods (effective_from, billing_day, "
                "block_minutes, timezone, currency_symbol, currency_code) "
                "VALUES (?,1,30,?,'£','GBP')", (ef, tz))
        st._conn.commit()
        self.assertEqual(st.site_timezone(), "Europe/London")

    def test_a_null_timezone_falls_back(self):
        st = _mk_store()
        st._conn.execute("UPDATE config_periods SET timezone = NULL")
        st._conn.commit()
        self.assertEqual(st.site_timezone(), "UTC")


class BlockStartsInRange(unittest.TestCase):

    def setUp(self):
        self.st = _mk_store()

    def test_half_open_and_ascending(self):
        for d in ("01", "02", "03"):
            _block(self.st, "2026-09-%sT00:00:00" % d)
        out = self.st.get_block_starts_in_range(
            "electricity_main", "2026-09-01T00:00:00", "2026-09-03T00:00:00")
        self.assertEqual(out, ["2026-09-01T00:00:00", "2026-09-02T00:00:00"])

    def test_distinct_is_redundant_given_the_unique_index(self):
        """Written believing a slot could carry two rows for one meter. It
        cannot: `blocks` has UNIQUE (block_start, meter_id), so a second insert
        raises. The query's DISTINCT is therefore a no-op for a single-meter
        scan — harmless, and worth knowing before anyone reads it as evidence
        that duplicate slots occur.

        Same shape as the SUM in `completed_dispatch_kwh`, which aggregates over
        a table whose PRIMARY KEY permits one row: a defensive clause that the
        schema already guarantees."""
        import sqlite3
        _block(self.st, S)
        with self.assertRaises(sqlite3.IntegrityError):
            _block(self.st, S, meter_id="electricity_main")
        out = self.st.get_block_starts_in_range(
            "electricity_main", "2026-09-01T00:00:00", "2026-09-09T00:00:00")
        self.assertEqual(out, [S])

    def test_it_is_meter_scoped(self):
        _block(self.st, S, meter_id="sub_meter_ev")
        self.assertEqual(self.st.get_block_starts_in_range(
            "electricity_main", "2026-09-01T00:00:00", "2026-09-09T00:00:00"), [])

    def test_it_returns_strings_not_rows(self):
        """The shape the caller expects. A mock stubbing the old `_conn` path
        returned rows, and silently yielded nothing once the call moved."""
        _block(self.st, S)
        out = self.st.get_block_starts_in_range(
            "electricity_main", "2026-09-01T00:00:00", "2026-09-09T00:00:00")
        self.assertIsInstance(out[0], str)


class BlockRateAuthority(unittest.TestCase):
    """*May I re-resolve this block's rate, or has something better decided?*"""

    def setUp(self):
        self.st = _mk_store()

    def test_missing_block_is_none(self):
        self.assertIsNone(self.st.get_block_rate_authority(S))

    def test_it_is_read_by_name(self):
        _block(self.st, S, rate_corrected=1, rate_reconciled=0,
               imp_rate=0.25, rate_source="corrected")
        r = self.st.get_block_rate_authority(S)
        self.assertEqual(r["rate_corrected"], 1)
        self.assertEqual(r["rate_source"], "corrected")
        self.assertAlmostEqual(r["imp_rate"], 0.25)

    def test_all_three_authorities_are_present(self):
        """A user correction, a completed reconciliation, or a measured rate each
        outranks the heuristic. BL-53 added the third so a settlement re-cost
        cannot re-resolve the overlay over the bill."""
        _block(self.st, S, rate_source="settled", imp_rate=0.0549)
        r = self.st.get_block_rate_authority(S)
        for k in ("rate_corrected", "rate_reconciled", "imp_rate", "rate_source"):
            self.assertIn(k, r)


class BlockId(unittest.TestCase):

    def test_missing_is_none(self):
        self.assertIsNone(_mk_store().get_block_id(S))

    def test_it_returns_the_surrogate_key(self):
        """`generation_mix` keys on blocks.id rather than (block_start, meter_id),
        so the mix write needs the id."""
        st = _mk_store()
        _block(st, S)
        bid = st.get_block_id(S)
        self.assertIsInstance(bid, int)
        st.upsert_generation_mix(bid, [{"fuel": "wind", "perc": 40.0}])
        self.assertEqual(len(st.get_generation_mix(bid)), 1)

    def test_it_is_meter_scoped(self):
        """The mix is a GRID property and is stored against the main row only —
        storing it per meter would triple the rows for no information."""
        st = _mk_store()
        _block(st, S, meter_id="sub_meter_ev")
        self.assertIsNone(st.get_block_id(S))
        self.assertIsNotNone(st.get_block_id(S, meter_id="sub_meter_ev"))


class BoostReusesTheDispatchAccessor(unittest.TestCase):

    def test_sources_come_back_for_the_slot(self):
        """`_iog_slot_is_boost` needed the sources on a slot's dispatch rows.
        `get_dispatch_history_for_slot` already returns them, so this site needed
        no new method — the filter moved into Python."""
        st = _mk_store()
        st.record_dispatch_history(slot_start=S, kind="planned", provider="octopus",
                                   source="bump-charge", energy_kwh=-3.0,
                                   raw_start=None, raw_end=None)
        rows = st.get_dispatch_history_for_slot(S)
        self.assertEqual([r["source"] for r in rows], ["bump-charge"])

    def test_a_completed_row_carries_no_source(self):
        """Which is why the boost test reads the planned/started record: a
        completed dispatch has lost the smart-charge signal."""
        st = _mk_store()
        st.record_dispatch_history(slot_start=S, kind="completed", provider="octopus",
                                   source=None, energy_kwh=-3.0,
                                   raw_start=None, raw_end=None)
        rows = st.get_dispatch_history_for_slot(S)
        self.assertEqual([r["source"] for r in rows], [None])


class Surfaces(unittest.TestCase):

    def test_the_four_new_ones_are_reads(self):
        from store_types import ReadStore
        for name in ("site_timezone", "get_block_starts_in_range",
                     "get_block_rate_authority", "get_block_id"):
            self.assertTrue(hasattr(ReadStore, name), name)


class EngineHoldsNoRawConnection(unittest.TestCase):
    """The property acceptance 5 bought. Asserted here as well as in the ratchet,
    because this is the file that finished the job."""

    def test_zero_sites(self):
        import ast
        tree = ast.parse(open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read())
        raw = [n.lineno for n in ast.walk(tree)
               if isinstance(n, ast.Attribute) and n.attr == "_conn"]
        self.assertEqual(raw, [], "raw connection use returned at %s" % raw)

    def test_no_aliases_either(self):
        """`conn = store._conn` hid three uses behind a local name for three
        slices before the scan learned to follow it."""
        import ast
        tree = ast.parse(open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read())
        aliases = [n.lineno for n in ast.walk(tree)
                   if isinstance(n, ast.Assign) and isinstance(n.value, ast.Attribute)
                   and n.value.attr == "_conn"]
        self.assertEqual(aliases, [])


if __name__ == "__main__":
    unittest.main()
