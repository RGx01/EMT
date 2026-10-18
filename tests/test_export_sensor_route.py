"""
The heatmap's export states know whether the main meter has a live export SENSOR (18 Oct 2026,
the user): without one, finalise stores 0.0 for an export no sensor read, and an unsettled
half-hour read grey ("none") between the amber ones on an API-only account.
`server._main_has_export_sensor` is a read sensor on the main meter's export channel, nothing
else. Discriminating: it does not exist on the parent.

Its own file: importing tests.test_server puts a stub engine in sys.modules.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _cfg(main_channels):
    return {"meters": {"electricity_main": {"meta": {}, "channels": main_channels},
                       "ev": {"meta": {"sub_meter": True},
                              "channels": {"export": {"read": "sensor.sub"}}}}}


class TheExportSensor(unittest.TestCase):

    def setUp(self):
        from tests.test_server import server
        self.server = server

    def _has(self, ch):
        with mock.patch.object(self.server, "load_config", return_value=_cfg(ch)):
            return self.server._main_has_export_sensor()

    def test_an_export_read_sensor_on_the_main_meter(self):
        self.assertIs(self._has({"import": {}, "export": {"read": "sensor.e"}}), True)

    def test_an_export_channel_without_one(self):
        self.assertIs(self._has({"import": {"read": "sensor.i"}, "export": {}}), False)

    def test_no_export_channel(self):
        self.assertIs(self._has({"import": {"read": "sensor.i"}}), False)

    def test_a_sub_meter_s_sensor_is_not_the_main_s(self):
        self.assertIs(self._has({"import": {}}), False)


if __name__ == "__main__":
    unittest.main()
