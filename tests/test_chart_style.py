"""
A meter's chart style, set on Settings (chart_style.py, 31 Oct 2026): the bar colour and the rate line
of the house (the main meter's import), its export, and each device.

What they pin:

- **The rule:** a colour is exactly `#rrggbb`, and a rate line is 'solid' or 'dashed'; anything
  else is dropped, because a colour reaches HTML. The rate line's colour is the bar mixed 30% toward
  white (a dark bar) or black (a light one). The page's JavaScript gives the same colours: the
  pairs below were read from `rateLineColor` in the preview.
- **The styles:** a chosen colour wins for a device, the house and export. Unset, a meter keeps its
  palette colour by position, and its rate line keeps its old style: export dashed, the rest solid.
- **The store:** a channel's style is saved and loaded with the config; junk is dropped at the
  write; an older database gains the columns on open.
- **The config token moves on a style change,** so the Charts page's kept rows (colours with them)
  are re-fetched. With no style set it is unchanged.
- **The surfaces** take the style: the Billing day chart's rate line draws `rate_dashed`, and the
  settings page offers a style on each device tab and two on Main meter.

Every test fails on the parent, where chart_style.py, the columns and the section do not exist.
test_unset_keeps_the_palette_by_position is a GUARD in substance: the colours it expects are the
parent's own, so no one's charts change until a style is chosen.
"""
import os
import re
import sqlite3
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import chart_style as CS                     # noqa: E402
import energy_charts as EC                   # noqa: E402
from block_store import BlockStore           # noqa: E402

PERIOD = "2026-01-01T00:00:00"

# rateLineColor in meter_config.html, read in the preview on 31 Oct 2026
JS_PAIRS = [("#2e8b57", "#6dae89"), ("#1f77b4", "#62a0cb"), ("#e377c2", "#9f5388"), ("#ff7f0e", "#b3590a"),
            ("#7f7f7f", "#a5a5a5"), ("#8c564b", "#af8981"), ("#bcbd22", "#848418"), ("#d62728", "#e26869"),
            ("#9467bd", "#b495d1"), ("#17becf", "#108591"), ("#000000", "#4d4d4d"), ("#ffffff", "#b3b3b3"),
            ("#808080", "#5a5a5a")]


def _cfg(house=None, export=None, ev=None, pump=None):
    return {"meters": {
        "electricity_main": {"meta": {"timezone": "UTC"},
                             "channels": {"import": dict(house or {}), "export": dict(export or {})}},
        "ev_charger": {"meta": {"sub_meter": True, "parent_meter": "electricity_main", "device": "Charger",
                                "meter_type": "ev"}, "channels": {"import": dict(ev or {})}},
        "heat_pump": {"meta": {"sub_meter": True, "parent_meter": "electricity_main", "device": "Pump",
                               "meter_type": "heat_pump"}, "channels": {"import": dict(pump or {})}}}}


class TheRule(unittest.TestCase):

    def test_only_an_exact_colour_and_line_are_kept(self):
        self.assertEqual(CS.clean_color("#2E8B57"), "#2e8b57")
        for junk in ("red", "#2e8b5", "#2e8b577", "#2e8b57;background:url(x)", "", None, 7):
            self.assertIsNone(CS.clean_color(junk), junk)
        self.assertEqual([CS.clean_rate_line(v) for v in ("solid", "dashed", "dotted", None)],
                         ["solid", "dashed", None, None])

    def test_the_rate_line_matches_the_pages_javascript(self):
        for bar, line in JS_PAIRS:
            self.assertEqual(CS.rate_line_color(bar), line, bar)


class TheStyles(unittest.TestCase):

    def test_unset_keeps_the_palette_by_position(self):
        """GUARD: with no style chosen, every meter keeps the colour it had."""
        st = EC.build_meter_styles(_cfg())
        self.assertEqual({m: s["bar"] for m, s in st.items()},
                         {"electricity_main": "#1f77b4", "ev_charger": "#e377c2", "heat_pump": "#ff7f0e",
                          "electricity_main_export": "#7f7f7f"})

    def test_unset_lines_are_dashed_for_export_only(self):
        st = EC.build_meter_styles(_cfg())
        self.assertEqual({m: s["dashed"] for m, s in st.items()},
                         {"electricity_main": False, "ev_charger": False, "heat_pump": False,
                          "electricity_main_export": True})

    def test_a_chosen_style_wins_for_each_kind_of_meter(self):
        st = EC.build_meter_styles(_cfg(house={"bar_color": "#111111", "rate_line": "dashed"},
                                        export={"bar_color": "#eeeeee", "rate_line": "solid"},
                                        pump={"bar_color": "#2e8b57"}))
        self.assertEqual((st["electricity_main"]["bar"], st["electricity_main"]["dashed"]), ("#111111", True))
        self.assertEqual((st["electricity_main_export"]["bar"], st["electricity_main_export"]["dashed"]),
                         ("#eeeeee", False))
        self.assertEqual((st["heat_pump"]["bar"], st["heat_pump"]["rate"]), ("#2e8b57", "#6dae89"))
        self.assertEqual(st["ev_charger"]["bar"], "#e377c2")                 # untouched keeps its own
        self.assertEqual(EC.build_meter_colors_from_config(_cfg(pump={"bar_color": "#2e8b57"}))["heat_pump"],
                         "#2e8b57")

    def test_junk_on_the_config_falls_back_to_the_palette(self):
        st = EC.build_meter_styles(_cfg(ev={"bar_color": "red\"><script>", "rate_line": "dotted"}))
        self.assertEqual((st["ev_charger"]["bar"], st["ev_charger"]["dashed"]), ("#e377c2", False))


