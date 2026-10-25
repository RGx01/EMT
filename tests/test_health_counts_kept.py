"""
The health strip's month counts are kept across restarts, and no longer all counted again hourly
(21 Oct 2026, the user: the prod box, a thin client, counted the whole history in 22 s at a restart).

- **Kept in a file beside the database** (`health_counts.json`), each month with the digest it was
  counted under, so a restart counts only what moved. The digest holds across processes: `hash()` of
  a str does not (PYTHONHASHSEED).
- **No hourly full count.** Its job — noticing what the marks did not see — moves into the marks: the
  VAT calendar and the agreements join the shared context. Agreements are known only once discovery
  has run, so counts made before it are counted again once it has, and a restart before discovery
  keeps what it has.
- **No warm-up after start:** with the counts kept, there is little left for it to do.

Discriminating: every test here fails on the parent.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import test_server as TS                       # noqa: E402  (stubs + make_client)
from block_store import BlockStore             # noqa: E402

OLD, NEW = "2025-03-10T08:00:00", "2026-10-21T08:00:00"


def _st():
    st = BlockStore(":memory:")
    st._conn.execute("INSERT INTO config_periods (id, effective_from, billing_day, block_minutes, "
                     "timezone) VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
    for bs in (OLD, NEW):
        st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                         "imp_kwh) VALUES (?, ?, 'electricity_main', 1, 0.5)", (bs, bs))
    st._conn.commit()
    return st


class _Base(unittest.TestCase):

    def setUp(self):
        self.server = server = TS.server
        self.st = _st()
        self.tmp = tempfile.mkdtemp()
        self.eng = eng = sys.modules["engine"]
        self.disc = None

        def states(store, lo, hi):
            rows = store._conn.execute("SELECT block_start FROM blocks WHERE block_start >= ? "
                                       "AND block_start < ?", (lo, hi)).fetchall()
            return {r[0]: {"bill": "applied", "flags": []} for r in rows}
        self.p = [mock.patch.object(server, "_settlement_states", states),
                  mock.patch.object(eng, "bill_split_held", lambda s: (set(), set()), create=True),
                  mock.patch.object(eng, "bill_no_cost", lambda s: set(), create=True),
                  mock.patch.object(eng, "_import_is_smb_capped", lambda: False, create=True),
                  mock.patch.object(server, "_health_cache_path",
                                    lambda: os.path.join(self.tmp, "health_counts.json"))]
        for x in self.p:
            x.start()
        self.restart()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def restart(self):
        """A new process: the in-memory cache gone, the file kept."""
        self.server._health_cache.clear()
        self.server._health_cache.update(key=None, views=None, missing=None, months=None)

    def count(self):
        with mock.patch.object(self.eng, "_kraken_discovery", self.disc, create=True):
            return self.server._health_views(self.st, ["bill"], OLD, "UTC")


class AcrossARestart(_Base):

    def test_only_the_young_month_is_counted_again(self):
        self.count()
        self.restart()
        self.count()
        self.assertEqual(self.server._health_cache["counted"], [])   # same window: nothing

    def test_the_file_holds_the_counts(self):
        self.count()
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "health_counts.json")))
        self.restart()
        totals, months = self.count()
        self.assertEqual(totals, {"bill": {"applied": 2}})
        self.assertEqual(sorted(months), ["2025-03", "2026-10"])


class TheHourlyCount(_Base):

    def test_is_gone(self):
        import time as _t
        self.count()
        later = _t.time() + 3 * 3600
        with mock.patch.object(_t, "time", lambda: later):
            self.count()
        self.assertNotIn("2025-03", self.server._health_cache["counted"])


class TheAgreements(_Base):

    AGR = {"import": {"agreements": [{"tariff_code": "E-1R-X-A", "valid_from": "2024-01-01",
                                      "valid_to": None}]}}

    def test_found_after_a_count_count_again(self):
        self.count()                                   # before discovery
        self.disc = self.AGR
        self.count()
        self.assertIn("2025-03", self.server._health_cache["counted"])

    def test_not_yet_found_after_a_restart_keeps_what_was_counted(self):
        self.disc = self.AGR
        self.count()
        self.restart(); self.disc = None
        self.count()
        self.assertEqual(self.server._health_cache["counted"], [])

    def test_a_changed_agreement_counts_again(self):
        self.disc = self.AGR
        self.count()
        self.disc = {"import": {"agreements": [{"tariff_code": "E-1R-INTELLI-Y-A",
                                                "valid_from": "2025-06-01", "valid_to": None}]}}
        self.count()
        self.assertIn("2025-03", self.server._health_cache["counted"])


class TheDiscoveryShape(_Base):

    def test_keys_that_are_not_channels_are_ignored(self):
        """Discovery holds more than the channels, and not all of it dicts: the digest read every
        key and raised, which took the health strip down (staging, 21 Oct 2026). Fails on the
        parent."""
        self.disc = {"account": "A-0000", "devices": [{"type": "EV"}],
                     "import": {"agreements": [{"tariff_code": "E-1R-X-A", "valid_from": "2024-01-01",
                                                "valid_to": None}]}, "export": None}
        totals, _ = self.count()
        self.assertEqual(totals, {"bill": {"applied": 2}})
        self.assertIsNotNone(self.server._health_cache["per"]["2025-03"]["mark"]["agr"])   # import's read


class NoWarmUp(unittest.TestCase):

    def test_start_does_not_count(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "web", "server.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("_warm_health", src)


if __name__ == "__main__":
    unittest.main()
