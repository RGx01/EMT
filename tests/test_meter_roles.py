"""
A meter's role comes from its type, and its id only names which one (14 Nov 2026, the user: "a
combination of type and id … useful if there are multiple chargers or heat pumps").

The display and setup code found the EV by testing the meter id as a substring — `"ev" in id`,
`id.startswith("ev_")`, `/ev|charger/` — wherever it needed one. So:
- a typed battery or heat pump whose id merely contained "ev" (`battery_level`, `heat_pump_dev`)
  was taken for THE EV when it came first, by the charts, Usage Stats and the setup page;
- a second EV the wizard made (`sub_meter_<n>`) drew its Billing price line on the house's curve
  (#133 fixed it for the first EV only).

meter_roles: the meter's `meter_type` when it has one; only an untyped meter is guessed, from the
whole words of its id. The pricing core (settle, the engine's device fit) already read the type.

Discriminating: the tests in TheEvIsFoundByType, ASecondEvMeter and TheSetupPage fail on the
parent; TheRoles tests a module that is new here; the GUARD tests pass on both.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import energy_charts as ec                       # noqa: E402
import usage_view as uv                          # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = "electricity_main"
WIZ1, WIZ2 = "sub_meter_1700000000001", "sub_meter_1700000000002"


def _cfg(*subs):
    m = {MAIN: {"meta": {}}}
    for mid, t in subs:
        m[mid] = {"meta": dict({"sub_meter": True}, **({"meter_type": t} if t else {}))}
    return {"meters": m}


class TheRoles(unittest.TestCase):

    def test_the_type_wins(self):
        import meter_roles as r
        self.assertEqual(r.role("battery_level", {"meter_type": "battery"}), "battery")
        self.assertEqual(r.role(WIZ1, {"meter_type": "ev"}), "ev")
        self.assertEqual(r.role("ev_charger", {"meter_type": "ev_charger"}), "ev")
        self.assertEqual(r.role("ev_spare", {"meter_type": "other"}), "other")   # never re-guessed

    def test_an_untyped_meter_is_guessed_by_whole_words(self):
        import meter_roles as r
        for name, want in (("ev_charger", "ev"), ("EV Charger", "ev"), ("ev_dispatch", "ev"),
                           ("house_battery", "battery"), ("heat_pump", "heat_pump"),
                           ("heatpump", "heat_pump"), ("solar", "inverter"),
                           ("battery_level", "battery"), ("devices", None), ("level", None),
                           (WIZ1, None), (MAIN, None), (None, None)):
            self.assertEqual(r.role(name, {}), want, name)

    def test_every_meter_of_a_role_in_config_order(self):
        import meter_roles as r
        cfg = _cfg((WIZ1, "ev"), ("house_battery", "battery"), (WIZ2, "ev"))
        self.assertEqual(r.meters_of(cfg, "ev"), [WIZ1, WIZ2])
        self.assertEqual(r.meters_of(cfg, "battery"), ["house_battery"])


class TheEvIsFoundByType(unittest.TestCase):

    def test_a_battery_named_level_is_not_the_ev(self):
        cfg = _cfg(("battery_level", "battery"), (WIZ1, "ev"))
        self.assertEqual(ec._ev_meter_id(cfg), WIZ1)
        self.assertEqual(uv._configured_ev_meter_id(cfg), WIZ1)

    def test_nor_a_heat_pump(self):
        cfg = _cfg(("heat_pump_dev", "heat_pump"), (WIZ1, "ev"))
        self.assertEqual(ec._ev_meter_id(cfg), WIZ1)
        self.assertEqual(uv._configured_ev_meter_id(cfg), WIZ1)

    def test_an_untyped_charger_is_still_the_ev(self):
        """GUARD: an older config with no meter_type keeps its EV by its id."""
        cfg = _cfg(("ev_charger", None), ("house_battery", None))
        self.assertEqual(ec._ev_meter_id(cfg), "ev_charger")
        self.assertEqual(uv._configured_ev_meter_id(cfg), "ev_charger")

    def test_no_ev_is_none(self):
        """GUARD."""
        cfg = _cfg(("house_battery", "battery"))
        self.assertIsNone(ec._ev_meter_id(cfg))
        self.assertIsNone(uv._configured_ev_meter_id(cfg))


OFF, DAY = 0.0523, 0.3077
DAYSTR = "2026-11-10"
IDLE, CHARGE = 20, 46


def _block(hh, evs):
    """A half-hour: the house on `DAY` at IDLE, on `OFF` at CHARGE, where the first EV draws 3 kWh."""
    ev_kwh = 3.0 if hh == CHARGE else 0.0
    house_rate = DAY if hh == IDLE else OFF
    segs = [{"kwh": 0.4, "inc_rate": house_rate, "exc_rate": None,
             "band": "day" if hh == IDLE else "off_peak", "attribution": "house"}]
    if ev_kwh:
        segs.append({"kwh": ev_kwh, "inc_rate": OFF, "exc_rate": None, "band": "off_peak",
                     "attribution": "ev"})
    kwh = 0.4 + ev_kwh
    cost = 0.4 * house_rate + ev_kwh * OFF
    meters = {MAIN: {"meta": {}, "standing_charge": 0.0,
                     "channels": {"import": {"kwh": kwh, "cost": round(cost, 6),
                                             "rate": round(cost / kwh, 6), "segments": segs}}}}
    for i, mid in enumerate(evs):
        k = ev_kwh if i == 0 else 0.0
        meters[mid] = {"meta": {"sub_meter": True, "meter_type": "ev"},
                       "channels": {"import": {"kwh": k, "kwh_grid": k, "cost": round(k * OFF, 6),
                                               "rate": OFF}}}
    start = "%sT%02d:%02d:00" % (DAYSTR, *divmod(hh * 30, 60))
    return (hh, {"start": start, "meters": meters})


class ASecondEvMeter(unittest.TestCase):

    def test_its_line_idles_off_peak_too(self):
        evs = [WIZ1, WIZ2]
        d = ec.day_chart_data(DAYSTR, [_block(IDLE, evs), _block(CHARGE, evs)],
                              {MAIN: "#1f77b4", WIZ1: "#bcbd22", WIZ2: "#e377c2"},
                              cap_from=[[DAYSTR, None]], import_schedule=None, timezone_name="UTC",
                              block_minutes=30, currency="£", ev_fold_meter=WIZ1, ev_label="EV")
        m = d["chart"]["meters"]
        self.assertAlmostEqual(m[MAIN]["rate"][IDLE], DAY, places=4)
        self.assertAlmostEqual(m[WIZ1]["rate"][IDLE], OFF, places=4)
        self.assertAlmostEqual(m[WIZ2]["rate"][IDLE], OFF, places=4)     # was the house's DAY


PAGE = open(os.path.join(_ROOT, "web", "templates", "meter_config.html"), encoding="utf-8").read()


def _fn(name):
    i = PAGE.index("function " + name + "(")
    return PAGE[i:PAGE.index("\n}\n", i) + 2]


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheSetupPage(unittest.TestCase):

    def _has_ev(self, cfg):
        names = [n for n in ("_roleFromId", "_hasEvDevice") if ("function " + n + "(") in PAGE]
        js = "\n".join(_fn(n) for n in names) + \
            "\nvar config = %s;\nconsole.log(JSON.stringify(_hasEvDevice()));" % json.dumps(cfg)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
            path = fh.name
        try:
            res = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(path)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def test_a_battery_named_level_is_no_ev_device(self):
        self.assertFalse(self._has_ev(_cfg(("battery_level", "battery"))))

    def test_a_wizard_ev_and_an_untyped_charger_are(self):
        """GUARD."""
        self.assertTrue(self._has_ev(_cfg((WIZ1, "ev"))))
        self.assertTrue(self._has_ev(_cfg(("ev_charger", None))))


if __name__ == "__main__":
    unittest.main()
