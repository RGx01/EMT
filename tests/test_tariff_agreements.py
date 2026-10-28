"""
G1a: the account's tariff agreements, stored (G1 design, 28 Oct 2026).

Discovery's agreements were held in memory only (`_kraken_discovery`), so after a restart, until
discovery answered — never, with the API down — everything judged by agreement saw nothing:
`tariff_kind_at`, `split_at`, `capped_at`, the SMB and Intelligent windows, `export_agreement_at`,
`published_rate_at`, the asker's gate. Now:

- each answer discovery takes (start-up, the daily re-discovery) replaces the account's current rows
  in `tariff_agreements` wholesale — a withdrawn agreement superseded and kept as history, a moved
  window superseded and re-added — and an answer it does not take writes nothing;
- start-up loads them before discovery, and the readers use them until discovery answers;
- the support file redacts the table's MPANs and account.

Discriminating: every test here fails on the parent (the table, the store methods and the engine's
reads are new; the meter-points case only because the table is).
"""
import asyncio
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine                                    # noqa: E402
import support_file                              # noqa: E402
from block_store import BlockStore               # noqa: E402
import test_daily_rediscovery as R               # noqa: E402  (_Client, _run)

AGILE = "E-1R-AGILE-23-12-06-B"
IOG = "E-1R-INTELLI-VAR-22-10-14-B"
OUT = "E-1R-OUTGOING-VAR-24-10-26-B"


def _disc(ags=None, export=None, mpan="M1", serial="S1"):
    ags = ags if ags is not None else [
        {"tariff_code": AGILE, "valid_from": "2024-01-01T00:00:00Z", "valid_to": "2025-01-01T00:00:00Z"},
        {"tariff_code": IOG, "valid_from": "2025-01-01T00:00:00Z", "valid_to": None}]
    return {"account_number": "A-1",
            "import": {"mpan": mpan, "serial": serial,
                       "tariff_code": ags[-1]["tariff_code"] if ags else None, "agreements": ags},
            "export": export}


def _row(code, vf, vt, mpan="M1", direction="import"):
    return {"mpan": mpan, "direction": direction, "tariff_code": code, "product_code": None,
            "valid_from": vf, "valid_to": vt}


class TheStore(unittest.TestCase):

    def setUp(self):
        self.st = BlockStore(":memory:")

    def _all(self):
        return [tuple(r) for r in self.st._conn.execute(
            "SELECT tariff_code, valid_to, superseded_at IS NOT NULL FROM tariff_agreements "
            "ORDER BY valid_from, superseded_at IS NOT NULL")]

    def test_an_answer_replaces_the_current_rows(self):
        a = _row(AGILE, "2024-01-01T00:00:00", "2025-01-01T00:00:00")
        b = _row(IOG, "2025-01-01T00:00:00", None)
        self.st.replace_tariff_agreements("A-1", [a, b], "2026-10-28T10:00:00")
        diff = self.st.replace_tariff_agreements("A-1", [b], "2026-10-29T10:00:00")
        self.assertEqual((len(diff["added"]), len(diff["superseded"]), len(diff["kept"])), (0, 1, 1))
        self.assertEqual(self._all(), [(AGILE, "2025-01-01T00:00:00", 1), (IOG, None, 0)])
        self.assertEqual([r["tariff_code"] for r in self.st.get_tariff_agreements("A-1")], [IOG])

    def test_a_moved_window_is_superseded_and_added(self):
        self.st.replace_tariff_agreements("A-1", [_row(IOG, "2025-01-01T00:00:00", None)], "t1")
        diff = self.st.replace_tariff_agreements(
            "A-1", [_row(IOG, "2025-01-01T00:00:00", "2026-03-17T00:00:00")], "t2")
        self.assertEqual((len(diff["added"]), len(diff["superseded"])), (1, 1))
        self.assertEqual(self.st.get_tariff_agreements("A-1")[0]["valid_to"], "2026-03-17T00:00:00")

    def test_another_accounts_rows_are_not_read(self):
        self.st.replace_tariff_agreements("A-2", [_row(IOG, "2025-01-01T00:00:00", None)], "t1")
        self.assertEqual(self.st.get_tariff_agreements("A-1"), [])
        self.assertEqual(self.st.get_tariff_agreements(None), [])

    def test_the_support_file_redacts_them(self):
        self.st.replace_tariff_agreements("A-9", [_row(OUT, "2024-10-26T00:00:00", None, mpan="EXP-ONLY",
                                                      direction="export")], "t1")
        ids = support_file.identifiers(self.st._conn)
        self.assertEqual((ids.get("EXP-ONLY", "")[:4], ids.get("A-9", "")[:7]), ("MPAN", "ACCOUNT"))


