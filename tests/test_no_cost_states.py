"""
A half-hour whose cost EMT no longer asks Octopus for reads `abandoned`, red hatched yellow
(19 Oct 2026, the user).

- A live half-hour asked 30 days without a bill (`bill_ask`'s give-up) reads `abandoned` where it
  would otherwise wait (`overdue`); one with a bill, or corrected, does not.
- An API-imported half-hour with no bill waits as a live one does (engine step 3e, 24 Oct 2026): the
  one asker asks imported history too (decided (b)), so it reads light amber on its settled kWh,
  red past the 9-day window, and `abandoned` once given up. Until step 3e the import's no-cost
  record read `abandoned` at once outside an Intelligent window, and history imported before
  engine step 2c read `applied`. A published tariff's still reads `applied` — settled kWh x the
  published rate is the bill.
- The heatmap draws `abandoned` red with yellow hatching, counts it as an issue, and the detail
  panel says since when EMT has not asked.

Discriminating (fail on the parent): TheImportedHalfHour's, but the GUARDs marked.
"""
import os
import sys
import unittest
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import settlement_state as ss                     # noqa: E402

NOW = datetime(2026, 10, 19, 12, 0)
S = "2026-05-01T12:00:00"                          # past the 9-day window: overdue if waiting
RECENT = "2026-10-17T12:00:00"                     # inside it


def _f(**kw):
    f = {"block_start": S, "imp_kwh": 1.0, "imp_cost": 0.3, "imp_rate": 0.3, "imp_kwh_api": 1.0,
         "source": "kraken_api", "rate_source": "schedule"}
    f.update(kw)
    return f


def _bill(f, **ctx):
    return ss.classify(f, smb=False, now=NOW, **ctx)["bill"]


class TheState(unittest.TestCase):

    def test_a_live_half_hour_given_up_is_abandoned(self):
        """GUARD: unchanged."""
        self.assertEqual(_bill(_f(), given_up={S}), "abandoned")

    def test_still_being_asked_it_is_overdue(self):
        """GUARD: the parent's red."""
        self.assertEqual(_bill(_f()), "overdue")

    def test_a_billed_or_corrected_one_is_not(self):
        """GUARD of what does not move."""
        self.assertEqual(_bill(_f(rate_source="settled", bill_cost=0.3), given_up={S}), "applied")
        self.assertEqual(_bill(_f(rate_source="corrected", rate_corrected=1), given_up={S}), "corrected")


class TheImportedHalfHour(unittest.TestCase):

    def _imp(self, **kw):
        return _f(source="imported_api", imp_kwh_api=None, **kw)

    def test_with_no_bill_it_waits_on_its_settled_kwh(self):
        self.assertEqual(_bill(self._imp(block_start=RECENT)), "estimate_dcc")

    def test_past_the_window_it_is_overdue(self):
        self.assertEqual(_bill(self._imp()), "overdue")

    def test_given_up_it_is_abandoned(self):
        self.assertEqual(_bill(self._imp(), given_up={S}), "abandoned")

    def test_a_bill_still_outranks_it(self):
        """GUARD: Octopus's bill cached is Octopus's cost, whatever was asked."""
        self.assertEqual(_bill(self._imp(bill_cost=0.3), given_up={S}), "applied")

    def test_on_a_published_tariff_it_is_applied(self):
        """GUARD (the parent read it applied too)."""
        self.assertEqual(_bill(self._imp(), kind_at=lambda s: "published"), "applied")


class TheScreens(unittest.TestCase):
    _SRC = open(os.path.join(_ROOT, "web", "templates", "heatmap_module.html"), encoding="utf-8").read()

    def test_red_hatched_yellow_and_an_issue(self):
        """GUARD."""
        self.assertIn("if (v === 'abandoned' || v === 'no_data') return hatchAbandoned();", self._SRC)
        self.assertIn("x.fillStyle = '#cc0000'", self._SRC)
        self.assertIn("overdue: 1, abandoned: 1,", self._SRC)            # ISSUE
        self.assertIn("overdue: 3, abandoned: 3,", self._SRC)            # CAT_RANK

    def test_the_panel_says_since_when(self):
        self.assertIn("'not asked since ' + esc(localTime(d.no_cost_since))", self._SRC)
        srv = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()
        self.assertIn('"no_cost_since": (_eng_nc.no_cost_since(store, d["block_start"])', srv)
        self.assertIn("given_up=given_up, export_deleted=_export_deleted(store))", srv)


if __name__ == "__main__":
    unittest.main()
