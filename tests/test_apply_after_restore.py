"""
The bill apply after a restore, and why a bill does not apply (18 Oct 2026, the Pi).

1. A restore rebuilds the store without restarting the process, so the dispatch tick's clocks
   carried on: the bill apply, the history drain and the reconcile — the tick's hourly branch —
   waited up to an hour after a restore, and a restored v4 database's cached bills sat unapplied.
   engine_startup (which a restore, a config save and an HA reconnect run) now clears the clocks,
   so the next tick runs them.
2. v4's import schedule stopped covering new days after 14 Oct, and its apply refused every bill
   from then on with nothing in the log. apply_measured_to_block now says why it refused
   (`_APPLY_REFUSAL`), and apply_measured_settled logs one line a pass per reason, with a count
   and the first half-hour.

All discriminating (fail on the parent) except test_an_applied_bill_logs_no_refusal, a guard —
it fails on the parent only by the new `refused` key.
"""
import asyncio
import logging
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engine                                    # noqa: E402
from block_store import BlockStore               # noqa: E402

MPAN = "2000000000001"
S = "2026-10-15T00:00:00"


class _Sched:
    def __init__(self, bounds): self.bounds = bounds
    def is_empty(self): return False
    def day_rate_bounds(self, ts): return self.bounds
    def resolve(self, ts): return 5.2314
    def is_off_peak(self, ts): return True


class TheClocks(unittest.TestCase):

    def test_a_startup_clears_the_ticks_clocks(self):
        save = (engine._last_reconcile, engine._last_dispatch_capture)
        engine._last_reconcile = engine._last_dispatch_capture = datetime.now(timezone.utc)

        async def noop(ha):
            return None
        try:
            with mock.patch.object(engine, "_engine_startup_impl", noop):
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(engine.engine_startup(None))
                finally:
                    loop.close()
            self.assertIsNone(engine._last_reconcile)
            self.assertIsNone(engine._last_dispatch_capture)
        finally:
            engine._last_reconcile, engine._last_dispatch_capture = save


class TheRefusals(unittest.TestCase):

    def setUp(self):
        self._save = (engine._store, engine._kraken_rate_schedules, engine._kraken_discovery,
                      engine._kraken_current_agreement_from)
        self.st = BlockStore(":memory:")
        engine._store = self.st
        engine._kraken_discovery = {"import": {"mpan": MPAN, "tariff_code": "E-1R-IOG-SMB-FIX-12M-H"}}
        engine._kraken_current_agreement_from = "2026-08-25T23:00:00"
        self.st._conn.execute("INSERT OR IGNORE INTO config_periods (id, effective_from, billing_day, "
                              "block_minutes, timezone) VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
        self.st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                              "imp_kwh, imp_kwh_api, imp_rate, imp_cost, rate_source, rate_corrected) "
                              "VALUES (?,?,?,1,3.0,3.0,0.052314,0.156942,'schedule',0)",
                              (S, S, "electricity_main"))
        self.st.upsert_measured_cost(S, mpan=MPAN, cost_incl=0.156942, cost_excl=0.156942,
                                     label="OFF_PEAK", kwh=3.0)
        # IOG-SMB: the band is the buckets' (engine step 2b)
        self.st.upsert_measured_breakdown(S, mpan=MPAN, home_kwh=3.0, home_rate=0.052314,
                                          ev_kwh=0.0, ev_rate=None)
        self.st._conn.commit()

    def tearDown(self):
        (engine._store, engine._kraken_rate_schedules, engine._kraken_discovery,
         engine._kraken_current_agreement_from) = self._save

    def test_a_day_the_schedule_does_not_cover_is_said(self):
        engine._kraken_rate_schedules = {"import": _Sched((None, None))}
        with self.assertLogs("engine", level="WARNING") as cm:
            r = engine.apply_measured_settled()
        self.assertEqual(r["refused"], {engine._REFUSE_NO_RATE: 1})
        self.assertTrue(any("1 cached bill(s) not applied" in m and engine._REFUSE_NO_RATE in m
                            and S in m for m in cm.output))

    def test_the_block_says_why_it_refused(self):
        engine._kraken_rate_schedules = {"import": _Sched((None, None))}
        self.assertFalse(engine.apply_measured_to_block(S, cost_incl=0.156942, cost_excl=0.156942,
                                                        label="OFF_PEAK"))
        self.assertEqual(engine._APPLY_REFUSAL, engine._REFUSE_NO_RATE)

    def test_an_applied_bill_logs_no_refusal(self):
        """GUARD."""
        engine._kraken_rate_schedules = {"import": _Sched((5.2314, 30.7707))}
        logging.getLogger("engine").setLevel(logging.INFO)
        r = engine.apply_measured_settled()
        self.assertEqual((r["applied"], r["refused"]), (1, {}))


if __name__ == "__main__":
    unittest.main()
