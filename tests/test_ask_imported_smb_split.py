"""
An API import's IOG-SMB history is asked for its split (4 Nov 2026, a beta tester's support file).

Since engine step 2c the API import caches each half-hour's cost and label (`upsert_imported_bills`) but
not the IOG-SMB four-bucket breakdown, and on IOG-SMB the band waits for the breakdown. The one asker's
selector (`_ask_queue`) asked a split only "where an IOG-SMB bill is cost-only and a completed dispatch
drew" — and imported history has no dispatch (Octopus keeps a short window). So a new IOG-SMB install's
imported history was never asked: on the tester's account, 2,730 half-hours since the agreement began,
none asked, 2,130 of their bills refused ("Octopus's band for it is not settled yet"), and 35 daytime
smart charges Octopus billed off-peak left at the day rate, amber `split_pending` for good. v4's drain
asked every imported capped half-hour, because its import cached no bill.

Now an API import's half-hour with a cost-only bill on IOG-SMB is asked as a split, dispatch or none.
A live half-hour still needs its completed dispatch; a CSV import's, or one off IOG-SMB, is not asked.

The figures are made up: the IOG-SMB loyalty tariff's published 6.6p off-peak rate.

Discriminating: test_an_imported_cost_only_bill_is_asked_for_its_split and
test_it_is_a_breakdown_page fail on the parent; the GUARDS pin what stays as it was.
"""
import os
import sys
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_engine  # noqa: F401  — installs stubs, imports engine
import engine
from block_store import BlockStore

MPAN = "2600000000001"
SMB = "E-1R-IOG-SMB-FIX-LOYAL-12M-26-09-28-A"
DISC = {"import": {"mpan": MPAN, "tariff_code": SMB,
                   "agreements": [{"tariff_code": SMB, "valid_from": "2026-10-04T23:00:00Z",
                                   "valid_to": "2027-10-04T23:00:00Z"}]}}
NOW = datetime(2026, 11, 4, 12, 0)
S = "2026-10-11T13:00:00"            # a daytime half-hour, 14:00 BST


class _Base(unittest.TestCase):

    def setUp(self):
        self.st = BlockStore(":memory:")
        self.st._conn.execute("INSERT INTO config_periods (id, effective_from, billing_day, "
                              "block_minutes, timezone) VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
        self.st._conn.commit()
        self.smb = True
        self.p = [mock.patch.object(engine, "_store", self.st),
                  mock.patch.object(engine, "_reader", lambda: self.st),
                  mock.patch.object(engine, "_kraken_discovery", DISC),
                  mock.patch.object(engine, "tariff_kind_at", lambda bs: "intelligent"),
                  mock.patch.object(engine, "split_at", lambda bs: self.smb)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def _blk(self, bs, *, source="imported_api", api=False):
        self.st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
            "imp_kwh_api, rate_corrected, source) VALUES (?,?,?,1,3.853,?,0,?)",
            (bs, bs, "electricity_main", 3.853 if api else None, source))
        self.st._conn.commit()

    def _bill(self, bs, label="OFF_PEAK", breakdown=False):
        # the API import's answer: Octopus's cost and label, no breakdown
        self.st.upsert_imported_bills([{"start": bs, "cost_incl": round(3.853 * 0.066, 4),
                                        "cost_excl": None, "label": label, "kwh": 3.853}], mpan=MPAN)
        if breakdown:
            self.st._conn.execute("UPDATE measured_cost SET home_kwh = 0.0, ev_kwh = 3.853 "
                                  "WHERE slot_start = ?", (bs,))
            self.st._conn.commit()

    def _sel(self):
        return engine._ask_selection(self.st, NOW)


class TheSelection(_Base):

    def test_an_imported_cost_only_bill_is_asked_for_its_split(self):
        self._blk(S)
        self._bill(S)
        self.assertEqual(self._sel(), {S: "split"})

    def test_it_is_a_breakdown_page(self):
        # the asker pages it as a breakdown half-hour (runs under 12 a query), not as a day of costs
        self._blk(S)
        self._bill(S)
        with mock.patch.object(engine, "bill_kind_at", lambda bs: "breakdown"):
            self.assertEqual(engine._ask_pages(self._sel()), [[S]])

    def test_GUARD_one_with_its_breakdown_is_not_asked(self):
        self._blk(S)
        self._bill(S, breakdown=True)
        self.assertEqual(self._sel(), {})

    def test_GUARD_a_live_half_hour_still_needs_its_dispatch(self):
        self._blk(S, source=None, api=True)
        self._bill(S)
        self.assertEqual(self._sel(), {})

    def test_GUARD_off_iog_smb_it_is_not_asked(self):
        self.smb = False
        self._blk(S)
        self._bill(S)
        self.assertEqual(self._sel(), {})

    def test_GUARD_a_csv_import_s_is_not_asked(self):
        self._blk(S, source="imported_csv")
        self._bill(S)
        self.assertEqual(self._sel(), {})


if __name__ == "__main__":
    unittest.main()
