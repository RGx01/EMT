"""
G1c: a half-hour on no Octopus agreement (G1 design §3, §6; the user, 28 Oct 2026).

Where no agreement covered a half-hour, `tariff_kind_at` answered the CURRENT tariff, and the asker's
only floor was Octopus's two years: on an account on Intelligent now, a live half-hour from before it
joined Octopus was asked of Octopus (an API call, no answer), read overdue, then hatched. Now, where
agreements are known:

- a half-hour none covers is `none` — not on an Octopus agreement (another supplier's, or a gap) — and
  the Import bill reads a new grey `no_agreement`, never asked;
- a config period labelled with another supplier reads the same, discovery or not;
- CSV history keeps `imported`; with no agreement known at all, the current tariff, as before;
- a period labelled Octopus that the agreements do not cover is logged (the agreement wins).

Discriminating: every test here fails on the parent, but those labelled GUARD.
"""
import os
import sys
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_ask_dry_run as D                     # noqa: E402  (store, discovery, a block helper)
import settlement_state as ss                    # noqa: E402
engine = D.engine

NOW = datetime(2026, 10, 28, 12, 0)
PRE = "2024-12-01T18:00:00"          # before DISC's one agreement (2025-01-01), inside two years
ON = "2025-06-01T18:00:00"


def _f(bs=PRE, **kw):
    f = {"block_start": bs, "imp_kwh": 1.0, "imp_kwh_api": 1.0, "imp_cost": 0.3, "imp_rate": 0.3,
         "rate_source": "schedule", "rate_corrected": 0, "source": None, "supplier": "Octopus Energy",
         "bill_cost": None, "bill_kwh": None, "bill_label": None, "completed": 0, "dispatched": 0}
    f.update(kw)
    return f


def _bill(f, kind="none"):
    return ss.classify(f, smb=False, now=NOW, kind_at=lambda s: kind)["bill"]


class TheState(unittest.TestCase):

    def test_outside_every_agreement(self):
        self.assertEqual(_bill(_f()), "no_agreement")
        self.assertEqual(_bill(_f(source="imported_api", imp_kwh_api=None, bill_cost=0.3, bill_kwh=1.0,
                                  bill_label="IMPORTED")), "no_agreement")

    def test_another_suppliers_period_before_discovery(self):
        self.assertEqual(_bill(_f(ON, supplier="British Gas"), kind="intelligent"), "no_agreement")

    def test_GUARD_csv_history_stays_imported(self):
        self.assertEqual(_bill(_f(source="imported_csv", bill_cost=0.3, bill_kwh=1.0,
                                  bill_label="IMPORTED")), "imported")

    def test_GUARD_an_octopus_or_unlabelled_period_is_not_another_suppliers(self):
        for sup in ("Octopus Energy", "octopus", "", None):
            self.assertEqual(_bill(_f(ON, supplier=sup), kind="intelligent"), "overdue", sup)

    def test_GUARD_nothing_imported_and_a_correction(self):
        self.assertEqual(_bill(_f(imp_kwh=0.0)), "none")
        self.assertEqual(_bill(_f(rate_corrected=1)), "corrected")


class TheKind(unittest.TestCase):

    AGS = [{"tariff_code": "E-1R-INTELLI-VAR-22-10-14-B", "valid_from": "2025-01-01T00:00:00Z",
            "valid_to": None}]

    def setUp(self):
        engine._AGREEMENT_WINDOWS.clear()

    def tearDown(self):
        engine._AGREEMENT_WINDOWS.clear()

    def test_known_agreements_none_covering_is_none(self):
        with mock.patch.object(engine, "_kraken_discovery",
                               {"import": {"tariff_code": self.AGS[0]["tariff_code"], "agreements": self.AGS}}):
            self.assertEqual(engine.tariff_kind_at(PRE), "none")
            self.assertEqual(engine.tariff_kind_at(ON), "intelligent")

    def test_GUARD_no_agreement_known_the_current_tariff(self):
        with mock.patch.object(engine, "_kraken_discovery",
                               {"import": {"tariff_code": self.AGS[0]["tariff_code"], "agreements": []}}), \
             mock.patch.object(engine, "_stored_agreements", None):
            self.assertEqual(engine.tariff_kind_at(PRE), "intelligent")


class TheAsker(D._Base):

    def test_another_suppliers_period_is_not_asked(self):
        self.st._conn.execute("UPDATE config_periods SET supplier = 'British Gas'")
        self.st._conn.commit()
        self._blk(ON)
        self.assertEqual(engine._ask_selection(self.st), {})

    def test_before_the_first_agreement_is_not_asked(self):
        self._blk(PRE)
        self._blk(ON)
        engine._AGREEMENT_WINDOWS.clear()
        with mock.patch.object(engine, "tariff_kind_at", _REAL_KIND):    # the harness forces Intelligent
            got = engine._ask_selection(self.st)
        engine._AGREEMENT_WINDOWS.clear()
        self.assertEqual(sorted(got), [ON])


_REAL_KIND = engine.tariff_kind_at              # before any harness patches it


class TheLabelCheck(D._Base):

    def test_octopus_labelled_half_hours_outside_the_agreements(self):
        self.st._conn.execute("UPDATE config_periods SET supplier = 'Octopus Energy'")
        self._blk(PRE)
        self._blk(ON)
        r = self.st.octopus_labelled_outside([("2025-01-01T00:00:00", None)])
        self.assertEqual((r["n"], r["first"]), (1, PRE))

    def test_another_suppliers_label_is_not_a_mismatch(self):
        self.st._conn.execute("UPDATE config_periods SET supplier = 'British Gas'")
        self._blk(PRE)
        self.assertEqual(self.st.octopus_labelled_outside([("2025-01-01T00:00:00", None)])["n"], 0)


class TheHeatmap(unittest.TestCase):

    def test_it_draws_and_names_it(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "web", "templates", "heatmap_module.html"), encoding="utf-8") as f:
            hm = f.read()
        self.assertIn("bill:  {none: 0, imported: 0, no_agreement: 0,", hm)
        self.assertIn("no_agreement: 'not on an Octopus agreement", hm)
        self.assertIn("corrected: C.blue, imported: C.grey, no_agreement: C.grey, none: C.grey};", hm)


if __name__ == "__main__":
    unittest.main()
