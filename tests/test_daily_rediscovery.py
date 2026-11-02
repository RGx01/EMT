"""
The account's agreements are asked for again once a day while the engine runs (17 Oct 2026, the
user: "once a day is adequate").

Discovery ran only at startup and on an in-app connect, so a long-running install never learnt of a
new agreement — a renewal, an export tariff starting or ending, a move off IOG-SMB — until it
restarted, and everything judged by agreement (`split_at`, `capped_at`, `_smb_windows`,
`tariff_kind_at`, `export_agreement_at`) read the startup list. `_maybe_rediscover` asks again at
most once a day, in the Kraken poll task just before the rate refresh, and takes the answer where
the meter points are the same. The pill's export flag is now "an export agreement covers today",
not "the account has an export MPAN".

All new code: on the parent every test fails by name (`_maybe_rediscover`,
`_export_agreement_now`). Behaviour each pins: a changed agreement is taken; within a day nothing
is asked; a changed meter point is not taken; a failure retries in an hour; the poll task calls it
before the rate refresh; an export MPAN with no agreement for today does not count export as
awaiting settlement.
"""
import asyncio
import inspect
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engine                                    # noqa: E402
from block_store import BlockStore               # noqa: E402

DAY = 24 * 3600


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _iso(days):
    t = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0) + timedelta(days=days)
    return t.isoformat() + "Z"


def _disc(import_tariff="E-1R-IOG-SMB-FIX-12M-26-03-17-H", serial="S1", export=None):
    return {"account_number": "A-1",
            "import": {"mpan": "1", "serial": serial, "tariff_code": import_tariff,
                       "agreements": [{"tariff_code": import_tariff, "valid_from": _iso(-60),
                                       "valid_to": None}]},
            "export": export}


class _Client:
    def __init__(self, answer=None, fail=False):
        self.answer, self.fail, self.calls = answer, fail, 0

    async def auto_discover(self, account):
        self.calls += 1
        if self.fail:
            raise RuntimeError("Octopus is down")
        return self.answer


class _Harness(unittest.TestCase):

    def setUp(self):
        self._save = (engine._store, engine._kraken_client, engine._kraken_discovery,
                      engine._kraken_account_number, engine._kraken_account_mismatch,
                      engine._kraken_discovered_at)
        engine._store = self.st = BlockStore(":memory:")
        engine._kraken_discovery = _disc()
        engine._kraken_account_number = "A-1"
        engine._kraken_account_mismatch = None
        engine._kraken_discovered_at = 1000.0

    def tearDown(self):
        (engine._store, engine._kraken_client, engine._kraken_discovery,
         engine._kraken_account_number, engine._kraken_account_mismatch,
         engine._kraken_discovered_at) = self._save

    def _again(self, client, after=DAY + 1):
        engine._kraken_client = client
        return _run(engine._maybe_rediscover(now=1000.0 + after))


class TheDailyAsk(_Harness):

    def test_a_changed_agreement_is_taken(self):
        new = _disc(import_tariff="E-1R-AGILE-24-10-01-H")
        self.assertTrue(self._again(_Client(new)))
        self.assertEqual(engine._kraken_discovery["import"]["tariff_code"], "E-1R-AGILE-24-10-01-H")

    def test_within_a_day_nothing_is_asked(self):
        client = _Client(_disc(import_tariff="E-1R-AGILE-24-10-01-H"))
        self.assertFalse(self._again(client, after=DAY - 60))
        self.assertEqual(client.calls, 0)

    def test_the_same_agreements_change_nothing(self):
        self.assertFalse(self._again(_Client(_disc())))

    def test_a_changed_meter_is_not_taken(self):
        self.assertFalse(self._again(_Client(_disc(import_tariff="E-1R-AGILE-24-10-01-H",
                                                   serial="S2"))))
        self.assertEqual(engine._kraken_discovery["import"]["serial"], "S1")

    def test_a_failure_retries_in_an_hour(self):
        self._again(_Client(fail=True))
        client = _Client(_disc(import_tariff="E-1R-AGILE-24-10-01-H"))
        self.assertFalse(self._again(client, after=DAY + 1 + 1800))      # half an hour later
        self.assertEqual(client.calls, 0)
        self.assertTrue(self._again(client, after=DAY + 1 + 3700))       # an hour later

    def test_no_client_no_ask(self):
        engine._kraken_client = None
        self.assertFalse(_run(engine._maybe_rediscover(now=1000.0 + DAY + 1)))

    def test_the_poll_task_asks_before_the_rate_refresh(self):
        src = inspect.getsource(engine)
        i = src.index("await _maybe_rediscover()")
        self.assertLess(i, src.index("await _refresh_kraken_rate_schedules()", i))


class TheExportFlag(_Harness):

    def _flag(self, export):
        engine._store_export_settlement_expected(_disc(export=export))
        return self.st.get_meta("export_settlement_expected", None)

    def test_an_export_agreement_covering_today(self):
        self.assertIs(self._flag({"mpan": "2", "agreements": [
            {"tariff_code": "E-1R-OUTGOING-FIX-12M-19-05-13-H", "valid_from": _iso(-30),
             "valid_to": None}]}), True)

    def test_an_export_mpan_with_no_agreement_for_today(self):
        self.assertIs(self._flag({"mpan": "2", "agreements": [
            {"tariff_code": "E-1R-OUTGOING-FIX-12M-19-05-13-H", "valid_from": _iso(-90),
             "valid_to": _iso(-30)}]}), False)

    def test_an_export_mpan_with_no_agreements(self):
        self.assertIs(self._flag({"mpan": "2", "agreements": []}), False)

    def test_no_export(self):
        self.assertIs(self._flag(None), False)

    def test_re_discovery_updates_it(self):
        ended = {"mpan": "2", "serial": "E1", "agreements": [
            {"tariff_code": "E-1R-OUTGOING-FIX-12M-19-05-13-H", "valid_from": _iso(-90),
             "valid_to": _iso(-1)}]}
        engine._kraken_discovery = _disc(export=dict(ended, agreements=[
            dict(ended["agreements"][0], valid_to=None)]))
        self.st.set_meta("export_settlement_expected", True)
        self._again(_Client(_disc(export=ended)))
        self.assertIs(self.st.get_meta("export_settlement_expected", None), False)


if __name__ == "__main__":
    unittest.main()
