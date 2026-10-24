"""
Engine step 3a (24 Oct 2026): the ask dry run — what the one selector (EMT-v5_engine_step3_ask_queue
design §3) would ask Octopus for, beside what the live pass and the drain ask today, and why each
half-hour differs. Read-only; no Octopus call.

Discriminating: every test here fails on the parent (the report is new).
"""
import asyncio
import json
import os
import sys
import tempfile
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


class _Base(unittest.TestCase):

    def setUp(self):
        self.st = BlockStore(":memory:")
        self.st._conn.execute("INSERT INTO config_periods (id, effective_from, billing_day, "
                              "block_minutes, timezone) VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
        self.st._conn.commit()
        self.p = [mock.patch.object(engine, "_store", self.st),
                  mock.patch.object(engine, "_reader", lambda: self.st),
                  mock.patch.object(engine, "_kraken_discovery", DISC),
                  mock.patch.object(engine, "tariff_kind_at", lambda bs: "intelligent"),
                  mock.patch.object(engine, "split_at", lambda bs: False)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def _blk(self, bs, *, source=None, api=True, corrected=0, rate_source=None):
        self.st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
            "imp_kwh_api, rate_corrected, rate_source, source) VALUES (?,?,?,1,1.0,?,?,?,?)",
            (bs, bs, "electricity_main", 1.0 if api else None, corrected, rate_source, source))
        self.st._conn.commit()

    def _run(self):
        out = tempfile.mkdtemp()
        s = asyncio.run(engine.run_ask_dry_run(out))
        rows = open(os.path.join(out, s["csv"])).read().strip().splitlines()[1:]
        return s, rows


class TheReport(_Base):

    def test_a_live_half_hour_with_no_bill_is_asked_by_both(self):
        self._blk("2026-09-01T12:00:00")
        s, rows = self._run()
        self.assertEqual((s["counts"], s["queue"]["bill"], rows), ({"both": 1}, 1, []))

    def test_imported_history_with_no_bill_is_the_queue_s_only(self):
        """Decided (b): off IOG-SMB the drain asks imported history only where the import recorded
        no cost; the queue asks any with no bill."""
        self._blk("2025-06-01T12:00:00", source="imported_api", api=False)
        s, rows = self._run()
        self.assertEqual(s["counts"],
                         {"queue only · imported history with no bill — asked like any other "
                          "(decided (b))": 1})
        self.assertIn("2025-06-01T12:00:00,bill,,", rows[0])

    def test_a_correction_the_drain_misses_is_today_s_only(self):
        self._blk("2026-09-01T13:00:00", corrected=1)
        s, _ = self._run()
        self.assertEqual(s["counts"],
                         {"today only · corrected (the drain tests rate_source, not rate_corrected)": 1})

    def test_a_given_up_half_hour_is_asked_by_neither(self):
        """The records still hold: the queue keeps today's give-up."""
        self._blk("2026-09-01T14:00:00")
        self.st.set_kraken_state(engine._BILL_ASK_KEY, json.dumps(
            {"2026-09-01T14:00:00": {"first": "2026-09-02T00:00:00", "last": "2026-09-02T00:00:00"}}))
        s, _ = self._run()
        self.assertEqual(s["counts"], {})

    def test_it_writes_a_summary(self):
        self._blk("2026-09-01T12:00:00")
        out = tempfile.mkdtemp()
        asyncio.run(engine.run_ask_dry_run(out))
        self.assertTrue(any(n.endswith(".json") for n in os.listdir(out)))
        self.assertEqual(engine.ask_dry_run_status()["status"], "done")


class TheConsidered(_Base):
    """Staging's first run (24 Oct 2026) read all zeros, which a missing agreement would read too:
    the summary says how many half-hours it read and why each was left out."""

    def test_every_half_hour_read_has_its_reason(self):
        from datetime import datetime, timedelta, timezone
        recent = (datetime.now(timezone.utc).replace(tzinfo=None)
                  - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0).isoformat()
        self._blk("2026-09-01T12:00:00")                         # asked (bill)
        self._blk("2026-09-01T12:30:00", api=False)              # kWh not settled
        self._blk("2026-09-01T13:00:00", corrected=1)            # corrected
        self._blk("2026-09-01T13:30:00")                         # bill cached
        self.st.upsert_measured_cost("2026-09-01T13:30:00", mpan=MPAN, cost_incl=0.1,
                                     label="OFF_PEAK", kwh=1.0)
        self._blk(recent)                                        # under the 6-hour gate
        s, _ = self._run()
        c = s["considered"]
        self.assertEqual((c["half_hours"], c["intelligent_agreements"]), (5, 1))
        self.assertEqual(c["by_reason"], {"asked (bill)": 1, "bill cached": 1, "corrected": 1,
                                          "kWh not settled": 1, "under the 6-hour gate": 1})

    def test_no_agreement_says_so(self):
        """A zero with no Intelligent agreement reads differently from a caught-up zero."""
        self._blk("2026-09-01T12:00:00")
        with mock.patch.object(engine, "tariff_kind_at", lambda bs: "published"), \
             mock.patch.object(engine, "_kraken_discovery", {"import": {"mpan": MPAN}}):
            s, _ = self._run()
        self.assertEqual(s["considered"]["intelligent_agreements"], 0)
        self.assertEqual(s["considered"]["by_reason"], {"not on an Intelligent agreement": 1})


class TheEstimate(unittest.TestCase):

    def test_a_cost_day_is_one_query_and_breakdowns_go_in_runs(self):
        cost = {"2025-03-06T18:00:00": "bill", "2025-03-06T19:00:00": "bill",
                "2025-03-07T19:00:00": "bill"}
        bd = {"2026-09-01T%02d:%02d:00" % (h, m): "bill" for h in range(0, 8) for m in (0, 30)}
        with mock.patch.object(engine, "bill_kind_at",
                               lambda s: "breakdown" if s.startswith("2026") else "cost"):
            e = engine._ask_query_estimate({**cost, **bd})
        self.assertEqual(e, {"cost_days": 2, "breakdown_runs": 2, "queries": 4})


if __name__ == "__main__":
    unittest.main()
