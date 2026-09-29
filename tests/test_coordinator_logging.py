"""
The coordinator's `coord:` log lines — what B's soak reads (B §5 acceptance 6).

The coordinator holds its state in memory and exposes no endpoint, so on the Pi
the add-on log is the only record of what it decided. These pin the line shape
the soak scanner parses, and the de-duplication that keeps a sweep asking on
every tick from flooding the log.
"""
import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import job_coordinator as C       # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        C.reset_for_tests()

    def tearDown(self):
        C.reset_for_tests()

    def _lines(self, fn):
        with self.assertLogs("job_coordinator", level="INFO") as cm:
            fn()
            C.logger.info("coord: sentinel job=none")   # assertLogs needs >= 1 line
        return [r.getMessage() for r in cm.records if "sentinel" not in r.getMessage()]


class Claims(_Base):

    def test_claim_mark_and_release_are_logged(self):
        def run():
            C.claim("import")
            C.mark("import", status="running")
            C.mark("import", status="done")
            C.claim("gap_fill")
            C.release("gap_fill")
        self.assertEqual(self._lines(run), [
            "coord: claim job=import key=import",
            "coord: mark job=import key=import status=running was=claimed",
            "coord: mark job=import key=import status=done was=running",
            "coord: claim job=gap_fill key=gap_fill",
            "coord: release job=gap_fill key=gap_fill",
        ])

    def test_a_refused_claim_says_why(self):
        C.claim("import")
        lines = self._lines(lambda: C.claim("import"))
        self.assertEqual(lines, ["coord: claim_refused job=import key=import "
                                 "reason=already_running"])

    def test_a_mark_that_changes_nothing_is_silent(self):
        C.claim("import")
        C.mark("import", status="running")
        self.assertEqual(self._lines(lambda: C.mark("import", status="running",
                                                    progress=5)), [])


class Refusals(_Base):

    def test_a_deferral_is_logged_once_not_every_tick(self):
        C.claim("import")
        def run():
            for _ in range(5):
                C.admission("reprice_sweep")
        self.assertEqual(self._lines(run), [
            "coord: defer job=reprice_sweep blocker=import edge=depends_on"])

    def test_clearing_is_logged_so_a_wait_has_an_end(self):
        C.claim("import")
        C.admission("reprice_sweep")
        C.mark("import", status="done")
        lines = self._lines(lambda: C.admission("reprice_sweep"))
        self.assertEqual(lines, ["coord: admit_after job=reprice_sweep was=defer "
                                 "blocker=import"])

    def test_an_exclusion_is_labelled_as_one(self):
        C.claim("delete")
        lines = self._lines(lambda: C.admission("carbon_backfill"))
        self.assertEqual(lines, ["coord: defer job=carbon_backfill blocker=delete "
                                 "edge=exclusive_with"])

    def test_the_live_tick_is_never_logged(self):
        C.claim("import")
        self.assertEqual(self._lines(lambda: C.admission("live_tick")), [])


if __name__ == "__main__":
    unittest.main()
