"""
The EV split reconciles against `block_segments`, NOT against `imp_cost_remainder`.

WHY THIS FILE EXISTS. Three tests currently assert the EV split via
`imp_cost_ev + imp_cost_remainder == imp_cost` (`test_unit_guard`,
`test_reconcile_write_accessors`, `test_measured_apply`). That identity is the
only redundancy in the schema that can catch a miscomputed EV cost -- but it is
anchored on a column that four writers define differently:

  * `engine._apply_pass2`         -> remainder_kwh x rate  (after EV **and** sub-meters)
  * `block_store._EV_RESTAMP_SQL` -> (imp_kwh - imp_kwh_ev) x rate  (after EV only)
  * `apply_measured_to_main`      -> caller-supplied, EV-only form
  * (historically, pre-2024-08)   -> imp_cost - SUM(sub costs)

Measured on production: of 40,007 main rows carrying the column, 169 contradict
their own `imp_kwh_remainder` companion, and a further 2,603 sit within 0.01p of
it for reasons not fully explained. No pricing path reads the column, so nothing
ever forced the writers to agree.

`imp_cost_remainder` is therefore retired as a stored value (dropped in step 5,
10 Oct 2026, which also retired this file's "does not read the column" test). Deriving it
(`imp_kwh_remainder x imp_rate`) would turn the identity above into
`cost_ev + (cost - cost_ev) == cost` -- true by algebra, catching nothing. So the
check is re-anchored here onto `block_segments`: an independent record of the
same pricing at finer granularity, per-band rows carrying their own kwh, rate and
attribution, written by the pricing path rather than by the column writers.

MEASURED ON PRODUCTION before adoption, over 37,027 segmented main blocks:
    SUM(seg.kwh  WHERE attribution='ev') == imp_kwh_ev   -> 0 violations
    SUM(seg.cost WHERE attribution='ev') == imp_cost_ev  -> 0 violations
    SUM(seg.cost over all segments)      == imp_cost     -> 0 violations (max 0.16p)

COVERAGE, stated honestly: segments exist on 64.0% of main rows (37,027 of
57,891) -- the backfill is incomplete, so this invariant is silent on unsegmented
history. It is NOT silent where it matters: 560 of the 562 blocks carrying an EV
split have EV segments. The two that do not are a known blind spot, not a pass.

Step 5c (10 Oct 2026): imp_kwh_ev / imp_cost_ev are dropped -- the EV segments ARE
the split -- so the two EV legs (and the NULL-column leg) have nothing left to
compare and went with their negative cases. The block-level anchor stays.
"""
import os
import tempfile
import unittest

from block_store import BlockStore

# The block-level anchor is loose because it sums every segment and so accumulates
# the most rounding (worst observed on prod: 0.16p).
TOL_BLOCK_COST = 5e-3


def ev_split_discrepancies(store, block_start, meter_id="electricity_main"):
    """Return human-readable invariant breaches for one block; [] == reconciles.

    Returns findings rather than asserting, so a repair sweep or the correction
    tool can reuse it without dragging unittest in. Blocks with no segments
    return [] -- absence is a backfill gap, not a breach, and conflating the two
    would make the invariant lie about its coverage.
    """
    segs = store.get_block_segments(block_start, meter_id)
    if not segs:
        return []

    row = store._conn.execute(
        "SELECT imp_cost FROM blocks "
        "WHERE block_start = ? AND meter_id = ?", (block_start, meter_id)).fetchone()
    if row is None:
        return ["%s: segments exist but no block row" % block_start]

    out = []
    all_cost = sum(s["kwh"] * s["inc_rate"] for s in segs)

    if row["imp_cost"] is not None and abs(all_cost - row["imp_cost"]) > TOL_BLOCK_COST:
        out.append("%s: block cost segments=%.5f imp_cost=%.5f"
                   % (block_start, all_cost, row["imp_cost"]))
    return out


class _Base(unittest.TestCase):
    """A main block priced in two bands, part of it attributed to the EV."""

    RATE_OFF = 0.05493
    RATE_DAY = 0.32309

    def setUp(self):
        self.tmp = os.path.join(tempfile.mkdtemp(), "blocks.db")
        self.store = BlockStore(self.tmp)
        with self.store._conn:
            self.store._conn.execute(
                "INSERT INTO config_periods (billing_day, block_minutes, timezone, "
                "currency_symbol, currency_code, effective_from) "
                "VALUES (1, 30, 'Europe/London', '£', 'GBP', '2026-01-01')")
            self.cp = self.store._conn.execute(
                "SELECT id FROM config_periods LIMIT 1").fetchone()[0]

        self.bs = "2026-04-07T02:00:00"
        self.ev_kwh, self.house_kwh = 4.0, 2.0
        self.ev_cost    = round(self.ev_kwh * self.RATE_OFF, 6)
        self.house_cost = round(self.house_kwh * self.RATE_DAY, 6)
        self.total_cost = round(self.ev_cost + self.house_cost, 6)

        self._write_block()
        self.store.set_block_segments(self.bs, "electricity_main", [
            (self.ev_kwh,    self.RATE_OFF, None, "off_peak", "ev"),
            (self.house_kwh, self.RATE_DAY, None, "day",      "house"),
        ])

    def _write_block(self, cost=None):
        with self.store._conn:
            self.store._conn.execute(
                "INSERT OR REPLACE INTO blocks (block_start, block_end, meter_id, "
                "config_period_id, imp_kwh, imp_rate, imp_cost, "
                "exp_kwh, exp_cost, standing_charge) "
                "VALUES (?, '2026-04-07T02:30:00', 'electricity_main', ?, ?, ?, ?, "
                "0.0, 0.0, 0.0)",
                (self.bs, self.cp, self.ev_kwh + self.house_kwh, self.RATE_DAY,
                 self.total_cost if cost is None else cost))

    def tearDown(self):
        self.store._conn.close()
        if os.path.exists(self.tmp):
            os.remove(self.tmp)


class TestEvSplitReconcilesToSegments(_Base):

    def test_clean_block_has_no_discrepancies(self):
        self.assertEqual(ev_split_discrepancies(self.store, self.bs), [])

    def test_unsegmented_block_is_silent_not_clean(self):
        """A backfill gap must not read as a pass -- see the coverage note above."""
        self.store.set_block_segments(self.bs, "electricity_main", [])
        self.assertEqual(ev_split_discrepancies(self.store, self.bs), [],
                         "no segments -> no finding")
        self.assertEqual(self.store.get_block_segments(self.bs, "electricity_main"), [],
                         "and the caller can tell the difference by asking")


class TestInvariantHasTeeth(_Base):
    """Negative cases. An invariant that cannot fail is not evidence.

    The check this file replaces passed on a fixture where both candidate
    implementations produced the same number, so it distinguished nothing.
    """

    def test_block_cost_drifting_from_its_segments_is_caught(self):
        self._write_block(cost=self.total_cost + 0.10)
        self.assertTrue(any("block cost" in x for x in
                            ev_split_discrepancies(self.store, self.bs)))


if __name__ == "__main__":
    unittest.main()
