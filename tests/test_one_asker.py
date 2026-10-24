"""
Engine step 3c (24 Oct 2026): the one asker (`run_measured_history_drain`, EMT-v5_engine_step3_ask_
queue_design.md §3) — every half-hour waiting for Octopus, newest first, a page at a time.

- under 25% of the allowance left it stops (decided (c)); the next pass carries on;
- an API error (a window that raised, after the client's retries) is not an unanswered ask: nothing
  is marked and the pass stops;
- newest first, in pages of `_ASK_PAGE_DAYS` local days;
- its progress is `ask_status()`, read by the banner (`/api/settlement/asking`);
- the live pass (`measure_settled_dispatched_blocks`) has left the hourly tick.

Discriminating: every test here fails on the parent, but the two GUARDs.
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_engine  # noqa: F401  — installs stubs, imports engine
import engine
from block_store import BlockStore

MPAN = "M1"
DISC = {"import": {"mpan": MPAN, "tariff_code": "E-1R-INTELLI-VAR-22-10-14-H",
                   "agreements": [{"tariff_code": "E-1R-INTELLI-VAR-22-10-14-H",
                                   "valid_from": "2025-01-01T00:00:00Z", "valid_to": None}]}}
DAYS = ["2026-09-%02dT12:00:00" % d for d in range(1, 8)]          # seven local days


class _Client:
    def __init__(self, *, remaining=1000, fail=False):
        self.remaining, self.fail, self.asked = remaining, fail, []
        self.last_fetch_failures = 0

    async def get_rate_limit(self):
        return {"remaining": self.remaining, "pointsLimit": 1000, "isBlocked": False}

    async def recover_measurement_costs(self, mpan, slots, **kw):
        self.asked.append(sorted(slots))
        self.last_fetch_failures = 1 if self.fail else 0
        if self.fail:
            return {}
        return {s: {"start": s, "kwh": 1.0, "cost_incl": 0.05493, "cost_excl": 0.0523,
                    "off_peak": True} for s in slots}

    async def recover_device_breakdown(self, mpan, slots, **kw):
        return {}


class _Base(unittest.TestCase):

    def setUp(self):
        self.st = BlockStore(":memory:")
        self.st._conn.execute("INSERT INTO config_periods (id, effective_from, billing_day, "
                              "block_minutes, timezone) VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
        for s in DAYS:
            self.st._conn.execute(
                "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
                "imp_kwh_api, imp_rate, imp_cost, rate_source) VALUES (?,?,?,1,1.0,1.0,0.3,0.3,'schedule')",
                (s, s, "electricity_main"))
        self.st._conn.commit()
        self.p = [mock.patch.object(engine, "_store", self.st),
                  mock.patch.object(engine, "_reader", lambda: self.st),
                  mock.patch.object(engine, "_kraken_discovery", DISC),
                  mock.patch.object(engine, "tariff_kind_at", lambda bs: "intelligent"),
                  mock.patch.object(engine, "split_at", lambda bs: False),
                  mock.patch.object(engine, "_MEASURED_HISTORY_DRAIN_PACE", 0),
                  mock.patch.object(engine, "_apply_bills_paced", mock.AsyncMock(return_value=0))]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def _run(self, client):
        with mock.patch.object(engine, "_kraken_client", client):
            return asyncio.run(engine.run_measured_history_drain())

    def _cached(self):
        return {r[0] for r in self.st._conn.execute("SELECT slot_start FROM measured_cost")}


class TheAsker(_Base):

    def test_newest_first_in_pages_of_three_days(self):
        c = _Client()
        r = self._run(c)
        self.assertEqual(c.asked, [sorted(DAYS[4:7]), sorted(DAYS[1:4]), DAYS[0:1]])
        self.assertEqual((r["asked"], r["stored"]), (7, 7))
        self.assertEqual(self._cached(), set(DAYS))

    def test_it_stops_under_a_quarter_of_the_allowance(self):
        c = _Client(remaining=249)
        r = self._run(c)
        self.assertEqual((c.asked, r["reason"]), ([], "allowance"))

    def test_at_a_quarter_it_asks(self):
        """GUARD of the boundary: 25% left is not under 25%."""
        self.assertEqual(self._run(_Client(remaining=250))["asked"], 7)

    def test_an_api_error_is_not_unanswered(self):
        c = _Client(fail=True)
        r = self._run(c)
        self.assertEqual((len(c.asked), r["reason"]), (1, "error"))   # the first page, then stop
        self.assertEqual(self.st.ask_record("bill"), {})                # nothing marked unanswered

    def test_no_answer_is_unanswered(self):
        """GUARD (the drain recorded it too): no answer, and no error, is an unanswered ask."""
        class _Empty(_Client):
            async def recover_measurement_costs(self, mpan, slots, **kw):
                self.asked.append(sorted(slots)); self.last_fetch_failures = 0; return {}
        self._run(_Empty())
        self.assertEqual(set(self.st.ask_record("bill")), set(DAYS))


class TheBanner(_Base):

    def test_its_progress_is_kept(self):
        self._run(_Client())
        st = engine.ask_status()
        self.assertEqual((st["status"], st["total"], st["done"], st["stored"]), ("idle", 7, 7, 7))

    def test_the_page_reads_it(self):
        base = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "web", "templates", "base.html"), encoding="utf-8").read()
        self.assertIn("fetch(apiUrl('/api/settlement/asking'))", base)
        self.assertIn("Asking Octopus for your bills", base)


class TheTick(unittest.TestCase):

    def test_the_live_pass_is_gone(self):
        import inspect
        self.assertFalse(hasattr(engine, "measure_settled_dispatched_blocks"))
        self.assertIn("_maybe_drain_measured_history()", inspect.getsource(engine._tick_dispatch_capture))


if __name__ == "__main__":
    unittest.main()
