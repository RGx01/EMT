"""
/api/periods?include=carbon — the Insights trend strip's per-period net carbon (C step 2,
1 Oct 2026), replacing /api/insights/periods.

  * ONE day-rollup pass over the span, folded per billing period (the old route ran a query
    per period), the main meter's carbon.
  * `has_carbon` from a count of blocks carrying carbon (`get_day_rollups.carbon_blocks`):
    SUM(carbon_g) is 0 both for a real net zero and for no intensity data at all.
  * Cut at LOCAL boundaries. /api/insights/periods called `.astimezone()` on naive local
    datetimes, which reads them in the HOST's timezone: on a Pi whose clock is UTC a BST
    billing boundary landed an hour late. On a production-like database run with TZ=UTC it
    mis-cut 28 of 40 periods, by up to 832 g; with the host on UK time it agreed with this
    facet on all 40 (within 0.33 g, the rollups' 1 dp).

New-facet tests: they fail on the parent tree (include=carbon is a 400 there).
"""
import importlib.util
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "web"))

from block_store import BlockStore              # noqa: E402

TZ = "Europe/London"
CFG = {"meters": {"electricity_main": {"meta": {"timezone": TZ, "currency_symbol": "£",
                                                "billing_day": 3, "block_minutes": 30}}}}


def _store():
    """Billing day 3. 1-2 Sep: no carbon data. 2 Sep 23:30Z (= 00:30 BST on 3 Sep, the
    new period's first hour): 100 g. 3-4 Sep: +50 g import, -50 g export each block —
    a real net zero over the period apart from that first block."""
    st = BlockStore(tempfile.mktemp(suffix=".db"))
    st._conn.execute("PRAGMA foreign_keys=OFF")
    st._conn.execute("INSERT INTO config_periods (id, effective_from, billing_day, block_minutes, "
                     "timezone) VALUES (1, '2026-01-01T00:00:00', 3, 30, ?)", (TZ,))
    st._conn.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter) "
                     "VALUES (1, 'electricity_main', 0)")

    def blk(bs, cg):
        be = (datetime.fromisoformat(bs) + timedelta(minutes=30)).isoformat()
        st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
            "imp_cost, carbon_g, is_provisional, interpolated) VALUES (?,?,?,1,1.0,0.25,?,0,0)",
            (bs, be, "electricity_main", cg))
    t = datetime(2026, 8, 31, 23, 0)
    while t < datetime(2026, 9, 2, 23, 0):            # 1-2 Sep local: no carbon
        blk(t.isoformat(), None); t += timedelta(minutes=30)
    blk("2026-09-02T23:00:00", None)                 # 00:00 BST 3 Sep
    blk("2026-09-02T23:30:00", 100.0)                # 00:30 BST 3 Sep — the new period's
    t, sign = datetime(2026, 9, 3, 0, 0), 1
    while t < datetime(2026, 9, 4, 23, 0):
        blk(t.isoformat(), 50.0 * sign); sign = -sign; t += timedelta(minutes=30)
    st._conn.commit()
    return st


def _server():
    spec = importlib.util.spec_from_file_location("server_pc", os.path.join(_ROOT, "web", "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    return srv


class ThePerPeriodCarbon(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.srv, cls.st = _server(), _store()

    def _get(self, q):
        with mock.patch.object(self.srv.store_access, "store", lambda: self.st), \
             mock.patch.object(self.srv, "load_config", lambda: CFG), \
             mock.patch.object(self.srv, "_config_timezone", lambda: TZ):
            return self.srv.app.test_client().get("/api/periods?" + q)

    def _periods(self):
        return {p["start"]: p for p in self._get("kind=billing&include=carbon").get_json()["periods"]}

    def test_no_intensity_data_is_not_net_zero(self):
        aug = self._periods()["2026-08-03"]
        self.assertFalse(aug["has_carbon"])

    def test_a_period_is_cut_at_local_midnight(self):
        """00:30 BST on 3 Sep (23:30Z on the 2nd) is the September period's."""
        sep = self._periods()["2026-09-03"]
        self.assertTrue(sep["has_carbon"])
        self.assertAlmostEqual(sep["carbon_g_net"], 100.0, places=1)

    def test_the_host_clock_does_not_move_it(self):
        """The old route's bug: naive local times read in the host's timezone."""
        saved = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "UTC"; time.tzset()
            sep = self._periods()["2026-09-03"]
        finally:
            if saved is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = saved
            time.tzset()
        self.assertAlmostEqual(sep["carbon_g_net"], 100.0, places=1)

    def test_still_no_aggregates_unless_asked(self):
        """GUARD: the picker's default response carries none (test_periods_endpoint)."""
        for p in self._get("kind=billing").get_json()["periods"]:
            self.assertNotIn("carbon_g_net", p)

    def test_an_unknown_facet_is_refused(self):
        """GUARD: passes on the parent tree too (where `carbon` itself was unknown)."""
        self.assertEqual(self._get("kind=billing&include=carbon,fish").status_code, 400)


if __name__ == "__main__":
    unittest.main()
