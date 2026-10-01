"""
Live Power's billing cards read /api/periods?kind=live — C, part 2, 1 Oct 2026.

  * One request for Today / This Bill / This Year (was /api/billing): each window's
    `net_cost`, `label` and `rows` (tests/test_live_periods.py). In a browser on a
    production-like database the three cards — totals, periods and every row — were
    identical to what /api/billing drew.
  * Row labels carry device names from the config: escaped, not inserted as HTML (checked
    in a browser: an <img onerror> label rendered as text, no element, no script).
  * A failed refresh keeps the cards — it rendered "undefinedNaN" on a card already drawn,
    from an error body read as data.
  * A hidden tab does not poll; a refresh that falls due waits until the tab is shown, and
    there is only ever one timer.

These fail on the parent tree.
"""
import os
import re
import unittest

_SRC = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "web", "templates", "live_power.html"), encoding="utf-8").read()


def _fn(name):
    m = re.search(r"function " + name + r"\([^)]*\)\s*\{(.*?)\n\}", _SRC, re.S)
    assert m, name
    return m.group(1)


class TheCardsReadTheOneEndpoint(unittest.TestCase):

    def test_one_request_for_the_three_windows(self):
        body = _fn("refreshBilling")
        self.assertIn("/api/periods?kind=live&include=net_cost,rows", body)
        self.assertNotIn("/api/billing", re.sub(r"//[^\n]*", "", _SRC))
        self.assertIn("[['today', 'today'], ['month', 'bill'], ['year', 'year']]", _SRC)

    def test_the_currency_comes_from_the_page(self):
        self.assertIn("var BILL_CURRENCY = {{ currency|tojson }};", _SRC)


class TheCardsAreSafe(unittest.TestCase):

    def test_labels_are_escaped(self):
        rows = _fn("_billRowsHtml")
        self.assertIn("_billEsc(r.label)", rows)
        self.assertNotIn("+ r.label +", _SRC)

    def test_an_error_keeps_the_cards(self):
        body = _fn("refreshBilling")
        self.assertIn("if (d.error) throw", body)
        self.assertIn("r.ok ? r.json() : Promise.reject", body)
        self.assertIn("if (typeof total !== 'number') return;", _fn("updateBillingCard"))


class AHiddenTabDoesNotPoll(unittest.TestCase):

    def test_deferred_while_hidden_and_run_when_shown(self):
        self.assertIn("if (document.hidden) { _billDue = true; return; }", _fn("refreshBilling"))
        self.assertIn("if (!document.hidden && _billDue) refreshBilling();", _SRC)

    def test_one_timer(self):
        self.assertIn("if (_billTimer) clearTimeout(_billTimer);", _fn("scheduleBillingRefresh"))


if __name__ == "__main__":
    unittest.main()
