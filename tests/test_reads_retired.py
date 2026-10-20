"""
The `reads` table retired (20 Oct 2026; soft delete design §2).

v4 2.0.0 created `reads` for raw sensor samples — within-block charting, auditing a block from
first principles, mid-block rates, gas (v4's SQLITE_MIGRATION_PLAN.md, "Raw reads table"). 2.1.0
put the in-progress buffer in `current_reads` instead, and nothing ever wrote a row (ROADMAP,
19 Sep 2026). HA's data is kept at the half-hour, in `ha_reads` (decided 19 Oct 2026, the user).
So: v5 creates no `reads`; v5's own database drops its empty one on open; a v4-shaped database
keeps it for the importer, which drops it.

Discriminating (fail on the parent, which creates the table), except the GUARD marked.
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from block_store import BlockStore                  # noqa: E402
from tests.test_v4import import _Dir                # noqa: E402

V4_READS = ("CREATE TABLE reads (id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at TEXT NOT NULL, "
            "meter_id TEXT NOT NULL, channel TEXT NOT NULL, reading_kwh REAL NOT NULL, rate REAL, "
            "block_id INTEGER)")


def _tables(path):
    c = sqlite3.connect(path)
    try:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        c.close()


class OnOpen(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "blocks.db")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_new_database_has_none(self):
        BlockStore(self.path).close()
        self.assertNotIn("reads", _tables(self.path))

    def test_v5s_own_database_drops_its_empty_one(self):
        BlockStore(self.path).close()
        c = sqlite3.connect(self.path); c.execute(V4_READS); c.commit(); c.close()
        BlockStore(self.path).close()
        self.assertNotIn("reads", _tables(self.path))

    def test_a_v4_shaped_database_keeps_it_for_the_importer(self):
        """GUARD (passes on the parent, which never drops it)."""
        BlockStore(self.path).close()
        c = sqlite3.connect(self.path)
        c.execute("DELETE FROM store_meta WHERE key = 'emt_schema'")
        c.execute("DROP TABLE IF EXISTS reads"); c.execute(V4_READS); c.commit(); c.close()
        BlockStore(self.path).close()
        self.assertIn("reads", _tables(self.path))


class TheImport(_Dir):

    def test_the_imported_database_has_none(self):
        conn, _ = self.imported(extra_sql=("INSERT INTO reads (captured_at, meter_id, channel, "
                                           "reading_kwh) VALUES ('2026-01-01', 'm', 'import', 1)",))
        self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name = 'reads'")
                          .fetchone())


if __name__ == "__main__":
    unittest.main()
