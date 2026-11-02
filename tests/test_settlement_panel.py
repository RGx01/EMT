"""
Settlement surfaces PR 2 (design §4): one half-hour's settlement evidence — the detail panel.

`GET /api/blocks/settlement?start=` returns the half-hour's kWh (stored, DCC), its stored cost
and rate, its cached bill with when it was fetched, its segments, its dispatch, and its state;
a click on a heatmap cell opens it. Checked in the browser pane on the 13 Oct dev backup: 10 Oct's
05:30 BST half-hour reads billed, split from the bill, completed dispatch 2.6 kWh, and — that
morning — the VAT flag.

Discriminating: every test here fails on the parent (the route, the read and the panel are new).
"""
import json
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(__file__))
from tests.test_server import make_client, _memory_uri    # noqa: E402
from block_store import BlockStore                       # noqa: E402
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

S = "2026-10-10T04:30:00"
OFF = 0.05493


def _store():
    st = BlockStore(_memory_uri())
    st.insert_config_period({"meters": {"electricity_main": {"meta": {
        "billing_day": 1, "block_minutes": 30, "timezone": "Europe/London",
        "currency_symbol": "£", "currency_code": "GBP"}, "channels": {"import": {}}}}})
    pid = st.get_current_config_period_id()
    st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, imp_kwh, "
                     "imp_kwh_api, imp_rate, imp_cost, imp_cost_exc, rate_source) "
                     "VALUES (?,?,?,?,2.738,2.738,?,0.150397,0.150397,'settled')",
                     (S, "2026-10-10T05:00:00", "electricity_main", pid, OFF))
    st.set_block_segments(S, "electricity_main", [(2.5714, OFF, OFF, "off_peak", "ev"),
                                                  (0.1666, OFF, OFF, "off_peak", "house")])
    st.upsert_measured_cost(S, mpan="m", cost_incl=0.150397, cost_excl=0.150397, label="OFF_PEAK",
                            kwh=2.738)
    st.upsert_measured_breakdown(S, mpan="m", home_kwh=0.1666, home_rate=OFF, ev_kwh=2.5714,
                                 ev_rate=OFF)
    st._conn.execute("INSERT INTO dispatch_history (slot_start, kind, energy_kwh, first_seen, last_seen) "
                     "VALUES (?, 'completed', -2.6, ?, ?)", (S, S, S))
    st._conn.commit()
    return st


class TheRead(unittest.TestCase):

    def test_the_block_and_its_bill(self):
        d = _store().get_settlement_detail(S)
        self.assertEqual((d["imp_kwh"], d["bill_cost"], d["bill_ev_kwh"]), (2.738, 0.150397, 2.5714))
        self.assertTrue(d["bill_fetched_at"])
        self.assertIsNone(_store().get_settlement_detail("2026-01-01T00:00:00"))


class TheRoute(unittest.TestCase):

    def _get(self, start):
        client = make_client(store=_store())
        eng = sys.modules["engine"]
        with unittest.mock.patch.object(eng, "bill_stage", create=True,
                                        return_value={"fetched": True, "every_half_hour": True,
                                                      "floor": "2020-01-01T00:00:00"}), \
             unittest.mock.patch.object(eng, "bill_split_held", create=True, return_value=(set(), set())), \
             unittest.mock.patch.object(eng, "bill_no_cost", create=True, return_value=set()), \
             unittest.mock.patch.object(eng, "split_at", create=True, return_value=True), \
             unittest.mock.patch.object(eng, "export_agreement_at", create=True, return_value=None), \
             unittest.mock.patch.object(eng, "tariff_kind_at", create=True,
                                        return_value="intelligent"), \
             unittest.mock.patch.object(eng, "_import_is_smb_capped", create=True, return_value=True):
            return client.get("/api/blocks/settlement?start=" + start)

    def test_the_three_facts_with_their_sources(self):
        r = self._get(S)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:200])
        d = json.loads(r.data)
        self.assertEqual(d["kwh"], {"stored": 2.738, "dcc": 2.738, "settled": True, "origin": "dcc"})
        self.assertEqual(d["bill"]["cost"], 0.150397)
        self.assertEqual(d["bill"]["origin"], "octopus")
        self.assertEqual(d["bill"]["ev_kwh"], 2.5714)
        self.assertEqual(d["dispatch"]["completed_kwh"], 2.6)
        self.assertEqual(d["state"], {"bill": "applied", "export": "none", "split": "bill", "flags": []})
        self.assertEqual(sorted(s["attribution"] for s in d["segments"]), ["ev", "house"])

    def test_the_band_against_the_rules(self):
        """Engine step 2b: the panel carries Octopus's band against EMT's rules, asked with the
        bill's label and its buckets."""
        eng = sys.modules["engine"]
        seen = []
        note = {"settled": "off_peak", "predicted": "peak", "disagrees": True}
        with unittest.mock.patch.object(eng, "band_note", create=True,
                                        side_effect=lambda *a, **k: seen.append((a, k)) or note):
            d = json.loads(self._get(S).data)
        self.assertEqual(d["band"], note)
        self.assertEqual(seen[0][0][2], "OFF_PEAK")
        self.assertEqual(seen[0][0][3]["ev_kwh"], 2.5714)
        # engine step 4a-2: and the bill's cost and kWh, for an imported bill with no label
        self.assertEqual(set(seen[0][1]), {"cost", "bill_kwh"})

    def test_no_block_is_404_and_a_bad_start_is_400(self):
        self.assertEqual(self._get("2026-01-01T00:00:00").status_code, 404)
        self.assertEqual(self._get("yesterday").status_code, 400)


class ThePanel(unittest.TestCase):
    PAGE = open(os.path.join(_ROOT, "web", "templates", "heatmap_module.html"), encoding="utf-8").read()

    def test_a_band_against_the_rules_is_a_note(self):
        """Green with a note (the user, 23 Oct 2026): a row, not the bad style."""
        i = self.PAGE.index("var bn = d.band")
        body = self.PAGE[i:i + 400]
        self.assertIn("if (bn && bn.disagrees)", body)
        self.assertIn("billed wins until Octopus corrects it');", body)
        self.assertNotIn("hm-bad", body)

    def test_a_cell_knows_its_half_hours_and_a_click_opens_the_panel(self):
        self.assertIn("(c.starts || (c.starts = [])).push(b.block_start)", self.PAGE)
        self.assertIn("row.addEventListener('click'", self.PAGE)
        self.assertIn("openPanel(h.starts,", self.PAGE)
        self.assertIn("/api/blocks/settlement?start=", self.PAGE)

    def test_it_escapes_what_it_shows(self):
        i = self.PAGE.index("function renderPanel(")
        body = self.PAGE[i:self.PAGE.index("\n  }\n", i)]
        self.assertIn("esc(label)", body)
        self.assertNotIn("+ b.label +", body)


if __name__ == "__main__":
    unittest.main()
