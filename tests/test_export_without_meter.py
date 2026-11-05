"""
An export channel is an export meter only with a sensor or an export MPAN (5 Nov 2026, a tester's support
file: Octopus Go, import only).

Setup writes the main meter an export channel holding only its rate and standing-charge sources. The
Export bill view, the gap fill's channels, the gaps list and what an unsettled export half-hour means
all asked only whether that key was there — so on an account with no export MPAN and no export sensor,
every API-imported half-hour (no export figure) read `no_data`: "Export bill 35058 to look at", two
years of half-hours, and an export layer on Data Management's heatmap. `_main_has_export` now asks for a
live export read sensor, or an export MPAN Octopus knows (the stored agreements' or discovery's).

Discriminating: the three not marked GUARD fail on the parent; the GUARDs pin what stays.
"""
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from tests.test_server import server              # noqa: E402

SERVER = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()


def _cfg(export):
    ch = {"import": {"read": "sensor.cad_import"}}
    if export is not None:
        ch["export"] = export
    return {"meters": {"electricity_main": {"meta": {}, "channels": ch}}}


STUB = {"rate_source": "api", "standing_charge_source": "api", "history_reads": []}


class TheGate(unittest.TestCase):

    def _has(self, export, stored=None, disc=None):
        eng = sys.modules["engine"]
        with mock.patch.object(server, "load_config", lambda: _cfg(export)), \
             mock.patch.object(eng, "_agreement_info", lambda ch: (stored or {}).get(ch) or {}, create=True), \
             mock.patch.object(eng, "_kraken_discovery", disc or {}, create=True):
            return server._main_has_export()

    def test_a_stub_with_no_meter_is_not_export(self):
        self.assertFalse(self._has(STUB))

    def test_the_settle_views_leave_out_export_then(self):
        eng = sys.modules["engine"]
        with mock.patch.object(server, "load_config", lambda: _cfg(STUB)), \
             mock.patch.object(eng, "_agreement_info", lambda ch: {}, create=True), \
             mock.patch.object(eng, "_kraken_discovery", {}, create=True), \
             mock.patch.object(eng, "mode_uses_api", lambda: True, create=True), \
             mock.patch.object(eng, "has_kraken_credentials", lambda: True, create=True):
            self.assertEqual(server._settle_views(eng), ["bill"])

    def test_GUARD_a_sensor_is_export(self):
        self.assertTrue(self._has(dict(STUB, read="sensor.cad_export")))

    def test_GUARD_an_export_mpan_is_export(self):
        self.assertTrue(self._has(STUB, stored={"export": {"mpan": "2600000000002"}}))
        # an export MPAN with no agreement (FIT-like) is not stored; discovery holds it
        self.assertTrue(self._has(STUB, disc={"export": {"mpan": "2600000000002", "agreements": []}}))

    def test_GUARD_no_channel_no_export(self):
        self.assertFalse(self._has(None, stored={"export": {"mpan": "2600000000002"}}))

    def test_the_gaps_list_asks_the_same(self):
        i = SERVER.index("    `fetchable` marks entries an API can (re)fetch. Read-only.")
        self.assertIn("has_export = _main_has_export()", SERVER[i:i + 800])


if __name__ == "__main__":
    unittest.main()
