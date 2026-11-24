"""Usage Stats: a Running total line by the day, the month or the year, and a rule between the
toolbar's groups (24 Nov 2026, the user).

HH's Net box drew the day's net so far; Day, Month and Year had only each bar's own net. They now
have a Running total box too: the net so far from the first bar, on the right-hand axis, summed from
each bar's own net as the table states it (in cost, the bill's net to the penny), so its last point
is the table's Total. Not on HH (its Net is that already), Rate brackets, Flow, the Running total view,
nor in month order, where the bars are not in time order.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
with open(os.path.join(ROOT, "web", "templates", "charts.html"), encoding="utf-8") as fh:
    PAGE = fh.read()


def _fn(name):
    i = PAGE.index("function " + name + "(")
    line = PAGE[i:PAGE.index("\n", i)]
    if line.count("{") == line.count("}"):
        return line + "\n"
    return PAGE[i:PAGE.index("\n}\n", i) + 2]


def _node(src):
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(src)
        path = fh.name
    try:
        res = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
    finally:
        os.unlink(path)
    if res.returncode != 0:
        raise AssertionError(res.stderr)
    return json.loads(res.stdout)


@unittest.skipUnless(shutil.which("node"), "node not available")
class WhereTheBoxApplies(unittest.TestCase):

    def test_by_period_view_and_order(self):
        src = ("var barPeriod, barMonthOrder, SHOW, MONTHFIRST = false;\n"
               "function barBrackets() { return SHOW === 'brackets'; } function barFlow() { return SHOW === 'flow'; }\n"
               "function barRunning() { return SHOW === 'running'; } function barSpanMonthFirst() { return MONTHFIRST; }\n"
               + _fn("barRunAvailable") +
               "\nvar cases = [['daily','devices','date',false], ['monthly','devices','date',false],"
               " ['yearly','devices','date',false], ['span','devices','date',false], ['hh','devices','date',false],"
               " ['monthly','devices','month',false], ['span','devices','month',true], ['daily','brackets','date',false],"
               " ['daily','flow','date',false], ['daily','running','date',false], ['daily','devices','month',false]];\n"
               "console.log(JSON.stringify(cases.map(function (c) { barPeriod = c[0]; SHOW = c[1]; barMonthOrder = c[2];"
               " MONTHFIRST = c[3]; return barRunAvailable(); })));")
        self.assertEqual(_node(src), [True, True, True, True, False, False, False, False, False, False, True])


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheLine(unittest.TestCase):

    def _series(self, metric, aggs):
        src = ("var barMetric = %s, barPeriod = 'daily';\n"
               "function barIncStanding() { return false; } function barStandingVal() { return 0; }\n"
               % json.dumps(metric) + _fn("barNetRaw") + _fn("barNetVal") + _fn("barNetSeries") +
               "\nconsole.log(JSON.stringify(barNetSeries(%s, false, true)));" % json.dumps(aggs))
        return _node(src)

    def test_cost_sums_each_bars_bill_net(self):
        # each day's bill net to the penny, then summed: the table's Total, not the unrounded pounds
        aggs = [{"net_cost": 1.004, "standing": 0}, {"net_cost": 1.004, "standing": 0},
                {"net_cost": 1.004, "standing": 0}]
        self.assertEqual(self._series("cost", aggs), [1.0, 2.0, 3.0])

    def test_kwh_runs_from_the_first_bar(self):
        aggs = [{"imp_kwh": 5, "exp_kwh": 1}, {"imp_kwh": 2, "exp_kwh": 4}, {"imp_kwh": 3, "exp_kwh": 0}]
        self.assertEqual(self._series("kwh", aggs), [4, 2, 5])

    def test_a_bar_with_no_value_carries_the_total(self):
        aggs = [{"carbon_g_net": None}, {"carbon_g_net": 10.5}, {"carbon_g_net": None}, {"carbon_g_net": 2}]
        self.assertEqual(self._series("co2", aggs), [None, 10.5, 10.5, 12.5])

    def test_drawn_on_the_right_hand_axis(self):
        f = _fn("barGetDatasets")
        self.assertIn("if (!isHH && barShowRun()) {", f)
        self.assertIn("data: barNetSeries(aggList, false, true),", f)
        self.assertIn("yAxisID: 'yNet',", f)
        self.assertIn("var netAxis = (!!pd.isHH && barShowNet()) || (!pd.isHH && barShowRun());", _fn("barRender"))

    def test_a_net_line_on_the_bars_axis_counts_on_the_left(self):
        # with Net (left axis) and Running total (right) together, the Net line sizes the left axis, the
        # running total the right
        src = (_fn("barNiceStep") + "function barTariffRoom(a, b) { return [a, b]; }\n" + _fn("barAlignZero") +
               "\nvar ds = [{data: [2, 2], yAxisID: 'y'}, {data: [9, 9], _net: true, yAxisID: 'y'},"
               " {data: [50, 100], _net: true, yAxisID: 'yNet'}];\n"
               "var a = barAlignZero(ds, null);\nconsole.log(JSON.stringify([a.y.max >= 9, a.yNet.max >= 100,"
               " a.y.max < 100]));")
        self.assertEqual(_node(src), [True, True, True])


@unittest.skipUnless(shutil.which("node"), "node not available")
class ThinWhereTheBarsAreMany(unittest.TestCase):
    # 24 Nov 2026, the user: a span by the day (~860 bars) drew Net and Running total as a thick band

    def test_by_the_number_of_bars(self):
        got = _node(_fn("barLineStyle") + "\nconsole.log(JSON.stringify([48, 62, 63, 186, 187, 860]"
                    ".map(barLineStyle)));")
        self.assertEqual(got, [{"point": 3, "width": 2}, {"point": 3, "width": 2}, {"point": 0, "width": 1.5},
                               {"point": 0, "width": 1.5}, {"point": 0, "width": 1}, {"point": 0, "width": 1}])

    def test_both_lines_take_it(self):
        f = _fn("barGetDatasets")
        self.assertIn("var _ls = barLineStyle(aggList.length);", f)
        self.assertIn("borderWidth: _ls.width, pointRadius: _ls.point, pointHitRadius: 6,", f)
        self.assertIn("borderWidth: _ls.width, pointRadius: _ls.point ? 2 : 0, pointHitRadius: 6,", f)


class TheToolbar(unittest.TestCase):

    def test_the_box(self):
        self.assertIn('<label id="bar-run-wrap"', PAGE)
        self.assertIn('<input type="checkbox" id="bar-run-cb"', PAGE)
        self.assertIn("run:      !!(document.getElementById('bar-run-cb') || {}).checked,", _fn("barSaveState"))
        self.assertIn("if (rc) rc.checked = st.run === true;", _fn("barLoadState"))
        self.assertIn("['bar-run-wrap', 'bar-run-cb', 'Running total']", _fn("barPdfSettings"))
        self.assertIn("'bar-net-wrap', 'bar-run-wrap',", PAGE)   # the Running total view hides it

    def test_a_rule_before_every_group(self):
        row = PAGE[PAGE.index('<div class="bar-toolbar-left">'):]
        row = row[:row.index('<!-- The Years row')]
        import re
        row = re.sub(r"<!--.*?-->", "", row, flags=re.S)
        for gid in ('id="bar-period-seg"', 'id="bar-res-seg"', 'id="bar-cal-seg"', 'id="bar-metric-seg"',
                    'id="bar-order-seg"', 'id="bar-acc-wrap"', 'id="bar-br-wrap"'):
            before = row[:row.index(gid)].rstrip()
            before = before[:before.rindex("<")]
            self.assertTrue(before.rstrip().endswith('<span class="bar-tb-sep" aria-hidden="true"></span>'),
                            "no rule before " + gid)
        self.assertIn("gap: 6px 10px;", PAGE)

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_rules_only_between_shown_groups(self):
        # a stand-in row: [is a rule, shown]; a rule shows only with a shown group before and after it
        src = ("function getComputedStyle(e) { return {display: e._shown ? 'flex' : 'none'}; }\n"
               "function el(sep, shown) { return {_shown: shown, style: {display: ''},"
               " classList: {contains: function () { return sep; }}}; }\n"
               "var kids = [el(false,true), el(true), el(false,true), el(true), el(false,false), el(true),"
               " el(false,true), el(true), el(false,false), el(true), el(false,false)];\n"
               "var document = {querySelector: function () { return {children: kids}; }};\n" +
               _fn("barTbSepSync") +
               "\nbarTbSepSync();\nconsole.log(JSON.stringify(kids.filter(function (k) { return k.classList.contains(); })"
               ".map(function (k) { return k.style.display; })));")
        self.assertEqual(_node(src), ["", "none", "", "none", "none"])


if __name__ == "__main__":
    unittest.main()
