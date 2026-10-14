"""
BL-85 PR 1 (settlement surfaces design §1–§3): each main-meter half-hour's bill and split state.

`settlement_state.classify` decides it from `BlockStore.get_settlement_facts`; `/api/energy`
returns it with `include=settle`; the Heatmaps tab draws two views of it. Checked against the 13 Oct
2026 backups: prod (after Octopus restated October) has nothing red; dev that morning shows 162
October half-hours whose bill still carried 5% VAT (#505) and one completed dispatch awaiting its
split.

Discriminating: every test here fails on the parent (none of this exists there).
"""
import os
import sys
import unittest
import unittest.mock
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
import test_rebuild_meter_type as H              # noqa: E402  (stubs, real engine)
from block_store import BlockStore               # noqa: E402
import settlement_state as ss                    # noqa: E402
engine = H.engine
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

OFF = 0.05493
NOW = datetime(2026, 10, 13, 12, 0)
S = "2026-10-12T02:00:00"


def _f(**kw):
    f = {"block_start": S, "imp_kwh": 6.0, "imp_kwh_api": 6.0, "imp_cost": round(6 * OFF, 6),
         "imp_rate": OFF, "rate_source": "schedule", "rate_corrected": 0, "source": None,
         "bill_cost": None, "bill_cost_exc": None, "bill_kwh": None, "bill_ev_kwh": None,
         "ev_kwh": None, "completed": 0, "dispatched": 0}
    f.update(kw)
    return f


def _c(f, **ctx):
    c = {"smb": True, "now": NOW, "vat_at": lambda s: 0.0}
    c.update(ctx)
    return ss.classify(f, **c)


BILLED = dict(rate_source="settled", bill_cost=round(6 * OFF, 6), bill_cost_exc=round(6 * OFF, 6),
              bill_kwh=6.0)


