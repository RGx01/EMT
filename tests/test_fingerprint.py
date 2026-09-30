"""
The fingerprint — C §2a.

`_blocks_data_version` is ONE GLOBAL TOKEN over the whole database, so any change
invalidates everything: a new half-hour tells a client looking at 2023 to re-fetch
four years. That is the wholesale problem migrated to the client, and §2a replaces
it with two levels matching the two real shapes of change.

  * **per-day**, derived from the rows in range — the routine 99.9%. Measured on
    prod-dev over 13.5 hours: 27 new blocks (exactly the clock) and 20 changed,
    none older than 1.8 days. Today moves twice an hour; 2023 never moves.
  * **an epoch**, bumped by the handful of operations that legitimately touch all
    history — import, delete, purge. One signal instead of thousands.

THE PROPERTY THAT IS EASY TO LOSE, and which `_blocks_data_version` already has
(BL-32): **it must cover rates, not just money.** A re-price can be cost-neutral —
4.5.13's band snap changed 24,128 rates for a total drift of £0.0028. Totals
identical, rate lines different. A fingerprint over money alone shows a stale rate
line indefinitely.
"""
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from block_store import BlockStore              # noqa: E402

FROM, TO = "2026-09-01T00:00:00", "2026-09-10T00:00:00"


class _Base(unittest.TestCase):
    def setUp(self):
        self.st = BlockStore(tempfile.mktemp(suffix=".db"))
        self.st._conn.execute("PRAGMA foreign_keys=OFF")
        self.addCleanup(self.st.close)

    def blk(self, start, **cols):
        base = {"block_start": start, "block_end": start, "config_period_id": 1,
                "meter_id": "electricity_main", "imp_kwh": 1.0, "imp_cost": 0.25}
        base.update(cols)
        self.st._conn.execute("INSERT INTO blocks (%s) VALUES (%s)"
                              % (",".join(base), ",".join("?" * len(base))),
                              tuple(base.values()))
        self.st._conn.commit()

    def fp(self):
        return self.st.day_fingerprints(FROM, TO)


class PerDayNotGlobal(_Base):

    def test_a_change_on_one_day_leaves_other_days_alone(self):
        """The whole point. A new half-hour today must not invalidate 2023."""
        self.blk("2026-09-01T00:00:00")
        self.blk("2026-09-05T00:00:00")
        before = self.fp()
        self.blk("2026-09-05T00:30:00")
        after = self.fp()
        self.assertEqual(before["2026-09-01"], after["2026-09-01"],
                         "an untouched day moved")
        self.assertNotEqual(before["2026-09-05"], after["2026-09-05"])

    def test_only_days_in_range_are_returned(self):
        self.blk("2026-08-01T00:00:00")
        self.blk("2026-09-02T00:00:00")
        self.assertEqual(sorted(self.fp()), ["2026-09-02"])

    def test_an_empty_range_is_an_empty_dict(self):
        self.assertEqual(self.fp(), {})


