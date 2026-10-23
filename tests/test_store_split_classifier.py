"""
The read/write classifier (.build/classify_store_methods.py) decides which of
BlockStore's 204 methods lives on `ReadStore` and which on `WriteStore`. That is
workstream A's central boundary, so the classifier is checked against cases
established by hand before its output is built on.

The two errors are not symmetric, and the tests below are shaped by that:

  * a writer mislabelled READ lands on ReadStore and is refused at runtime by
    `query_only` — loud, and any test touching the path catches it;
  * a reader mislabelled WRITE lands only on WriteStore, so a route needing it
    reaches for `writer()`. Nothing breaks, nothing complains, and adoption drifts
    back to v4's 4% — which is the failure this whole workstream exists to stop.

So `test_no_method_is_undecided` matters more than it looks: an UNDECIDED method
cannot go on either type, and the temptation is to sweep it into WriteStore.
"""
import json
import os
import subprocess
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TOOL = os.path.join(_ROOT, ".build", "classify_store_methods.py")
_BASE = os.path.join(_ROOT, ".build", "store_split_baseline.json")


def _run(*args):
    return subprocess.run([sys.executable, _TOOL] + list(args),
                          capture_output=True, text=True, cwd=_ROOT)


class TestClassifier(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.d = json.loads(_run("--json").stdout)
        cls.side = {}
        for k in ("write", "read", "no_sql", "undecided"):
            for m in cls.d[k]:
                cls.side[m] = k

    def _is(self, side, *names):
        for n in names:
            self.assertIn(n, self.side, "%s is not a BlockStore method" % n)
            self.assertEqual(self.side[n], side,
                             "%s classified %s, expected %s" % (n, self.side[n], side))

    def test_obvious_writers(self):
        self._is("write", "set_meta", "append_block", "upsert_imported_bills",
                 "set_block_segments", "upsert_measured_cost", "add_deleted_range",
                 "prune_dispatch_slots")

    def test_obvious_readers(self):
        self._is("read", "get_meta", "get_blocks_for_range", "get_settings",
                 "get_block_segments", "get_measured_cost", "tombstoned_slots_in",
                 "count_blocks_needing_reprice", "vat_rate_at")

    def test_transitive_write_through_a_helper(self):
        """`note_import_cost` executes no SQL of its own — it calls `set_kraken_state`.
        A classifier that only read each method's own SQL would put it on
        ReadStore, where it would fail at runtime. (The reprice queue's methods were
        the example until engine step 2d removed them, 23 Oct 2026.)"""
        self._is("write", "note_import_cost")
        self._is("read", "import_no_cost_slots")

    def test_sql_assembled_in_a_local_is_still_classified(self):
        """Both of these build their statement in a local and execute the
        variable. Reading only the execute() argument sees a bare Name; 16 public
        readers landed in UNDECIDED that way on the first run."""
        self._is("read", "get_blocks_needing_reprice",
                 "get_import_blocks_missing_exc", "get_block_starts_missing_carbon")
        self._is("write", "_insert_block_rows", "upsert_imported_blocks")

    def test_fragments_do_not_win_over_the_verb(self):
        """`get_blocks_lightweight` reassembled as ' ORDER BY ... SELECT ...'
        because ast.walk is breadth-first, and was called UNDECIDED. Source-order
        reassembly is what makes 'the first verb' mean anything."""
        self._is("read", "get_blocks_lightweight",
                 "get_block_starts_missing_carbon_in_range")

    def test_no_method_is_undecided(self):
        """An undecided method belongs to neither type. Leaving any here invites
        the safe-looking fix — put it on WriteStore — which is the error that
        silently erodes adoption."""
        self.assertEqual(self.d["undecided"], [],
                         "adjudicate these before building the split")

    def test_connection_lifecycle_is_on_neither_side(self):
        """`close` and `backup` manage the connection rather than query through
        it. `backup` writes, but to the DESTINATION database, not this store."""
        self._is("no_sql", "close", "backup")

    def test_the_split_is_roughly_even(self):
        """A §2 claims the boundary can be drawn mechanically. If the read side
        ever collapses, the premise of the workstream has changed."""
        w = self.d["counts"]["write"]["public"]
        r = self.d["counts"]["read"]["public"]
        self.assertGreater(r, 0.4 * (w + r),
                           "the read-only share has collapsed — re-read A §2")


class TestBaseline(unittest.TestCase):
    def test_baseline_matches(self):
        self.assertTrue(os.path.exists(_BASE))
        r = _run("--baseline", _BASE)
        self.assertEqual(r.returncode, 0,
                         "a method has changed side, which changes which TYPE it "
                         "lives on:\n" + r.stdout)


if __name__ == "__main__":
    unittest.main()
