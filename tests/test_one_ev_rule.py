"""
One EV rule's pieces — bill and split paths design §3.3, consolidation PR 5 (16 Oct 2026).

The dispatch's EV for a half-hour was computed twice (`_iog_slot_ev_kwh` through `_reader()` on the
snapped slot; `completed_dispatch_kwh` + min in the settle's rule on the exact slot), and "are the
capped schedules loaded?" written four times. Now `dispatch_ev_kwh` and `_capped_schedules`.
The rule change itself (the bill's split at the final stage) is tested in test_settled_ev_split.py.

Discriminating: each fails on the parent.
"""
import os
import re
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import engine                                   # noqa: E402
from block_store import BlockStore              # noqa: E402

SRC = open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read()


class OneDispatchEv(unittest.TestCase):

    def test_it_is_the_completed_dispatch_clipped_to_the_meter(self):
        st = BlockStore(":memory:")
        st.record_dispatch_history("2026-09-01T02:00:00", "completed", provider="Myenergi",
                                   source="unknown", energy_kwh=-3.46)
        self.assertAlmostEqual(engine.dispatch_ev_kwh("2026-09-01T02:00:00", 5.0, st), 3.46)
        self.assertAlmostEqual(engine.dispatch_ev_kwh("2026-09-01T02:00:00", 0.809, st), 0.809)
        self.assertEqual(engine.dispatch_ev_kwh("2026-09-01T02:30:00", 5.0, st), 0.0)

    def test_settle_uses_it(self):
        """The seam (`_apply_iog_split`) used it too, until engine step 4e removed the seam."""
        self.assertNotIn("def _iog_slot_ev_kwh", SRC)
        settle = SRC[SRC.index("def settle_facts"):SRC.index("def settle_parity_row")]
        self.assertIn("dispatch_ev_kwh(bs, kwh, store)", settle)
        self.assertNotIn("completed_dispatch_kwh(", settle)


class OneCappedTest(unittest.TestCase):

    def test_the_schedule_check_is_written_once(self):
        both = re.findall(r'"ev_device_off_peak" in _kraken_rate_schedules\s*\n?\s*and '
                          r'"ev_device_peak" in _kraken_rate_schedules', SRC)
        self.assertEqual(len(both), 1, "the capped-schedules test is written more than once")
        # the definition and settle_facts — AND the half-hour's own agreement. (The BL-34 relabel,
        # which asked only the agreement — `_capped_band = capped_at(bs)` — went with the
        # reconcile's own repricing, engine step 4c; the seam's, with the seam in 4e.)
        self.assertGreaterEqual(SRC.count("_capped_schedules()"), 2)
        self.assertEqual(SRC.count("capped_at(block_start) and _capped_schedules()"), 0)
        # settle_facts — the bill apply's mixed rebuild went with engine step 4b (the bill apply
        # through settle()); 4a had made it two
        self.assertEqual(SRC.count("capped_at(bs) and _capped_schedules()"), 1)


if __name__ == "__main__":
    unittest.main()