class TheBill(unittest.TestCase):

    def test_applied(self):
        self.assertEqual(_c(_f(**BILLED))["bill"], "applied")

    def test_noise_below_a_thousandth_of_a_penny_is_not_a_difference(self):
        """Prod, 11 Sep 02:00: stored £0.336996, bill £0.336993."""
        self.assertEqual(_c(_f(**dict(BILLED, imp_cost=round(6 * OFF, 6) + 3e-6)))["bill"], "applied")

    def test_differs(self):
        st = _c(_f(**dict(BILLED, imp_cost=round(6 * OFF, 6) + 0.02, imp_rate=OFF + 0.01)))
        self.assertEqual((st["bill"], st["flags"]), ("differs", []))

    def test_a_pre_fix_re_cost_is_known(self):
        """BL-82: the stored cost is kWh x rate, the bill is not."""
        st = _c(_f(**dict(BILLED, bill_cost=round(6 * OFF, 6) + 0.0004)))
        self.assertEqual((st["bill"], st["flags"]), ("differs", ["recost_bl82"]))

    def test_estimate_on_which_kwh_then_overdue(self):
        """§9.3: settled kWh x rate is light amber, a kWh not settled amber; red past the window."""
        self.assertEqual(_c(_f())["bill"], "estimate_dcc")
        self.assertEqual(_c(_f(imp_kwh_api=None))["bill"], "estimate_cad")
        self.assertEqual(_c(_f(block_start="2026-10-01T02:00:00"))["bill"], "overdue")
        self.assertEqual(_c(_f(block_start="2026-10-01T02:00:00", imp_kwh_api=None))["bill"], "overdue")

    def test_a_published_rate_half_hour_is_final_on_its_settled_kwh(self):
        """§9.1: on flat / Go / Cosy / Agile the settled kWh x the published rate is the bill."""
        pub = lambda s: "published"
        self.assertEqual(_c(_f(), kind_at=pub)["bill"], "applied")
        self.assertEqual(_c(_f(block_start="2026-10-01T02:00:00"), kind_at=pub)["bill"], "applied")
        self.assertEqual(_c(_f(imp_kwh_api=None), kind_at=pub)["bill"], "estimate_cad")
        self.assertEqual(_c(_f(block_start="2026-10-01T02:00:00", imp_kwh_api=None),
                            kind_at=pub)["bill"], "overdue")

    def test_octopuss_cost_with_emts_split_is_teal(self):
        """§9.3: a green cost whose split is still EMT's says so; a missing split does not."""
        emt = dict(BILLED, completed=1, ev_kwh=2.0)
        self.assertEqual(_c(_f(**emt))["bill"], "applied_emt_split")
        self.assertEqual(_c(_f(**dict(emt, bill_ev_kwh=2.0)))["bill"], "applied")
        self.assertEqual(_c(_f(**dict(BILLED, completed=1, ev_kwh=0.0)))["bill"], "applied")

    def test_kwh_x_rate_outside_the_fetch_scope_is_red_after_the_window(self):
        """14 Oct 2026, revised §1.2: the view says whose cost a half-hour holds. 9 Aug 2026 on the
        soak — before the agreement, undispatched, grid used — read grey "no bill stage" while it
        held only kWh x rate. No tariff or agreement gate enters the colour now."""
        self.assertEqual(_c(_f(block_start="2026-08-09T03:00:00"))["bill"], "overdue")

    def test_an_api_import_holds_octopuss_cost(self):
        """The API importer stores Octopus's billed cost per half-hour."""
        self.assertEqual(_c(_f(source="imported_api", imp_kwh_api=None))["bill"], "applied")

    def test_a_csv_import_does_not(self):
        self.assertEqual(_c(_f(source="imported_csv", imp_kwh_api=None))["bill"], "imported")

    def test_nothing_imported_has_no_cost_to_settle(self):
        """GUARD: as before — a zero-kWh half-hour (solar covered it)."""
        self.assertEqual(_c(_f(imp_kwh=0.0, imp_cost=0.0))["bill"], "none")

    def test_corrected(self):
        """GUARD: blue for a manual correction, whatever else holds."""
        self.assertEqual(_c(_f(rate_source="corrected"))["bill"], "corrected")

    def test_the_views_need_no_fetch_scope(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "web", "server.py"), encoding="utf-8").read()
        i = src.index("def _settlement_states(")
        body = src[i:i + 900]
        self.assertNotIn("floor=", body)
        self.assertNotIn("every_half_hour=", body)

    def test_a_cached_bill_not_yet_applied_reads_as_an_estimate(self):
        """Transient: the next pass applies it."""
        self.assertEqual(_c(_f(**dict(BILLED, rate_source="schedule")))["bill"], "estimate_dcc")


class TheFlags(unittest.TestCase):

    def test_the_bill_priced_another_kwh(self):
        self.assertIn("bill_kwh_differs", _c(_f(**dict(BILLED, bill_kwh=5.5)))["flags"])

    def test_the_bills_vat_is_not_the_calendars(self):
        """#505: 5% on a 0% day."""
        st = _c(_f(**dict(BILLED, bill_cost=round(6 * OFF * 1.05, 6),
                          imp_cost=round(6 * OFF * 1.05, 6))))
        self.assertIn("vat_disagrees", st["flags"])


class TheSplit(unittest.TestCase):

    def test_no_completed_dispatch_no_split_state(self):
        self.assertIsNone(_c(_f())["split"])

    def test_off_smb_no_split_state(self):
        self.assertIsNone(_c(_f(completed=1, ev_kwh=3.1), smb=False)["split"])

    def test_the_states(self):
        self.assertEqual(_c(_f(completed=1, ev_kwh=3.1))["split"], "emt")
        self.assertEqual(_c(_f(completed=1, bill_ev_kwh=3.1, **BILLED))["split"], "bill")
        self.assertEqual(_c(_f(completed=1))["split"], "missing")
        self.assertEqual(_c(_f(completed=1, bill_ev_kwh=0.0, **BILLED))["split"], "missing")
        self.assertEqual(_c(_f(completed=1, ev_kwh=3.1), held={S})["split"], "held")
        self.assertEqual(_c(_f(completed=1, ev_kwh=3.1), kept={S})["split"], "kept")


