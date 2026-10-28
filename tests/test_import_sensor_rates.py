"""
v4's live rate kept as a fact (sensor rate fact design, 28 Oct 2026; import spec v28, transform 3d).

v4 priced a live half-hour at the last reading of the import channel's rate sensor until it was
connected to Octopus's API; that sensor is Octopus's live rate, off-peak during an Intelligent
dispatch. On staging's v4 database it matched Octopus's later bills on all 185 half-hours outside the
night window it can be checked on, before v4 recorded any dispatch. v5's settle() reads no stored
rate, so where Octopus will not answer (87 half-hours of Mar-Apr 2026) three smart charges past the
window read peak. The import now keeps that rate as `sensor_rates`, per database's own era, and
settle() bands by it where there is no bill.

Discriminating: every test here fails on the parent (the table, the transform and the band are new),
but those labelled GUARD.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import test_v4import as T                        # noqa: E402  (the v4 fixture and its importer run)

SENSOR = "sensor.octopus_energy_electricity_current_rate"
APR = "2026-04-01T04:30:00"          # 05:30 BST, a smart charge past the window: v4 had off-peak
APR_IMPORTED = "2026-04-01T05:00:00"
JUL = "2026-07-01T04:30:00"          # after the go-live: the schedule priced it, not the sensor


def _meters(rate_source="api", sensor=SENSOR):
    return ("INSERT INTO meters (id, config_period_id, meter_id, is_sub_meter) VALUES "
            "(1, 1, 'electricity_main', 0)",
            "INSERT INTO meter_channels (meter_id, channel, rate_sensor, rate_source) VALUES "
            "(1, 'import', %s, '%s')" % ("'%s'" % sensor if sensor else "NULL", rate_source))


def _blocks():
    row = ("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
           "imp_rate, imp_cost, source) VALUES ('%s', '%s', 'electricity_main', 1, %s, %s, %s, %s)")
    return (row % (APR, "2026-04-01T05:00:00", 2.281, 0.0549, 0.1253, "NULL"),
            row % (APR_IMPORTED, "2026-04-01T05:30:00", 1.0, 0.0549, 0.0549, "'imported_api'"),
            row % (JUL, "2026-07-01T05:00:00", 1.0, 0.3231, 0.3231, "NULL"))


class TheTransform(T._Dir):

    def _rates(self, conn):
        return [(r["block_start"], r["rate"], r["origin"]) for r in conn.execute(
            "SELECT * FROM sensor_rates ORDER BY block_start")]

    def _ok(self, report):
        self.assertTrue(all(g["ok"] for g in report["gates"].values()), report["gates"])

    def test_the_sensor_era_is_kept_before_the_go_live(self):
        conn, report = self.imported(state={"pre_live_snapshot_done": "20260626T232418"},
                                     extra_sql=_meters() + _blocks())
        self.assertEqual(self._rates(conn), [(APR, 0.0549, "v4")])
        self.assertEqual(report["transforms"]["sensor_rates"]["go_live"], "2026-06-26T23:24:18")
        self._ok(report)

    def test_a_sensor_kept_throughout_prices_every_live_half_hour(self):
        conn, report = self.imported(extra_sql=_meters(rate_source="sensor") + _blocks())
        got = [r[0] for r in self._rates(conn)]
        self.assertIn(APR, got)
        self.assertIn(JUL, got)
        self.assertNotIn(APR_IMPORTED, got)              # imported: its IMPORTED bill, 3c
        self._ok(report)

    def test_no_go_live_stamp_writes_nothing_for_an_api_channel(self):
        conn, report = self.imported(extra_sql=_meters() + _blocks())
        self.assertEqual(self._rates(conn), [])
        self._ok(report)

    def test_no_rate_sensor_writes_nothing(self):
        conn, report = self.imported(state={"pre_live_snapshot_done": "20260626T232418"},
                                     extra_sql=_meters(sensor=None) + _blocks())
        self.assertEqual(self._rates(conn), [])
        self._ok(report)


if __name__ == "__main__":
    unittest.main()
