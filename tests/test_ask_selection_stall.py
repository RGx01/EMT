"""
Dev's hourly "Unclosed client session" (27 Oct 2026): a stall, then a leak.

1. The stall. The hourly dispatch tick works out the ask backlog (`_ask_selection`) on the engine
   loop; it asks `tariff_kind_at` and `split_at` of every settled half-hour in two years, and each
   call parsed every agreement's dates again (`_agreement_era`). On dev that held the loop for up to
   26 s every hour — long enough for HA's WebSocket to drop, and its reconnect re-ran startup. The
   agreements' windows are now parsed once per discovery (`_agreement_windows`): on a copy of dev's
   database the selection went from 0.50 s to 0.11 s.
2. The leak. That restart closed the Kraken client the running ask still held, and the ask's next
   request had `_ensure_session` open a NEW session on the closed client, which nothing closed. A
   closed client now raises instead.

Discriminating: every test here fails on the parent, but those labelled GUARD.
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import test_engine  # noqa: F401  — installs stubs, imports engine
import engine
import api_import
from kraken_api_client import KrakenAPIClient, KrakenAPIError

AGS = [{"tariff_code": "E-1R-AGILE-23-12-06-B", "valid_from": "2024-01-01T00:00:00Z",
        "valid_to": "2025-01-01T00:00:00Z"},
       {"tariff_code": "E-1R-INTELLI-VAR-22-10-14-B", "valid_from": "2025-01-01T00:00:00Z",
        "valid_to": None}]


class TheWindowsAreParsedOnce(unittest.TestCase):

    def setUp(self):
        getattr(engine, "_AGREEMENT_WINDOWS", {}).clear()
        self.p = mock.patch.object(engine, "_kraken_discovery",
                                   {"import": {"tariff_code": AGS[1]["tariff_code"],
                                               "agreements": AGS}})
        self.p.start()

    def tearDown(self):
        self.p.stop()
        getattr(engine, "_AGREEMENT_WINDOWS", {}).clear()

    def test_a_thousand_half_hours_parse_the_agreements_once(self):
        real = api_import._naive_utc
        calls = []
        with mock.patch.object(api_import, "_naive_utc", lambda v: calls.append(v) or real(v)):
            for i in range(1000):
                engine.tariff_kind_at("2024-06-%02dT%02d:00:00" % (1 + i % 28, i % 24))
        self.assertEqual(len(calls), 3, "two agreements, one open-ended: three dates, once")

    def test_GUARD_the_answers_are_the_agreements(self):
        self.assertEqual(engine.tariff_kind_at("2024-06-01T00:00:00"), "published")
        self.assertEqual(engine.tariff_kind_at("2025-06-01T00:00:00"), "intelligent")
        self.assertEqual(engine._agreement_era("import", "2023-06-01T00:00:00"), None)

    def test_GUARD_a_new_discovery_is_read_afresh(self):
        engine.tariff_kind_at("2024-06-01T00:00:00")
        with mock.patch.object(engine, "_kraken_discovery", {"import": {"agreements": [
                dict(AGS[0], tariff_code="E-1R-INTELLI-VAR-22-10-14-B")]}}):
            self.assertEqual(engine.tariff_kind_at("2024-06-01T00:00:00"), "intelligent")


class AClosedClientStaysClosed(unittest.TestCase):

    def test_no_new_session_after_close(self):
        c = KrakenAPIClient("k", account_number="A-1")
        asyncio.run(c.close())
        with self.assertRaises(KrakenAPIError):
            c._ensure_session()
        self.assertIsNone(c._session)

    def test_GUARD_an_open_client_makes_its_session(self):
        async def _go():
            c = KrakenAPIClient("k", account_number="A-1")
            s = c._ensure_session()
            self.assertIsNotNone(s)
            await c.close()
        asyncio.run(_go())


if __name__ == "__main__":
    unittest.main()
