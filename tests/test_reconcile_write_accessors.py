"""
`reconcile_dispatch_overlay`'s writes — A acceptance 5, writes half.

Five statements, four transactions, three store methods. Two findings.

1. THE EV RESTAMP WAS WRITTEN THREE TIMES, two of the three guarded by
   `AND imp_kwh_ev IS NOT NULL`. Folded into one `_EV_RESTAMP_SQL`, then deleted
   with the EV columns themselves (step 5c, 10 Oct 2026): the split is now only
   the block's segments, which the verdict re-writes.

2. THE VERDICT IS ONE TRANSACTION. Main row, sub-meters, segments. A partial
   verdict leaves sub-meters priced at the old rate while the main row moves,
   and sub-meter cost derives from the main rate — so the block disagrees with
   itself. That atomicity is why `apply_reconcile_verdict` is one method rather
   than three.
"""
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from block_store import BlockStore          # noqa: E402

S = "2026-09-01T00:00:00"


def _mk_store():
    st = BlockStore(tempfile.mktemp(suffix=".db"))
    st._conn.execute("PRAGMA foreign_keys=OFF")
    st._conn.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter) "
                     "VALUES (1,'sub_meter_ev',1)")
    st._conn.commit()
    return st


def _block(st, meter_id="electricity_main", **cols):
    base = {"block_start": S, "block_end": S, "config_period_id": 1,
            "meter_id": meter_id, "imp_kwh": 5.0, "imp_rate": 0.25,
            "imp_cost": 1.25, "rate_corrected": 0}
    base.update(cols)
    st._conn.execute("INSERT INTO blocks (%s) VALUES (%s)"
                     % (",".join(base), ",".join("?" * len(base))),
                     tuple(base.values()))
    st._conn.commit()


def _segs(st, meter_id="electricity_main"):
    return [(r["kwh"], r["inc_rate"], r["band"], r["attribution"])
            for r in st.get_block_segments(S, meter_id)]


def _row(st, meter_id="electricity_main"):
    return st._conn.execute(
        "SELECT imp_rate, imp_cost, rate_source, needs_review "
        "FROM blocks WHERE block_start=? AND meter_id=?", (S, meter_id)).fetchone()


# TheEvColumnInvariant (restamp_ev_columns and its `imp_kwh_ev IS NOT NULL` guard) went
# with the EV columns (dropped, step 5c, 10 Oct 2026): the split is the block's segments,
# so there is nothing derived left to restamp.



class ReconcileHasNoRawConnectionLeft(unittest.TestCase):

    def test_the_function_is_clean(self):
        import ast
        tree = ast.parse(open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "reconcile_dispatch_overlay")
        raw = [n.lineno for n in ast.walk(fn)
               if isinstance(n, ast.Attribute) and n.attr == "_conn"]
        self.assertEqual(raw, [], "raw connection use remains at %s" % raw)


if __name__ == "__main__":
    unittest.main()
