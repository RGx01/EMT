"""
The agreement in force now (4 Nov 2026, a beta tester's support file).

`KrakenAPIClient._current_agreement` names discovery's current tariff — the live half-hours' standing
charge schedule, `_import_is_smb_capped`, the running agreement's prices — and the refresh's current
agreement. It was "the one with no valid_to, else the latest start". Every agreement of a fixed-term
tariff has a valid_to; on the tester's account a zero-length Agile agreement (from and to the same
instant, a blip Octopus lists) started two days after their IOG-SMB loyalty fixed and was "the
latest", so discovery read the account as on Agile and its live half-hours took the blip's standing
charge (≈5p a day under the loyalty tariff's). An open renewal starting in the future was taken over
the agreement running, too.

Now: the agreement started and not ended at `now`, blips never; of several, the latest start; with
none in force, the latest started; else the latest. The tariff codes are Octopus's published product
codes; the dates are made up.

Each test fails on the parent but the two GUARDS, which pin what was already right.
"""
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kraken_api_client import KrakenAPIClient   # noqa: E402

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
cur = KrakenAPIClient._current_agreement


def _a(code, frm, to):
    return {"tariff_code": code, "valid_from": frm, "valid_to": to}


class TheAgreementInForce(unittest.TestCase):

    def test_a_fixed_term_tariff_not_a_blip_after_it(self):
        # the tester's shape: the loyalty fixed runs to 2027; a zero-length Agile two days after its start
        ags = [_a("E-1R-IOG-SMB-FIX-12M-26-03-23-A", "2026-08-25T23:00:00Z", "2026-10-04T23:00:00Z"),
               _a("E-1R-IOG-SMB-FIX-LOYAL-12M-26-09-28-A", "2026-10-04T23:00:00Z", "2027-10-04T23:00:00Z"),
               _a("E-1R-AGILE-24-10-01-A", "2026-10-06T23:00:00Z", "2026-10-06T23:00:00Z")]
        self.assertEqual(cur(ags, NOW)["tariff_code"], "E-1R-IOG-SMB-FIX-LOYAL-12M-26-09-28-A")

    def test_not_a_renewal_still_to_come(self):
        ags = [_a("E-1R-INTELLI-FIX-12M-26-03-23-A", "2026-03-31T23:00:00Z", "2027-03-31T23:00:00Z"),
               _a("E-1R-INTELLI-VAR-24-10-29-A", "2027-03-31T23:00:00Z", None)]
        self.assertEqual(cur(ags, NOW)["tariff_code"], "E-1R-INTELLI-FIX-12M-26-03-23-A")

    def test_GUARD_an_open_agreement_running(self):
        ags = [_a("E-1R-VAR-22-11-01-A", "2023-06-27T23:00:00+00:00", "2023-09-07T23:00:00+01:00"),
               _a("E-1R-GO-VAR-22-10-14-A", "2023-09-07T23:00:00+01:00", None)]
        self.assertEqual(cur(ags)["tariff_code"], "E-1R-GO-VAR-22-10-14-A")     # by the clock, as before

    def test_none_in_force_the_latest_started_not_a_blip(self):
        # the account's agreements all ended (left Octopus, or a gap): the last real one, not the blip
        ags = [_a("E-1R-GO-VAR-22-10-14-A", "2025-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
               _a("E-1R-AGILE-24-10-01-A", "2026-02-01T00:00:00Z", "2026-02-01T00:00:00Z")]
        self.assertEqual(cur(ags, NOW)["tariff_code"], "E-1R-GO-VAR-22-10-14-A")

    def test_naive_times_are_utc(self):
        # the stored agreements (engine's `_agreement_info`) are naive UTC
        ags = [_a("E-1R-IOG-SMB-FIX-LOYAL-12M-26-09-28-A", "2026-10-04T23:00:00", "2027-10-04T23:00:00"),
               _a("E-1R-AGILE-24-10-01-A", "2026-10-06T23:00:00", "2026-10-06T23:00:00")]
        self.assertEqual(cur(ags, NOW)["tariff_code"], "E-1R-IOG-SMB-FIX-LOYAL-12M-26-09-28-A")

    def test_GUARD_none(self):
        self.assertIsNone(cur([]))


class TheClock(unittest.TestCase):

    def test_the_default_now_is_the_clocks(self):
        # in force from yesterday to tomorrow, by the real clock; a blip after it
        from datetime import timedelta
        t = datetime.now(timezone.utc)
        iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")         # noqa: E731
        ags = [_a("E-1R-IOG-SMB-FIX-LOYAL-12M-26-09-28-A", iso(t - timedelta(days=1)), iso(t + timedelta(days=1))),
               _a("E-1R-AGILE-24-10-01-A", iso(t - timedelta(hours=1)), iso(t - timedelta(hours=1)))]
        self.assertEqual(cur(ags)["tariff_code"], "E-1R-IOG-SMB-FIX-LOYAL-12M-26-09-28-A")


if __name__ == "__main__":
    unittest.main()
