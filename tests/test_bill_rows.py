"""
#48 (3 Oct 2026) — a period's bill from flat rows, not block dicts.

A year's bill spent most of its time building every block as nested dicts
(`get_blocks_lightweight`, 38 columns per meter row) and then reading them back through
`.get` chains in `calculate_billing_summary_for_period`. The bill now reads
`BlockStore.get_bill_rows`: one summary loop (`billing._summarise`) fed either by those
rows or, through an adapter, by block dicts as before. The bill's other readers (the EV
slot maps, the EV carve, the bill method, the day list) get `billing_view._bill_blocks`,
which carries only what they read.

The claim is that NOTHING in the response moves. So every test here compares the new
path with the old one on the same store, exactly: JSON text, so key order and every
float's last bit count. The fixture is built to reach every branch the summary has.

The equivalence tests fail on the parent tree (the row path does not exist there).
Tests marked GUARD pin behaviour the parent already had.
"""
import json
import os
import random
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import billing                                   # noqa: E402
import billing_view as bv                        # noqa: E402
import energy_charts as ec                       # noqa: E402
import vat_calendar                              # noqa: E402
from block_store import BlockStore               # noqa: E402

TZ = "Europe/London"
# 30 Sep 2026 local → 27 Oct: across 1 Oct's VAT change and 25 Oct's clock change.
LO, HI = "2026-09-29T23:00:00", "2026-10-27T00:00:00"
N = 48 * 27 + 2


def _store():
    st = BlockStore(os.path.join(tempfile.mkdtemp(), "blocks.db"))
    c = st._conn
    c.execute("PRAGMA foreign_keys=OFF")
    c.execute("INSERT INTO config_periods (id, effective_from, billing_day, block_minutes, timezone) "
              "VALUES (1, '2026-01-01T00:00:00', 1, 30, ?)", (TZ,))
    c.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter) "
              "VALUES (1, 'electricity_main', 0)")
    c.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter, parent_meter_id, "
              "device_label, meter_type) VALUES (1, 'ev_charger', 1, 'electricity_main', "
              "'EV Charger', 'ev')")
    c.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter, parent_meter_id, "
              "device_label) VALUES (1, 'house_battery', 1, 'electricity_main', NULL)")
    rnd = random.Random(48)
    t0 = datetime(2026, 9, 29, 23, 0)
    reg = 1000.0
    for i in range(N):
        t = t0 + timedelta(minutes=30 * i)
        bs = t.isoformat()
        night = t.hour < 5
        after_vat = t >= datetime(2026, 9, 30, 23, 0)
        vat = 0.0 if after_vat else 0.05
        rate = 0.07 if night else (-0.05 if i % 97 == 0 else 0.30)     # a negative (plunge) slot
        k = round(rnd.uniform(0.05, 2.5), 3)
        cost = round(k * rate, 6)
        if i % 211 == 0:
            rate_col, cost = None, 0.0                                  # unpriced
        else:
            rate_col = rate
        exc = round(cost / (1 + vat), 6) if i % 3 == 0 else None
        rs = 0.0 if i % 53 == 0 else reg
        reg += k
        re_ = 0.0 if i % 53 == 0 else reg
        day = (t - t0).days
        sc = 0.0 if (day == 4 and t.hour < 2) else (0.61 if day < 12 else 0.58)
        ev = round(k * rnd.uniform(0.3, 1.2), 3) if night else 0.0
        bat = round(rnd.uniform(0.0, 0.4), 3) if i % 4 == 0 else 0.0
        segs = []
        # Step 5c (10 Oct 2026): the split is only segments. A night block with
        # i % 4 == 2 used to carry a column split with no segments (the bill's
        # 'mixed' transition rows); it is now a plain no-segment block (no EV).
        if night and i % 4 == 0:                                        # priced segments
            segs = [(round(ev * 0.9, 6), rate, (rate / (1 + vat)) if i % 8 else None, "off_peak", "ev"),
                    (round(k - ev * 0.9, 6), rate, rate / (1 + vat), "off_peak", "house"),
                    (0.0, 0.30, None, "peak", "house")]                   # skipped (no kWh)
        c.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
            "imp_rate, imp_cost, imp_read_start, imp_read_end, imp_cost_exc, "
            "exp_kwh, exp_rate, exp_cost, exp_read_start, exp_read_end, standing_charge"
            ") VALUES (?,?,'electricity_main',1,?,?,?,?,?,?,?,?,?,?,?,?)",
            (bs, bs, k, rate_col, cost, rs, re_, exc,
             0.2 if not night else 0.0, 0.15 if i % 7 else None, 0.03 if not night else 0.0,
             10.0 + i if i % 9 else None, 10.2 + i if i % 9 else None, sc))
        for seq, (sk, si, sx, band, attr) in enumerate(segs):
            c.execute("INSERT INTO block_segments (block_start, meter_id, channel, seq, kwh, inc_rate, "
                      "exc_rate, band, attribution) VALUES (?,'electricity_main','import',?,?,?,?,?,?)",
                      (bs, seq, sk, si, sx, band, attr))
        if night:
            grid = round(ev * 0.95, 6) if i % 6 else None               # kwh_grid NULL → kwh
            c.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
                      "imp_kwh_grid, imp_cost, imp_rate) VALUES (?,?,'ev_charger',1,?,?,?,?)",
                      (bs, bs, ev, grid, round(ev * rate, 6), rate if i % 10 else None))
            if i % 3 == 0:
                c.execute("INSERT INTO dispatch_history (slot_start, kind, energy_kwh, first_seen, "
                          "last_seen) VALUES (?, 'completed', ?, ?, ?)", (bs, -ev, bs, bs))
        if bat:
            c.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
                      "imp_kwh_grid, imp_cost, imp_rate) VALUES (?,?,'house_battery',1,?,?,?,?)",
                      (bs, bs, bat, round(bat * 0.8, 6), round(bat * rate, 6), rate_col))
    c.commit()
    st.save_settings({**(st.get_settings() or {}), "bill_rounding_summary": True})
    return st


