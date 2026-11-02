"""
BL-101 PR 4 (1 Nov 2026, home eras design §4) — the bar colours modal behind the Charts ⚙︎.

Every meter's chart style in one place, as each one's Settings tab has it: the house (the main
meter's import), each device, export, and the EV with no device where the account has had one
(BL-97). `GET /api/chart-styles` lists them in the charts' order; `POST` writes the chosen ones
straight to the current config period's channels (`chart_style.apply_styles`, on the engine thread)
— no engine restart, as a config save makes — and the EV's to the store's meta. The page's change
poll counts the channels' styles (`_config_token`), so the charts follow; the modal also asks.

Checked in the local preview on 1 Nov 2026, on a scratch copy of staging: the modal from the ⚙︎, a
swatch for the battery saved and Usage Stats' battery colour changed with no reload, then cleared to
its default through the route.

Discriminating: every test fails on the parent tree.
"""
import importlib.util
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import chart_style                               # noqa: E402
from block_store import BlockStore              # noqa: E402

PAGE = open(os.path.join(_ROOT, "web", "templates", "charts.html"), encoding="utf-8").read()


def _store():
    st = BlockStore(":memory:")
    c = st._conn
    c.execute("INSERT OR IGNORE INTO config_periods (id, effective_from, billing_day, block_minutes, timezone) "
              "VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
    c.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter) VALUES (1,'electricity_main',0)")
    main = c.execute("SELECT id FROM meters WHERE meter_id='electricity_main'").fetchone()[0]
    c.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter, device_label) VALUES (1,'heat_pump',1,'Pump')")
    hp = c.execute("SELECT id FROM meters WHERE meter_id='heat_pump'").fetchone()[0]
    for mid, ch in ((main, "import"), (main, "export"), (hp, "import")):
        c.execute("INSERT INTO meter_channels (meter_id, channel, read_sensor) VALUES (?, ?, 'sensor.x')", (mid, ch))
    c.commit()
    return st


def _style(st, meter, ch):
    return tuple(st._conn.execute(
        "SELECT mc.bar_color, mc.rate_line FROM meter_channels mc JOIN meters m ON m.id = mc.meter_id "
        "WHERE m.meter_id = ? AND mc.channel = ?", (meter, ch)).fetchone())


class TheWrite(unittest.TestCase):

    def test_a_style_set_and_cleared(self):
        st = _store()
        self.assertEqual(chart_style.apply_styles(st, 1, [("heat_pump", "import", "#2E8B57", "dashed")]), 1)
        self.assertEqual(_style(st, "heat_pump", "import"), ("#2e8b57", "dashed"))
        chart_style.apply_styles(st, 1, [("heat_pump", "import", None, None)])
        self.assertEqual(_style(st, "heat_pump", "import"), (None, None))

    def test_junk_is_cleaned_again(self):
        st = _store()
        chart_style.apply_styles(st, 1, [("electricity_main", "export", "red\"><script>", "dotted")])
        self.assertEqual(_style(st, "electricity_main", "export"), (None, None))

    def test_only_its_own_period(self):
        st = _store()
        self.assertEqual(chart_style.apply_styles(st, 2, [("heat_pump", "import", "#111111", "solid")]), 0)


class TheRoute(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("server_bl101c", os.path.join(_ROOT, "web", "server.py"))
        cls.srv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.srv)

    def setUp(self):
        self.st = _store()
        self.cfg = self.st.config_from_db(1)
        import energy_charts
        self.p = [mock.patch.object(self.srv, "load_config", lambda: self.cfg),
                  mock.patch.object(self.srv.store_access, "store", lambda: self.st),
                  mock.patch.object(self.srv, "_engine_write", lambda op: op(self.st)),
                  mock.patch.object(energy_charts, "has_had_ev", lambda s: False)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        self.st.close()

    def test_get_lists_each_meter_in_the_charts_order(self):
        d = self.srv.app.test_client().get("/api/chart-styles").get_json()
        self.assertEqual([s["key"] for s in d["styles"]], ["electricity_main", "heat_pump", "electricity_main_export"])
        self.assertEqual(d["styles"][0]["label"], "Direct import (the house)")
        self.assertEqual(d["styles"][1]["label"], "Pump")
        self.assertEqual(d["palette"], chart_style.PALETTE)

    def test_post_writes_each_to_its_channel(self):
        r = self.srv.app.test_client().post("/api/chart-styles", json={"styles": [
            {"key": "electricity_main", "bar_color": "#111111", "rate_line": "solid"},
            {"key": "electricity_main_export", "bar_color": "", "rate_line": "solid"},
            {"key": "heat_pump", "bar_color": "#2e8b57", "rate_line": "dashed"},
            {"key": "not_a_meter", "bar_color": "#222222"}]})
        self.assertEqual(r.get_json(), {"ok": True, "written": 3})
        self.assertEqual(_style(self.st, "electricity_main", "import"), ("#111111", "solid"))
        self.assertEqual(_style(self.st, "electricity_main", "export"), (None, "solid"))
        self.assertEqual(_style(self.st, "heat_pump", "import"), ("#2e8b57", "dashed"))


class ThePage(unittest.TestCase):

    def test_in_the_cog_menu(self):
        i = PAGE.index("function chartsCogMenu(")
        self.assertIn("Bar colours\\u2026', coloursOpen]", PAGE[i:i + 700])

    def test_saved_together_and_the_charts_follow(self):
        i = PAGE.index("function coloursSave(")
        f = PAGE[i:PAGE.index("\n}\n", i)]
        self.assertIn("fetch(apiUrl('/api/chart-styles'), {method: 'POST'", f)
        self.assertIn("_refreshIfChanged(true)", f)

    def test_the_rate_line_follows_the_bar_as_on_the_server(self):
        i = PAGE.index("function _colRate(")
        self.assertIn("(0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]) / 255 < 0.5 ? 255 : 0", PAGE[i:i + 500])


if __name__ == "__main__":
    unittest.main()
