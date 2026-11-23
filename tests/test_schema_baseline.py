"""
EMT's own backups restore into every later EMT, from the first release on (23 Nov 2026, the first-release PR).

Until then a restore refused a v5 backup of any revision but this build's (`web/server.py` `_restore_phase1`), and
`block_store`'s comment said so "until the first release fixes the baseline". Every beta (2026.11.0b1 on) and the first
release (2026.12.0) write revision 5 (`EMT_SCHEMA`, since 10 Oct 2026), so it is the baseline: `EMT_SCHEMA_RELEASED`.

- A new column needs no new revision: `BlockStore._ensure_schema` adds it on open, so a restored backup gains it.
- A raise of `EMT_SCHEMA` would make every released user's backups unrestorable, because the restore refuses an older
  revision. So the raise is stopped here until a revision-5 backup is brought forward on restore; whoever writes that
  changes this test with it.

test_the_baseline fails on the parent tree (no EMT_SCHEMA_RELEASED). test_a_baseline_backup_restores and
test_a_new_column_reaches_a_restored_backup are GUARDS: they pass on the parent tree too.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "web"))

import block_store as bs     # noqa: E402


class TheBaseline(unittest.TestCase):

    def test_the_baseline(self):
        self.assertEqual(bs.EMT_SCHEMA_RELEASED, 5)
        self.assertEqual(bs.EMT_SCHEMA, bs.EMT_SCHEMA_RELEASED,
                         "a new revision refuses every released backup: bring a revision-%d backup forward on "
                         "restore first, then change this test" % bs.EMT_SCHEMA_RELEASED)

    def test_a_baseline_backup_restores(self):
        # GUARD: a fresh database is the baseline's, and the restore's first phase admits it
        import importlib.util
        spec = importlib.util.spec_from_file_location("server_baseline", os.path.join(_ROOT, "web", "server.py"))
        srv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(srv)
        p = os.path.join(tempfile.mkdtemp(), "blocks.db")
        bs.BlockStore(p).close()
        self.assertEqual(bs.read_emt_schema(p), 5)
        self.assertEqual(srv._restore_phase1(p), 5)

    def test_a_new_column_reaches_a_restored_backup(self):
        # GUARD: a backup from before a column was added gains it on open (here: v37's temp_c)
        p = os.path.join(tempfile.mkdtemp(), "blocks.db")
        bs.BlockStore(p).close()
        c = sqlite3.connect(p)
        # rebuilt without it, not DROP COLUMN: CI's older SQLite cannot drop past the table's comments (#221)
        keep = ", ".join(r[1] for r in c.execute("PRAGMA table_info(sub_meter_history)") if r[1] != "temp_c")
        c.executescript("CREATE TABLE _old AS SELECT %s FROM sub_meter_history; DROP TABLE sub_meter_history; "
                        "ALTER TABLE _old RENAME TO sub_meter_history;" % keep)
        self.assertNotIn("temp_c", [r[1] for r in c.execute("PRAGMA table_info(sub_meter_history)")])
        c.commit()
        c.close()
        bs.BlockStore(p).close()
        cols = [r[1] for r in sqlite3.connect(p).execute("PRAGMA table_info(sub_meter_history)")]
        self.assertIn("temp_c", cols)


if __name__ == "__main__":
    unittest.main()
