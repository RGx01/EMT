"""
The third round from staging (21 Oct 2026, the user).

1. Import history from Octopus with nothing to fetch (both channels' windows empty: the history
   already covers what Octopus holds) offers no Start, and says so. It used to run, write 0 and look
   as though it never finished: the page held for a verify the server had skipped.
2. A run the server says it skipped the verify for (`verify_skipped`) is not held for one. A run that
   wrote nothing ends "nothing new".
3. The Bill Summary card is compacted (a 735 px card is 622), and one bill keeps the width it has
   beside a comparison.
4. The import page no longer sends anyone to the retired Delete Blocks page.

Discriminating: every test here fails on the parent.
"""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _t(name):
    with open(os.path.join(ROOT, "web", "templates", name), encoding="utf-8") as f:
        return f.read()


def _fn(src, head):
    i = src.index(head)
    return src[i:src.index("\n}\n", i)]


class TheOctopusImport(unittest.TestCase):

    def setUp(self):
        self.hi = _t("historical_import.html")

    def test_nothing_to_fetch_offers_no_start(self):
        fn = _fn(self.hi, "function planApi(){")
        self.assertIn("if(!fetchable){", fn)
        nothing = fn[fn.index("if(!fetchable){"):]
        nothing = nothing[:nothing.index("return;")]
        self.assertIn("Nothing to import", nothing)
        self.assertIn("getElementById('api-apply-row').style.display = 'none'", nothing)

    def test_the_plans_note_reads_as_a_sentence(self):
        self.assertIn("nothing to fetch \\u2014 already in EMT, or older than Octopus keeps", self.hi)
        fn = _fn(self.hi, "function planApi(){")
        self.assertNotIn("""'<div style="text-transform:capitalize;">'+chn+': '+_esc(c.note""", fn)


    def test_a_run_that_wrote_nothing_says_so(self):
        fn = _fn(self.hi, "function _renderApiStatus(d){")
        self.assertIn("status==='done' && !((w.import||0) + (w.export||0))", fn)
        self.assertIn("nothing new", fn)

    def test_no_delete_blocks_page(self):
        self.assertNotIn("Delete Blocks", self.hi)


class TheBillCard(unittest.TestCase):

    def setUp(self):
        self.b = _t("billing_module.html")

    def test_one_bill_keeps_its_half_width(self):
        self.assertIn(".emt-bill .bill-compare-col { flex: 1 1 0; min-width: 300px; max-width: calc(50% - 10px); }", self.b)
        self.assertIn(".emt-bill .bill-compare-col { min-width: 0; max-width: none; }", self.b)   # a phone

    def test_its_rows_are_compact(self):
        self.assertIn(".emt-bill .billing-table { border-collapse: collapse; width: 100%; font-size: 12px; line-height: 1.25; }", self.b)
        self.assertIn(".emt-bill .billing-table td { padding: 1px 0; text-align: right; }", self.b)


if __name__ == "__main__":
    unittest.main()
