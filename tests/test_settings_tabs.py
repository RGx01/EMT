"""
The Settings page in tabs (31 Oct 2026): Site, Main meter, one per device, Add device.

What they pin:

- **The tab list:** Site always. Main meter, the devices and Add device appear only once there is a
  main meter. Archived devices go last. A flagged battery that hasn't been kept carries ⚠.
- **The main meter is split in two:** site-wide settings, including the supplier connection, go on
  Site; the power sensor and the channels go on Main meter.
- **Add device is a tab, not a strip button.** Its form sits in the tab, and a device just added
  opens on its own tab.
- **Links land on a tab:** the battery banner goes to that battery's tab. The old one-page anchor
  (`#sub-meter-container`) still opens a device.

Discriminating: every test here fails on the parent (the tabs are new).
"""
import json
import os
import re
import shutil
import subprocess
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PAGE = open(os.path.join(_ROOT, "web", "templates", "meter_config.html"), encoding="utf-8").read()
_BASE = open(os.path.join(_ROOT, "web", "templates", "base.html"), encoding="utf-8").read()


def _fn(name):
    i = _PAGE.index("function " + name + "(")
    return _PAGE[i:_PAGE.index("\n}\n", i) + 2]


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheTabList(unittest.TestCase):

    def _tabs(self, meters, flagged=None):
        # BL-97's EV tab: no EV here (test_api_ev_device covers it)
        js = (_fn("_cfgTabList") + "\n" + _fn("_hasEvDevice") + "\n" + _fn("_showApiEv") + "\nvar HAS_HAD_EV = false;" +
              "\nvar config = " + json.dumps({"meters": meters}) + ";\nvar batteryFlagged = " +
              json.dumps(flagged or {}) + ";\nconsole.log(JSON.stringify(_cfgTabList()));")
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout
        return json.loads(out)

    def test_site_alone_with_no_main_meter(self):
        self.assertEqual([t["key"] for t in self._tabs({})], ["site"])

    def test_the_devices_sit_between_main_and_add_archived_last(self):
        meters = {"main": {"meta": {}},
                  "a": {"meta": {"sub_meter": True, "meter_type": "heat_pump", "device": "Pump",
                                 "retired_at": "2026-01-01"}},
                  "b": {"meta": {"sub_meter": True, "meter_type": "battery", "device": "Batt"}}}
        tabs = self._tabs(meters, {"b": {"confirmed_at": None}})
        self.assertEqual([t["key"] for t in tabs], ["site", "main", "device-b", "device-a", "add"])
        self.assertEqual((tabs[2]["label"], tabs[2]["flag"], tabs[3]["archived"]), ("🔋 Batt", True, True))

    def test_a_kept_battery_sensor_carries_no_flag(self):
        meters = {"main": {"meta": {}}, "b": {"meta": {"sub_meter": True, "meter_type": "battery", "device": "B"}}}
        self.assertFalse(self._tabs(meters, {"b": {"confirmed_at": "2026-10-31T00:00:00"}})[2]["flag"])


class TheMainMeterSplit(unittest.TestCase):

    def setUp(self):
        body = _fn("buildMainCards")
        self.site = body[body.index("site.innerHTML"):body.index("var div = document.createElement")]
        self.meter = body[body.index("div.innerHTML"):]

    def test_site_holds_the_site_wide_settings_and_the_connection(self):
        for key in ('data-key="site"', 'data-key="billing_day"', 'data-key="block_minutes"',
                    "buildTzSelectForMeter(", 'data-key="postcode_prefix"', "js-disconnect-octopus",
                    "js-reconnect-octopus"):
            self.assertIn(key, self.site)
            self.assertNotIn(key, self.meter)

    def test_main_meter_holds_the_power_sensor_and_the_channels(self):
        for key in ("'power', 'power_sensor'", "meterId+'__import__read'", "meterId+'__export__read'",
                    "js-remove-meter"):
            self.assertIn(key, self.meter)
            self.assertNotIn(key, self.site)

    def test_each_card_is_a_panel(self):
        body = _fn("buildMainCards")
        self.assertIn("site.dataset.panel = 'site';", body)
        self.assertIn("div.dataset.panel = 'main';", body)
        self.assertIn("div.dataset.panel = 'device-' + meterId;", _fn("buildSubCard"))
        self.assertIn("['site-container','main-meter-container','sub-meter-container']", _fn("attachListeners"))


class TheAddDeviceTab(unittest.TestCase):

    def test_the_strip_has_no_add_device_button(self):
        strip = _PAGE[_PAGE.index("{% block subbar %}"):_PAGE.index("{% endblock %}", _PAGE.index("{% block subbar %}"))]
        self.assertNotIn("Add Device", strip)
        self.assertNotIn("add-device-overlay", _PAGE)

    def test_the_tab_offers_the_three_types_and_holds_the_form(self):
        panel = _PAGE[_PAGE.index('<div id="add-device-panel" data-panel="add">'):]
        panel = panel[:panel.index("<!-- ── A device's live sensor changed")]
        for t in ("battery", "ev", "heat_pump"):
            self.assertIn("addDevice('" + t + "')", panel)
        self.assertIn('id="add-device-form"', panel)
        self.assertIn('id="ad-confirm-btn"', panel)

    def test_a_device_just_added_opens_on_its_tab(self):
        body = _fn("confirmAddDevice")
        self.assertIn("_cfgTab = 'device-' + id;", body)
        self.assertLess(body.index("_cfgTab = 'device-' + id;"), body.index("renderAll();"))


class TheLinks(unittest.TestCase):

    def test_the_battery_banner_goes_to_its_tab(self):
        self.assertIn("#device-' + encodeURIComponent(b.meter_id) + '\">Choose the battery", _BASE)
        self.assertNotIn("#sub-meter-container", _BASE)

    def test_the_old_anchor_opens_a_device_and_an_unknown_tab_falls_back_to_site(self):
        body = _fn("showTab")
        self.assertIn("if (key === 'sub-meter-container')", body)
        self.assertTrue(re.search(r"if \(keys\.indexOf\(key\) === -1\) key = 'site';", body))


if __name__ == "__main__":
    unittest.main()
