"""
Accepting Octopus's changed agreements (G1 design §2; the user, 28 Oct 2026: "add the accept action").

G1d's guard does not take an answer that drops or moves an agreement that started and ended over 30
days ago — an API change, more likely than a customer's. But a real correction of an old agreement looks
the same to code, and the guard would have refused it every day with a log line only. Now the refused
answer is kept; the page shows it with what EMT holds; accepting stores it as any answer is (its moved
history re-settled); and a later answer that is taken forgets it.

Discriminating: every test here fails on the parent.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine                                    # noqa: E402
import test_daily_rediscovery as R               # noqa: E402

IOG = "E-1R-INTELLI-VAR-22-10-14-B"
AGILE = "E-1R-AGILE-23-12-06-B"


class TheAccept(R._Harness):

    def setUp(self):
        super().setUp()
        self._saved = engine._stored_agreements
        engine._AGREEMENT_WINDOWS.clear()
        self.st.set_kraken_state(engine._ACCOUNT_KEY, "A-1")
        self.st._conn.execute("INSERT INTO config_periods (id, effective_from, billing_day, "
                              "block_minutes, timezone) VALUES (1, '2020-01-01T00:00:00', 1, 30, 'UTC')")
        self.now = datetime.utcnow().replace(microsecond=0)
        self.st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                              "imp_kwh) VALUES (?, ?, 'electricity_main', 1, 1.0)",
                              (self._ago(600, z=False), self._ago(600, z=False)))
        self.st._conn.commit()
        engine._keep_agreements(self._disc([
            {"tariff_code": AGILE, "valid_from": self._ago(700), "valid_to": self._ago(400)},
            {"tariff_code": IOG, "valid_from": self._ago(400), "valid_to": None}]))
        self.st.set_meta(engine._SETTLE_SPANS_KEY, [])
        # Octopus now dates the change 300 days ago: not a customer's change
        self.corrected = self._disc([
            {"tariff_code": AGILE, "valid_from": self._ago(700), "valid_to": self._ago(300)},
            {"tariff_code": IOG, "valid_from": self._ago(300), "valid_to": None}])

    def tearDown(self):
        engine._stored_agreements = self._saved
        engine._AGREEMENT_WINDOWS.clear()
        super().tearDown()

    def _ago(self, days, z=True):
        return (self.now - timedelta(days=days)).isoformat() + ("Z" if z else "")

    def _disc(self, ags):
        return {"import": {"mpan": "M1", "tariff_code": ags[-1]["tariff_code"], "agreements": ags}}

    def _windows(self):
        return [(r["tariff_code"], r["valid_to"]) for r in self.st.get_tariff_agreements("A-1")]

    def test_a_refused_answer_is_kept_for_the_page(self):
        self.assertIsNone(engine._keep_agreements(self.corrected))
        c = engine.refused_agreements()
        self.assertEqual(len(c["odd"]), 2)
        self.assertEqual([a[1] for a in c["old"]], [AGILE, IOG])
        self.assertEqual(c["new"][0][3], self._ago(300, z=False))
        self.assertNotIn("M1", str(c))                                 # no MPAN for the page

    def test_accepting_stores_it_and_re_settles(self):
        engine._keep_agreements(self.corrected)
        out = engine.accept_refused_agreements()
        self.assertEqual((out["ok"], out["added"], out["superseded"]), (True, 2, 2))
        self.assertEqual(self._windows(), [(AGILE, self._ago(300, z=False)), (IOG, None)])
        self.assertIsNone(engine.refused_agreements())
        self.assertTrue(self.st.get_meta(engine._SETTLE_SPANS_KEY))

    def test_a_later_answer_taken_forgets_it(self):
        engine._keep_agreements(self.corrected)
        engine._keep_agreements(self._disc([
            {"tariff_code": AGILE, "valid_from": self._ago(700), "valid_to": self._ago(400)},
            {"tariff_code": IOG, "valid_from": self._ago(400), "valid_to": None},
            {"tariff_code": AGILE, "valid_from": self._ago(5), "valid_to": None}]))
        self.assertIsNone(engine.refused_agreements())

    def test_nothing_waiting_accepts_nothing(self):
        self.assertEqual(engine.accept_refused_agreements()["ok"], False)


if __name__ == "__main__":
    unittest.main()
