"""
Octopus is asked for an API import's half-hours inside its two years whose only bill is the
import's own (28 Oct 2026, the user). The v4 import gives every imported half-hour an IMPORTED bill
from its stored cost — on most API history v4's price at Octopus's label — and an IMPORTED bill
stopped the asker, so history Octopus still holds never got Octopus's cost and read grey (staging,
28 Oct 2026: 11,628 half-hours of 29 Oct 2024 - 2 Mar 2026). Now:

- they are asked, after every other half-hour (kind "imported", on the bill's record);
- Octopus's answer replaces the IMPORTED bill and the half-hour is settled from it (the paced apply
  takes unsettled blocks only, and the IMPORTED bill had settled it);
- with no cost from Octopus the IMPORTED bill stays, and the half-hour reads grey `imported` —
  asked, unanswered or given up alike;
- a CSV's IMPORTED bill is its own and is not asked.

Discriminating: every test here fails on the parent, but those labelled GUARD.
"""
import os
import sys
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_ask_dry_run as D                    # noqa: E402  (store, discovery and a block helper)
engine = D.engine

OLD = "2025-06-01T12:00:00"


class _Base(D._Base):

    def _imported(self, bs, source="imported_api", label="IMPORTED"):
        self._blk(bs, source=source, api=False)
        self.st.upsert_measured_cost(bs, mpan=D.MPAN, cost_incl=0.3, cost_excl=0.2857,
                                     label=label, kwh=1.0)


class TheSelection(_Base):

    def test_an_api_imports_half_hour_is_asked(self):
        self._imported(OLD)
        self.assertEqual(engine._ask_selection(self.st), {OLD: "imported"})

    def test_GUARD_a_csvs_is_its_own_bill(self):
        self._imported(OLD, source="imported_csv")
        self.assertEqual(engine._ask_selection(self.st), {})

    def test_GUARD_octopuss_bill_is_not_asked_again(self):
        self._imported(OLD, label="OFF_PEAK")
        self.assertEqual(engine._ask_selection(self.st), {})

    def test_they_come_after_everything_else(self):
        pages = engine._ask_pages({"2026-02-01T12:00:00": "imported", "2025-12-01T12:00:00": "bill"})
        flat = [s for p in pages for s in p]
        self.assertEqual(flat, ["2025-12-01T12:00:00", "2026-02-01T12:00:00"])


class TheAnswer(_Base):

    def _write(self, row):
        self._imported(OLD)
        bills = {}
        st = {"stored": 0, "held": 0}
        with mock.patch.object(engine, "parse_bill", lambda node, k, kwh: row), \
             mock.patch.object(engine, "settle_half_hour", lambda store, bs: self.settled.append(bs)):
            engine._ask_write_page(self.st, D.MPAN, [OLD], {OLD: "imported"}, {OLD: (None, {})},
                                   {}, {}, 0, st, ({}, {}), (bills, {}), ({}, {}))
        return bills

    def setUp(self):
        super().setUp()
        self.settled = []

    def _label(self):
        return self.st._conn.execute("SELECT label FROM measured_cost WHERE slot_start = ?",
                                     (OLD,)).fetchone()[0]

    def test_octopuss_bill_replaces_it_and_is_settled(self):
        self._write({"cost_incl": 0.0549, "cost_excl": 0.0523, "label": "OFF_PEAK", "kwh": 1.0})
        self.assertEqual((self._label(), self.settled), ("OFF_PEAK", [OLD]))

    def test_GUARD_no_cost_leaves_the_imported_bill(self):
        self._write(None)
        self.assertEqual((self._label(), self.settled), ("IMPORTED", []))


class TheColour(unittest.TestCase):

    def test_GUARD_asked_and_given_up_it_is_still_grey(self):
        import settlement_state as ss
        f = {"block_start": OLD, "imp_kwh": 1.0, "imp_kwh_api": None, "imp_cost": 0.3,
             "imp_rate": 0.3, "rate_source": "settled", "rate_corrected": 0, "source": "imported_api",
             "bill_cost": 0.3, "bill_cost_exc": 0.2857, "bill_kwh": 1.0, "bill_label": "IMPORTED",
             "bill_ev_kwh": None, "ev_kwh": None, "completed": 0, "dispatched": 0}
        st = ss.classify(f, smb=False, now=datetime(2026, 10, 28, 12), given_up={OLD},
                         vat_at=lambda s: 0.05)
        self.assertEqual(st["bill"], "imported")


if __name__ == "__main__":
    unittest.main()
