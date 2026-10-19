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

    def test_before_the_split_agreement_emts_split_is_not_pending(self):
        """PR 5 on the Pi, 14 Oct 2026: before the IOG-SMB agreement Octopus has no split to give,
        so a dispatched half-hour with Octopus's cost is green, not teal, and has no split state."""
        emt = dict(BILLED, completed=1, ev_kwh=2.0)
        st = _c(_f(**emt), split_at=lambda s: False)
        self.assertEqual((st["bill"], st["split"]), ("applied", None))
        self.assertEqual(_c(_f(**emt), split_at=lambda s: True)["bill"], "applied_emt_split")

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


class EveryIntelligentHalfHourNeedsItsBill(unittest.TestCase):
    """18 Oct 2026, the user, reverting #202: on EVERY Intelligent tariff, capped (IOG-SMB) or not, a
    dispatch can re-band a half-hour to off-peak, and EMT knows only the dispatches it saw — so only
    Octopus's cost is final there. #202 made an undispatched half-hour on an uncapped Intelligent
    agreement final at the schedule's rate, as on a published tariff; it is not. Discriminating
    against #202 (fail with it, pass on its parent and here): the first two. The rest are guards."""

    def _b(self, **kw):
        ctx = {"split_at": lambda s: False, "kind_at": lambda s: "intelligent"}
        ctx.update(kw.pop("ctx", {}))
        return _c(_f(**kw), **ctx)["bill"]

    def test_undispatched_off_iog_smb_waits_for_its_bill(self):
        self.assertEqual(self._b(), "estimate_dcc")

    def test_and_past_the_window_it_is_overdue(self):
        self.assertEqual(self._b(block_start="2026-08-09T02:00:00"), "overdue")

    def test_its_bill_makes_it_final(self):
        self.assertEqual(self._b(**BILLED), "applied")

    def test_on_a_published_tariff_the_settled_kwh_is_the_bill(self):
        self.assertEqual(self._b(ctx={"kind_at": lambda s: "published"}), "applied")


class TheExport(unittest.TestCase):
    """The Export bill's state is keyed on whether the export kWh has SETTLED (17 Oct 2026). A meter
    with no live export sensor has no export kWh until DCC settles it; an unsettled half-hour read
    0 and showed "none" — grey, like a night with nothing exported — so the view never showed what
    was still to settle. Discriminating (fail on the parent's behaviour; on the parent itself they
    also fail by the new `export_channel` argument): the first three. The rest are guards."""

    def _e(self, start=S, **kw):
        return _c(_f(block_start=start, **kw), export_channel=True)["export"]

    def test_no_live_reading_not_settled_is_waiting(self):
        self.assertEqual(self._e(exp_kwh=None, exp_kwh_api=None), "waiting")

    def test_no_live_reading_not_settled_after_7_days_is_overdue(self):
        self.assertEqual(self._e("2026-10-01T12:00:00", exp_kwh=None, exp_kwh_api=None), "overdue")

    def test_a_settled_export_is_applied(self):
        self.assertEqual(self._e(exp_kwh=None, exp_kwh_api=1.2), "applied")

    def test_a_settled_nothing_is_none(self):
        self.assertEqual(self._e(exp_kwh=None, exp_kwh_api=0.0), "none")

    def test_a_live_reading_waits_as_before(self):
        self.assertEqual(self._e(exp_kwh=0.8, exp_kwh_api=None), "waiting")

    def test_a_live_reading_of_nothing_is_none(self):
        self.assertEqual(self._e(exp_kwh=0.0, exp_kwh_api=None), "none")

    # ── no live export SENSOR (18 Oct 2026, the user): finalise's 0.0 is no reading ──
    def _n(self, start=S, **kw):
        return _c(_f(block_start=start, **kw), export_channel=True, export_sensor=False)["export"]

    def test_no_sensor_a_stored_zero_not_settled_is_waiting(self):
        """Discriminating: the grey runs between amber on an API-only account."""
        self.assertEqual(self._n(exp_kwh=0.0, exp_kwh_api=None), "waiting")

    def test_no_sensor_a_stored_zero_after_7_days_is_overdue(self):
        """Discriminating."""
        self.assertEqual(self._n("2026-10-01T12:00:00", exp_kwh=0.0, exp_kwh_api=None), "overdue")

    def test_no_sensor_a_settled_nothing_is_still_none(self):
        """GUARD."""
        self.assertEqual(self._n(exp_kwh=0.0, exp_kwh_api=0.0), "none")

    def test_no_sensor_csv_history_is_still_none(self):
        """GUARD: imported history with nothing exported stays grey."""
        self.assertEqual(self._n(source="imported_csv", exp_kwh=0.0, exp_kwh_api=None), "none")

    def test_the_route_says_whether_there_is_an_export_sensor(self):
        """Discriminating: the route passes it."""
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "web", "server.py"), encoding="utf-8").read()
        body = src[src.index("def _settlement_states"):]
        self.assertIn("export_sensor=_main_has_export_sensor()", body[:body.index("\n\n\n")])

    def test_no_export_channel_is_none(self):
        """An import-only install: nothing to settle."""
        self.assertEqual(_c(_f(exp_kwh=None, exp_kwh_api=None))["export"], "none")

    # ── no export agreement for the half-hour (17 Oct 2026, the user): new code, guards ──
    def _a(self, covered, **kw):
        kw.setdefault("exp_kwh", None)
        kw.setdefault("exp_kwh_api", None)
        return _c(_f(**kw), export_channel=True,
                  export_agreement_at=lambda s: covered)["export"]

    def test_no_agreement_is_not_waiting(self):
        self.assertEqual(self._a(False), "no_agreement")

    def test_no_agreement_settled_kwh_is_not_applied(self):
        """No export rate: the settled kWh earns nothing, so there is no bill to call final."""
        self.assertEqual(self._a(False, exp_kwh_api=1.2), "no_agreement")

    def test_an_agreement_waits(self):
        self.assertEqual(self._a(True), "waiting")

    def test_not_known_waits(self):
        self.assertEqual(self._a(None), "waiting")

    def test_nothing_exported_stays_none(self):
        self.assertEqual(self._a(False, exp_kwh_api=0.0), "none")

    def test_an_api_import_keeps_its_cost(self):
        self.assertEqual(self._a(False, source="imported_api", exp_kwh=1.2), "applied")

    def test_the_route_passes_the_agreement_check(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "web", "server.py"), encoding="utf-8").read()
        body = src[src.index("def _settlement_states"):]
        self.assertIn("export_agreement_at=_eng.export_agreement_at", body[:body.index("\n\n\n")])

    def test_the_route_says_whether_there_is_an_export_channel(self):
        """GUARD: the heatmap's states are classified with the main meter's export channel."""
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "web", "server.py"), encoding="utf-8").read()
        body = src[src.index("def _settlement_states"):]
        self.assertIn("export_channel=_main_has_export()", body[:body.index("\n\n\n")])


