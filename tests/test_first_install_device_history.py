"""
A first install's device history, set up before its Octopus import (BL-87, closed 29 Oct 2026).

Three pieces built for other reasons make it work, and this pins them together:

- **The rebuild offer** a new device's save makes (device sensors design §4) starts Device History
  while the house has no history yet. Its sanity check compares only against a house with kWh, so
  it passes (`attribution_preflight`).
- **The run keeps the recorder's hours** (`record_recorder_hours`, soft delete design §2) and writes
  no device row: a device row needs a house half-hour (`_write_device_into_block`).
- **The import's finish rebuilds** each device with hours kept in its span (#253), from those
  alone, with no recorder call — so the hours land on the imported half-hours.

GUARD: every test here passes on the parent. It exists so a change to one piece cannot quietly
break the chain (a run that refused with no house, kept nothing, or wrote orphan device rows).
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
from tests.test_device_sensors_storage import _store            # noqa: E402
from tests.test_rebuild_from_ha_reads import H, S1, S2, _main, _ev   # noqa: E402


class FakeHA:
    """HA's recorder: 2 kWh for the device in the hour."""
    async def get_statistics(self, ids, start, end, period="hour", timeout=0):
        return {i: [{"start": H + "+00:00", "change": 2.0}] for i in ids}


class BeforeTheImport(unittest.TestCase):

    def setUp(self):
        self.st = _store()                              # devices configured, no house history
        self.p = [mock.patch.object(engine, "get_store", lambda: self.st),
                  mock.patch.object(engine, "_store", self.st)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        C.reset_for_tests()
        engine._attribution_jobs.pop("ev_charger", None)

    def _rebuild_offer(self):
        C.reset_for_tests()
        engine._attribution_jobs.pop("ev_charger", None)
        C.claim("attribution", "ev_charger", force=True)
        with mock.patch.object(engine, "_engine_ha", FakeHA()):
            return asyncio.run(engine.run_attribution_job("ev_charger", ["sensor.charger_energy"]))

    def test_the_sanity_check_passes_with_no_house(self):
        with mock.patch.object(engine, "_engine_ha", FakeHA()):
            pf = asyncio.run(engine.attribution_preflight("ev_charger", ["sensor.charger_energy"]))
        self.assertEqual((pf["verdict"], pf["house_kwh"]), ("ok", 0.0))

    def test_the_run_keeps_the_hours_and_writes_no_device_row(self):
        res = self._rebuild_offer()
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.st.ha_reads_for("ev_charger")[0], {H: 2.0})
        self.assertIsNone(_ev(self.st, S1))
        self.assertIsNone(_ev(self.st, S2))

    def test_the_import_then_rebuilds_the_devices_from_them(self):
        self._rebuild_offer()
        self.st.append_block(_main(S1, 1.0))           # Octopus's import: the house only
        self.st.append_block(_main(S2, 3.0))
        started, real = [], engine.run_attribution_job

        async def _job(mid, sensors, **kw):
            started.append(kw)
            return await real(mid, sensors, **kw)

        async def go():
            with mock.patch.object(engine, "run_attribution_job", side_effect=_job):
                got = await engine.rebuild_devices_after_refill(S1, "2026-10-01T11:00:00")
                for _ in range(50):
                    if (engine._attribution_jobs.get("ev_charger") or {}).get("status") == "done":
                        break
                    await asyncio.sleep(0.05)
            return got
        C.reset_for_tests()
        engine._attribution_jobs.pop("ev_charger", None)
        with mock.patch.object(engine, "_engine_ha", None):     # no recorder call
            got = asyncio.run(go())
        self.assertEqual(got, ["ev_charger"])
        self.assertEqual(started[0]["fetch"], False)
        a, b = _ev(self.st, S1), _ev(self.st, S2)
        self.assertEqual((a["imp_kwh"], b["imp_kwh"]), (0.5, 1.5))    # by the house's shape
        self.assertEqual(b["source"], "recorder_attributed")


class TheImportsFinish(unittest.TestCase):

    def test_the_api_import_starts_the_rebuild(self):
        """The third piece's caller (#253): the API import's finish, not only the gap fill's."""
        src = open(engine.__file__, encoding="utf-8").read()
        body = src[src.index("async def run_api_import_job"):]
        body = body[:body.index("\nasync def ", 1)]
        self.assertIn("await rebuild_devices_after_refill(", body)


if __name__ == "__main__":
    unittest.main()
