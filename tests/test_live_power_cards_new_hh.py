"""
Live Power's bill cards (Today / This Bill / This Year) and a half-hour that lands late (21 Nov 2026, the user:
"fix the cards").

The cards fetched once, 60 s after each boundary. A half-hour EMT finished after that (3 of 262 in two support
files, worst 63 s, both on slow disks), or a bill or settled reading landing mid-half-hour, waited for the next
boundary: half an hour. The cards now note Charts' change token (/api/energy?fields=fingerprint) when they load,
read before the fetch, and a minute's check fetches them again only when it has moved.

Discriminating: the change check is new, so these fail on the parent tree.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = open(os.path.join(_ROOT, "web", "templates", "live_power.html"), encoding="utf-8").read()


def _fn(name):
    i = PAGE.index("function %s(" % name)
    depth, j = 0, PAGE.index("{", i)
    while True:
        if PAGE[j] == "{":
            depth += 1
        elif PAGE[j] == "}":
            depth -= 1
            if depth == 0:
                return PAGE[i:j + 1]
        j += 1


HARNESS = """
var TOKEN = 'v1', asked = [], drawn = [];
function apiUrl(u) { return u; }
function json(o) { return Promise.resolve({ok: true, json: function () { return Promise.resolve(o); }}); }
function fetch(u) {
  asked.push(u);
  if (u.indexOf('fields=fingerprint') !== -1) return json({fingerprint: {token: TOKEN}});
  return json({periods: [{key: 'today', net_cost: TOKEN === 'v1' ? 1.5 : 2.0, rows: [], label: 'Today'}]});
}
var document = {hidden: false};
var BILL_CURRENCY = '£', BILL_CARDS = [['today', 'today']];
var _billTimer = null, _billDue = false, _billToken = null;
function updateBillingCard(id, total) { drawn.push(total); }
function scheduleBillingRefresh() {}
function _billRedrawPill() {}            // the settlement pill (#199): not under test here
function settle() { return new Promise(function (r) { setTimeout(r, 0); }); }
%s
%s
%s
(async function () {
  refreshBilling(); await settle(); await settle();
  var first = {token: _billToken, drawn: drawn.slice()};
  asked.length = 0;
  var still = await checkBillingChanged();           // nothing moved: one token read, no card fetch
  var quiet = asked.slice();
  TOKEN = 'v2';                                      // a half-hour lands, late
  asked.length = 0;
  var moved = await checkBillingChanged(); await settle(); await settle(); await settle();
  console.log(JSON.stringify({first: first, still: still, quiet: quiet, moved: moved, asked: asked,
                              drawn: drawn, token: _billToken}));
})();
"""


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheCards(unittest.TestCase):

    def _run(self):
        src = HARNESS % (_fn("_billFetchToken"), _fn("refreshBilling"), _fn("checkBillingChanged"))
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.js")
            with open(p, "w") as f:
                f.write(src)
            out = subprocess.run(["node", p], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_a_load_notes_the_token(self):
        r = self._run()
        self.assertEqual(r["first"], {"token": "v1", "drawn": [1.5]})

    def test_an_unmoved_token_fetches_no_cards(self):
        r = self._run()
        self.assertFalse(r["still"])
        self.assertEqual(r["quiet"], ["/api/energy?fields=fingerprint"])

    def test_a_moved_token_fetches_the_cards_again(self):
        r = self._run()
        self.assertTrue(r["moved"])
        self.assertIn("/api/periods?kind=live&include=net_cost,rows", r["asked"])
        self.assertEqual(r["drawn"], [1.5, 2.0])
        self.assertEqual(r["token"], "v2")


if __name__ == "__main__":
    unittest.main()