def _local(utc_iso):
    return (datetime.fromisoformat(utc_iso).replace(tzinfo=timezone.utc)
            .astimezone(ZoneInfo(TZ)).replace(tzinfo=None))


def _j(x):
    return json.dumps(x, sort_keys=False, default=str)


# Whole range, one billing period, one day across the clock change, and an empty range.
RANGES = [(LO, HI), ("2026-09-30T23:00:00", "2026-10-31T23:00:00"),
          ("2026-10-24T23:00:00", "2026-10-26T00:00:00"), ("2027-01-01T00:00:00", "2027-02-01T00:00:00")]


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.st = _store()
        cls.cfg = cls.st.config_from_db(1)
        cls.cfg_no_ev = {**cls.cfg, "meters": {k: v for k, v in cls.cfg["meters"].items()
                                               if k != "ev_charger"}}


class TheSummary(Base):

    def _both(self, lo, hi, store=True):
        ps, pe = _local(lo), _local(hi)
        st = self.st if store else None
        tz = TZ if store else None
        old = billing.calculate_billing_summary_for_period(
            self.st.get_blocks_lightweight(lo, hi), ps, pe, store=st, tz_name=tz)
        new = billing.summarise_bill_rows(*self.st.get_bill_rows(lo, hi), ps, pe,
                                          store=st, tz_name=tz)
        return old, new

    def test_the_fixture_reaches_every_branch(self):
        """GUARD on the fixture: each branch the row path must match is exercised
        (read through the old path, which the parent has too)."""
        old = billing.calculate_billing_summary_for_period(
            self.st.get_blocks_lightweight(LO, HI), _local(LO), _local(HI),
            store=self.st, tz_name=TZ)
        self.assertTrue(old["ev_by_rate"] and old["home_by_rate"])
        # The ev/home transition rows came only from a no-segment block's column
        # bands, dropped in step 5c; no stored block reaches them now.
        self.assertTrue(any(v["is_submeter"] for v in old["totals"].values()))
        self.assertTrue(any(r < 0 for r in old["main_import_raw"]))
        self.assertEqual(len({round(v, 2) for v in old["standing"].values()}), 2)

    def test_rows_and_dicts_agree_to_the_bit(self):
        for lo, hi in RANGES:
            old, new = self._both(lo, hi)
            self.assertEqual(_j(bv.summary_json(new)), _j(bv.summary_json(old)), (lo, hi))

    def test_without_a_store_too(self):
        """The no-store total sums the blocks' own day nets — a second pass."""
        for lo, hi in RANGES[:3]:
            old, new = self._both(lo, hi, store=False)
            self.assertEqual(_j(bv.summary_json(new)), _j(bv.summary_json(old)), (lo, hi))

    def test_the_column_contract(self):
        """billing.py imports no storage, so the row layout is stated twice."""
        self.assertEqual(billing.BILL_ROW_COLUMNS, BlockStore.BILL_ROW_COLUMNS)
        rows, _ = self.st.get_bill_rows(LO, HI)
        self.assertEqual(tuple(rows[0].keys()), BlockStore.BILL_ROW_COLUMNS)


