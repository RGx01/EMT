"""
Usage Stats (charts.html) reads /api/energy — C step 1, part 2, 1 Oct 2026.

The daily/monthly/yearly views take every local day's row from
`/api/energy?resolution=day&kind=all&include=stats`, the half-hour view one day's slots from
`resolution=block&kind=day&include=stats` (usage_view; the same rows /api/charts/blocks-summary
and blocks-day built, tests/test_energy_stats.py). The refresh poll compares a change token —
/api/charts/data-version until part 4, /api/energy?fields=fingerprint since — and the payload is
stamped with it, read BEFORE the data: a change between the two requests leaves the older token,
and the next poll refreshes.

Checked in a browser on a production-like database: the billing period's daily chart, and the
26 Oct 2025 clock change as 50 half-hours (01:00 twice) summing to that day's row.

These fail on the parent tree, where Usage Stats called the two /api/charts routes.
"""
import os
import re
import unittest

_SRC = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "web", "templates", "charts.html"), encoding="utf-8").read()


def _fn(name):
    m = re.search(r"async function " + name + r"\(\)\s*\{(.*?)\n\}", _SRC, re.S)
    assert m, name
    return m.group(1)


class UsageStatsReadsTheOneEndpoint(unittest.TestCase):

    def test_the_days_come_from_api_energy(self):
        """Through the shared day-row cache (EMTDayRows, 2 Oct 2026), which reads
        /api/energy — whole once, then only the days that moved."""
        body = _fn("_barFetchFresh")
        self.assertIn("await EMTDayRows.get(ver)", body)
        self.assertIn("/api/energy?resolution=day&kind=all&fields=stats", _SRC)
        self.assertNotIn("/api/charts/blocks-summary", body)

    def test_the_half_hours_come_from_api_energy(self):
        body = _fn("barLoadHH")
        self.assertIn("/api/energy?resolution=block&kind=day&fields=stats&date=", body)
        self.assertNotIn("/api/charts/blocks-day", body)

    def test_the_token_is_read_before_the_data(self):
        body = _fn("_barFetchFresh")
        self.assertLess(body.index("_fetchDataVersion()"), body.index("EMTDayRows.get(ver)"))

    def test_the_poll_token_is_api_energys_fingerprint(self):
        body = _fn("_fetchDataVersion")
        self.assertIn("/api/energy?fields=fingerprint", body)
        self.assertIn("j.fingerprint.token", body)
        self.assertNotIn("/api/charts/", _SRC)

    def test_no_blocks_is_an_empty_chart_not_an_error(self):
        self.assertIn("if (res.status === 404) { write(null); return EMPTY; }", _SRC)


if __name__ == "__main__":
    unittest.main()
