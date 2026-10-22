"""
The health counts stand while a job rewrites history (22 Oct 2026, the user).

After #263, staging still held the engine loop 14.5 s: a device rebuild after a re-import wrote
across 33 months, and the count it set off ran beside it. Now:
- while an import, gap fill, device rebuild, pricing check, re-price, re-fetch, delete or restore
  runs, the last counts stand, the strip says they update when it finishes, and it asks again every
  30 s; the first request after the job counts. With no counts yet, they are made anyway;
- the states pass sleeps 1 ms every 500 half-hours, handing the interpreter to the engine loop.

Discriminating: every test here fails on the parent.
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import test_server as TS                       # noqa: E402  (stubs + make_client)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import settlement_state as ss                  # noqa: E402

OLD = "2025-03-10T08:00:00"


class TheFreeze(unittest.TestCase):

    def setUp(self):
        self.server = s = TS.server
        self.calls, self.jobs = [], set()
        s._health_cache.clear()
        s._health_cache.update(key=("old", 0, ("bill",)), views={"bill": {"applied": 1}},
                               missing={}, months={}, at=0.0)     # long ago: the interval is past
        import job_coordinator as jc
        self.p = [mock.patch.object(s, "_blocks_data_version", lambda st: "new"),
                  mock.patch.object(s, "_health_views",
                                    lambda *a: (self.calls.append(1), ({"bill": {"applied": 2}}, {}))[1]),
                  mock.patch.object(s, "_missing_half_hours", lambda st, tz: {"half_hours": 0}),
                  mock.patch.object(s, "_config_timezone", lambda: "UTC"),
                  mock.patch.object(jc, "running", lambda name, key=None: name in self.jobs)]
        for x in self.p:
            x.start()
        self.st = mock.Mock(block_span=lambda: (OLD, OLD))

    def tearDown(self):
        for x in self.p:
            x.stop()

    def _ask(self):
        return self.server._health_answer(self.st, ["bill"])

    def test_a_rebuild_keeps_the_last_counts(self):
        self.jobs.add("attribution")
        d = self._ask()
        self.assertEqual((self.calls, d["views"], d["updating"]), ([], {"bill": {"applied": 1}}, ["attribution"]))

    def test_after_it_the_first_request_counts(self):
        self.jobs.add("import"); self._ask()
        self.jobs.clear()
        d = self._ask()
        self.assertEqual((self.calls, d["views"], d["updating"]), ([1], {"bill": {"applied": 2}}, []))

    def test_with_no_counts_yet_they_are_made(self):
        self.jobs.add("import")
        self.server._health_cache["key"] = None
        d = self._ask()
        self.assertEqual((self.calls, d["updating"]), ([1], []))

    def test_the_drain_does_not_freeze(self):
        """The hourly bill drain and the live tick are not history rewrites."""
        self.jobs.update({"measured_drain", "live_tick", "iog_split_carve"})
        self.assertEqual(self._ask()["updating"], [])
        self.assertEqual(self.calls, [1])


class TheStrip(unittest.TestCase):

    def test_says_so_and_asks_again(self):
        with open(os.path.join(ROOT, "web", "templates", "data_management.html"), encoding="utf-8") as f:
            dm = f.read()
        fn = dm[dm.index("function loadHealth() {"):]
        fn = fn[:fn.index("\n}\n")]
        self.assertIn("if (d.updating && d.updating.length) {", fn)
        self.assertIn("loadHealth._t = setTimeout(loadHealth, 30000);", fn)
        self.assertIn("attribution: 'the device rebuild'", dm)


class TheYield(unittest.TestCase):

    def test_every_500_half_hours(self):
        facts = [{"block_start": "2025-03-10T%02d:%02d:00" % (i // 60 % 24, i % 60)} for i in range(1200)]
        slept = []
        with mock.patch.object(ss, "classify", lambda f, **k: {}), \
             mock.patch("time.sleep", lambda s: slept.append(s)):
            ss.classify_range(facts, yield_every=500)
        self.assertEqual(slept, [0.001, 0.001])

    def test_the_server_asks_for_it(self):
        with open(os.path.join(ROOT, "web", "server.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("store.get_settlement_facts(utc_start, utc_end), yield_every=_STATES_YIELD_EVERY,", src)


if __name__ == "__main__":
    unittest.main()
