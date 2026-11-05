"""
Half-hours the Octopus poll backfills get their devices rebuilt (5 Nov 2026, the user, a scratch setup).

A new install imports Octopus's history to its last settled half-hour, rebuilds each device from HA's
recorder over it, then records live from its first tick. The half-hours between — a day or two — are
created later by the poll (BL-8: Octopus settled them, EMT had none), the main meter only. Nothing
came back for the devices: the import's rebuild could not reach half-hours that did not exist yet, so
a device's use there stayed the house's. The support file: 3 Nov backfilled, the Zappi with no rows
that day (the battery had them only because a Replace history ran after the poll).

Now every poll that backfills rebuilds the devices over what it created, from the recorder hours EMT
already keeps (`rebuild_devices_after_refill`, as a gap fill does): no call to Home Assistant, and only
absent device rows are filled.

Discriminating: every test here fails on the parent (no `backfilled_span`, no
`rebuild_devices_after_backfill`) but the GUARDs, marked.
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import engine                                       # noqa: E402
import job_coordinator as C                         # noqa: E402
from kraken_ingester import KrakenIngester          # noqa: E402
from tests.test_device_sensors_storage import _store            # noqa: E402
from tests.test_kraken_ingester import FakeClient   # noqa: E402
from tests.test_rebuild_from_ha_reads import H, S1, S2, _ev     # noqa: E402

ENGINE = open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read()


def _rows(*starts, kwh=1.5):
    return [{"consumption": kwh, "interval_start": s + "Z", "interval_end": s + "Z"} for s in starts]


class _Base(unittest.TestCase):

    def setUp(self):
        self.st = _store()
        # what the import's rebuild kept: HA's recorder hours, up to the first live tick
        self.st.record_recorder_hours("ev_charger", "import", {H: 2.0})
        self.p = [mock.patch.object(engine, "get_store", lambda: self.st),
                  mock.patch.object(engine, "_store", self.st)]
        for x in self.p:
            x.start()
        C.reset_for_tests()
        engine._attribution_jobs.pop("ev_charger", None)

    def tearDown(self):
        for x in self.p:
            x.stop()
        C.reset_for_tests()
        engine._attribution_jobs.pop("ev_charger", None)

    def _poll(self, *starts):
        ing = KrakenIngester(FakeClient(import_rows=_rows(*starts)), self.st, import_mpan="M",
                             import_serial="S", billing_source="api")
        return asyncio.run(ing.poll())

    def _rebuild(self, summary):
        async def go():
            got = await engine.rebuild_devices_after_backfill(summary)
            for _ in range(60):
                if (engine._attribution_jobs.get("ev_charger") or {}).get("status") in (
                        "done", "error"):
                    break
                await asyncio.sleep(0.05)
            return got
        with mock.patch.object(engine, "_engine_ha", None):     # no call to HA needed
            return asyncio.run(go())


class ThePoll(_Base):

    def test_it_says_which_half_hours_it_backfilled(self):
        s = self._poll(S2, S1)
        self.assertEqual((s["backfilled"], s["backfilled_span"]), (2, [S1, S2]))

    def test_GUARD_by_itself_it_leaves_the_devices_empty(self):
        self._poll(S1, S2)
        self.assertEqual((_ev(self.st, S1), _ev(self.st, S2)), (None, None))


class TheRebuild(_Base):

    def test_the_devices_are_rebuilt_over_the_backfill(self):
        got = self._rebuild(self._poll(S1, S2))
        self.assertEqual(got, ["ev_charger"])
        # the hour's 2.0 kWh, by the house's shape (1.5, 1.5)
        self.assertEqual((_ev(self.st, S1)["imp_kwh"], _ev(self.st, S2)["imp_kwh"]), (1.0, 1.0))

    def test_from_the_hour_a_backfill_starts_in(self):
        # the hour row starts at 10:00; a backfill from 10:30 still takes it in
        got = self._rebuild(self._poll(S2))
        self.assertEqual(got, ["ev_charger"])
        self.assertIsNotNone(_ev(self.st, S2))

    def test_nothing_backfilled_nothing_queued(self):
        self.assertEqual(self._rebuild({"backfilled": 0, "backfilled_span": None}), [])
        self.assertEqual(self._rebuild({}), [])


class TheCallers(unittest.TestCase):

    def test_every_poll_is_followed_by_it(self):
        for head in ("async def kraken_poll_task(", "async def resolve_history_gaps(",
                     "async def retry_settlement_for_unsettled("):
            i = ENGINE.index(head)
            body = ENGINE[i:ENGINE.find("\nasync def ", i + 1)]
            j = body.index("await ingester.poll(")
            self.assertIn("await rebuild_devices_after_backfill(summary)", body[j:j + 400], head)


if __name__ == "__main__":
    unittest.main()
