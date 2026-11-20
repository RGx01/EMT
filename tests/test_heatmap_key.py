"""
The heatmap's key (19 Nov 2026, the user): left-aligned, and hovering an item shows its state as one cell
of the grid — the grid's own size (`cellSize() - 1`, as the matrix draws a cell) and the grid's own fill
(`settleColour`, so a hatch is the canvas's hatch at its true scale), beside its words.

Checked in the local preview on the demo database (1440 px wide): the key starts at the panel's left
edge, and hovering "missing" showed an 18 px hatched cell, the grid's 19 px cells less their 1 px gap.

Source-level: the module is a template, and its drawing needs a browser. Each test fails on the parent.
"""
import os
import re
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = open(os.path.join(_ROOT, "web", "templates", "heatmap_module.html"), encoding="utf-8").read()


class TheKey(unittest.TestCase):

    def test_it_is_left_aligned(self):
        rule = re.search(r"\.hm-key \{([^}]*)\}", PAGE).group(1)
        self.assertIn("justify-content:flex-start", rule)

    def test_each_item_names_its_state(self):
        self.assertIn('<span class="hm-key-item" data-k="\' + esc(k[0]) + \'">', PAGE)

    def test_the_hover_draws_a_grid_cell(self):
        body = PAGE[PAGE.index("function attachKeyHover"):PAGE.index("function tipEl")]
        self.assertIn("cellSize() - 1", body)
        self.assertIn("settleColour(state.metric, it.dataset.k)", body)
        self.assertIn("x.scale(dpr, dpr)", body)
        self.assertIn("attachKeyHover(root);", PAGE)


if __name__ == "__main__":
    unittest.main()
