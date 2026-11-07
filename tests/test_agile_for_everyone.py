"""
BL-103 (1 Nov 2026, agile for everyone design) — the Agile card on every account.

On an account not on Agile, Overview's Agile card shows **Agile's prices in its region** (the letter
that ends its own tariff code), as *Agile in your region (C)*, import and — where it exports — Agile
Outgoing (the user), and a line on what its last 30 days would have cost at them (the user): the
account's own half-hours at Agile's unit rates against what it paid or earned, standing charges
aside. Shown by default (the user).

The Octopus calls, within the API rule (capped, paced, persisted): the product list once a week, for
the current Agile products (their codes change); the prices once a day after Octopus publishes
tomorrow's — from 16:30 local, 90 minutes apart, at most four times either side of it — from where the record
ends (31 days back the first time) to 23:00 tomorrow, one call a direction; kept in the store's
meta, so a restart asks for nothing it has. Not for a direction the account is on Agile for itself
(its own schedule has those). REST, so the account's GraphQL points are not spent. Display only.

Checked in the local preview on 1 Nov 2026 with a made-up price record in a scratch store (the
preview has no API): the route's two public directions and their comparisons (27 days, the data's),
the card's title, now line and comparison line; the record cleared after.

Discriminating: every test fails on the parent tree.
"""
import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from zoneinfo import ZoneInfo

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import agile_public as A                         # noqa: E402

LON = ZoneInfo("Europe/London")
PRODUCTS = [
    {"code": "AGILE-23-12-06", "available_from": "2023-12-06T00:00:00Z", "available_to": "2024-10-01T00:00:00Z"},
    {"code": "AGILE-24-10-01", "available_from": "2024-10-01T00:00:00Z", "available_to": None},
    {"code": "AGILE-OUTGOING-19-05-13", "available_from": "2019-05-13T00:00:00Z", "available_to": None},
    {"code": "AGILE-BUSINESS-1", "available_from": "2025-01-01T00:00:00Z", "available_to": None, "is_business": True},
    {"code": "VAR-22-11-01", "available_from": "2022-11-01T00:00:00Z", "available_to": None},
]


class TheRules(unittest.TestCase):

    def test_the_region(self):
        self.assertEqual(A.region_of("E-1R-IOG-SMB-FIX-12M-26-03-17-C"), "C")
        self.assertIsNone(A.region_of("E-1R-VAR-22-11-01"))

    def test_the_product_on_sale_for_each_direction(self):
        self.assertEqual(A.pick_product(PRODUCTS, "import"), "AGILE-24-10-01")
        self.assertEqual(A.pick_product(PRODUCTS, "export"), "AGILE-OUTGOING-19-05-13")
        self.assertEqual(A.tariff_code("AGILE-24-10-01", "C"), "E-1R-AGILE-24-10-01-C")

    def test_tomorrows_from_half_past_four(self):
        before = datetime(2026, 11, 1, 16, 0, tzinfo=LON)
        after = datetime(2026, 11, 1, 16, 30, tzinfo=LON)
        self.assertEqual(A.target_through(before, LON), datetime(2026, 11, 1, 23, 0))
        self.assertEqual(A.target_through(after, LON), datetime(2026, 11, 2, 23, 0))

    def test_due_short_paced_and_capped(self):
        now_l = datetime(2026, 11, 1, 16, 40, tzinfo=LON)
        now_u = now_l.astimezone(timezone.utc).replace(tzinfo=None)
        full = {"import": {"prices": [["2026-11-02T23:30:00", 10.0]]}}
        self.assertFalse(A.due(full, now_u, now_l, LON, ["import"]))
        short = {"import": {"prices": [["2026-11-01T23:30:00", 10.0]]}}
        self.assertTrue(A.due(short, now_u, now_l, LON, ["import"]))
        self.assertFalse(A.due(dict(short, asked=(now_u - timedelta(minutes=30)).isoformat()), now_u, now_l, LON, ["import"]))
        self.assertFalse(A.due(dict(short, tries={"2026-11-01-pm": 4}), now_u, now_l, LON, ["import"]))

    def test_merged_by_half_hour_the_old_dropped(self):
        now = datetime(2026, 11, 1, 12, 0)
        kept = [["2026-09-01T00:00:00", 9.0], ["2026-11-01T00:00:00", 10.0]]
        recs = [{"valid_from": "2026-11-01T00:00:00Z", "value_inc_vat": 11.0},
                {"valid_from": "2026-11-01T00:30:00Z", "value_inc_vat": 12.5}]
        self.assertEqual(A.merge(kept, recs, now), [["2026-11-01T00:00:00", 11.0], ["2026-11-01T00:30:00", 12.5]])

    def test_the_comparison_over_the_half_hours_with_both(self):
        prices = [["2026-10-01T00:00:00", 10.0], ["2026-10-01T00:30:00", 20.0]]
        slots = [("2026-10-01T00:00:00", 2.0, 0.50), ("2026-10-01T00:30:00", 1.0, 0.30), ("2026-10-02T00:00:00", 5.0, 1.0)]
        self.assertEqual(A.compare(prices, slots), {"kwh": 3.0, "agile": 0.40, "actual": 0.80, "days": 1})
        self.assertIsNone(A.compare(prices, []))


