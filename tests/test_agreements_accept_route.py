"""
The routes and the notice for Octopus's changed agreements (G1 design §2; the user, 28 Oct 2026).

GET /api/tariff-agreements/changed gives the answer EMT did not take; POST /api/tariff-agreements/accept
accepts it on the engine loop. Every page shows a warning with an accept button, which confirms first.

Discriminating: every test here fails on the parent (the routes and the notice are new).
"""
import asyncio
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from tests.test_server import make_client      # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHANGED = {"at": "2026-10-28T22:00:00", "odd": [["import", "E-1R-AGILE-B", "2024-01-01T00:00:00",
                                                 "2025-01-01T00:00:00"]],
           "old": [], "new": []}


class TheRoutes(unittest.TestCase):

    def test_it_reads_the_changed_agreements(self):
        client = make_client()
        eng = sys.modules["engine"]
        with mock.patch.object(eng, "refused_agreements", lambda: CHANGED, create=True):
            d = json.loads(client.get("/api/tariff-agreements/changed").data)
        self.assertEqual(d["changed"]["odd"][0][1], "E-1R-AGILE-B")

    def test_accepting_runs_on_the_engine_loop(self):
        client = make_client()
        eng, srv = sys.modules["engine"], sys.modules["server"]
        ran = []
        with mock.patch.object(eng, "accept_refused_agreements",
                               lambda: ran.append(1) or {"ok": True, "added": 2, "superseded": 2},
                               create=True), \
             mock.patch.object(srv, "_run_on_engine_loop", lambda coro, timeout=30.0: asyncio.run(coro)):
            r = client.post("/api/tariff-agreements/accept")
        self.assertEqual((r.status_code, json.loads(r.data)["ok"], ran), (200, True, [1]))

    def test_nothing_waiting_is_a_conflict(self):
        client = make_client()
        eng, srv = sys.modules["engine"], sys.modules["server"]
        with mock.patch.object(eng, "accept_refused_agreements",
                               lambda: {"ok": False, "error": "no changed agreements are waiting"},
                               create=True), \
             mock.patch.object(srv, "_run_on_engine_loop", lambda coro, timeout=30.0: asyncio.run(coro)):
            self.assertEqual(client.post("/api/tariff-agreements/accept").status_code, 409)


class TheNotice(unittest.TestCase):

    def test_it_warns_and_confirms_before_accepting(self):
        with open(os.path.join(ROOT, "web", "templates", "base.html"), encoding="utf-8") as f:
            b = f.read()
        i = b.index("var ID = 'agreements-changed';")
        js = b[i:i + 3500]
        self.assertIn("fetch(apiUrl('/api/tariff-agreements/changed'))", js)
        self.assertIn("level: 'warn',", js)
        self.assertLess(js.index("window.confirm(ask)"), js.index("'/api/tariff-agreements/accept'"))


if __name__ == "__main__":
    unittest.main()
