"""
The battery check leaves out a half-hour whose main block has no import kWh (5 Nov 2026, a fresh install's
support file, b12).

The battery evidence compared the battery's reading with `COALESCE(m.imp_kwh, 0)`, so a main block with no
import figure read as nothing imported. On an install whose import had kept only the export of September and
October (fixed in #88), every night's grid charge — 2.7 kWh at 03:00 against "0" — counted against the
battery: 398 half-hours on 58 days, and a sensor that reports grid charge, as it should, was flagged as
counting solar. `battery_evidence_rows` now takes only half-hours whose main block has an import kWh, settled
(`imp_kwh_api`) where it has no live one. On that database: 0 half-hours, so the banner clears at the next
hourly check.

Discriminating: the tests not marked GUARD fail on the parent.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine                                    # noqa: E402
from test_battery_sensor_evidence import _Store  # noqa: E402

DAYS = ("2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11")


class TheUnknownImport(_Store):

    def _night_charges(self):
        for d in DAYS:
            self.blk(d + "T03:00:00", None, 2.724)        # the main kept its export, not its import

    def test_it_is_no_evidence(self):
        self._night_charges()
        self.assertEqual(self.rows(), [])

    def test_the_battery_is_not_flagged(self):
        self._night_charges()
        with self.assertNoLogs("engine", level="WARNING"):
            out = engine.check_battery_sensors()
        self.assertEqual((out["house_battery"]["half_hours"], out["house_battery"]["conclusive_on"]), (0, None))

    def test_GUARD_a_settled_import_is_judged(self):
        self.blk("2026-07-01T12:00:00", None, 1.8)
        self.st._conn.execute("UPDATE blocks SET imp_kwh_api=0.1 WHERE meter_id='electricity_main'")
        self.st._conn.commit()
        self.assertEqual([r[0] for r in self.rows()], ["2026-07-01T12:00:00"])

    def test_GUARD_solar_against_a_real_zero_still_counts(self):
        self.blk("2026-07-01T12:00:00", 0.0, 1.8)
        self.assertEqual([r[0] for r in self.rows()], ["2026-07-01T12:00:00"])


if __name__ == "__main__":
    unittest.main()
