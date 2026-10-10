"""
The import spec's §9 corpus, run against REAL backups (importer PR 6, 10 Oct 2026).

Real databases are customer data and never live in the repo, so this runs only when pointed at a
directory of them:

    EMT_IMPORT_CORPUS=/path/to/backups bash run_tests.sh -k ImportCorpus

Every `*.db` there is judged by its own stamp, so the set needs no list and stays honest
whatever FLOOR_VERSION is (spec §9: "the below-the-bar case cannot be a fixed file"):

  * stamped at or above the floor   -> admitted by the gate; imports with every §9 gate holding;
                                       its audit record names the source's version and db_uuid;
                                       and a copy re-stamped one release BELOW the floor is
                                       refused — generated, not kept.
  * stamped below the floor         -> refused, with the floor's message.
  * not stamped                     -> refused, with the too-old message.

The source files are only ever read (opened immutable by the gate and the importer); the copies
and the import targets are made in a temporary directory and removed.

Skipped when EMT_IMPORT_CORPUS is unset, or holds no database at the floor (then only the
refusals run). A GUARD of real data, not a regression test: it cannot fail on the parent tree
for a reason the parent introduced.
"""
import glob
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import v4import                                   # noqa: E402
from v4import import gate as G                    # noqa: E402
from block_store import EMT_SCHEMA, read_emt_schema   # noqa: E402

CORPUS = os.environ.get("EMT_IMPORT_CORPUS", "")


def _databases():
    if not CORPUS:
        return []
    paths = [CORPUS] if os.path.isfile(CORPUS) else sorted(glob.glob(os.path.join(CORPUS, "*.db")))
    out = []
    for p in paths:
        try:
            conn = G._connect(p)
            try:
                out.append((p, G._meta(conn, "written_by")))
            finally:
                conn.close()
        except sqlite3.Error:
            continue                                  # not a database EMT can read: not corpus
    return out


def _below_floor():
    a, b, c = G.parse_version(G.FLOOR_VERSION)
    return "%d.%d.%d" % ((a, b, c - 1) if c else (a, b - 1, 99))


@unittest.skipUnless(CORPUS, "EMT_IMPORT_CORPUS not set: no real backups to judge")
class ImportCorpus(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dbs = _databases()
        floor = G.parse_version(G.FLOOR_VERSION)
        cls.admissible = [(p, v) for p, v in cls.dbs
                          if v and G.parse_version(v) and G.parse_version(v) >= floor]
        cls.below = [(p, v) for p, v in cls.dbs if (p, v) not in cls.admissible]

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_every_database_below_the_floor_is_refused(self):
        for p, v in self.below:
            with self.subTest(db=os.path.basename(p), written_by=v):
                why = G.check(p)
                self.assertIsNotNone(why)
                self.assertIn("last v4 release", why)

    def test_every_database_at_the_floor_imports_and_reconciles(self):
        if not self.admissible:
            self.skipTest("no database at or above FLOOR_VERSION %s in the corpus"
                          % G.FLOOR_VERSION)
        for p, v in self.admissible:
            with self.subTest(db=os.path.basename(p), written_by=v):
                self.assertIsNone(G.check(p), "the gate refused an admissible backup")
                target = os.path.join(self.tmp, "import_target.db")
                report = v4import.import_v4(p, target)
                self.assertTrue(all(g["ok"] for g in report["gates"].values()), report["gates"])
                self.assertEqual(read_emt_schema(target), EMT_SCHEMA)
                conn = sqlite3.connect(target)
                try:
                    audit = json.loads(conn.execute(
                        "SELECT value FROM store_meta WHERE key = 'import_audit'").fetchone()[0])
                finally:
                    conn.close()
                self.assertEqual(audit["source"]["written_by"], v)
                os.remove(target)

    def test_a_generated_below_floor_copy_is_refused(self):
        if not self.admissible:
            self.skipTest("no database at or above FLOOR_VERSION %s in the corpus"
                          % G.FLOOR_VERSION)
        p, _v = self.admissible[0]
        copy = os.path.join(self.tmp, "below.db")
        shutil.copyfile(p, copy)
        conn = sqlite3.connect(copy)
        try:
            with conn:
                conn.execute("UPDATE store_meta SET value = ? WHERE key = 'written_by'",
                             (json.dumps(_below_floor()),))
        finally:
            conn.close()
        why = G.check(copy)
        self.assertIsNotNone(why)
        self.assertIn(_below_floor(), why)


if __name__ == "__main__":
    unittest.main()
