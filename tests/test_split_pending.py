"""
`split_pending` — an IOG-SMB half-hour waiting for its House/EV breakdown, not a disagreement
(4 Nov 2026, a beta tester's support file).

A new IOG-SMB account imports its history: the API import caches each half-hour's cost and label
(`upsert_imported_bills`) but not the four-bucket breakdown, and on IOG-SMB the band waits for the
breakdown, which the asker fetches — newest first, paced. Until it does, a half-hour Octopus billed
off-peak in the daytime (a smart charge EMT saw no dispatch for) is priced by EMT at the day rate, and
its cached bill disagrees: it read red `differs` — 35 of them on the tester's heatmap, 16 minutes
after the import, before the asker had started. It is now light amber `split_pending`: Octopus's cost
is in, waiting for the split to apply it. Where the bill does carry a breakdown, or the half-hour is
not on IOG-SMB, or EMT no longer asks for it, a disagreement is still `differs`.

The figures are made up: the IOG-SMB loyalty tariff's published 6.6p off-peak and 36.18p day rates.

Discriminating: test_the_import_s_cost_waiting_for_its_split and the page's three fail on the parent;
the GUARDS pin the cases that stay `differs` or `applied`.
"""
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import settlement_state as ss                    # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = open(os.path.join(_ROOT, "web", "templates", "heatmap_module.html"), encoding="utf-8").read()
NOW = datetime(2026, 11, 4, 12, 8)
S = "2026-10-11T13:00:00"               # a daytime half-hour, 14:00 BST
DAY, OFF = 0.3618, 0.066


def _f(**kw):
    f = {"block_start": S, "imp_kwh": 3.853, "imp_kwh_api": None, "imp_cost": round(3.853 * DAY, 4),
         "imp_rate": DAY, "rate_source": "schedule", "rate_corrected": 0, "source": "imported_api",
         "bill_cost": round(3.853 * OFF, 4), "bill_cost_exc": None, "bill_kwh": 3.853,
         "bill_ev_kwh": None, "bill_home_kwh": None, "bill_label": "OFF_PEAK",
         "ev_kwh": None, "completed": 0, "dispatched": 0}
    f.update(kw)
    return f


def _bill(f, **ctx):
    c = {"smb": True, "now": NOW, "vat_at": lambda s: 0.0}
    c.update(ctx)
    return ss.classify(f, **c)["bill"]


class TheState(unittest.TestCase):

    def test_the_import_s_cost_waiting_for_its_split(self):
        self.assertEqual(_bill(_f()), "split_pending")
        self.assertEqual(_bill(_f(), split_at=lambda s: True), "split_pending")

    def test_GUARD_a_bill_with_its_breakdown_that_disagrees_differs(self):
        self.assertEqual(_bill(_f(bill_ev_kwh=3.853, bill_home_kwh=0.0)), "differs")

    def test_GUARD_not_on_iog_smb_differs(self):
        self.assertEqual(_bill(_f(), smb=False, split_at=lambda s: False), "differs")

    def test_GUARD_no_longer_asked_differs(self):
        self.assertEqual(_bill(_f(), given_up={S}), "differs")

    def test_GUARD_agreeing_is_applied(self):
        self.assertEqual(_bill(_f(imp_cost=round(3.853 * OFF, 4))), "applied")


class ThePage(unittest.TestCase):

    def test_light_amber_and_waiting(self):
        self.assertIn("split_pending: C.lamber,", PAGE)
        self.assertIn("estimate_cad: 2, split_pending: 2, overdue: 3,", PAGE)

    def test_in_the_key(self):
        self.assertIn("['split_pending', 'Octopus\\u2019s cost is in \\u2014 waiting for its House/EV split to apply it'],",
                      PAGE)

    def test_an_issue_to_look_at(self):
        self.assertIn("estimate_cad: 1, split_pending: 1,", PAGE)


if __name__ == "__main__":
    unittest.main()
