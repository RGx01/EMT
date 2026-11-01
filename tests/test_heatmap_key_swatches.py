"""
The data heatmap's key as swatches (ROADMAP BL-105, 1 Nov 2026, the user: "we also need to add visual colour
keys to the wordy key").

The Import bill, Export bill and Missing views' keys were one sentence of colour names. Each now has a `key`, a
swatch a state, drawn in the state's own colour (settleColour's) and hatched where the grid hatches it (missing:
black with grey diagonals; no cost from Octopus, and no export figure: red with yellow), with its words beside it;
the Export bill's opening sentence stays above its swatches. Red names every reason a half-hour reads red: not
final after 7 days, Octopus's figures disagreeing (its cost, kWh, VAT or split), or the devices costing more than
the meter; the help page is to say what each means. The energy views keep their sentence.

Checked in the local preview on 1 Nov 2026, light and dark.

Discriminating: every test fails on the parent tree.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(_ROOT, "web", "templates", "heatmap_module.html"), encoding="utf-8").read()


def _fn(name):
    i = SRC.index("  function " + name + "(")
    return SRC[i:SRC.index("\n  }\n", i) + 4]


def _var(name):
    i = SRC.index("  var " + name + " = {")
    return SRC[i:SRC.index("\n  };\n", i) + 5]


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheKey(unittest.TestCase):

    def _run(self, expr):
        js = "function dark() { return false; }\n" + _var("SETTLE_METRICS") + "\n" + \
             "var PRESENCE_METRIC = {sub: 'x', key: [['present', 'recorded']]};\n" + \
             "var METRICS = {kwh: {sub: 'Energy <in> & out'}, bill: SETTLE_METRICS.bill, export: SETTLE_METRICS.export," \
             " presence: PRESENCE_METRIC};\n" + \
             "\n".join(_fn(n) for n in ("settleColour", "swatchCss", "keyHtml")) + \
             "\nfunction hatch() { return 'H'; } function hatchAbandoned() { return 'A'; }\n" + \
             "console.log(JSON.stringify(" + expr + "));"
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
            path = fh.name
        try:
            res = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(path)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def test_a_swatch_a_state_in_its_colour(self):
        html = self._run("keyHtml('bill')")
        self.assertEqual(html.count('class="hm-sw"'), 9)
        self.assertIn('<span class="hm-sw" style="background:#2e9e5b;"></span>final', html)
        self.assertIn('<span class="hm-sw" style="background:#2a9d9f;"></span>Octopus’s cost, EMT’s House/EV split', html)

    def test_the_hatched_states_hatched(self):
        self.assertEqual(self._run("swatchCss('bill', 'missing')"),
                         "background:#000 repeating-linear-gradient(135deg,#6b7080 0 1.2px,transparent 1.2px 4px);")
        self.assertIn("#cc0000 repeating-linear-gradient(135deg,#f2c94c", self._run("swatchCss('export', 'no_data')"))
        self.assertIn("#cc0000 repeating-linear-gradient(135deg,#f2c94c", self._run("swatchCss('bill', 'abandoned')"))

    def test_red_names_every_reason(self):
        red = [k[1] for k in self._run("SETTLE_METRICS.bill.key") if k[0] == "overdue"][0]
        for words in ("not final after 7 days", "Octopus\u2019s figures disagreeing (its cost, kWh, VAT or split)",
                      "the devices costing more than the meter"):
            self.assertIn(words, red)

    def test_the_export_sentence_above_its_swatches(self):
        html = self._run("keyHtml('export')")
        self.assertTrue(html.startswith('<div class="hm-key-intro">EMT fetches no export bill'))
        self.assertEqual(html.count('class="hm-sw"'), 6)

    def test_an_energy_view_keeps_its_sentence_escaped(self):
        self.assertEqual(self._run("keyHtml('kwh')"), "Energy &lt;in> &amp; out")


class TheView(unittest.TestCase):

    def test_the_title_row_draws_it(self):
        self.assertIn("state.root.querySelector('.hm-sub').innerHTML = keyHtml(state.metric);", _fn("redrawAll"))


if __name__ == "__main__":
    unittest.main()
