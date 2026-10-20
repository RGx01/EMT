"""
A half-hour that drew nothing reads `none` on the Import bill, whatever its rate's provenance
(staging, 20 Oct 2026).

A rate correction made over a span (v4's, carried by the import) keeps its flag on the half-hours
in it that drew nothing: on staging 21 of the 33 corrected half-hours had no kWh, drawn blue as
`corrected`, and a re-fetch from Octopus had nothing to change, so they stayed blue. There is no
cost to settle and no rate to have corrected; `none` is checked first.

Discriminating (fail on the parent), except the GUARD marked.
"""
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import settlement_state as ss            # noqa: E402

NOW = datetime(2026, 10, 20, 12, 0)


def _bill(**kw):
    f = {"block_start": "2026-09-10T12:00:00", "imp_kwh": 0.0, "imp_kwh_api": 0.0,
         "imp_cost": 0.0, "imp_rate": 0.323092, "source": "kraken_api",
         "rate_source": "corrected", "rate_corrected": 1}
    f.update(kw)
    return ss.classify(f, smb=True, now=NOW)["bill"]


class TheState(unittest.TestCase):

    def test_a_corrected_half_hour_that_drew_nothing_reads_none(self):
        self.assertEqual(_bill(), "none")

    def test_by_either_flag(self):
        self.assertEqual(_bill(rate_corrected=0), "none")
        self.assertEqual(_bill(rate_source="schedule"), "none")

    def test_one_that_drew_something_is_still_corrected(self):
        """GUARD: the correction stands where there is a cost (2 of the 1-10 Sep half-hours)."""
        self.assertEqual(_bill(imp_kwh=0.001, imp_kwh_api=0.001, imp_cost=0.000323), "corrected")


if __name__ == "__main__":
    unittest.main()