class ItCoversEverythingDisplayed(_Base):

    def _seg(self, start, inc_rate, kwh=1.0):
        import pricing_segments as ps
        self.st.set_block_segments(start, "electricity_main",
                                   [ps.Segment(kwh, inc_rate, None, "off_peak", "house")])

    def test_a_cost_neutral_reprice_still_moves_it(self):
        """BL-32, carried forward rather than rediscovered. The rate changes, the
        money does not, and a money-only fingerprint would sit still while the
        user's rate line went stale."""
        self.blk("2026-09-03T00:00:00", imp_kwh=1.0, imp_cost=0.25)
        self._seg("2026-09-03T00:00:00", 0.25)
        before = self.fp()["2026-09-03"]
        self._seg("2026-09-03T00:00:00", 0.30)          # rate moves, cost column does not
        after = self.fp()["2026-09-03"]
        self.assertNotEqual(before, after, "a rate-only re-price was invisible")

    def test_an_export_only_settlement_moves_it(self):
        """Export settles later than import, so an export-only settlement moves
        `exp_*` and nothing else — and must still invalidate."""
        self.blk("2026-09-04T00:00:00", exp_kwh=0.0, exp_cost=0.0)
        before = self.fp()["2026-09-04"]
        self.st._conn.execute("UPDATE blocks SET exp_kwh=2.0, exp_cost=0.30 "
                              "WHERE block_start='2026-09-04T00:00:00'")
        self.st._conn.commit()
        self.assertNotEqual(before, self.fp()["2026-09-04"])

    def test_a_carbon_backfill_moves_it(self):
        self.blk("2026-09-06T00:00:00")
        before = self.fp()["2026-09-06"]
        self.st._conn.execute("UPDATE blocks SET carbon_g=140 "
                              "WHERE block_start='2026-09-06T00:00:00'")
        self.st._conn.commit()
        self.assertNotEqual(before, self.fp()["2026-09-06"])

    def test_an_in_place_cost_edit_moves_it(self):
        """The row count and newest block are unchanged — this is what the value
        fingerprint exists to catch."""
        self.blk("2026-09-07T00:00:00", imp_cost=0.25)
        before = self.fp()["2026-09-07"]
        self.st._conn.execute("UPDATE blocks SET imp_cost=0.99 "
                              "WHERE block_start='2026-09-07T00:00:00'")
        self.st._conn.commit()
        self.assertNotEqual(before, self.fp()["2026-09-07"])

    def test_an_ev_split_only_change_moves_it(self):
        """REGRESSION (C, 30 Sep 2026): the Billing tab shows the IOG EV/Home split,
        and the carve / settled-split heal move it without touching a total."""
        self.blk("2026-09-09T00:00:00", imp_kwh=2.0, imp_cost=0.14)
        before = self.fp()["2026-09-09"]
        self.st._conn.execute("UPDATE blocks SET imp_kwh_ev=1.2, imp_cost_ev=0.084 "
                              "WHERE block_start='2026-09-09T00:00:00'")
        self.st._conn.commit()
        self.assertNotEqual(before, self.fp()["2026-09-09"])

    def test_an_attribution_move_moves_it(self):
        """REGRESSION (C, 30 Sep 2026): house <-> EV at an unchanged rate."""
        import pricing_segments as ps
        self.blk("2026-09-02T00:00:00", imp_kwh=2.0, imp_cost=0.14)
        self.st.set_block_segments("2026-09-02T00:00:00", "electricity_main",
                                   [ps.Segment(1.0, 0.07, None, "off_peak", "house"),
                                    ps.Segment(1.0, 0.07, None, "off_peak", "house")])
        before = self.fp()["2026-09-02"]
        self.st.set_block_segments("2026-09-02T00:00:00", "electricity_main",
                                   [ps.Segment(1.0, 0.07, None, "off_peak", "house"),
                                    ps.Segment(1.0, 0.07, None, "off_peak", "ev")])
        self.assertNotEqual(before, self.fp()["2026-09-02"])

    def test_a_band_relabel_moves_it(self):
        """REGRESSION (C, 30 Sep 2026): standard -> off_peak at an unchanged rate
        (two labels of the same length)."""
        import pricing_segments as ps
        self.blk("2026-09-05T00:00:00", imp_kwh=1.0, imp_cost=0.07)
        self.st.set_block_segments("2026-09-05T00:00:00", "electricity_main",
                                   [ps.Segment(1.0, 0.07, None, "standard", "house")])
        before = self.fp()["2026-09-05"]
        self.st.set_block_segments("2026-09-05T00:00:00", "electricity_main",
                                   [ps.Segment(1.0, 0.07, None, "off_peak", "house")])
        self.assertNotEqual(before, self.fp()["2026-09-05"])

    def test_it_is_stable_when_nothing_changes(self):
        """A fingerprint that moves on its own is a poll that never settles."""
        self.blk("2026-09-08T00:00:00")
        self.assertEqual(self.fp(), self.fp())