class TheEngine(R._Harness):

    def setUp(self):
        super().setUp()
        self._saved_stored = engine._stored_agreements
        engine._stored_agreements = None
        engine._AGREEMENT_WINDOWS.clear()
        self.st.set_kraken_state(engine._ACCOUNT_KEY, "A-1")

    def tearDown(self):
        engine._stored_agreements = self._saved_stored
        engine._AGREEMENT_WINDOWS.clear()
        super().tearDown()

    def test_an_answer_taken_is_stored(self):
        engine._keep_agreements(_disc())
        self.assertEqual([(r["tariff_code"], r["valid_from"]) for r in self.st.get_tariff_agreements("A-1")],
                         [(AGILE, "2024-01-01T00:00:00"), (IOG, "2025-01-01T00:00:00")])

    def test_a_restart_with_discovery_down_still_knows_them(self):
        engine._keep_agreements(_disc())
        engine._stored_agreements = None
        engine._kraken_discovery = None                  # a restart: discovery has not answered
        engine._AGREEMENT_WINDOWS.clear()
        engine._load_stored_agreements()
        self.assertEqual(engine.tariff_kind_at("2024-06-01T12:00:00"), "published")
        self.assertEqual(engine.tariff_kind_at("2025-06-01T12:00:00"), "intelligent")
        self.assertEqual(engine._agreement_info("import")["tariff_code"], IOG)

    def test_the_stored_export_answers_export_agreement_at(self):
        engine._keep_agreements(_disc(export={"mpan": "X1", "serial": "S2", "tariff_code": OUT,
                                              "agreements": [{"tariff_code": OUT,
                                                              "valid_from": "2024-10-26T00:00:00Z",
                                                              "valid_to": None}]}))
        engine._kraken_discovery = None
        engine._AGREEMENT_WINDOWS.clear()
        self.assertIs(engine.export_agreement_at("2025-06-01T12:00:00"), True)
        self.assertIs(engine.export_agreement_at("2024-06-01T12:00:00"), False)

    def test_rediscovery_stores_what_it_takes(self):
        engine._kraken_client = R._Client(_disc(serial="S1", mpan="1"))
        engine._kraken_discovery = R._disc()
        R._run(engine._maybe_rediscover(now=1000.0 + 2 * R.DAY))
        self.assertEqual([r["tariff_code"] for r in self.st.get_tariff_agreements("A-1")], [AGILE, IOG])

    def test_changed_meter_points_write_nothing(self):
        engine._kraken_client = R._Client(_disc(serial="NEW", mpan="1"))
        engine._kraken_discovery = R._disc()
        R._run(engine._maybe_rediscover(now=1000.0 + 2 * R.DAY))
        self.assertEqual(self.st.get_tariff_agreements("A-1"), [])

    def test_an_answer_with_no_import_agreement_writes_nothing(self):
        self.assertIsNone(engine._keep_agreements(_disc(ags=[])))
        self.assertEqual(self.st.get_tariff_agreements("A-1"), [])

    def test_discoverys_answer_wins_over_the_stored(self):
        engine._keep_agreements(_disc())
        engine._kraken_discovery = _disc(ags=[{"tariff_code": AGILE, "valid_from": "2020-01-01T00:00:00Z",
                                               "valid_to": None}])
        engine._AGREEMENT_WINDOWS.clear()
        self.assertEqual(engine.tariff_kind_at("2025-06-01T12:00:00"), "published")

    def test_startup_loads_them_before_discovery(self):
        import inspect
        src = inspect.getsource(engine._engine_startup_impl)
        self.assertLess(src.index("_load_stored_agreements()"), src.index("await _kraken_startup_discovery()"))


if __name__ == "__main__":
    unittest.main()
