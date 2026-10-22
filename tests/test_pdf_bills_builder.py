"""
The PDF bills builder (Whole history → Import PDF bills; 22 Oct 2026, staging: 21 bills).

- **A file chosen twice is read once.** Matched by content, so a copy under another name is still a
  copy; the warning says how many were read once. A bill uploaded twice had put its notes in the
  list twice.
- **An export-only bill** (a re-issued export bill) has no import MPAN. It joins the group whose
  export MPAN it shares, instead of making a group of its own, "MPAN unknown", that couldn't be
  imported alongside. With no group to join it is "Export only".
- **A group's notes are listed once**, as a re-issue repeats them.
- **The page:**
  - Back from a bill group's review returns to the bills, not the CSV upload.
  - A group imported is marked so.
  - The done step leads back to the bills while any are left; once one was imported, the other's
    CSVs had been out of reach.

Discriminating: every test here fails on the parent.
"""
import io
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(__file__))
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import test_server as TS                       # noqa: E402  (stubs + make_client)

IMP, EXP = "2600000000001", "2600000000002"


def _bill(bp, name, mpan_import, mpan_export, day, note=None):
    b = bp.Bill(source=name, mpan_import=mpan_import, mpan_export=mpan_export,
                vat_import=0.05, vat_export=0.0)
    if mpan_import:
        b.hh_days = [[bp.HHSlot(day, 0, 0, 0, 30, 6.67, 1.0, 6.67)]]
        b.import_periods = [bp.ImportPeriod(day, day, [(6.67, 1.0, 0.07)], 1.0, 1, 52.24)]
    b.export_periods = [bp.ExportPeriod(day, day, 5.0, 15.0, 0.75)]
    b.reconciliation = {"days": [], "import_periods": [], "ok": True}
    if note:
        b.warnings = [note]
    return b


class _Base(unittest.TestCase):
    """The parser stubbed by content: each upload's bytes name the bill it is."""

    BILLS = {}

    def setUp(self):
        import bill_parser as bp
        self.client = TS.make_client()
        self._ps, self._pb = bp.pdf_support, bp.parse_bill
        bp.pdf_support = lambda: True
        bills = self.BILLS
        bp.parse_bill = lambda data, source_name="": bills[data](bp, source_name)

    def tearDown(self):
        import bill_parser as bp
        bp.pdf_support, bp.parse_bill = self._ps, self._pb

    def post(self, *files):
        data = {"files": [(io.BytesIO(content), name) for name, content in files]}
        return self.client.post("/api/historical/bills/build", data=data,
                                content_type="multipart/form-data").get_json()


NOTE = "Export 2024-09-03–2024-10-02: 489.9 kWh at 15.0p flat — daylight shape reconstructed."


class TheDuplicates(_Base):
    BILLS = {b"sep": lambda bp, n: _bill(bp, n, IMP, EXP, date(2024, 9, 10), NOTE),
             b"oct": lambda bp, n: _bill(bp, n, IMP, EXP, date(2024, 10, 10))}

    def test_a_file_chosen_twice_is_read_once(self):
        j = self.post(("sep.pdf", b"sep"), ("sep (1).pdf", b"sep"), ("oct.pdf", b"oct"))
        self.assertEqual(len(j["groups"]), 1)
        self.assertEqual(j["groups"][0]["sources"], ["sep.pdf", "oct.pdf"])
        self.assertEqual(j["duplicates"], [{"file": "sep (1).pdf", "same_as": "sep.pdf"}])
        self.assertTrue(any("read once" in w for w in j["warnings"]))

    def test_its_notes_once(self):
        j = self.post(("sep.pdf", b"sep"), ("sep (1).pdf", b"sep"))
        w = j["groups"][0]["warnings"]
        self.assertEqual(w.count(NOTE), 1)
        self.assertEqual(len(w), len(set(w)))                # and every other note once too


class TheExportOnlyBill(_Base):
    BILLS = {b"imp": lambda bp, n: _bill(bp, n, IMP, EXP, date(2024, 7, 20)),
             b"reissue": lambda bp, n: _bill(bp, n, None, EXP, date(2024, 8, 10)),
             b"other": lambda bp, n: _bill(bp, n, None, "2600000000099", date(2024, 8, 10))}

    def test_joins_the_group_sharing_its_export(self):
        j = self.post(("jul.pdf", b"imp"), ("aug-reissued.pdf", b"reissue"))
        self.assertEqual(len(j["groups"]), 1)
        g = j["groups"][0]
        self.assertEqual((g["mpan_import"], g["export_only"]), (IMP, False))
        self.assertEqual(g["sources"], ["jul.pdf", "aug-reissued.pdf"])
        one = self.post(("jul.pdf", b"imp"))["groups"][0]["export_rows"]
        self.assertEqual(g["export_rows"], 2 * one)         # both bills' export days

    def test_with_none_to_join_is_export_only(self):
        j = self.post(("jul.pdf", b"imp"), ("x.pdf", b"other"))
        lone = [g for g in j["groups"] if g["export_only"]]
        self.assertEqual(len(lone), 1)
        self.assertIsNone(lone[0]["mpan_import"])
        self.assertFalse(any("house move" in w for w in j["warnings"]))


class ThePage(unittest.TestCase):

    def setUp(self):
        with open(os.path.join(_ROOT, "web", "templates", "historical_import.html"), encoding="utf-8") as f:
            self.hi = f.read()

    def test_back_returns_to_the_bills(self):
        self.assertIn("function reviewBack(){ goStep(_billsMode ? 'bills' : 'upload'); }", self.hi)
        self.assertIn('onclick="reviewBack()">← Back</button>', self.hi)

    def test_an_imported_group_is_marked_and_the_rest_are_a_click_away(self):
        self.assertIn("window._BILLS.groups[_billsGroup].imported = true;", self.hi)
        self.assertIn('id="done-bills-back" onclick="goStep(\'bills\')"', self.hi)
        self.assertIn("'Import again →' : 'Import this history →'", self.hi)

    def test_export_only_says_so(self):
        self.assertIn("(g.export_only ? 'Export only' : 'MPAN unknown')", self.hi)


if __name__ == "__main__":
    unittest.main()