class TheFacts(unittest.TestCase):

    def test_one_row_per_main_half_hour_with_its_bill_split_and_dispatch(self):
        st = BlockStore(":memory:")
        st._conn.execute("INSERT OR IGNORE INTO config_periods (id, effective_from, billing_day, "
                         "block_minutes, timezone) VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
        st._conn.execute("INSERT INTO blocks (block_start, block_end, meter_id, config_period_id, "
                         "imp_kwh, imp_kwh_api, imp_rate, imp_cost, rate_source) "
                         "VALUES (?,?,?,1,6.0,6.0,?,?,'settled')", (S, S, "electricity_main", OFF, 0.33))
        st.set_block_segments(S, "electricity_main", [(3.1, OFF, OFF, "off_peak", "ev"),
                                                      (2.9, OFF, OFF, "off_peak", "house")])
        st.upsert_measured_cost(S, mpan="m", cost_incl=0.33, cost_excl=0.33, label="OFF_PEAK", kwh=6.0)
        st.upsert_measured_breakdown(S, mpan="m", home_kwh=2.9, home_rate=OFF, ev_kwh=3.1, ev_rate=OFF)
        st._conn.execute("INSERT INTO dispatch_history (slot_start, kind, energy_kwh, first_seen, "
                         "last_seen) VALUES (?, 'completed', -3.1, ?, ?)", (S, S, S))
        st._conn.commit()
        (f,) = st.get_settlement_facts("2026-10-12T00:00:00", "2026-10-13T00:00:00")
        self.assertAlmostEqual(f["ev_kwh"], 3.1)
        self.assertAlmostEqual(f["bill_ev_kwh"], 3.1)
        self.assertEqual((f["completed"], f["bill_cost"]), (1, 0.33))
        self.assertEqual(_c(f, vat_at=lambda s: 0.0), {"bill": "applied", "export": "none", "split": "bill", "flags": []})


class TheSurfaces(unittest.TestCase):

    def test_the_route_sends_settle_only_when_asked(self):
        import ast
        src = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "api_energy")
        self.assertIn("if 'settle' in include:", ast.unparse(fn))

    def test_the_route_serves_it(self):
        """A real request: the facet must be one the route accepts (it refused it at first)."""
        from tests.test_server import make_client
        import json
        client = make_client()
        eng = sys.modules["engine"]            # whatever the server imports (test_server's stub)
        with unittest.mock.patch.object(eng, "bill_stage", create=True,
                                        return_value={"fetched": True, "every_half_hour": True,
                                                      "floor": "2020-01-01T00:00:00"}), \
             unittest.mock.patch.object(eng, "bill_split_held", create=True,
                                        return_value=(set(), set())), \
             unittest.mock.patch.object(eng, "tariff_kind_at", create=True,
                                        return_value="intelligent"), \
             unittest.mock.patch.object(eng, "_import_is_smb_capped", create=True,
                                        return_value=True):
            r = client.get("/api/energy?resolution=block&kind=month&year=2026&month=10"
                           "&include=settle")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:200])
        self.assertIsInstance(json.loads(r.data)["settle"], dict)
        bad = make_client().get("/api/energy?resolution=day&kind=month&include=settle")
        self.assertEqual(bad.status_code, 400)

    def test_the_bill_views_are_gated_on_the_api_connection(self):
        """§9.2: any tariff, an API connection — not bill_stage's Intelligent-only gate."""
        import ast
        src = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "_settle_views")
        body = ast.unparse(fn)
        self.assertIn("_eng.mode_uses_api() and _eng.has_kraken_credentials()", body)
        self.assertNotIn("bill_stage", body)

    def test_billing_source_names_the_views(self):
        import ast
        src = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "api_billing_source_get")
        self.assertIn("'settle_views': _settle_views(_eng)", ast.unparse(fn))

    def test_the_heatmap_draws_both_views(self):
        page = open(os.path.join(_ROOT, "web", "templates", "heatmap_module.html"), encoding="utf-8").read()
        for needle in ("var SETTLE_METRICS", "settleColour(state.metric, v)", "&include=settle",
                       "r.settle_views", "settle: r.settle"):
            self.assertIn(needle, page)


if __name__ == "__main__":
    unittest.main()
