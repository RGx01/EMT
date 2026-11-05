"""
Settings' export displacement intensity, honoured (5 Nov 2026).

Settings → Export Carbon Offset offers the grid's average (recommended) or a custom fixed g/kWh. Since
v4 2.6.0 (20 Apr 2026) the choice was saved and never read: every export carbon figure was the export's
kWh at the half-hour's grid average, from the stored net carbon. Decided with the user: honour it.
`carbon.export_g_per_kwh` reads the choice; where it is `custom`, the export's grams are its kWh × the
custom g/kWh in the three places they are made — the Usage Stats rows (`usage_view._aggregate_tuples`:
the bars, the table's Export offset, Running total), Flow's brackets (`_rate_brackets`) and Insights
(`_aggregate_insights`) — and the net (import − export) moves by the same amount, so Net = Import −
Export holds. The page's config token moves with it, so an open Charts page refreshes.

Discriminating: the five not marked GUARD fail on the parent. The GUARDs pin the grid average as it
was; two of them (the choice's, the usage rows') call the new function and argument, so they cannot
run on the parent.
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import carbon                                       # noqa: E402
import usage_view                                   # noqa: E402
from tests.test_device_sensors_storage import _store    # noqa: E402

BS = "2026-06-01T12:00:00"
CUSTOM = {"co2_export_method": "custom", "co2_export_custom_intensity": 400.0}


class TheChoice(unittest.TestCase):

    def test_custom_is_its_intensity(self):
        self.assertEqual(carbon.export_g_per_kwh(CUSTOM), 400.0)
        self.assertEqual(carbon.export_g_per_kwh({"co2_export_method": "custom"}),
                         carbon.EXPORT_CUSTOM_DEFAULT_G)

    def test_GUARD_grid_average_is_none(self):
        # (a test of the new function, so it cannot run on the parent; it pins the default)
        for s in ({}, None, {"co2_export_method": "grid_average", "co2_export_custom_intensity": 400.0},
                  {"co2_export_method": "custom", "co2_export_custom_intensity": 0}):
            self.assertIsNone(carbon.export_g_per_kwh(s), s)


def _main_row(imp=2.0, exp=1.0, cg=100.0):
    # a half-hour drawing 2 kWh and exporting 1 at a 100 g/kWh grid: its stored NET carbon is 100 g
    return (BS, "electricity_main", 0, 1, imp, imp, 0.25, imp * 0.25, exp, exp * 0.15, 0.0, cg)


class TheUsageRows(unittest.TestCase):

    def _main(self, export_g):
        d = usage_view._aggregate_tuples([_main_row()], lambda bs: bs[:10], export_g=export_g)
        return usage_view._bucket_to_row_values(d["2026-06-01"])["meters"]["electricity_main"]

    def test_custom_export_grams_and_the_net_with_them(self):
        r = self._main(400.0)
        self.assertEqual((r["carbon_g_imp"], r["carbon_g_exp"]), (200.0, 400.0))
        self.assertAlmostEqual(r["carbon_g_imp"] - r["carbon_g_exp"], -200.0)

    def test_GUARD_grid_average_as_before(self):
        r = self._main(None)
        self.assertEqual((r["carbon_g_imp"], r["carbon_g_exp"]), (200.0, 100.0))


class TheBracketsAndInsights(unittest.TestCase):

    def setUp(self):
        self.st = _store()
        with self.st._conn:
            self.st._conn.execute(
                "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, imp_cost, "
                "exp_kwh, exp_rate, exp_cost, carbon_g) VALUES (?, ?, 'electricity_main', ?, 2.0, 0.5, "
                "1.0, 0.15, 0.15, 100.0)", (BS, BS, self.st.get_current_config_period_id()))

    def _export_g(self):
        b = usage_view._rate_brackets(self.st, "2026-06-01T00:00:00", "2026-06-02T00:00:00", lambda bs: bs)
        return sum(v[2] for v in b[BS]["export"].values())

    def test_flow_s_export_grams_follow_the_setting(self):
        self.st.save_settings(CUSTOM)
        self.assertAlmostEqual(self._export_g(), 400.0)

    def test_GUARD_flow_s_grid_average(self):
        self.assertAlmostEqual(self._export_g(), 100.0)


class TheServer(unittest.TestCase):
    """Insights, and the config token an open Charts page refreshes on."""

    def setUp(self):
        from tests.test_server import server
        self.server = server
        self.st = _store()
        with self.st._conn:
            self.st._conn.execute(
                "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, imp_cost, "
                "exp_kwh, exp_rate, exp_cost, carbon_g) VALUES (?, ?, 'electricity_main', ?, 2.0, 0.5, "
                "1.0, 0.15, 0.15, 100.0)", (BS, BS, self.st.get_current_config_period_id()))

    def _insights(self):
        cfg = {"meters": {"electricity_main": {"meta": {}, "channels": {"import": {}}}}}
        return self.server._aggregate_insights(self.st, cfg, "2026-06-01T00:00:00", "2026-06-02T00:00:00")

    def test_insights_export_offset_and_net_follow_the_setting(self):
        self.st.save_settings(CUSTOM)
        d = self._insights()
        self.assertEqual((d["carbon_g_imp"], d["carbon_g_exp"], d["carbon_g_net"]), (200.0, 400.0, -200.0))

    def test_GUARD_insights_grid_average(self):
        d = self._insights()
        self.assertEqual((d["carbon_g_imp"], d["carbon_g_exp"], d["carbon_g_net"]), (200.0, 100.0, 100.0))

    def test_the_config_token_moves_with_a_custom_choice(self):
        before = self.server._config_token(self.st)
        self.st.save_settings({"co2_export_method": "grid_average"})
        self.assertEqual(self.server._config_token(self.st), before)
        self.st.save_settings(CUSTOM)
        mid = self.server._config_token(self.st)
        self.assertNotEqual(mid, before)
        self.st.save_settings(dict(CUSTOM, co2_export_custom_intensity=450.0))
        self.assertNotEqual(self.server._config_token(self.st), mid)


if __name__ == "__main__":
    unittest.main()
