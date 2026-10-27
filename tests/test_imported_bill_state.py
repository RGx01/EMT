"""
The Import bill's `imported` state (27 Oct 2026, the user: "imported for both").

1. A bill labelled IMPORTED is the import's own cost (v4import transform 3c, v5's CSV import), not
   Octopus's. settle() prices the half-hour from it, so it reads `rate_source = 'settled'` with the
   cost equal to the bill — and classify read `applied`, Octopus's cost, final: v4's tariff price on
   most API history, and CSV history turned from grey to green. It reads `imported`.
2. A half-hour with no bill, past Octopus's two years: the asker asks nothing older
   (engine._API_RETENTION_DAYS), so it never gets a bill or a give-up, and read `overdue` for good
   (dev, 27 Oct 2026: 1,242 API-imported half-hours of Sep–Oct 2024 red). It reads `imported`.
3. The health strip counts the month holding that line again as the line moves, as it does the
   young months, since a half-hour there turns with age alone.

Discriminating: every test here fails on the parent but those labelled GUARD.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import test_settlement_state as T              # noqa: E402  (classify's fixtures)
import settlement_state as ss                  # noqa: E402

_f, _c, NOW, OFF = T._f, T._c, T.NOW, T.OFF
IMPORTED_BILL = dict(rate_source="settled", bill_cost=round(6 * OFF, 6),
                     bill_cost_exc=round(6 * OFF, 6), bill_kwh=6.0, bill_label="IMPORTED")
OLD = (NOW - timedelta(days=731)).isoformat()          # past Octopus's two years
INSIDE = (NOW - timedelta(days=729)).isoformat()


class AnImportedBill(unittest.TestCase):

    def test_an_api_import_reads_imported(self):
        """dev's v4 API history once re-imported: v4's price at Octopus's label, not Octopus's cost."""
        self.assertEqual(_c(_f(source="imported_api", imp_kwh_api=None, **IMPORTED_BILL))["bill"],
                         "imported")

    def test_csv_history_stays_imported(self):
        self.assertEqual(_c(_f(source="imported_csv", imp_kwh_api=None, **IMPORTED_BILL))["bill"],
                         "imported")

    def test_the_label_is_read_whatever_its_case(self):
        f = _f(source="imported_csv", imp_kwh_api=None, **dict(IMPORTED_BILL, bill_label="imported"))
        self.assertEqual(_c(f)["bill"], "imported")

    def test_GUARD_octopuss_bill_is_applied(self):
        f = _f(source="imported_api", imp_kwh_api=None, **dict(IMPORTED_BILL, bill_label="OFF_PEAK"))
        self.assertEqual(_c(f)["bill"], "applied")

    def test_GUARD_a_correction_is_blue(self):
        f = _f(source="imported_api", **dict(IMPORTED_BILL, rate_source="corrected"))
        self.assertEqual(_c(f)["bill"], "corrected")

    def test_GUARD_nothing_imported_is_none(self):
        f = _f(source="imported_api", imp_kwh=0.0, imp_cost=0.0, **IMPORTED_BILL)
        self.assertEqual(_c(f)["bill"], "none")


class NoBillPastTwoYears(unittest.TestCase):

    def test_an_api_import(self):
        self.assertEqual(_c(_f(block_start=OLD, source="imported_api", imp_kwh_api=None))["bill"],
                         "imported")

    def test_a_live_half_hour(self):
        self.assertEqual(_c(_f(block_start=OLD))["bill"], "imported")

    def test_one_given_up_too(self):
        """Hatched only while a re-fetch could still get it."""
        self.assertEqual(_c(_f(block_start=OLD, source="imported_api", imp_kwh_api=None),
                            given_up={OLD})["bill"], "imported")

    def test_GUARD_inside_the_two_years_it_still_waits(self):
        self.assertEqual(_c(_f(block_start=INSIDE, source="imported_api", imp_kwh_api=None))["bill"],
                         "overdue")

    def test_GUARD_a_bill_held_past_them_is_still_applied(self):
        self.assertEqual(_c(_f(block_start=OLD, **T.BILLED))["bill"], "applied")


class TheConstants(unittest.TestCase):

    def test_they_are_the_engines_and_the_importers(self):
        import block_store
        from v4import import transforms
        self.assertEqual(ss.API_RETENTION_DAYS, T.engine._API_RETENTION_DAYS)
        self.assertEqual(ss.IMPORTED_LABEL, block_store.IMPORTED_BILL_LABEL)
        self.assertEqual(ss.IMPORTED_LABEL, transforms.IMPORTED_LABEL)


class TheHealthStrip(unittest.TestCase):
    """The month holding the two-year line is counted again when the window moves."""

    def setUp(self):
        import test_server as TS
        from block_store import BlockStore
        server = self.server = TS.server
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        line = now - timedelta(days=ss.API_RETENTION_DAYS)
        line = line.replace(minute=0 if line.minute < 30 else 30, second=0, microsecond=0)
        self.edge, self.mid = line.isoformat(), (now - timedelta(days=400)).isoformat()[:19]
        self.edge_m, self.mid_m = self.edge[:7], self.mid[:7]
        st = self.st = BlockStore(":memory:")
        st._conn.execute("INSERT INTO config_periods (id, effective_from, billing_day, block_minutes, "
                         "timezone) VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
        for bs in (self.edge, self.mid):
            st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                             "imp_kwh) VALUES (?, ?, 'electricity_main', 1, 0.5)", (bs, bs))
        st._conn.commit()
        server._health_cache.clear()
        server._health_cache.update(key=None, views=None, missing=None, months=None, per={})
        import tempfile
        tmp = tempfile.mkdtemp()

        def states(store, lo, hi):
            rows = store._conn.execute("SELECT block_start FROM blocks WHERE block_start >= ? "
                                       "AND block_start < ?", (lo, hi)).fetchall()
            return {r[0]: {"bill": "applied", "flags": []} for r in rows}
        eng = sys.modules["engine"]
        self.p = [mock.patch.object(server, "_settlement_states", states),
                  mock.patch.object(eng, "bill_split_held", lambda s: (set(), set()), create=True),
                  mock.patch.object(eng, "bill_no_cost", lambda s: set(), create=True),
                  mock.patch.object(eng, "_import_is_smb_capped", lambda: False, create=True),
                  mock.patch.object(server, "_HEALTH_PASS_PAUSE_S", 0),
                  mock.patch.object(server, "_health_cache_path",
                                    lambda: os.path.join(tmp, "health_counts.json"))]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_the_month_holding_the_line_turns_with_the_window(self):
        import time as _t
        if self.edge_m == self.mid_m:
            self.skipTest("the two months coincide")
        self.server._health_views(self.st, ["bill"], self.edge, "UTC")
        later = _t.time() + self.server._HEALTH_TTL_S
        with mock.patch.object(_t, "time", lambda: later):
            self.server._health_views(self.st, ["bill"], self.edge, "UTC")
        self.assertIn(self.edge_m, self.server._health_cache["counted"])
        self.assertNotIn(self.mid_m, self.server._health_cache["counted"])

    def test_the_version_moved(self):
        self.assertGreaterEqual(self.server._HEALTH_FILE_V, 7)


if __name__ == "__main__":
    unittest.main()
