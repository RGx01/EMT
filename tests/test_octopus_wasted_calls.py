"""
Calls to Octopus that could never answer (5 Nov 2026, a new tester's support file, b6, Octopus Go).

- **A closed agreement Octopus refuses was asked again at every rebuild.** Two closed two-rate tariffs
  (`E-2R-…`, 2022–23) answer `standard-unit-rates` with HTTP 400 ("This tariff has day and night rates,
  not standard"). `agreement_schedules` cached only a schedule it got, so each refresh — and each retry
  of a slow import — asked again: 129 times each. A closed agreement refused with a 4xx (not 401/403,
  a key's, nor 429, a limit's) is now remembered for the process as an empty schedule.
- **The import built its rate and standing segments on every call.** The job calls
  `import_api_history` until the history is in; a slow account's chunk timed out and was retried about
  every three minutes for seven hours, each call fetching the running agreement again. The job now
  passes `segs`, built once.
- **The dispatch capture asked a Go account for dispatches every five minutes**, and Octopus answered
  "Unable to find device for given account" 217 times. It is now asked only on an Intelligent tariff,
  and the skip is logged once.

Discriminating: four fail on the parent; the five GUARDs pin what must stay as it was (a server error, a
key or a limit asked again; the running agreement fetched each time; another caller building its
segments each call; Intelligent asked).
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import engine                                       # noqa: E402
import kraken_rates                                 # noqa: E402
from kraken_api_client import KrakenAPIError        # noqa: E402

CLOSED = {"tariff_code": "E-1R-VAR-22-11-01-A", "valid_from": "2023-03-31T23:00:00Z",
          "valid_to": "2023-07-25T23:00:00Z"}
OPEN = {"tariff_code": "E-1R-GO-FIX-12M-26-10-30-A", "valid_from": "2026-11-04T00:00:00Z",
        "valid_to": None}


class Client:
    """Octopus's REST: `status` answers every unit-rate call with that HTTP error."""

    def __init__(self, status):
        self.status, self.calls = status, 0

    async def get_unit_rates(self, product, tariff, rate_type="standard-unit-rates", **kw):
        self.calls += 1
        raise KrakenAPIError("HTTP %s for …" % self.status, status=self.status)


def _segs(client, agreements):
    return asyncio.run(kraken_rates.agreement_schedules(client, agreements, kind="rate",
                                                        product_of=lambda tc: "P"))


class TheRefusedAgreement(unittest.TestCase):

    def setUp(self):
        kraken_rates._AGREEMENT_SCHED_CACHE.clear()

    def tearDown(self):
        kraken_rates._AGREEMENT_SCHED_CACHE.clear()

    def test_a_closed_agreement_refused_is_not_asked_again(self):
        c = Client(400)
        self.assertEqual(_segs(c, [CLOSED]), [])
        self.assertEqual(_segs(c, [CLOSED]), [])
        self.assertEqual(c.calls, 1)

    def test_GUARD_a_server_error_is_asked_again(self):
        c = Client(500)
        _segs(c, [CLOSED]); _segs(c, [CLOSED])
        self.assertEqual(c.calls, 2)

    def test_GUARD_a_key_or_limit_problem_is_asked_again(self):
        for status in (401, 403, 429):
            kraken_rates._AGREEMENT_SCHED_CACHE.clear()
            c = Client(status)
            _segs(c, [CLOSED]); _segs(c, [CLOSED])
            self.assertEqual(c.calls, 2, status)

    def test_GUARD_the_running_agreement_is_asked_each_time(self):
        # v4 #513: a running agreement is rebuilt at every refresh, refused or not
        c = Client(400)
        _segs(c, [OPEN]); _segs(c, [OPEN])
        self.assertEqual(c.calls, 2)


class _Stop(Exception):
    pass


class TheImportJob(unittest.TestCase):

    def _calls(self, segs):
        built = {"rate": 0, "standing": 0}

        async def rate(ch):
            built["rate"] += 1
            return ["r"]

        async def standing(ch):
            built["standing"] += 1
            return ["s"]

        async def floor(*a, **k):
            raise _Stop()                                   # past the segments: enough
        with mock.patch.object(engine, "kraken_available", lambda: True), \
             mock.patch.object(engine, "_store", mock.MagicMock()), \
             mock.patch.object(engine, "_import_ceiling", lambda: ("2026-11-04T00:00:00", "blocks")), \
             mock.patch.object(engine, "_kraken_discovery", {"import": {"mpan": "M"}}), \
             mock.patch.object(engine, "_build_channel_rate_segs", rate), \
             mock.patch.object(engine, "_build_channel_standing_segs", standing), \
             mock.patch.object(engine, "_cached_data_floor", floor):
            for _ in range(3):
                with self.assertRaises(_Stop):
                    kw = {} if segs is None else {"segs": segs}
                    asyncio.run(engine.import_api_history(channels=("import",), **kw))
        return built

    def test_the_job_s_segments_are_built_once(self):
        d = {}
        self.assertEqual(self._calls(d), {"rate": 1, "standing": 1})
        self.assertEqual(d[("import", "rate")], ["r"])

    def test_GUARD_another_caller_builds_them_each_call(self):
        self.assertEqual(self._calls(None), {"rate": 3, "standing": 3})

    def test_the_job_passes_its_segments(self):
        src = open(engine.__file__, encoding="utf-8").read()
        i = src.index("async def run_api_import_job(")
        body = src[i:src.index("\nasync def ", i + 1)]
        self.assertIn("restart=first, segs=_segs)", body)


class TheDispatchCapture(unittest.TestCase):

    def _tick(self, kind, n=2):
        asked = []

        async def capture():
            asked.append(1)
        with mock.patch.object(engine, "_kraken_client", object()), \
             mock.patch.object(engine, "_kraken_discovery", {"import": {}}), \
             mock.patch.object(engine, "_kraken_rate_schedules", {}), \
             mock.patch.object(engine, "tariff_kind_at", lambda s: kind), \
             mock.patch.object(engine, "_capture_dispatch_slots", capture):
            engine._dispatch_skip_said = False
            with self.assertLogs("engine", level="INFO") as cm:
                engine.logger.info("start")
                for _ in range(n):
                    engine._last_dispatch_capture = None
                    asyncio.run(engine._tick_dispatch_capture())
        said = [m for m in cm.output if "not Intelligent" in m]
        return len(asked), len(said)

    def test_not_asked_on_a_published_tariff_and_said_once(self):
        self.assertEqual(self._tick("published"), (0, 1))

    def test_GUARD_asked_on_intelligent(self):
        self.assertEqual(self._tick("intelligent"), (2, 0))


if __name__ == "__main__":
    unittest.main()
