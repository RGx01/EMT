"""
The Billing tab and a new half-hour (21 Nov 2026, the user: "billing charts are not auto refreshing after a new HH
arrives").

Charts refreshes the open tab when the change token (/api/energy?fields=fingerprint) moves. Three gaps:

- Billing and the heatmap noted no token when they loaded, so the first poll only seeded it. A half-hour landing
  between the load and that poll was in the seed: on a copy of a support file's database, one added 4 s after the
  page loaded was still missing a minute later, and stayed missing until the next one. `_loadNotingToken` reads
  the token before the load and keeps it, as Usage Stats does.
- The refresh 90 s after each boundary called `loadChartContent(name, true)`, which for these tabs only redraws
  what they hold (the resize and theme path). It now goes through `_refreshIfChanged`.
- A refresh dropped Billing's months but not one still being fetched; landing after, it went back in the cache
  and the refreshed days were filled from it. A month fetched before a refresh no longer goes in the cache.

Discriminating: each test fails on the parent tree.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = open(os.path.join(_ROOT, "web", "templates", "charts.html"), encoding="utf-8").read()
MOD = open(os.path.join(_ROOT, "web", "templates", "billing_module.html"), encoding="utf-8").read()


def _fn(src, name, prefix="function "):
    """A function's whole text, by matching its braces."""
    i = src.index(prefix + name + "(")
    depth, j = 0, src.index("{", i)
    while True:
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
        j += 1


def _page_fn(name):
    try:
        return _fn(PAGE, name, "async function ")
    except ValueError:
        return _fn(PAGE, name)


def _run(js):
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "t.js")
        with open(p, "w") as f:
            f.write(js)
        out = subprocess.run(["node", p], capture_output=True, text=True, timeout=60)
    if out.returncode:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout.strip().splitlines()[-1])


PAGE_HARNESS = """
var calls = [], TOKEN = 'v1';
async function _fetchDataVersion() { calls.push('token'); return TOKEN; }
var document = {getElementById: function () { return {}; }, visibilityState: 'visible',
                querySelector: function () { return {dataset: {chart: 'daily'}}; }};
var window = {};
var EMTBilling = window.EMTBilling = {_state: {inited: false},
  init: async function () { calls.push('init'); this._state.inited = true; },
  refresh: async function () { calls.push('refresh'); }, rerender: function () { calls.push('rerender'); }};
var EMTHeatmap = window.EMTHeatmap = {_state: {inited: true}, init: async function () {}, refresh: async function () {},
  rerender: function () {}};
var _lastDataVersion = null, _refreshInFlight = false, _activeChart = 'daily', chartsLoaded = {},
    barLastFetch = 0, barHHDataDate = null, barBlockMinutes = 30;
function _refreshSettleNote() {}
async function barInit() {}
%s
%s
%s
"""


def _page(body, *fns):
    return PAGE_HARNESS % ("\n".join(_page_fn(n) for n in fns), "", body)


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheChangeToken(unittest.TestCase):

    def test_a_half_hour_landing_after_the_load_refreshes_at_the_first_poll(self):
        r = _run(_page("""
(async function () {
  await loadChartContent('daily');          // the page opens on Billing: token v1
  TOKEN = 'v2';                             // a half-hour lands
  calls.length = 0;
  var changed = await _refreshIfChanged(false);   // the first poll
  console.log(JSON.stringify({changed: changed, calls: calls, ver: _lastDataVersion}));
})();
""", "loadChartContent", "_loadNotingToken", "_refreshIfChanged"))
        self.assertTrue(r["changed"])
        self.assertIn("refresh", r["calls"])
        self.assertEqual(r["ver"], "v2")

    def test_the_token_is_read_before_the_load(self):
        r = _run(_page("""
(async function () {
  await loadChartContent('daily');
  console.log(JSON.stringify({calls: calls, ver: _lastDataVersion}));
})();
""", "loadChartContent", "_loadNotingToken"))
        self.assertEqual(r["calls"], ["token", "init"])
        self.assertEqual(r["ver"], "v1")

    def test_the_boundary_refresh_fetches_when_the_data_moved(self):
        body = """
var timers = [];
function setTimeout(f) { timers.push(f); }
function msUntilNextBarRefresh() { return 0; }
(async function () {
  await loadChartContent('daily');
  TOKEN = 'v2';
  calls.length = 0;
  scheduleChartRefresh();
  timers.shift()();                          // 90 s after the boundary
  await new Promise(function (r) { setImmediate(r); });
  await new Promise(function (r) { setImmediate(r); });
  console.log(JSON.stringify({calls: calls}));
})();
"""
        r = _run(_page(body, "loadChartContent", "_loadNotingToken", "_refreshIfChanged", "scheduleChartRefresh"))
        self.assertIn("refresh", r["calls"])
        self.assertNotIn("rerender", r["calls"])


MOD_HARNESS = """
var state = {lists: {}, bills: {}, months: {}, monthLoading: {}, gen: 0, inited: true};
var pending = [];
function fetchJSON(url) { return new Promise(function (ok) { pending.push(ok); }); }
function show() { return Promise.resolve(); }
function day(kwh) { return {billing: {days: [{day: '2026-11-21', kwh: kwh}]}}; }
%s
%s
(async function () {
  var old = loadMonth('2026-11');            // a day being filled when the data moves
  refresh();
  pending.shift()(day(14.8));                // the month asked for before the change lands
  await old;
  var fresh = loadMonth('2026-11');
  var asked = pending.length;
  if (asked) pending.shift()(day(19.3));
  var m = await fresh;
  console.log(JSON.stringify({asked: asked, kwh: m['2026-11-21'].kwh}));
})();
"""


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheMonthCache(unittest.TestCase):

    def test_a_month_fetched_before_a_refresh_is_not_kept(self):
        r = _run(MOD_HARNESS % (_fn(MOD, "loadMonth"), _fn(MOD, "refresh")))
        self.assertEqual(r["asked"], 1, "the refreshed day must be fetched again")
        self.assertEqual(r["kwh"], 19.3)


if __name__ == "__main__":
    unittest.main()