class DerivedFromRowsNotDeclared(_Base):

    def test_a_writer_that_declares_nothing_still_invalidates(self):
        """No job announces its scope; the fingerprint is computed from the data.
        A job that forgets to declare cannot serve stale data — the fingerprint
        can only be slow, never wrong."""
        self.blk("2026-09-09T00:00:00")
        before = self.fp()["2026-09-09"]
        self.st._conn.execute("UPDATE blocks SET imp_kwh = imp_kwh + 1 "
                              "WHERE block_start='2026-09-09T00:00:00'")
        self.st._conn.commit()
        self.assertNotEqual(before, self.fp()["2026-09-09"])


class TheEpoch(_Base):

    def test_it_starts_at_zero_and_increments(self):
        self.assertEqual(self.st.get_data_epoch(), 0)
        self.assertEqual(self.st.bump_data_epoch(), 1)
        self.assertEqual(self.st.get_data_epoch(), 1)

    def test_it_survives_a_reopen(self):
        """Stored in store_meta, so a restart does not silently tell every client
        their cache is fine."""
        self.st.bump_data_epoch()
        path = self.st._path
        self.st.close()
        st2 = BlockStore(path)
        self.addCleanup(st2.close)
        self.assertEqual(st2.get_data_epoch(), 1)

    def test_bulk_operations_bump_it(self):
        """Wired at the three places that touch all history — a range delete, the
        imported-history purge, and post-import finalisation. Asserted
        structurally so a fourth bulk operation added later is a visible
        omission rather than a silent one."""
        import ast
        found = set()
        for rel in ("web/server.py", "engine.py"):
            tree = ast.parse(open(os.path.join(_ROOT, rel), encoding="utf-8").read())
            for n in ast.walk(tree):
                if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "bump_data_epoch":
                    for fn in ast.walk(tree):
                        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                                and fn.lineno <= n.lineno <= fn.end_lineno:
                            found.add(fn.name)
        for expected in ("_delete_worker", "_purge_worker", "_finalise_after_import"):
            self.assertIn(expected, found, "%s no longer bumps the epoch" % expected)

    def test_it_is_a_write_and_reading_it_is_not(self):
        from store_types import ReadStore, WriteStore
        self.assertTrue(hasattr(ReadStore, "get_data_epoch"))
        self.assertTrue(hasattr(ReadStore, "day_fingerprints"))
        self.assertTrue(hasattr(WriteStore, "bump_data_epoch"))
        self.assertFalse(hasattr(ReadStore, "bump_data_epoch"))


class TheEpochAcrossAFileSwap(_Base):
    """REGRESSION (30 Sep 2026): a restore or zip import replaces the database, and
    the epoch lives IN it — so the restored file brought back its own epoch, which
    a client may already hold. Neither path bumped it."""

    def test_the_floor_puts_it_above_the_previous_database(self):
        # the restored file says 2; the database it replaced had reached 5
        self.st.set_meta(self.st._EPOCH_KEY, "2")
        self.assertEqual(self.st.bump_data_epoch(floor=5), 6)

    def test_a_restored_file_ahead_of_the_old_one_still_moves(self):
        self.st.set_meta(self.st._EPOCH_KEY, "9")
        self.assertEqual(self.st.bump_data_epoch(floor=5), 10)

    def test_both_swap_paths_bump_with_the_pre_swap_epoch(self):
        import ast
        tree = ast.parse(open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read())
        for name in ("_restore_worker", "api_import_from_zip"):
            fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
            src = ast.unparse(fn)
            self.assertIn("bump_data_epoch(floor=_epoch_before)", src, name)
            self.assertLess(src.index("_epoch_before = store_access.store().get_data_epoch()"),
                            src.index("bump_data_epoch(floor=_epoch_before)"), name)


class TheEndpointCarriesIt(unittest.TestCase):

    def test_api_energy_returns_epoch_and_days(self):
        from tests.test_server import make_client
        import json
        d = json.loads(make_client().get("/api/energy?kind=month").data)
        self.assertIn("fingerprint", d)
        self.assertIn("epoch", d["fingerprint"])
        self.assertIsInstance(d["fingerprint"]["days"], dict)


if __name__ == "__main__":
    unittest.main()
