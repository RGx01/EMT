"""
C acceptance 7 — billing totals reconcile with v4 to the penny.

`.build/reconcile_v4.py` runs the real check: v4.5.16's own `compute_period_net`
(the Billing card), loaded from a v4 checkout, against v5's surfaces on a real
database. First run, 30 Sep 2026, 80 periods over three years: the card and the
local-day client path matched to the penny everywhere; /api/energy's period
`net_cost` — `_aggregate_usage` rounding once over the period — was up to 3p out in
49 of 80. It now comes from the same day rule, and all 80 match.

The suite cannot hold a real database or a v4 checkout, so it pins the PROPERTY on
synthetic data: the day rule over local-day rollups equals compute_period_net, in a
BST month and across a clock change, where once-over-the-period rounding does not.
"""
import ast
import os
import random
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, ".build"))

import billing                                  # noqa: E402
from block_store import BlockStore              # noqa: E402

TZ = "Europe/London"


def _store(days, start="2026-10-10T23:00:00", seed=7):
    """A main meter with random half-hourly costs over `days` local days, the same
    daily standing charge on every block, and some export."""
    st = BlockStore(os.path.join(tempfile.mkdtemp(), "blocks.db"))
    st._conn.execute("PRAGMA foreign_keys=OFF")
    st._conn.execute("INSERT INTO config_periods (id, effective_from, billing_day, "
                     "block_minutes, timezone) VALUES (1, '2020-01-01T00:00:00', 1, 30, ?)", (TZ,))
    st._conn.execute("INSERT INTO meters (meter_id, config_period_id, is_sub_meter) "
                     "VALUES ('electricity_main', 1, 0), ('ev_charger', 1, 1)")
    rnd = random.Random(seed)
    t = datetime.fromisoformat(start)
    for i in range(days * 48 + 2):                 # +2 covers the 25-hour day
        bs = (t + timedelta(minutes=30 * i)).isoformat()
        st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
            "imp_cost, exp_kwh, exp_cost, standing_charge) VALUES (?,?,'electricity_main',1,"
            "?,?,?,?,?)", (bs, bs, 1.0, round(rnd.uniform(0, 0.4), 6), 0.2,
                           round(rnd.uniform(0, 0.03), 6), 0.6123))
        # a device on the same slot: attributes supply, never adds to the bill
        st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
            "imp_cost) VALUES (?,?,'ev_charger',1,?,?)", (bs, bs, 0.5, round(rnd.uniform(0, 0.2), 6)))
    st._conn.commit()
    return st


class TheDayRuleIsTheBill(unittest.TestCase):

    def _check(self, st, s, e):
        want = round(st.compute_period_net(s, e, TZ), 2)
        got = billing.bill_net_from_day_rollups(st.get_day_rollups(s, e, TZ))
        self.assertEqual(round(got * 100), round(want * 100))

    def test_across_the_clocks_back_day(self):
        """25 Oct 2026: a 25-hour local day, three weeks either side."""
        st = _store(30)
        self._check(st, "2026-10-10T23:00:00", "2026-11-09T00:00:00")

    def test_over_many_random_periods(self):
        st = _store(60, seed=11)
        base = datetime(2026, 10, 10, 23, 0)
        rnd = random.Random(3)
        for _ in range(25):
            a = base + timedelta(days=rnd.randint(0, 40))
            b = a + timedelta(days=rnd.randint(1, 19))
            self._check(st, a.isoformat(), b.isoformat())

    def test_once_over_the_period_rounding_is_what_it_replaces(self):
        """GUARD on the fixture: rounding once over the period really does differ
        here, so the tests above discriminate between the two rules."""
        st = _store(60, seed=11)
        base = datetime(2026, 10, 10, 23, 0)
        rnd = random.Random(3)
        differs = 0
        for _ in range(25):
            a = base + timedelta(days=rnd.randint(0, 40))
            b = (a + timedelta(days=rnd.randint(1, 19))).isoformat()
            a = a.isoformat()
            rows = [r for r in st.get_day_rollups(a, b, TZ) if r["meter_id"] == "electricity_main"]
            once = round(sum(r["imp_cost"] + r["standing_charge"] - r["exp_cost"] for r in rows), 2)
            differs += round(once * 100) != round(st.compute_period_net(a, b, TZ) * 100)
        self.assertGreater(differs, 0)


class TheRouteUsesTheDayRule(unittest.TestCase):

    def test_period_net_cost_comes_from_the_day_rule(self):
        """REGRESSION: /api/energy?resolution=period returned _aggregate_usage's
        once-rounded net_cost (up to 3p from v4 in 49 of 80 real periods)."""
        tree = ast.parse(open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read())
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "api_energy")
        src = ast.unparse(fn)
        self.assertIn('out[\'usage\'][\'net_cost\'] = bill_net_from_day_rollups(', src)
        self.assertIn("store.get_day_rollups(utc_start, utc_end, tz_name)", src)


class TheTool(unittest.TestCase):
    """GUARD: the reconciliation tool runs end to end. v5's own tree stands in for
    the v4 checkout, so this proves the plumbing, not agreement with v4."""

    def test_it_runs_and_prints_no_money_by_default(self):
        import reconcile_v4
        st = _store(35)
        path = st._path
        st._conn.close()
        report, ok = reconcile_v4.run(_ROOT, path)
        self.assertTrue(ok, report)
        self.assertIn("verdict: PASS", report)
        self.assertNotRegex(report, r"v4 \d+\.\d\d")