class TheExportAgreement(unittest.TestCase):
    """GUARD (new code): engine.export_agreement_at, from the agreements discovery holds."""

    def setUp(self):
        import engine
        self.eng, self._save = engine, engine._kraken_discovery

    def tearDown(self):
        self.eng._kraken_discovery = self._save

    def _at(self, export, start="2026-10-12T02:00:00"):
        self.eng._kraken_discovery = {"import": {}, "export": export}
        return self.eng.export_agreement_at(start)

    def test_no_export_mpan_is_not_known(self):
        self.assertIsNone(self._at(None))

    def test_an_mpan_with_no_agreements_is_none_covered(self):
        self.assertIs(self._at({"mpan": "1", "agreements": []}), False)

    def test_inside_an_agreement(self):
        ag = [{"tariff_code": "E-1R-OUTGOING-FIX-12M-19-05-13-H",
               "valid_from": "2026-01-01T00:00:00Z", "valid_to": None}]
        self.assertIs(self._at({"mpan": "1", "agreements": ag}), True)

    def test_before_an_agreement(self):
        ag = [{"tariff_code": "E-1R-OUTGOING-FIX-12M-19-05-13-H",
               "valid_from": "2026-10-13T00:00:00Z", "valid_to": None}]
        self.assertIs(self._at({"mpan": "1", "agreements": ag}), False)


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

    # The Split view went on 17 Oct 2026; the Import bill carries `missing` as attention.
    def test_a_missing_split_is_an_attention_flag(self):
        """Discriminating: on the parent the flag does not exist."""
        self.assertIn("split_missing", _c(_f(completed=1, bill_ev_kwh=0.0, **BILLED))["flags"])
        self.assertIn("split_missing", _c(_f(completed=1))["flags"])

    def test_no_flag_for_any_other_split(self):
        """GUARD."""
        for f, ctx in ((_f(completed=1, ev_kwh=3.1), {}),
                       (_f(completed=1, bill_ev_kwh=3.1, **BILLED), {}),
                       (_f(completed=1, ev_kwh=3.1), {"held": {S}}),
                       (_f(completed=1, ev_kwh=3.1), {"kept": {S}}),
                       (_f(), {})):
            self.assertNotIn("split_missing", _c(f, **ctx)["flags"])


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
             unittest.mock.patch.object(eng, "bill_no_cost", create=True, return_value=(set(), set())), \
             unittest.mock.patch.object(eng, "split_at", create=True, return_value=True), \
             unittest.mock.patch.object(eng, "export_agreement_at", create=True, return_value=None), \
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

    def test_the_split_view_is_gone(self):
        """17 Oct 2026, the user: the Import bill carries the split. Discriminating."""
        import ast
        src = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "_settle_views")
        self.assertNotIn("'split'", ast.unparse(fn))

    def test_billing_source_names_the_views(self):
        import ast
        src = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "api_billing_source_get")
        self.assertIn("'settle_views': _settle_views(_eng)", ast.unparse(fn))

    def test_the_heatmap_draws_both_views(self):
        page = open(os.path.join(_ROOT, "web", "templates", "heatmap_module.html"), encoding="utf-8").read()
        for needle in ("var SETTLE_METRICS", "settleColour(state.metric, v)", "'settle,'",
                       "r.settle_views", "settle: r.settle"):
            self.assertIn(needle, page)


if __name__ == "__main__":
    unittest.main()
