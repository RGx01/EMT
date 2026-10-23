"""
An import's cached bills reach its blocks straight after it, a page at a time (23 Oct 2026).

On staging the re-import's 11,785 bills waited 52 minutes for the hourly pass (`_tick_dispatch_capture`
→ `apply_measured_settled`), the half-hours red meanwhile, and that one pass then held the engine
loop 5.6 s. `_after_import_settle` now applies them after the history sweep through
`_apply_bills_paced`: pages of `_APPLY_PAGE`, oldest first, a pause between, a cursor so a bill
the apply refuses cannot keep it going.

Discriminating: every test here fails on the parent (the paging and the call are new).
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import engine                                     # noqa: E402
from block_store import BlockStore                # noqa: E402

LO, HI = 5.493, 32.3092


class _Sched:
    def is_empty(self): return False
    def day_rate_bounds(self, ts): return (LO, HI)
    def resolve(self, ts): return HI


def _slot(i):
    return "2025-03-%02dT19:00:00" % (i + 1)


class ThePaging(unittest.TestCase):

    def setUp(self):
        self.st = BlockStore(":memory:")
        self.st._conn.execute("INSERT INTO config_periods (id, effective_from, billing_day, "
                              "block_minutes, timezone) VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
        for i in range(5):
            s = _slot(i)
            self.st._conn.execute(
                "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
                "imp_rate, imp_cost, rate_source, rate_corrected, source) "
                "VALUES (?,?,'electricity_main',1,1.0,?,?,'schedule',0,'imported_api')",
                (s, s, HI / 100, HI / 100))
            self.st.upsert_measured_cost(s, mpan="M", cost_incl=LO / 100, label="OFF_PEAK", kwh=1.0)
        self.st._conn.commit()
        self.p = [mock.patch.object(engine, "_store", self.st),
                  mock.patch.object(engine, "_kraken_rate_schedules", {"import": _Sched()}),
                  mock.patch.object(engine, "_kraken_discovery", {"import": {"mpan": "M"}}),
                  mock.patch.object(engine, "_measured_floor", lambda: "2020-01-01T00:00:00"),
                  mock.patch.object(engine, "tariff_kind_at", lambda bs: "intelligent"),
                  mock.patch.object(engine, "split_at", lambda bs: False),
                  mock.patch.object(engine, "_agreement_priced_ok", lambda bs: True),
                  mock.patch.object(engine, "_APPLY_PAGE", 2),
                  mock.patch.object(engine, "_APPLY_PAGE_PAUSE_S", 0)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def _settled(self):
        return self.st._conn.execute("SELECT COUNT(*) FROM blocks WHERE rate_source='settled'").fetchone()[0]

    def test_every_bill_lands_a_page_at_a_time(self):
        pages = []
        real = engine.apply_measured_settled
        with mock.patch.object(engine, "apply_measured_settled",
                               lambda **kw: pages.append(kw) or real(**kw)):
            n = asyncio.run(engine._apply_bills_paced())
        self.assertEqual((n, self._settled()), (5, 5))
        self.assertEqual([p["limit"] for p in pages], [2, 2, 2])
        self.assertEqual(pages[1]["after"], _slot(1))

    def test_a_refused_bill_does_not_keep_it_going(self):
        """A day with no rate refuses every bill: the cursor still moves on, and it ends."""
        pages = []
        real = engine.apply_measured_settled
        with mock.patch.object(_Sched, "day_rate_bounds", lambda self, ts: (None, None)), \
             mock.patch.object(engine, "apply_measured_settled",
                               lambda **kw: pages.append(kw) or real(**kw)):
            n = asyncio.run(engine._apply_bills_paced())
        self.assertEqual((n, self._settled(), len(pages)), (0, 0, 3))

    def test_the_store_pages_oldest_first(self):
        rows = self.st.get_measured_costs_to_apply("M", "2020-01-01T00:00:00", after=_slot(0), limit=2)
        self.assertEqual([r["block_start"] for r in rows], [_slot(1), _slot(2)])


class TheImportFinish(unittest.TestCase):

    def test_after_an_import_the_bills_are_applied_before_the_drain(self):
        calls = []

        async def _sweep(force=False):
            calls.append("sweep")

        async def _apply():
            calls.append("apply")
            return 0

        with mock.patch.object(engine, "_run_historical_reprice_sweep", _sweep), \
             mock.patch.object(engine, "_apply_bills_paced", _apply), \
             mock.patch.object(engine, "_maybe_drain_measured_history", lambda: calls.append("drain")), \
             mock.patch.object(engine, "_store", mock.MagicMock(get_meta=lambda k, d=None: {"done": True})), \
             mock.patch.object(engine._coord, "running", lambda name, key=None: False):
            asyncio.run(engine._after_import_settle())
        self.assertEqual(calls, ["sweep", "apply", "drain"])


if __name__ == "__main__":
    unittest.main()