class TheStore(unittest.TestCase):

    def _store(self, cfg):
        st = BlockStore(":memory:")
        st.insert_config_period(cfg, effective_from=PERIOD)
        return st

    def _loaded(self, st):
        return st.config_from_db(st.get_current_config_period_id())["meters"]

    def test_a_style_is_saved_and_loaded_with_the_config(self):
        st = self._store(_cfg(export={"bar_color": "#ABCDEF", "rate_line": "solid"},
                              pump={"bar_color": "#2e8b57", "rate_line": "dashed"}))
        m = self._loaded(st)
        self.assertEqual((m["electricity_main"]["channels"]["export"]["bar_color"],
                          m["electricity_main"]["channels"]["export"]["rate_line"]), ("#abcdef", "solid"))
        self.assertEqual((m["heat_pump"]["channels"]["import"]["bar_color"],
                          m["heat_pump"]["channels"]["import"]["rate_line"]), ("#2e8b57", "dashed"))
        self.assertNotIn("bar_color", m["ev_charger"]["channels"]["import"])

    def test_junk_is_dropped_at_the_write(self):
        st = self._store(_cfg(ev={"bar_color": "url(x)", "rate_line": "zigzag"}))
        row = st._conn.execute("SELECT bar_color, rate_line FROM meter_channels mc JOIN meters m "
                               "ON m.id = mc.meter_id WHERE m.meter_id = 'ev_charger'").fetchone()
        self.assertEqual((row["bar_color"], row["rate_line"]), (None, None))

    def test_an_older_database_gains_the_columns_on_open(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "blocks.db")
        BlockStore(p)._conn.close()
        con = sqlite3.connect(p)
        con.execute("ALTER TABLE meter_channels DROP COLUMN bar_color")
        con.execute("ALTER TABLE meter_channels DROP COLUMN rate_line")
        con.commit(); con.close()
        st = BlockStore(p)
        cols = {r[1] for r in st._conn.execute("PRAGMA table_info(meter_channels)")}
        self.assertTrue({"bar_color", "rate_line"} <= cols)


class TheConfigToken(unittest.TestCase):

    def _token(self, st):
        sys.path.insert(0, os.path.join(_ROOT, "web"))
        from tests.test_server import server
        return server._config_token(st)

    def _restyle(self, st, cfg):
        pid = st.get_current_config_period_id()
        with st._conn:
            for (mid,) in st._conn.execute("SELECT id FROM meters WHERE config_period_id=?", (pid,)):
                st._conn.execute("DELETE FROM meter_channels WHERE meter_id=?", (mid,))
            st._conn.execute("DELETE FROM meters WHERE config_period_id=?", (pid,))
            st._write_meters(cfg, pid)

    def test_a_style_change_moves_it_and_none_keeps_its_old_shape(self):
        st = BlockStore(":memory:")
        st.insert_config_period(_cfg(), effective_from=PERIOD)
        plain = self._token(st)
        self.assertEqual(plain.count("/"), 3)                    # the shape it had before styles
        self._restyle(st, _cfg(pump={"bar_color": "#2e8b57"}))
        green = self._token(st)
        self._restyle(st, _cfg(pump={"bar_color": "#2e8b57", "rate_line": "dashed"}))
        dashed = self._token(st)
        self.assertEqual(len({plain, green, dashed}), 3)
        self._restyle(st, _cfg())
        self.assertEqual(self._token(st), plain)


class TheSurfaces(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        rd = lambda *p: open(os.path.join(_ROOT, *p), encoding="utf-8").read()   # noqa: E731
        cls.page, cls.bill, cls.view = (rd("web", "templates", "meter_config.html"),
                                        rd("web", "templates", "billing_module.html"), rd("billing_view.py"))

    def test_the_day_chart_draws_the_chosen_line(self):
        self.assertIn("borderDash: m.rate_dashed ? [6, 4] : []", self.bill)
        self.assertIn('rate_dashed=ctx["meter_rate_dashed"]', self.view)

    def test_each_device_and_the_main_meter_offer_a_style(self):
        self.assertIn("buildChartStyle(meterId, 'import', meta.device || 'Device')", self.page)
        self.assertIn("buildChartStyle(meterId, 'import', 'Direct import (the house)')", self.page)
        self.assertIn("buildChartStyle(meterId, 'export', 'Export')", self.page)
        self.assertTrue(re.search(r"var CHART_PALETTE = \{\{ chart_palette \| tojson \}\};", self.page))


if __name__ == "__main__":
    unittest.main()
