"""
One reference for the 4.5.23 hold-back — bill and split paths design §2, consolidation PR 4
(16 Oct 2026).

`_bill_drops_ev(emt_ev, bill_ev)` holds back an answer with no EV where the car is known to have
charged. "Known" was read three ways: EMT's EV segments (the live pass, the drain), the cached
bill's EV (the VAT resync), the larger of the two (the re-fetch). Now `emt_ev_reference` — the
larger — for all four, through `emt_ev_at`. Where nothing is cached (the live pass, the drain) it is
the segments, as before. (The VAT resync was the fourth, until engine step 2d removed it, 23 Oct
2026.)

Discriminating (fail on the parent), except the guards named.
"""
import asyncio
import ast
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import engine                                                    # noqa: E402
from block_store import BlockStore                               # noqa: E402


class TheReference(unittest.TestCase):

    def test_the_larger_of_emts_split_and_a_cached_bill(self):
        self.assertEqual(engine.emt_ev_reference(3.0, None), 3.0)
        self.assertEqual(engine.emt_ev_reference(None, 2.5), 2.5)
        self.assertEqual(engine.emt_ev_reference(1.0, 3.4), 3.4)
        self.assertEqual(engine.emt_ev_reference(None, None), 0.0)

    def test_the_store_reads_the_latest_cached_bills_ev(self):
        st = BlockStore(":memory:")
        st.upsert_measured_cost("S", mpan="A", cost_incl=0.1, cost_excl=0.1, label="OFF_PEAK",
                                kwh=3.0, fetched_at="2026-10-01T00:00:00")
        st.upsert_measured_breakdown("S", mpan="A", home_kwh=1.0, home_rate=0.05, ev_kwh=2.0,
                                     ev_rate=0.05)
        st.upsert_measured_cost("S", mpan="B", cost_incl=0.1, cost_excl=0.1, label="OFF_PEAK",
                                kwh=3.0, fetched_at="2026-10-02T00:00:00")   # later, no breakdown
        st.upsert_measured_cost("T", mpan="A", cost_incl=0.1, cost_excl=0.1, label="OFF_PEAK",
                                kwh=3.0)
        self.assertEqual(st.get_bill_ev_kwh(["S", "T", "U"]), {})       # latest states none
        st.upsert_measured_breakdown("T", mpan="A", home_kwh=1.0, home_rate=0.05, ev_kwh=0.0,
                                     ev_rate=None)
        self.assertEqual(st.get_bill_ev_kwh(["T"]), {"T": 0.0})



class EveryPathUsesIt(unittest.TestCase):

    SRC = open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read()

    def _fn(self, name):
        tree = ast.parse(self.SRC)
        return next(ast.get_source_segment(self.SRC, n) for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)

    def test_the_three_paths(self):
        """Four until the VAT resync went (engine step 2d, 23 Oct 2026)."""
        for name in ("measure_settled_dispatched_blocks", "run_measured_history_drain"):
            self.assertIn("emt_ev_at(store,", self._fn(name), name)
        self.assertIn("emt_ev_reference(", self._fn("_refetch_diff"))
        for name in ("measure_settled_dispatched_blocks", "run_measured_history_drain",
                     "_refetch_diff"):
            body = self._fn(name)
            self.assertNotIn("get_ev_split_kwh(", body, name)
            self.assertNotIn('get_measured_breakdown(slot) or {}).get("ev_kwh")', body, name)


if __name__ == "__main__":
    unittest.main()
