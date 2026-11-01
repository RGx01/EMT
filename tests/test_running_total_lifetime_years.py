"""
The Lifetime line a stretch a year, in the spiral's year colours (1 Nov 2026, the user: "lifetime spiral vs
lifetime line, line should hold the same principles with colouring as spiral").

The line was one colour. Now each year's points are a dataset in that year's colour — the spiral's, by the
year's place among the years shown — and each stretch starts at the year before's last point, so the line
stays one line. A point carries its year (the year its year began in, so a "Year from" choice re-cuts it).
The legend names the years, as on Year by year.

Checked in the local preview on 1 Nov 2026 (a scratch copy of staging): 2023–2026 in teal, violet, amber
and blue, as on the spiral, each joined to the last.

Discriminating: every test fails on the parent tree.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = open(os.path.join(_ROOT, "web", "templates", "charts.html"), encoding="utf-8").read()


def _fn(name):
    i = PAGE.index("function " + name + "(")
    line = PAGE[i:PAGE.index("\n", i)]
    if line.count("{") == line.count("}"):
        return line + "\n"
    return PAGE[i:PAGE.index("\n}\n", i) + 2]


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheYears(unittest.TestCase):

    def _run(self, expr):
        yc = PAGE[PAGE.index("var BAR_ACC_YC = "):].split("\n", 1)[0]
        js = yc + "\n" + _fn("barAccLifeYears") + "\nconsole.log(JSON.stringify(" + expr + "));"
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
            path = fh.name
        try:
            res = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(path)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    LIFE = "[{x: 1, y: 1, yr: 2024}, {x: 2, y: 2, yr: 2024}, {x: 3, y: 4, yr: 2025}, {x: 4, y: 5, yr: 2025}, {x: 5, y: 7, yr: 2026}]"

    def test_a_stretch_a_year_joined_to_the_last(self):
        out = self._run("barAccLifeYears(%s, [2024, 2025, 2026]).map(function (d) { "
                        "return [d.y, d.data.map(function (p) { return p.x; })]; })" % self.LIFE)
        self.assertEqual(out, [[2024, [1, 2]], [2025, [2, 3, 4]], [2026, [4, 5]]])

    def test_the_spirals_colours_by_the_years_place(self):
        out = self._run("barAccLifeYears(%s, [2024, 2025, 2026]).map(function (d) { return d.color; })" % self.LIFE)
        self.assertEqual(out, ["#00d4aa", "#7c6af7", "#f9a825"])

    def test_a_point_carries_its_year(self):
        self.assertIn("pts.push({x: barAccMs(r), y: Math.round(life * 1000) / 1000, yr: y});", _fn("barAccSeriesOf"))

    def test_the_line_draws_them_with_a_legend(self):
        f = _fn("barAccRender")
        self.assertIn("? barAccLifeYears(s.life, yearsShown).map(function(d) {", f)
        self.assertIn("legend: {display: true,", f)


if __name__ == "__main__":
    unittest.main()
