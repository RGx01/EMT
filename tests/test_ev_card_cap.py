"""
The car's 6 hours on the Overview's EV card (22 Nov 2026, the user: "if the tariff agreement is SMB could show
estimated charge length or remaining charge time against the 6hr cap").

On an agreement the 6-hour cap prices (`engine.capped_at`, IOG-SMB), `/api/overview/days` carries `ev_cap` for
today's noon-to-noon cap-day (`server._ev_cap_now`): the minutes EMT counts as charged so far — the Smart Charging
card's count, `iog_cap.cap_usage` over the same rows (`server._cap_rows`, now shared by both) — the minutes
Octopus's current plan would charge before the next noon (`_build_upcoming_dispatches`), and the plan's windows,
local. The EV card shows it as a bar and two lines: used and planned; left (or over), and the next window.

Driven in a browser on a scratch copy with a car: the strip with 1h 40m used and 2h 30m planned, and with a plan
past the cap (red, "over the cap"); the row kept its height. The copy loads no agreements, so the cap was fed to
the page there; the route is tested here.

New route field, helper and strip: every test fails on the parent tree.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from zoneinfo import ZoneInfo

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "web"))

from block_store import BlockStore      # noqa: E402

PAGE = open(os.path.join(_ROOT, "web", "templates", "live_power.html"), encoding="utf-8").read()
TZ = "Europe/London"
LON = ZoneInfo(TZ)


def _server():
    spec = importlib.util.spec_from_file_location("server_evcap", os.path.join(_ROOT, "web", "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    return srv


def _u(t):
    return t.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


class TheCapToday(unittest.TestCase):
    """At 20:00 local on 22 Nov (GMT): today's cap-day runs from noon. Charged 13:00–14:00 at 7 kW (one hour),
    and yesterday morning (the cap-day before) at 02:00; planned tonight 02:00–04:00 at 7 kW (two hours) and
    tomorrow 13:00 (the next cap-day)."""

    NOW = datetime(2026, 11, 22, 20, 0, tzinfo=LON)

    @classmethod
    def setUpClass(cls):
        cls.srv = _server()
        cls.st = BlockStore(os.path.join(tempfile.mkdtemp(), "blocks.db"))
        n = cls.NOW

        def slot(t, kind, kwh, seen=None):
            cls.st.record_dispatch_history(_u(t), kind, source="smart-charge", energy_kwh=kwh,
                                           raw_start=_u(t), raw_end=_u(t + timedelta(minutes=30)),
                                           seen_at=_u(seen or n - timedelta(minutes=5)))
        noon = n.replace(hour=12)
        for m in (60, 90):                                           # 13:00, 13:30 completed: 1 h
            slot(noon + timedelta(minutes=m), "completed", -3.5)
        slot(noon - timedelta(hours=10), "completed", -3.5)            # 02:00: yesterday's cap-day
        night = (n + timedelta(days=1)).replace(hour=2)
        for m in (0, 30, 60, 90):                                     # 02:00–04:00 planned: 2 h
            slot(night + timedelta(minutes=m), "planned", -3.5)
        slot((n + timedelta(days=1)).replace(hour=13), "planned", -3.5)  # past the next noon

    def _cap(self, capped=True):
        import engine
        with mock.patch.object(engine, "capped_at", lambda s: capped), \
             mock.patch.object(engine, "cap_excluded", lambda s, store=None: False):
            return self.srv._ev_cap_now(self.st, "electricity_main", LON,
                                        self.NOW.astimezone(timezone.utc).replace(tzinfo=None))

    def test_used_planned_and_the_windows(self):
        c = self._cap()
        self.assertEqual((c["cap_day"], c["cap_minutes"], c["used_minutes"], c["planned_minutes"]),
                         ("2026-11-22", 360, 60, 120))
        self.assertEqual([(p["start"], p["end"], p["minutes"]) for p in c["plans"]], [("02:00", "04:00", 120)])

    def test_off_a_capped_agreement_there_is_none(self):
        self.assertIsNone(self._cap(capped=False))

    def test_the_route_carries_it_only_with_a_car(self):
        cfg_car = {"meters": {"electricity_main": {"meta": {"timezone": TZ}},
                              "ev_1": {"meta": {"sub_meter": True, "meter_type": "ev",
                                                "device_power_sensor": "sensor.car"}}}}
        cfg_none = {"meters": {"electricity_main": {"meta": {"timezone": TZ}}}}
        got = {}
        for name, cfg in (("car", cfg_car), ("none", cfg_none)):
            with mock.patch.object(self.srv.store_access, "store", lambda: self.st), \
                 mock.patch.object(self.srv, "load_config", lambda c=cfg: c), \
                 mock.patch.object(self.srv, "_config_timezone", lambda: TZ), \
                 mock.patch.object(self.srv, "_ev_cap_now", lambda *a: {"cap_minutes": 360}):
                got[name] = self.srv.app.test_client().get("/api/overview/days").get_json()["ev_cap"]
        self.assertEqual((got["car"], got["none"]), ({"cap_minutes": 360}, None))

    def test_the_smart_charging_card_counts_with_the_same_rows(self):
        # the sessions route reads _cap_rows, not a copy of its rules
        src = open(os.path.join(_ROOT, "web", "server.py"), encoding="utf-8").read()
        j = src.index("def api_charge_sessions(")
        self.assertIn("cap = _cap.cap_usage(_cap_rows(cap_hist, store), tz_name, None)", src[j:j + 9000])


class ThePage(unittest.TestCase):

    def test_the_car_card_has_the_strip(self):
        i = PAGE.index("function buildSocCard(")
        self.assertIn("(isEV ? '<div class=\"ev-cap\" id=\"ev-cap-' + meterId + '\" style=\"display:none;\"></div>' : '')",
                      PAGE[i:PAGE.index("\n}\n", i)])
        i = PAGE.index("function _tdRenderAll(")
        self.assertIn("_evCapRender(el, _tdData && _tdData.ev_cap)", PAGE[i:PAGE.index("\n}\n", i)])

    def test_compact_hides_it(self):
        self.assertIn(".ov-power-row.compact .td, .ov-power-row.compact .ev-cap { display: none !important; }", PAGE)


@unittest.skipUnless(shutil.which("node"), "node not available")
class ThePure(unittest.TestCase):

    def _js(self, expr):
        i = PAGE.index("function _evCapParts(")
        src = PAGE[i:PAGE.index("\n}\n", i) + 2] + "\nconsole.log(JSON.stringify(%s));" % expr
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(src)
        try:
            r = subprocess.run(["node", fh.name], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(fh.name)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_left(self):
        p = self._js("_evCapParts({cap_minutes:360, used_minutes:100, planned_minutes:150})")
        self.assertEqual((p["left"], p["over"]), (110, 0))

    def test_over(self):
        p = self._js("_evCapParts({cap_minutes:360, used_minutes:100, planned_minutes:320})")
        self.assertEqual((p["left"], p["over"], round(p["usedPct"] + p["planPct"])), (0, 60, 100))


if __name__ == "__main__":
    unittest.main()
