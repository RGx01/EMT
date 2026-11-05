"""
BL-97 (1 Nov 2026) — the EV on an account with no EV device (EV design: EMT-v5_api_ev_device_design.md).

Octopus's EV — its split of the bill, or a completed dispatch where the bill has not said yet — was
"EV (from dispatch)", a name from before the bill carried the split, in a purple fixed in code: it is
no device, so it had no channel to keep a colour on. Now:

- it is **"EV"**, as an account with an EV device has it;
- its style is kept in the store's meta (`ev_style`), read for the day charts' colours, the bill's
  breakdown and Usage Stats; unset, the purple it had;
- a Settings tab **🚗 EV** holds its colour card, where the account has ever had an EV (an EV share
  of a half-hour, or a completed dispatch — `dispatch_history` is kept for good) and has no EV device;
- a new EV device, added or from the wizard, starts with its colour (the user).

Checked in the local preview on 1 Nov 2026 (see the PR).

Discriminating: every test fails on the parent tree but those labelled GUARD.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_billing_view as V                    # noqa: E402  (a store with priced half-hours)
import energy_charts as ec                       # noqa: E402
from block_store import BlockStore               # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = open(os.path.join(_ROOT, "web", "templates", "meter_config.html"), encoding="utf-8").read()


def _fn(name):
    i = PAGE.index("function " + name + "(")
    return PAGE[i:PAGE.index("\n}\n", i) + 2]


def _dispatch(st, slot, kind="completed", kwh=-2.0):
    st._conn.execute("INSERT INTO dispatch_history (slot_start, kind, energy_kwh, first_seen, last_seen) "
                     "VALUES (?, ?, ?, ?, ?)", (slot, kind, kwh, slot, slot))
    st._conn.commit()


class TheStyle(unittest.TestCase):

    def setUp(self):
        self.st = BlockStore(":memory:")

    def tearDown(self):
        self.st.close()

    def test_GUARD_unset_it_is_the_purple_it_had(self):
        self.assertEqual(ec.ev_dispatch_style(self.st)["bar"], "#8b5cf6")
        self.assertFalse(ec.ev_dispatch_style(self.st)["dashed"])

    def test_the_chosen_style(self):
        self.st.set_meta("ev_style", {"bar_color": "#2E8B57", "rate_line": "dashed"})
        st = ec.ev_dispatch_style(self.st)
        self.assertEqual((st["bar"], st["dashed"]), ("#2e8b57", True))
        self.assertEqual(st["rate"], "#6dae89")              # the bar's own rate line, as a device's

    def test_junk_falls_back(self):
        self.st.set_meta("ev_style", {"bar_color": "red\"><script>", "rate_line": "dotted"})
        self.assertEqual(ec.ev_dispatch_style(self.st)["bar"], "#8b5cf6")
        self.st.set_meta("ev_style", "not a dict")
        self.assertEqual(ec.ev_dispatch_style(self.st)["bar"], "#8b5cf6")


class EverHadAnEv(unittest.TestCase):

    def setUp(self):
        self.st = BlockStore(":memory:")

    def tearDown(self):
        self.st.close()

    def test_none(self):
        self.assertFalse(ec.has_had_ev(self.st))

    def test_an_ev_share_of_a_half_hour(self):
        self.st._conn.execute("INSERT INTO block_segments (block_start, meter_id, channel, seq, kwh, inc_rate, "
                              "band, attribution) VALUES ('2025-01-01T01:00:00', 'electricity_main', 'import', "
                              "0, 3.0, 0.07, 'off_peak', 'ev')")
        self.assertTrue(ec.has_had_ev(self.st))

    def test_a_completed_dispatch_alone(self):
        _dispatch(self.st, "2023-01-01T01:00:00")             # old: dispatch_history is kept for good
        self.assertTrue(ec.has_had_ev(self.st))

    def test_a_planned_one_is_not(self):
        _dispatch(self.st, "2026-01-01T01:00:00", kind="planned")
        self.assertFalse(ec.has_had_ev(self.st))


class TheCharts(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # the store's EV device taken out: the account is the main meter alone
        cls.st = V._store()
        cls.cfg = cls.st.config_from_db(1)
        cls.cfg["meters"].pop("ev_charger", None)
        cls.blocks = cls.st.get_blocks_lightweight()
        for b in cls.blocks:
            (b.get("meters") or {}).pop("ev_charger", None)
        slot = next(b["start"] for b in cls.blocks
                    if float((((b.get("meters") or {}).get("electricity_main") or {})
                              .get("channels", {}).get("import", {}) or {}).get("kwh") or 0) > 0.5)
        _dispatch(cls.st, slot, kwh=-0.4)

    @classmethod
    def tearDownClass(cls):
        cls.st.close()

    def test_GUARD_there_is_no_ev_device(self):
        self.assertIsNone(ec._ev_meter_id(self.cfg))

    def test_the_day_charts_take_the_chosen_colour(self):
        self.st.set_meta("ev_style", {"bar_color": "#2e8b57", "rate_line": "dashed"})
        try:
            ctx = ec.billing_context(self.cfg, self.st, self.blocks)
            self.assertEqual(ctx["meter_colors"]["ev_dispatch"], "#2e8b57")
            self.assertTrue(ctx["meter_rate_dashed"]["ev_dispatch"])
        finally:
            self.st.set_meta("ev_style", None)
        self.assertEqual(ec.billing_context(self.cfg, self.st, self.blocks)["meter_colors"]["ev_dispatch"],
                         "#8b5cf6")


class TheName(unittest.TestCase):

    def test_ev_everywhere(self):
        for f in ("energy_charts.py", "usage_view.py", "bill_card.py", os.path.join("web", "server.py")):
            src = open(os.path.join(_ROOT, f), encoding="utf-8").read()
            code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
            self.assertNotIn('"EV (from dispatch)"', code, f)

    def test_usage_stats_takes_its_colour(self):
        src = open(os.path.join(_ROOT, "usage_view.py"), encoding="utf-8").read()
        self.assertIn("color=_ec.ev_dispatch_style(store)[\"bar\"])", src)


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheSettingsTab(unittest.TestCase):

    def _run(self, meters, had, ev_style=None, add=None):
        js = "\n".join(_fn(n) for n in ("_cfgTabList", "_hasEvDevice", "_showApiEv", "_inheritEvStyle")) + \
            "\nvar HAS_HAD_EV = %s, EV_STYLE = %s, EV_DEFAULT = '#8b5cf6', batteryFlagged = {}, wholeHouseFlagged = {};" % (
                json.dumps(had), json.dumps(ev_style or {})) + \
            "\nvar config = %s;" % json.dumps({"meters": meters}) + \
            "\nvar out = {tabs: _cfgTabList().map(function (t) { return t.key; })};" + \
            ("\nvar shown = _showApiEv(); var imp = {}; config.meters.new_ev = {meta: {sub_meter: true, meter_type: 'ev'}};"
             "\n_inheritEvStyle(imp, shown); out.imp = imp; out.after = _cfgTabList().map(function (t) { return t.key; });"
             if add else "") + \
            "\nconsole.log(JSON.stringify(out));"
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
            path = fh.name
        try:
            res = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(path)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    MAIN = {"electricity_main": {"meta": {}}}

    def test_shown_where_the_account_has_had_an_ev_and_no_device(self):
        self.assertEqual(self._run(self.MAIN, True)["tabs"], ["site", "main", "api-ev", "add"])

    def test_not_without_one(self):
        self.assertNotIn("api-ev", self._run(self.MAIN, False)["tabs"])

    def test_not_beside_an_ev_device(self):
        meters = dict(self.MAIN, sub_meter_1={"meta": {"sub_meter": True, "meter_type": "ev", "device": "Zappi"}})
        self.assertNotIn("api-ev", self._run(meters, True)["tabs"])

    def test_a_new_ev_device_starts_with_its_colour(self):
        out = self._run(self.MAIN, True, {"bar_color": "#2e8b57", "rate_line": "dashed"}, add=True)
        self.assertEqual(out["imp"], {"bar_color": "#2e8b57", "rate_line": "dashed"})
        self.assertNotIn("api-ev", out["after"])                # the device takes over

    def test_unset_it_starts_with_the_purple(self):
        self.assertEqual(self._run(self.MAIN, True, add=True)["imp"], {"bar_color": "#8b5cf6"})

    def test_no_colour_given_where_there_was_no_ev(self):
        self.assertEqual(self._run(self.MAIN, False, add=True)["imp"], {})


class TheWiring(unittest.TestCase):

    def test_its_card_points_to_bar_colours(self):
        # 5 Nov 2026: the colour picker on its card went with Settings' other chart styles; the Charts
        # page's Bar colours sets it, with every meter's
        f = _fn("buildApiEvPanel")
        self.assertIn("Bar colours", f)
        self.assertNotIn("buildChartStyle", f)

    def test_both_ways_to_add_an_ev_device_inherit(self):
        self.assertIn("if (type === 'ev') _inheritEvStyle(config.meters[id].channels.import, evShown);", PAGE)
        self.assertIn("if (devId === 'ev' && !existing) _inheritEvStyle(config.meters[subId].channels.import, evShown);",
                      PAGE)


if __name__ == "__main__":
    unittest.main()