class TheOtherReaders(Base):
    """`_bill_blocks` against get_blocks_lightweight, through each reader."""

    def _pair(self, cfg, lo=LO, hi=HI):
        rows, segs = self.st.get_bill_rows(lo, hi)
        return self.st.get_blocks_lightweight(lo, hi), bv._bill_blocks(rows, segs, ec._ev_meter_id(cfg))

    def test_the_dispatch_map(self):
        for cfg in (self.cfg, self.cfg_no_ev):
            lw, slim = self._pair(cfg)
            for gated in (True, False):
                a = ec._dispatch_ev_slot_map(self.st, lw, cfg, gated=gated)
                b = ec._dispatch_ev_slot_map(self.st, slim, cfg, gated=gated)
                self.assertEqual(_j(b), _j(a))
            self.assertTrue(ec._dispatch_ev_slot_map(self.st, lw, cfg, gated=False))

    def test_the_hybrid_map(self):
        lw, slim = self._pair(self.cfg)
        a = ec._hybrid_ev_slot_map(self.st, lw, self.cfg)
        self.assertTrue(a)
        self.assertEqual(_j(ec._hybrid_ev_slot_map(self.st, slim, self.cfg)), _j(a))

    def test_the_bill_method(self):
        lw, slim = self._pair(self.cfg)
        s = billing.calculate_billing_summary_for_period(lw, _local(LO), _local(HI),
                                                         store=self.st, tz_name=TZ)
        look = vat_calendar.rate_lookup(self.st.get_vat_calendar())
        kw = dict(period_vat=0.05, standing_inc_by_day=s["standing"], vat_at=look)
        a = ec._bill_method_breakdown(lw, **kw)
        self.assertTrue(a)
        self.assertEqual(_j(ec._bill_method_breakdown(slim, **kw)), _j(a))

    def test_the_day_list(self):
        lw, slim = self._pair(self.cfg)
        self.assertEqual(bv._local_days(slim, ZoneInfo(TZ)), bv._local_days(lw, ZoneInfo(TZ)))


class TheResponse(Base):
    """period_billing's whole answer, new path against the old composition."""

    def _old(self, cfg, lo, hi):
        blocks = self.st.get_blocks_lightweight(lo, hi)
        ctx = bv._context(self.st, cfg, blocks)
        summary = ec.period_summary(ctx, blocks, blocks, _local(lo), _local(hi),
                                    store=self.st, tz_name=TZ)
        return summary, ctx

    def test_summary_card_and_context(self):
        """Passes on the parent too, where period_summary_raw IS the old composition;
        the regression test for the change of path is the next one."""
        import bill_card
        for cfg in (self.cfg, self.cfg_no_ev):
            for lo, hi in RANGES:
                old, octx = self._old(cfg, lo, hi)
                new, nctx, _ = bv.period_summary_raw(self.st, cfg, TZ, lo, hi)
                self.assertEqual(_j(bv.summary_json(new)), _j(bv.summary_json(old)), (lo, hi))
                self.assertEqual(_j(bill_card.card(new, currency="£")),
                                 _j(bill_card.card(old, currency="£")))
                self.assertEqual(_j(nctx), _j(octx))

    def test_it_no_longer_builds_the_block_dicts(self):
        """The point of #48: with a configuration, a period bill never calls
        get_blocks_lightweight. Fails on the parent, which always did."""
        calls = []
        real = BlockStore.get_blocks_lightweight

        def _spy(self_, *a, **kw):
            calls.append(a)
            return real(self_, *a, **kw)
        BlockStore.get_blocks_lightweight = _spy
        try:
            bv.period_billing(self.st, self.cfg, TZ, LO, HI, {"kind": "year"})
        finally:
            BlockStore.get_blocks_lightweight = real
        self.assertEqual(calls, [])

    def test_with_no_configuration_it_keeps_the_full_fetch(self):
        """GUARD: billing_context colours meters from the blocks when there is no
        configuration, so that case reads them as it always did."""
        new, _, blocks = bv.period_summary_raw(self.st, {}, TZ, LO, HI)
        self.assertIn("totals", blocks[0])          # get_blocks_lightweight's shape


class TheDictAdapter(unittest.TestCase):
    """The dict API keeps its old behaviour on inputs the store never produces."""

    def _blk(self, imp, **extra):
        ch = {"kwh": 1.0, "cost": 0.30, "rate": 0.30, **imp}
        return {"start": "2026-01-01T02:00:00", "_timezone": "UTC",
                "meters": {"electricity_main": {"meta": {"timezone": "UTC"}, "standing_charge": 0.5,
                                                "channels": {"import": ch, **extra}}}}

    def _sum(self, blocks):
        return billing.calculate_billing_summary_for_period(
            blocks, datetime(2026, 1, 1), datetime(2026, 1, 2), tz_name=None)

    def test_kwh_total_and_rate_used_win(self):
        """GUARD: the engine's live blocks carry kwh_total and rate_used."""
        s = self._sum([self._blk({"kwh_total": 2.0, "rate_used": 0.25})])
        self.assertEqual(s["main_import_raw"], {0.25: {"kwh": 2.0, "cost": 0.3}})

    def test_a_stored_remainder_is_not_read(self):
        """Step 5 (8 Oct 2026): the bill's Direct is direct_import's rule, so a stored remainder —
        even one that will not convert, which used to skip the meter line — changes nothing."""
        s = self._sum([self._blk({"kwh_remainder": None})])
        self.assertEqual(s["main_import_raw"][0.3]["kwh"], 1.0)
        self.assertIn("Electricity Main / Import", s["meters"])

    def test_an_empty_channel_still_opens_a_zero_rate_line(self):
        """GUARD: an empty channel dict reads as zeros, as `.get` on {} did."""
        s = self._sum([self._blk({}, export={})])
        self.assertIn("Electricity Main / Export", s["meters"])
        self.assertEqual(s["total_cost"], round(0.30 + 0.5, 2))


if __name__ == "__main__":
    unittest.main()
