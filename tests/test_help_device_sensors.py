"""
Help on a device's sensor (5 Nov 2026, the user: "help points about choosing the correct sensors especially
around what EMT needs ... the steps to correct a bad sensor").

Settings' help gains two sections, each with an id a link can reach: choosing a device's sensor (energy in
kWh as a running total, Home Assistant's long-term statistics, what a charger, a heat pump and a battery must
count, a believable size) and fixing one (the three answers to "why did the sensor change", the wrong
sensor step by step through Replace history, undoing a rebuild, removing a device). The device's tab carries
a ? on its Import Channel and its "History from Home Assistant" headings, opening those sections in a new tab so
an unsaved edit stays.

Discriminating: every test fails on the parent tree, where neither section nor point existed.
"""
import os
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = open(os.path.join(_ROOT, "web", "templates", "meter_config.html"), encoding="utf-8").read()


class TheHelpPage(unittest.TestCase):

    def setUp(self):
        from tests.test_server import make_client
        self.html = make_client().get("/help/settings").get_data(as_text=True)

    def test_the_two_sections_and_their_ids(self):
        self.assertIn('id="device-sensors"', self.html)
        self.assertIn("Choosing a device's sensor", self.html)
        self.assertIn('id="fixing-a-sensor"', self.html)
        self.assertIn("The wrong sensor, step by step", self.html)

    def test_it_names_the_controls_as_the_card_does(self):
        # the help's words are the buttons' and the question's own, so a reader finds them
        for label in ("Check sensors", "↻ Replace history", "▶ Rebuild history", "🗑 Remove",
                      "Renamed — keep for history", "A new device", "It was the wrong sensor"):
            self.assertIn(label, self.html, label)
            self.assertIn(label, CFG, label)


class ThePoints(unittest.TestCase):

    def test_beside_the_read_sensor_and_the_history(self):
        # on the section headings (the user, 5 Nov 2026): Import Channel and History from Home Assistant
        self.assertIn("'<div class=\"section-divider\">Import Channel' + helpPoint('device-sensors',", CFG)
        self.assertIn("History from Home Assistant' + helpPoint('fixing-a-sensor',", CFG)

    def test_a_new_tab_so_an_unsaved_edit_stays(self):
        i = CFG.index("function helpPoint(")
        body = CFG[i:CFG.index("\n}\n", i)]
        self.assertIn('target="_blank" rel="noopener"', body)
        self.assertIn("HELP_SETTINGS_URL + '#' + anchor", body)
        self.assertIn("var HELP_SETTINGS_URL = \"{{ url_for('help_section', section='settings') }}\";", CFG)


if __name__ == "__main__":
    unittest.main()
