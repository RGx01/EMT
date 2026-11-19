"""
Overview's Agile cards show the power under the prices (19 Nov 2026, the user: "on the overview page agile import
and agile export cards could we show the power history (split by import power/export power appropriately) on y2
axis?").

The 48-hour history card's readings (`_phData`, net kW, import positive) are drawn on each Agile card's own right-hand
axis: import's side on the import card, export's on the export card, averaged five minutes at a time over the 24 hours
before now. The hover adds the half-hour's average power. Without readings (no power sensor) the card is as it was.

Checked in the local preview on 19 Nov 2026 on a copy of a support database, its readings and Agile prices moved
forward 27 hours so the last 24 h had both: the import card's line in amber, the export card's in teal, a kW axis on
the right, light and dark; the hover "06:30–07:00 · 30.46p · Importing 12.80 kW avg".

Discriminating: every test fails on the parent tree, where none of it existed.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = open(os.path.join(_ROOT, "web", "templates", "live_power.html"), encoding="utf-8").read()


def _fn(n):
    i = PAGE.index("function " + n + "(")
    depth, j = 0, PAGE.index("{", i)
    while True:
        depth += {"{": 1, "}": -1}.get(PAGE[j], 0)
        if PAGE[j] == "}" and depth == 0:
            return PAGE[i:j + 1]
        j += 1


T0 = "Date.UTC(2026, 10, 18, 12)"            # 18 Nov 12:00Z; now 24 h later
NOW = "Date.UTC(2026, 10, 19, 12)"
ROWS = [  # 12:00:00 and 12:02:00 in one bucket (import 2 and 4 kW), 12:06 exporting 1.5 kW, one before t0, one after now
    {"captured_at": "2026-11-18T12:00:00", "net_kw": 2.0},
    {"captured_at": "2026-11-18T12:02:00", "net_kw": 4.0},
    {"captured_at": "2026-11-18T12:06:00", "net_kw": -1.5},
    {"captured_at": "2026-11-18T11:59:00", "net_kw": 9.0},
    {"captured_at": "2026-11-19T12:01:00", "net_kw": 9.0},
    {"captured_at": "2026-11-18T13:00:00", "net_kw": None},
]


@unittest.skipUnless(shutil.which("node"), "node not available")
class ThePower(unittest.TestCase):

    def _run(self, expr):
        js = "var AGILE_PW_BUCKET = 300000;\n" + "\n".join(
            _fn(n) for n in ("_agileMs", "_agilePowerSeries", "_agilePowerScale")) + \
            "\nconsole.log(JSON.stringify(" + expr + "));"
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.js")
            with open(p, "w", encoding="utf-8") as f:
                f.write(js)
            r = subprocess.run(["node", p], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def _series(self, ch):
        return self._run("_agilePowerSeries(%s, %s, %s, %s, 300000)" % (json.dumps(ROWS), json.dumps(ch), T0, NOW))

    def test_import_is_the_positive_side_averaged_a_bucket_at_a_time(self):
        s = self._series("import")
        t0 = 1795003200000                                   # 18 Nov 2026 12:00Z
        self.assertEqual(s, [[t0, 3.0], [t0 + 300000, 0]])   # (2 + 4) / 2; the exporting bucket imports nothing

    def test_export_is_the_negative_side_turned_round(self):
        t0 = 1795003200000
        self.assertEqual(self._series("export"), [[t0, 0], [t0 + 300000, 1.5]])

    def test_the_axis_steps_in_round_kilowatts(self):
        self.assertEqual(self._run("_agilePowerScale([[0, 0.2]])"), {"max": 0.5, "step": 0.5})
        self.assertEqual(self._run("_agilePowerScale([[0, 3.2]])"), {"max": 4, "step": 1})
        self.assertEqual(self._run("_agilePowerScale([[0, 12.8]])"), {"max": 15, "step": 5})


class TheCard(unittest.TestCase):

    def test_drawn_on_the_right_from_the_history_cards_readings(self):
        d = _fn("_agileDraw")
        self.assertIn("var pw = _agilePowerSeries(typeof _phData !== 'undefined' ? _phData : [], ch, t0, now, AGILE_PW_BUCKET);", d)
        self.assertIn("PAD_R = pw.length ? AGILE_PAD_R : 0", d)            # no readings, no axis
        self.assertIn("ctx.fillText((+kv.toFixed(1)) + ' kW', PAD_L + CW + 4, y2(kv));", d)

    def test_the_hover_gives_the_half_hours_power_on_the_same_width(self):
        i = PAGE.index("cv.addEventListener('mousemove', function(e){\n    if (!_agile) return;")
        h = PAGE[i:PAGE.index("cv.addEventListener('mouseleave'", i)]
        self.assertIn("(r.width - AGILE_PAD_L - padR)", h)
        self.assertIn("(ch === 'export' ? 'Exporting ' : 'Importing ') + kw.toFixed(2) + ' kW avg'", h)

    def test_redrawn_when_the_readings_land_and_named_in_the_legend(self):
        i = PAGE.index("async function _phLoad()")
        self.assertIn("_agileDraw(ch);           // the power on the Agile cards' y2", PAGE[i:i + 800])
        self.assertIn("{{ 'Export' if ch == 'export' else 'Import' }} power (kW, right)", PAGE)


if __name__ == "__main__":
    unittest.main()
