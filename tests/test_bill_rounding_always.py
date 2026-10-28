"""
The bill summary always uses the statement's method (28 Oct 2026, the user: "make bill rounding the
default", then "we may as well remove the ui option also").

BL-24's bill-style summary — ex-VAT summed raw, rounded at the subtotal, VAT on top, as Octopus's
statement does — was an opt-in, because v4 captured an ex-VAT figure only where Octopus sent one. Since
engine step 4 a half-hour with kWh has one (staging, 28 Oct 2026: all but 1 of 37,869). So it is no
longer a choice: the summary uses it wherever EMT holds ex-VAT figures, and a CAD-only install, with
none, keeps the inc-VAT sum through the capability gate. A setting saved by the retired page is ignored.

Discriminating: every test here fails on the parent, but those labelled GUARD.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_billing_view as V                    # noqa: E402  (a store with ex-VAT, two periods)
import energy_charts as ec                       # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TheSummary(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.st = V._store()
        cls.cfg = cls.st.config_from_db(1)

    @classmethod
    def tearDownClass(cls):
        cls.st.close()

    def _ctx(self):
        return ec.billing_context(self.cfg, self.st, self.st.get_blocks_lightweight())

    def test_with_no_setting_it_is_bill_style(self):
        self.st.save_settings({})
        self.assertTrue(self._ctx()["bill_rounding"])

    def test_a_setting_saved_off_is_ignored(self):
        self.st.save_settings({"bill_rounding_summary": False})
        try:
            self.assertTrue(self._ctx()["bill_rounding"])
        finally:
            self.st.save_settings({})

    def test_GUARD_no_ex_vat_anywhere_keeps_the_inc_vat_sum(self):
        st = V._store()
        self.addCleanup(st.close)
        st._conn.execute("UPDATE blocks SET imp_cost_exc = NULL")
        st._conn.commit()
        self.assertFalse(ec.billing_context(st.config_from_db(1), st, st.get_blocks_lightweight())
                         ["bill_rounding"])


class TheSwitchIsGone(unittest.TestCase):

    def test_no_page_and_no_button(self):
        self.assertFalse(os.path.exists(os.path.join(ROOT, "web", "templates", "bill_rounding.html")))
        with open(os.path.join(ROOT, "web", "templates", "meter_config.html"), encoding="utf-8") as f:
            self.assertNotIn("bill_rounding_page", f.read())


if __name__ == "__main__":
    unittest.main()
