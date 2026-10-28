"""
settle() bands a half-hour by v4's recorded sensor rate where Octopus has given no bill (sensor rate
fact design §3, 28 Oct 2026; the rows come from the v4 import's transform 3d — test_import_sensor_rates).
The rate is Octopus's own live view, so it outranks EMT's prediction; a bill still settles the band.

Discriminating: every test here fails on the parent (the table, the transform and the band are new),
but those labelled GUARD.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import test_settle_seams as S                     # noqa: E402  (settle()'s harness: prediction = peak)
engine = S.engine


class SettleReadsIt(S._Base):

    def test_the_sensors_off_peak_outranks_the_prediction(self):
        self._main()
        self.st._conn.execute("INSERT INTO sensor_rates (meter_id, channel, block_start, rate, "
                              "origin) VALUES ('electricity_main', 'import', ?, ?, 'v4')",
                              (S.BS, S.LO))
        self.st._conn.commit()
        engine.settle_half_hour(self.st, S.BS)
        self.assertEqual(self._bands(), [("house", "off_peak")])

    def test_GUARD_without_it_the_prediction_stands(self):
        self._main()
        engine.settle_half_hour(self.st, S.BS)
        self.assertEqual(self._bands(), [("house", "day")])

    def test_a_bill_still_settles_it(self):
        self._main()
        self.st._conn.execute("INSERT INTO sensor_rates (meter_id, channel, block_start, rate, "
                              "origin) VALUES ('electricity_main', 'import', ?, ?, 'v4')",
                              (S.BS, S.LO))
        self.st.upsert_measured_cost(S.BS, mpan="M", cost_incl=round(1.0 * S.HI, 6),
                                     label="STANDARD_RATE", kwh=1.0)
        self.st._conn.commit()
        engine.settle_half_hour(self.st, S.BS)
        self.assertEqual(self._bands(), [("house", "day")])


if __name__ == "__main__":
    unittest.main()
