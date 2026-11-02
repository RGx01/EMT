"""
Carbon intensity per half-hour: the GRID's gCO2/kWh at that time — a property of the
grid, not a usage-derived ratio. A zero-net block therefore still has one.

The rule was v4's, inside the pre-rendered heatmap (`energy_charts.generate_net_heatmap`,
deleted 30 Sep 2026 when the Heatmaps tab moved into the browser). It now lives in the
payload — `BlockStore.get_energy_blocks`, what /api/energy?resolution=block serves — so
every surface gets the same number and it can be tested without a browser. The four
cases are v4's, unchanged:

  * stored carbon_intensity_g is used directly when present (net != 0) — the same
    number the old carbon_g/net path produced, so populated cells do not move;
  * a ZERO-NET block with a stored intensity renders it;
  * a pre-3.0.0 block without the stored column falls back to carbon_g / net;
  * a zero-net block with no intensity data stays empty — a grid intensity that was
    never captured cannot be invented.
"""
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from block_store import BlockStore          # noqa: E402

FROM, TO = "2025-05-17T00:00:00", "2025-05-18T00:00:00"


class IntensityInThePayload(unittest.TestCase):

    def setUp(self):
        self.st = BlockStore(os.path.join(tempfile.mkdtemp(), "blocks.db"))
        self.st._conn.execute("PRAGMA foreign_keys=OFF")
        self.addCleanup(self.st.close)

    def blk(self, start, imp, exp, carbon_g, intensity):
        self.st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
            "exp_kwh, carbon_g, carbon_intensity_g) VALUES (?, ?, 'electricity_main', 1, ?, ?, ?, ?)",
            (start, start, imp, exp, carbon_g, intensity))
        self.st._conn.commit()

    def intensity(self, start):
        return next(b["carbon_intensity_g"] for b in self.st.get_energy_blocks(FROM, TO)
                    if b["block_start"] == start)

    def test_stored_intensity_used_when_net_nonzero(self):
        self.blk("2025-05-17T02:00:00", 5.0, 0.0, 750.0, 150.0)
        self.assertEqual(self.intensity("2025-05-17T02:00:00"), 150.0)

    def test_populated_cell_value_unchanged_vs_derived(self):
        """GUARD: the stored value equals what carbon_g/net gives, so nothing moves."""
        self.blk("2025-05-17T02:00:00", 5.0, 0.0, 750.0, 150.0)
        self.assertEqual(self.intensity("2025-05-17T02:00:00"), round(750.0 / 5.0, 1))

    def test_zero_net_block_renders_stored_intensity(self):
        self.blk("2025-05-17T03:00:00", 1.0, 1.0, 0.0, 120.0)
        self.assertEqual(self.intensity("2025-05-17T03:00:00"), 120.0)

    def test_pre_v3_block_falls_back_to_derived(self):
        """REGRESSION (30 Sep 2026): the payload served the raw column, so a
        pre-3.0.0 block had no intensity at all once the heatmap stopped deriving it."""
        self.blk("2025-05-17T04:00:00", 4.0, 0.0, 400.0, None)
        self.assertEqual(self.intensity("2025-05-17T04:00:00"), 100.0)

    def test_an_export_slot_is_never_a_negative_intensity(self):
        """carbon.slot_intensity takes abs(): an export slot stores a negative value,
        and the grid's intensity is not negative. (A first draft of this payload
        re-derived the rule in SQL and missed it — it is ONE function now.)"""
        self.blk("2025-05-17T06:00:00", 0.0, 2.0, -280.08, -140.04)
        self.assertEqual(self.intensity("2025-05-17T06:00:00"), 140.0)

    def test_zero_net_without_intensity_stays_empty(self):
        self.blk("2025-05-17T05:00:00", 1.0, 1.0, 0.0, None)
        self.assertIsNone(self.intensity("2025-05-17T05:00:00"))

    def test_the_browser_draws_what_the_payload_says(self):
        """GUARD: the Heatmaps module does not re-derive intensity or blank a zero-net
        cell (it did, briefly, in its first draft)."""
        src = open(os.path.join(_ROOT, "web", "templates", "heatmap_module.html"),
                   encoding="utf-8").read()
        self.assertIn("c.int = b.carbon_intensity_g", src)
        self.assertNotIn("Math.abs(net) > 1e-9)\n          c.int", src)


if __name__ == "__main__":
    unittest.main()