# A stand-in for a v4 checkout: v5's store with v4.5.16's import rule, which rebuilds a
# day's import as round(direct, 4) + Σ round(sub, 4), direct clamped at zero. BREAK adds
# a penny a day, a difference that rule does not explain.
_V4_STANDIN = """
import sys
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import block_store as _v5
BREAK = %r


class BlockStore(_v5.BlockStore):
    def compute_period_net(self, utc_s, utc_e, tz_name, finalised_only=False):
        tz = ZoneInfo(tz_name)
        main = defaultdict(float); exp = defaultdict(float); sc = {}; first = {}
        subs = defaultdict(lambda: defaultdict(float))
        for bs, ic, ec, s, sub, mid in self._conn.execute(
                "SELECT b.block_start, b.imp_cost, b.exp_cost, b.standing_charge, "
                "m.is_sub_meter, m.meter_id FROM blocks b JOIN meters m "
                "ON m.meter_id = b.meter_id AND m.config_period_id = b.config_period_id "
                "WHERE b.block_start >= ? AND b.block_start < ?", (utc_s, utc_e)):
            d = datetime.fromisoformat(bs).replace(tzinfo=timezone.utc).astimezone(tz).date()
            if sub:
                subs[d][mid] += float(ic or 0)
                continue
            main[d] += float(ic or 0)
            exp[d] += float(ec or 0)
            s = float(s or 0)
            if s > 0 and (d not in first or bs < first[d]):
                first[d], sc[d] = bs, s
        nets = []
        for d in sorted(main):
            raw = list(subs[d].values())
            dm = main[d] - sum(raw)
            direct = round(dm if main[d] < 0 else max(0.0, dm), 4)
            imp = round(direct + sum(round(v, 4) for v in raw), 4)
            nets.append(round(imp + round(sc.get(d, 0.0), 4) - round(exp[d], 4), 2) + BREAK)
        return round(sum(nets), 2)
"""

# 12 Oct 2026 (local): costs at which rounding each sub-meter on its own tips the day's
# total across a half-penny — v5 rounds 1.2048..., v4 rebuilds 1.2049..., 1.20 vs 1.21.
_FLIP = {"electricity_main": 1.204875, "ev_charger": 0.1000655, "house_battery": 0.2000565}


def _flip_store():
    st = BlockStore(os.path.join(tempfile.mkdtemp(), "blocks.db"))
    st._conn.execute("PRAGMA foreign_keys=OFF")
    st._conn.execute("INSERT INTO config_periods (id, effective_from, billing_day, "
                     "block_minutes, timezone) VALUES (1, '2020-01-01T00:00:00', 1, 30, ?)", (TZ,))
    st._conn.execute("INSERT INTO meters (meter_id, config_period_id, is_sub_meter) VALUES "
                     "('electricity_main', 1, 0), ('ev_charger', 1, 1), ('house_battery', 1, 1)")
    for mid, cost in _FLIP.items():
        st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                         "imp_kwh, imp_cost) VALUES ('2026-10-12T09:00:00', '2026-10-12T09:00:00', "
                         "?, 1, 1.0, ?)", (mid, cost))
    st._conn.commit()
    path = st._path
    st._conn.close()
    return path


class TheToolExplainsSubMeterRounding(unittest.TestCase):
    """3 Oct 2026: on a production-like database the tool failed 6 of 81 periods by 1p.
    Each was one day where v4's sub-meter rebuild rounded across a half-penny and v5's
    main-meter rule did not; the tool now explains such a period instead of failing it."""

    def _v4(self, brk=0.0):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "block_store.py"), "w") as f:
            f.write(_V4_STANDIN % brk)
        return d

    def test_the_fixture_flips_a_penny(self):
        """GUARD on the fixture: v4's rule and v5's really are a penny apart here."""
        import reconcile_v4
        v4 = reconcile_v4._load(os.path.join(self._v4(), "block_store.py"), "v4_standin_a")
        s, e = "2026-10-11T23:00:00", "2026-10-12T23:00:00"
        a = round(v4.BlockStore(_flip_store()).compute_period_net(s, e, TZ) * 100)
        b = round(BlockStore(_flip_store()).compute_period_net(s, e, TZ) * 100)
        self.assertEqual(a - b, 1)

    def test_a_sub_meter_rounding_difference_passes_as_explained(self):
        """Fails on the parent tree, which reported FAIL here."""
        import reconcile_v4
        report, ok = reconcile_v4.run(self._v4(), _flip_store())
        self.assertTrue(ok, report)
        self.assertIn("explained: v4 rebuilds the import from sub-meters", report)
        self.assertIn("0 of ", report)
        self.assertRegex(report, r"; [1-9]\d* by v4's sub-meter rebuild")

    def test_any_other_difference_still_fails(self):
        """GUARD: a penny the rebuild does not explain fails, as it always did."""
        import reconcile_v4
        report, ok = reconcile_v4.run(self._v4(brk=0.01), _flip_store())
        self.assertFalse(ok, report)
        self.assertIn("verdict: FAIL", report)


if __name__ == "__main__":
    unittest.main()
