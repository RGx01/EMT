"""
The Overview's text is never under 12px (22 Nov 2026, the user: "the fonts in the power cards chart section could do
with being bumped a few points. The new font size should be considered the minimum used on any card in the overview
page") — and the Meter's reading is not cramped ("in comparison to the EV charger it looks cramped").

- Every font size on the page — styles, inline styles, the canvases' text — is 12px or more; the card titles, 11px
  app-wide, are 12px here.
- The gauges' end and zero labels (Export/Import, Out/In, Charging, 0) are gone: drawn in the arc's own units they
  came out about 5px, and the reading's own word, the Meter's pill and the sign say the same.
- The Meter's reading is the car's size (15px) and never wraps ("+7.44 kW" went onto two lines at 17px).

Checked in a browser on a scratch copy: no text under 12px on a card or an expanded card; "+7.44 kW" on one line.
Fails on the parent tree.
"""
import os
import re
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = open(os.path.join(_ROOT, "web", "templates", "live_power.html"), encoding="utf-8").read()


class TheFloor(unittest.TestCase):

    def test_no_style_under_12px(self):
        self.assertEqual(re.findall(r"font-size: ?(?:[0-9]|1[01])(?:\.\d+)?px", PAGE), [])

    def test_no_canvas_text_under_12px(self):
        self.assertEqual(re.findall(r"font *= *'(?:[0-9]|1[01])px", PAGE), [])

    def test_the_card_titles_too(self):
        self.assertIn("#ov-rows .card-title, #ov-costs .card-title { font-size: 12px; }", PAGE)

    def test_the_gauges_tiny_end_labels_are_gone(self):
        self.assertNotIn('<text x="18" y="125"', PAGE)
        self.assertNotIn('<text x="202" y="125"', PAGE)
        self.assertNotIn("zeroLabel", PAGE)

    def test_the_meters_reading_is_the_cars_size_on_one_line(self):
        self.assertIn(".ov-power-row .gauge-kw { font-size: 15px; }", PAGE)
        self.assertIn(".gauge-value, .inv-gauge-value { white-space: nowrap; }", PAGE)


if __name__ == "__main__":
    unittest.main()
