"""
A published tariff's half-hour is final by its own agreement (published tariff final design, 28 Oct
2026, the user).

On a published tariff (Agile, Go, Flexible…) the settled kWh x the half-hour's published rate is the
bill. The Import bill read it green only inside Octopus's two years — past them "no bill" tested first
and read grey — and a v4 API import's IMPORTED bill read grey at any age. Now, where the half-hour's
own agreement is known and published and the schedule gives its rate, it reads `applied` at any age
when the stored cost is the settled kWh x that rate, and `differs` when not. CSV history stays grey;
an Octopus bill still decides; with no known agreement nothing changes (no fallback to the current
tariff).

Discriminating: every test here fails on the parent. The `unchanged` ones pin what must not move, and
fail there only because classify has no `published_rate_at`.
"""
import os
import sys
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import test_engine  # noqa: F401,E402  — installs stubs, imports engine
import engine                                    # noqa: E402
import settlement_state as ss                    # noqa: E402

NOW = datetime(2026, 10, 28, 12, 0)
OLD = "2024-03-01T18:00:00"          # past Octopus's two years
NEW = "2026-06-01T18:00:00"
AGILE = 0.2412                        # this half-hour's published rate


def _f(bs=OLD, **kw):
    f = {"block_start": bs, "imp_kwh": 2.0, "imp_kwh_api": 2.0, "imp_cost": round(2 * AGILE, 6),
         "imp_rate": AGILE, "rate_source": "schedule", "rate_corrected": 0, "source": None,
         "bill_cost": None, "bill_cost_exc": None, "bill_kwh": None, "bill_label": None,
         "bill_ev_kwh": None, "ev_kwh": None, "completed": 0, "dispatched": 0}
    f.update(kw)
    return f


V4 = dict(source="imported_api", imp_kwh_api=None, bill_cost=round(2 * AGILE, 4),
          bill_cost_exc=None, bill_kwh=2.0, bill_label="IMPORTED", rate_source="settled",
          imp_cost=round(2 * AGILE, 4))


def _bill(f, rate=AGILE):
    return ss.classify(f, smb=False, now=NOW, kind_at=lambda s: "published",
                       published_rate_at=(lambda s: rate))["bill"]


class TheRule(unittest.TestCase):

    def test_past_the_two_years_a_settled_half_hour_is_final(self):
        self.assertEqual(_bill(_f()), "applied")

    def test_a_v4_api_import_is_final(self):
        self.assertEqual(_bill(_f(**V4)), "applied")

    def test_a_cost_that_is_not_kwh_times_the_rate_differs(self):
        self.assertEqual(_bill(_f(imp_cost=0.60)), "differs")
        self.assertEqual(_bill(_f(**dict(V4, imp_cost=0.60))), "differs")

    def test_unchanged_inside_the_two_years_as_before(self):
        self.assertEqual(_bill(_f(NEW)), "applied")

    def test_unchanged_csv_history_stays_grey(self):
        self.assertEqual(_bill(_f(**dict(V4, source="imported_csv"))), "imported")

    def test_unchanged_no_known_agreement_reads_as_before(self):
        self.assertEqual(_bill(_f(), rate=None), "imported")
        self.assertEqual(_bill(_f(**V4), rate=None), "imported")

    def test_unchanged_no_settled_kwh_reads_as_before(self):
        self.assertEqual(_bill(_f(imp_kwh_api=None)), "imported")

    def test_unchanged_an_octopus_bill_decides(self):
        billed = dict(bill_cost=0.60, bill_kwh=2.0, bill_label="STANDARD", rate_source="settled",
                      imp_cost=0.60)
        self.assertEqual(_bill(_f(NEW, **billed)), "applied")

    def test_unchanged_a_correction_is_blue(self):
        self.assertEqual(_bill(_f(rate_corrected=1)), "corrected")


class _Sched:
    def is_empty(self):
        return False

    def resolve(self, ts):
        return AGILE * 100


class TheRate(unittest.TestCase):

    AGS = [{"tariff_code": "E-1R-AGILE-23-12-06-B", "valid_from": "2023-12-01T00:00:00Z",
            "valid_to": "2025-01-01T00:00:00Z"},
           {"tariff_code": "E-1R-INTELLI-VAR-22-10-14-B", "valid_from": "2025-01-01T00:00:00Z",
            "valid_to": None}]

    def setUp(self):
        getattr(engine, "_AGREEMENT_WINDOWS", {}).clear()
        self.ps = [mock.patch.object(engine, "_kraken_discovery",
                                     {"import": {"tariff_code": self.AGS[1]["tariff_code"],
                                                 "agreements": self.AGS}}),
                   mock.patch.object(engine, "_kraken_rate_schedules", {"import": _Sched()}),
                   mock.patch.object(engine, "_import_schedule_covers_history", True)]
        for p in self.ps:
            p.start()

    def tearDown(self):
        for p in self.ps:
            p.stop()
        getattr(engine, "_AGREEMENT_WINDOWS", {}).clear()

    def test_its_own_published_agreement(self):
        self.assertEqual(engine.published_rate_at(OLD), AGILE)

    def test_none_on_intelligent(self):
        self.assertIsNone(engine.published_rate_at("2025-06-01T18:00:00"))

    def test_none_before_the_oldest_agreement(self):
        """tariff_kind_at would answer the current tariff's kind here; this does not guess."""
        self.assertIsNone(engine.published_rate_at("2023-06-01T18:00:00"))

    def test_none_where_the_schedule_does_not_price_it(self):
        with mock.patch.object(engine, "_agreement_priced_ok", lambda bs: False):
            self.assertIsNone(engine.published_rate_at(OLD))


if __name__ == "__main__":
    unittest.main()
