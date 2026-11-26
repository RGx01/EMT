"""The bill card prices an Agile half-hour as the statement does (26 Nov 2026, the user).

A beta tester's Agile card read above his statement, and EMT-Lite agreed with the card.
The statement's half-hourly pages showed why: every half-hour is priced at its rate × its kWh rounded
to 0.01, half to even, and a house that runs from a battery has hundreds of half-hours a month of a
few watt-hours, which the statement prices at nothing. Rounding his stored readings that way
reproduced all 1,440 of the statement's half-hours, and its totals.

The card's statement method (always on where EMT holds ex-VAT figures) now takes an Agile half-hour,
import or Agile Outgoing export, at that kWh. What EMT stores, and every other surface, keeps the
meter's reading. An Intelligent statement prints rounded kWh but charges Octopus's own cost, so it
is left as stored. Figures here are made up.

Discriminating: every test fails on the parent but those labelled GUARD.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from block_store import BlockStore                    # noqa: E402
import bill_card                                      # noqa: E402
import billing_view                                   # noqa: E402

TZ = "Europe/London"
ACC = "A-0000ABC7"
AGILE_IN = "E-1R-AGILE-24-10-01-A"
AGILE_OUT = "E-1R-AGILE-OUTGOING-19-05-13-A"
INTELLI = "E-1R-INTELLI-VAR-22-10-14-A"

# 5 Sep 2026 (BST), five half-hours from 12:00 local: (imp kWh, imp rate ex VAT £, exp kWh, exp rate £)
HH = [(0.004, 0.30, 0.005, 0.15),       # statement: 0.00 import, 0.00 export (a tie, to even)
      (0.005, 0.30, 0.015, 0.15),       # 0.00 (a tie, to even) / 0.02 (a tie, to even)
      (0.015, 0.10, 0.0125, 0.15),      # 0.02 / 0.01
      (1.945, 0.20, 2.0, 0.15),         # 1.94 (a tie, to even) / 2.00
      (0.333, -0.05, 0.0, 0.15)]        # 0.33 at a plunge price
START, END = "2026-09-05T11:00:00", "2026-09-05T13:30:00"     # UTC


def _store(agreements):
    st = BlockStore(os.path.join(tempfile.mkdtemp(), "blocks.db"))
    st._conn.execute("PRAGMA foreign_keys=OFF")
    st._conn.execute("INSERT INTO config_periods (id, effective_from, billing_day, block_minutes, timezone) "
                     "VALUES (1, '2026-01-01T00:00:00', 5, 30, ?)", (TZ,))
    st._conn.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter) VALUES (1, 'electricity_main', 0)")
    h, m = 11, 0
    for k, r, ek, er in HH:
        bs = "2026-09-05T%02d:%02d:00" % (h, m)
        st._conn.execute(
            "INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, imp_rate, imp_cost, "
            "imp_cost_exc, exp_kwh, exp_rate, exp_cost, standing_charge) VALUES (?,?,'electricity_main',1,?,?,?,?,?,?,?,?)",
            (bs, bs, k, r * 1.05, round(k * r * 1.05, 6), round(k * r, 6), ek, er, round(ek * er, 6), 0.5))
        h, m = (h, 30) if m == 0 else (h + 1, 0)
    for direction, code, vf, vt in agreements:
        st._conn.execute("INSERT INTO tariff_agreements (account, mpan, direction, tariff_code, product_code, "
                         "valid_from, valid_to, fetched_at) VALUES (?, '2600000000001', ?, ?, '', ?, ?, '2026-11-26')",
                         (ACC, direction, code, vf, vt))
    st._conn.commit()
    st.set_kraken_state("kraken_account_number", ACC)
    return st


def _card(agreements):
    st = _store(agreements)
    try:
        sm, _ctx, _b = billing_view.period_summary_raw(st, st.config_from_db(1), TZ, START, END)
    finally:
        st.close()
    bm = sm["bill_method"]
    exp = next(v for k, v in sm["totals"].items() if k.endswith("/ Export"))
    return {"imp_kwh": round(sum(r["kwh"] for r in bm["rows"]), 6), "energy_exc": bm["energy_exc"],
            "exp_kwh": round(exp["kwh"], 6), "exp_cost": round(exp["cost"], 6), "total": bill_card.bill_total(sm)}


AGILE = [("import", AGILE_IN, "2026-02-15T00:00:00", None), ("export", AGILE_OUT, "2026-01-01T00:00:00", None)]


class TheStatementKwh(unittest.TestCase):

    def test_half_to_even(self):
        import statement_kwh as sk
        got = [sk.statement_kwh(x) for x in (0.004, 0.005, 0.0050000000001, 0.006, 0.015, 0.025, 1.945, 0.0125, 0)]
        self.assertEqual(got, [0.0, 0.0, 0.0, 0.01, 0.02, 0.02, 1.94, 0.01, 0.0])

    def test_agile_import_and_outgoing_only(self):
        import agile_bill as ab
        import statement_kwh as sk
        agr = [{"direction": "import", "tariff_code": INTELLI, "valid_from": "2026-01-01T00:00:00", "valid_to": "2026-03-01T00:00:00"},
               {"direction": "import", "tariff_code": AGILE_IN, "valid_from": "2026-03-01T00:00:00", "valid_to": None},
               {"direction": "export", "tariff_code": "E-1R-OUTGOING-FIX-12M-19-05-13-A", "valid_from": "2026-01-01T00:00:00", "valid_to": None}]
        self.assertEqual(ab.agile_windows(agr, "import"), [("2026-03-01T00:00:00", None)])
        self.assertEqual(ab.agile_windows(agr, "export"), [])
        inside = sk.covers(ab.agile_windows(agr, "import"))
        self.assertEqual([inside("2026-02-28T23:30:00"), inside("2026-03-01T00:00:00")], [False, True])
        self.assertIsNone(sk.covers([]))


class TheCard(unittest.TestCase):

    def test_agile_import_at_the_statements_kwh(self):
        c = _card(AGILE)
        self.assertEqual(c["imp_kwh"], 2.29)                  # 0 + 0 + 0.02 + 1.94 + 0.33; the meter 2.302
        # 0.02 × 0.10 + 1.94 × 0.20 + 0.33 × −0.05 = 0.3735 (the meter's 0.37655 → 0.38)
        self.assertEqual(c["energy_exc"], 0.37)

    def test_agile_outgoing_at_the_statements_kwh(self):
        c = _card(AGILE)
        self.assertEqual(c["exp_kwh"], 2.03)                  # 0 + 0.02 + 0.01 + 2.00; the meter 2.0325

    def test_GUARD_intelligent_is_summed_as_stored(self):
        c = _card([("import", INTELLI, "2026-02-15T00:00:00", None)])
        self.assertEqual(c["imp_kwh"], 2.302)
        self.assertEqual(c["energy_exc"], 0.38)
        self.assertEqual(c["exp_kwh"], 2.033)

    def test_only_the_half_hours_on_agile(self):
        # Agile from 12:30 local (11:30 UTC): the first half-hour keeps its 0.004, the rest are rounded
        c = _card([("import", INTELLI, "2026-02-15T00:00:00", "2026-09-05T11:30:00"),
                   ("import", AGILE_IN, "2026-09-05T11:30:00", None)])
        self.assertEqual(c["imp_kwh"], 2.294)

    def test_GUARD_no_account_no_rounding(self):
        st = _store(AGILE)
        st.set_kraken_state("kraken_account_number", "")
        try:
            sm, ctx, _b = billing_view.period_summary_raw(st, st.config_from_db(1), TZ, START, END)
        finally:
            st.close()
        self.assertEqual(round(sum(r["kwh"] for r in sm["bill_method"]["rows"]), 6), 2.302)


class ThePickerLabel(unittest.TestCase):
    """The Billing tab's picker labels a period with each day's net, summed; on Agile each day takes
    what the card's rounding moves it by (agile_bill.statement_day_adjustments), so the label reads
    within pennies of the card below it."""

    def _adj(self, agreements):
        import agile_bill as ab
        st = _store(agreements)
        try:
            return ab.statement_day_adjustments(st, TZ, START, END)
        finally:
            st.close()

    def test_import_less_export(self):
        got = self._adj(AGILE)
        self.assertEqual(list(got), ["2026-09-05"])
        # import £ inc VAT −0.0032025 (the meter's half-hours priced at the statement's kWh), less the
        # export credit's −0.000375
        self.assertAlmostEqual(got["2026-09-05"], -0.0028275, places=7)

    def test_GUARD_intelligent_moves_nothing(self):
        self.assertEqual(self._adj([("import", INTELLI, "2026-02-15T00:00:00", None)]), {})


class TheWiringOfTheCards(unittest.TestCase):

    def test_the_picker_and_this_bill(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "server.py"),
                   encoding="utf-8").read()
        self.assertIn("_ab.statement_day_adjustments(store, tz_name, span[0], span[1])\n"
                      "                    if kind != \"live\" else {})", src)
        self.assertIn('p["statement_rounded"] = bool(_ab.statement_day_adjustments(', src)

    def test_this_bills_question_mark(self):
        page = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "templates",
                                 "live_power.html"), encoding="utf-8").read()
        self.assertIn("updateBillingCard(c[0], p.net_cost, p.rows, p.label, BILL_CURRENCY, !!p.statement_rounded);", page)
        self.assertIn("function _billQ(id, on) {", page)
        self.assertEqual(page.count("_billQ(id, rounded);"), 2)


if __name__ == "__main__":
    unittest.main()
