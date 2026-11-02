"""
Request threads read through their own connection — PR 5 on the Pi, 15 Oct 2026.

Delete Blocks' preview failed with "SQLite objects created in a thread can only be used in that
same thread … created in thread id X and this is thread id Y", and worked "after a few presses";
Gap fill's API button stayed grey. Both routes (and eleven others) read through `_get_store()`:
one module-global connection, opened by whichever request thread got there first. Since
workstream A retired `check_same_thread=False` (tests/test_thread_affinity.py), any other of
waitress's four threads gets ProgrammingError — so the route answered only when waitress happened
to serve it on the opening thread. The gap fill page asks /api/backfill/plan whether the API is
available; its error read as "no API", and the button was greyed.

tests/test_route_store_use.py checks GET routes only (POSTs were assumed to write through the
engine); these were POSTs that read. Discriminating: every test here fails on the parent.
"""
import concurrent.futures
import os
import re
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from tests.test_server import make_client, server   # noqa: E402
import store_access                                  # noqa: E402
from block_store import open_block_store             # noqa: E402


class NoRouteUsesTheSharedConnection(unittest.TestCase):

    def test_only_the_no_engine_branch_calls_it(self):
        src = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()
        uses = [m.start() for m in re.finditer(r"_get_store\(\)", src)]
        allowed = []
        for pos in uses:
            line = src[src.rfind("\n", 0, pos) + 1: src.find("\n", pos)]
            if line.lstrip().startswith(("#", "def _get_store")) or "return mutate(_get_store())" in line:
                allowed.append(pos)
            elif "`" in line or '"' in line.split("_get_store")[0][-3:]:
                allowed.append(pos)                        # prose in a comment / docstring
        stray = [src[src.rfind("\n", 0, p) + 1: src.find("\n", p)].strip()
                 for p in uses if p not in allowed]
        self.assertEqual(stray, [], "request code reading through the shared connection")


class FromAnotherRequestThread(unittest.TestCase):
    """The shared connection opened on this thread; the route served on another — the Pi."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "blocks.db")
        st = open_block_store(self.path)               # opened on THIS thread
        st._conn.execute("INSERT OR IGNORE INTO config_periods (id, effective_from, billing_day, "
                         "block_minutes, timezone) VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
        st._conn.execute("INSERT INTO blocks (block_start, block_end, config_period_id, meter_id, "
                         "imp_kwh) VALUES ('2026-08-01T00:00:00','2026-08-01T00:30:00',1,"
                         "'electricity_main',1.0)")
        st._conn.commit()
        self.client = make_client(store=st)         # binds store_access to the same database
        self.web = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    def tearDown(self):
        try:
            self.web.submit(lambda: store_access.store().close()).result(timeout=10)
        except Exception:
            pass
        self.web.shutdown()
        store_access.reset_for_tests()
        self.tmp.cleanup()

    def _post(self, url, body):
        import unittest.mock
        eng = sys.modules["engine"]                 # whatever the server imports (test_server's stub)
        with unittest.mock.patch.object(eng, "kraken_available", create=True, return_value=False):
            r = self.web.submit(lambda: self.client.post(url, json=body)).result(timeout=30)
        return r.status_code, r.get_json()

    def test_the_delete_preview(self):
        code, j = self._post("/api/blocks/delete/preview",
                             {"from_date": "2026-08-01", "to_date": "2026-08-01"})
        self.assertEqual(code, 200, j)
        self.assertEqual(j.get("blocks"), 1, j)

    def test_the_backfill_plan_answers(self):
        """The probe the gap fill page gates its API button on."""
        code, j = self._post("/api/backfill/plan", {"scope": "whole_history", "source": "csv"})
        self.assertEqual(code, 200, j)
        self.assertIn("api_available", j)


if __name__ == "__main__":
    unittest.main()