class _Octopus:
    def __init__(self):
        self.products_asked = 0
        self.rates_asked = []

    async def get_products(self):
        self.products_asked += 1
        return PRODUCTS

    async def get_unit_rates(self, product, tariff, period_from=None, period_to=None):
        self.rates_asked.append((product, tariff, period_from, period_to))
        t = datetime.fromisoformat(period_from[:19])
        out = []
        while t.isoformat() + "Z" < period_to:
            out.append({"valid_from": t.isoformat() + "Z", "value_inc_vat": 15.0})
            t += timedelta(minutes=30)
        return out


class TheFetch(unittest.TestCase):

    def setUp(self):
        import engine
        from block_store import BlockStore
        self.engine = engine
        self.st = BlockStore(":memory:")
        self.st._conn.execute("INSERT OR IGNORE INTO config_periods (id, effective_from, billing_day, block_minutes, "
                              "timezone) VALUES (1,'2020-01-01T00:00:00',1,30,'Europe/London')")
        self.st._conn.commit()
        self.oct = _Octopus()
        self.disc = {"import": {"tariff_code": "E-1R-IOG-SMB-FIX-12M-26-03-17-C"},
                     "export": {"tariff_code": "E-1R-OUTGOING-FIX-12M-19-05-13-C"}}
        self.p = [mock.patch.object(engine, "_store", self.st), mock.patch.object(engine, "_reader", lambda: self.st),
                  mock.patch.object(engine, "_kraken_client", self.oct), mock.patch.object(engine, "_kraken_discovery", self.disc)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        self.st.close()

    def test_the_first_fetch_both_directions_31_days_back(self):
        rec = asyncio.run(self.engine._fetch_agile_public())
        self.assertEqual(self.oct.products_asked, 1)
        self.assertEqual([r[1] for r in self.oct.rates_asked], ["E-1R-AGILE-24-10-01-C", "E-1R-AGILE-OUTGOING-19-05-13-C"])
        self.assertGreaterEqual(len(rec["import"]["prices"]), 31 * 48)
        self.assertEqual(self.st.get_meta(A.META_KEY)["region"], "C")

    def test_then_nothing_is_due(self):
        asyncio.run(self.engine._fetch_agile_public())
        self.assertFalse(self.engine._agile_public_due())

    def test_not_for_a_direction_the_account_is_on_agile_for(self):
        self.disc["import"]["tariff_code"] = "E-1R-AGILE-24-10-01-C"
        self.assertEqual(self.engine._agile_public_channels(), ["export"])


class TheCard(unittest.TestCase):

    PAGE = open(os.path.join(_ROOT, "web", "templates", "live_power.html"), encoding="utf-8").read()
    SERVER = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()

    def test_on_every_account_with_a_region(self):
        self.assertIn("or bool(_ap_ag.region_of((_disc.get(\"import\") or {}).get(\"tariff_code\") or \"\")))", self.SERVER)

    def test_named_for_the_region_with_the_comparison(self):
        self.assertIn("(rec.public ? ' in your region' + (rec.region ? ' (' + rec.region + ')' : '') : '')", self.PAGE)
        self.assertIn("' at Agile\\u2019s unit rates: ' +", self.PAGE.replace("\u2019", "\\u2019"))

    def test_the_route_falls_back_to_the_regions_prices(self):
        i = self.SERVER.index("def api_agile_prices(")
        body = self.SERVER[i:i + 2500]
        self.assertIn('out[ch] = {"tariff": c.get("tariff"), "public": True, "region": rec.get("region"),', body)
        self.assertIn('"compare": _ap.compare(c["prices"], slots)}', body)

    def test_off_the_engine_tick(self):
        src = open(os.path.join(_ROOT, "engine.py"), encoding="utf-8").read()
        self.assertIn("    _start_agile_public_fetch()\n", src)


if __name__ == "__main__":
    unittest.main()
