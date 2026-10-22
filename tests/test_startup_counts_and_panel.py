"""
After the staging rebuild with #262 (22 Oct 2026, the user's three).

1. **The fill opened from the heatmap showed Historical Import's choosers** (API / CSV, Fill gaps /
   Date range / Bills) above it. The menu chose the source and the span, so the chooser card goes.
2. **The Export bill's panel gave the import rate alone.** The panel shows import and export side by
   side, whichever view opened it: each its kWh, cost and rate. The route sends the export always.
3. **The health count at start-up.** Today's whole-history count was #262's version bump, once. But
   the log showed the pattern behind the slow days: a refill rebuilding the devices wrote across
   18 months for minutes, and every page poll found the data moved and counted again — three
   requests in the count at once, the engine loop held up to 11.4 s.
   - A request that waited for a count takes its answer.
   - While the data keeps moving, the counts are made again at most every 15 s.
   - The bill drain's slots (held, kept, given up, no cost, asked) are each month's own: a drain
     giving up one half-hour had counted the whole history again.
   - A count goes three months a pass, with a pause between, giving the engine loop back the
     interpreter.

Discriminating: every test here fails on the parent.
"""
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import test_server as TS                       # noqa: E402  (stubs + make_client)
from test_health_counts_kept import _Base, OLD  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _t(name):
    with open(os.path.join(ROOT, "web", "templates", name), encoding="utf-8") as f:
        return f.read()


class TheAnswer(unittest.TestCase):
    """_health_answer: who counts, and how often."""

    def setUp(self):
        self.server = s = TS.server
        self.calls = []
        s._health_cache.clear()
        s._health_cache.update(key=("old", 0, ("bill",)), views={"bill": {"applied": 1}},
                               missing={}, months={}, at=time.time())
        self.p = [mock.patch.object(s, "_blocks_data_version", lambda st: "new"),
                  mock.patch.object(s, "_health_views",
                                    lambda *a: (self.calls.append(1), ({"bill": {"applied": 2}}, {}))[1]),
                  mock.patch.object(s, "_missing_half_hours", lambda st, tz: {"half_hours": 0}),
                  mock.patch.object(s, "_config_timezone", lambda: "UTC")]
        for x in self.p:
            x.start()
        self.st = mock.Mock(block_span=lambda: (OLD, OLD))

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_within_the_interval_the_last_counts_stand(self):
        d = self.server._health_answer(self.st, ["bill"])
        self.assertEqual((self.calls, d["views"]), ([], {"bill": {"applied": 1}}))

    def test_after_it_the_data_is_counted_again(self):
        """The other side: a moved token still counts, once the interval has passed."""
        self.server._health_cache["at"] = time.time() - self.server._HEALTH_MIN_S - 1
        d = self.server._health_answer(self.st, ["bill"])
        self.assertEqual((self.calls, d["views"]), ([1], {"bill": {"applied": 2}}))

    def test_a_request_that_waited_takes_the_answer(self):
        self.server._health_cache["at"] = 0.0          # long ago: alone, it would count
        got = {}
        self.server._health_lock.acquire()             # another request is counting
        th = threading.Thread(target=lambda: got.update(self.server._health_answer(self.st, ["bill"])))
        th.start(); time.sleep(0.05)
        self.server._health_lock.release(); th.join(2)
        self.assertEqual((self.calls, got["views"]), ([], {"bill": {"applied": 1}}))


class TheDrainsSlots(_Base):
    """A slot the drain gives up counts its own month again, not the history."""

    def test_only_its_month(self):
        gone = set()
        with mock.patch.object(self.eng, "bill_no_cost", lambda s: (set(gone), set(), set()), create=True):
            self.count()
            gone.add(OLD)                              # 2025-03's half-hour given up
            self.count()
        self.assertEqual(self.server._health_cache["counted"], ["2025-03"])


class ThePasses(_Base):

    def test_three_months_a_pass(self):
        spans = []
        real = self.server._settlement_states

        def states(store, lo, hi):
            spans.append((lo, hi)); return real(store, lo, hi)
        with mock.patch.object(self.server, "_settlement_states", states), \
             mock.patch.object(self.server, "_HEALTH_PASS_PAUSE_S", 0):
            self.count()
        # 2025-03 .. 2026-10 is 20 months: seven passes, the first 2025-03 .. 2025-06
        self.assertEqual(len(spans), 7)
        self.assertEqual((spans[0][0][:7], spans[0][1][:7]), ("2025-03", "2025-06"))


class ThePanel(unittest.TestCase):

    def test_the_route_sends_the_export_always(self):
        with open(os.path.join(ROOT, "web", "server.py"), encoding="utf-8") as f:
            src = f.read()
        fn = src[src.index("def api_block_settlement"):]
        fn = fn[:fn.index("\n\n\n")]
        self.assertIn('"export": {"kwh": _r(d["exp_kwh"]), "dcc": _r(d["exp_kwh_api"]),', fn)
        self.assertIn('"cost": _r(d["exp_cost"]), "rate": _r(d["exp_rate"])},', fn)

    def test_both_sections(self):
        hm = _t("heatmap_module.html")
        fn = hm[hm.index("function renderPanel(d, label, more) {"):]
        fn = fn[:fn.index("\n  }\n  // a missing half-hour")]
        self.assertLess(fn.index("head('Import');"), fn.index("head('Export');"))
        self.assertIn("row('rate', pence(x.rate), '');", fn)
        self.assertIn("if (x && (METRICS.export || x.kwh !== null || x.dcc !== null)) {", fn)


class TheFillModal(unittest.TestCase):

    def test_hides_the_choosers(self):
        hi = _t("historical_import.html")
        fn = hi[hi.index("function bfOpenGapFromUrl() {"):]
        fn = fn[:fn.index("\n}\n")]
        self.assertIn("document.getElementById('bf-card')", fn)
        self.assertEqual(fn.count("hideChoosers();"), 2)         # the bills and the CSV


if __name__ == "__main__":
    unittest.main()
