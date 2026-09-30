"""
Nothing renders a chart — C, 30 Sep 2026.

v4 pre-rendered the Billing page and the heatmap to HTML files next to the database,
re-rendering EVERYTHING whenever anything changed (C design §5: the document is the
unit of update). For three years of history the Billing page was 36 MB, took ~11 s
to build, and on the Pi it stalled the engine loop: fourth soak, a restore's regen
held the loop 492 s. The renders were moved off the loop (#12) and are now gone: the
Billing tab and the heatmap are drawn in the browser from /api/energy.

What must not come back, pinned structurally:

  * no function renders or schedules a render, in the engine or the web server;
  * no route serves or rebuilds a pre-rendered file;
  * nothing writes a chart file, and the stale ones an install already has are
    removed at setup;
  * the Charts page change token does not depend on file mtimes (it asks about the
    config, the epoch and the bill-rounding option directly — test_server.py).
"""
import ast
import os
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_GONE = ("generate_charts", "_generate_charts_offloaded", "_schedule_chart_regen",
         "render_recently_active", "_regen_charts_after_import", "_regen_charts_after_import_async",
         "_regen_charts_safely", "_schedule_offloaded_regen", "generate_daily_import_export_charts",
         "generate_net_heatmap", "build_day_chart_html", "render_billing_summary",
         "api_regenerate_charts", "api_chart_daily", "serve_daily", "_serve_lovelace_chart")


def _src(rel):
    return open(os.path.join(_ROOT, rel), encoding="utf-8").read()


def _names(tree):
    """Every name defined or referred to in a module."""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(n.name)
        elif isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
    return out


class NothingRenders(unittest.TestCase):

    def test_the_render_path_is_gone_everywhere(self):
        for rel in ("engine.py", "web/server.py", "energy_charts.py", "main.py", "billing_view.py"):
            names = _names(ast.parse(_src(rel)))
            for gone in _GONE:
                self.assertNotIn(gone, names, "%s still mentions %s" % (rel, gone))

    def test_no_chart_directory(self):
        for rel in ("engine.py", "web/server.py", "main.py"):
            self.assertNotIn("CHART_DIR", _names(ast.parse(_src(rel))), rel)

    def test_nothing_writes_a_chart_file(self):
        """The filenames survive, as code, in exactly one place: the stale-file list.
        (Docstrings may still name them as history.)"""
        def code_strings(rel):
            tree = ast.parse(_src(rel))
            docs = set()
            for n in ast.walk(tree):
                if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant):
                        docs.add(id(n.body[0].value))
            return [n.value for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs]
        for f in ("daily_usage.html", "net_heatmap.html"):
            self.assertFalse([v for v in code_strings("web/server.py") if f in v], f)
            self.assertEqual([v for v in code_strings("engine.py") if f in v], [f], f)

    def test_the_charts_page_embeds_no_iframe(self):
        page = _src("web/templates/charts.html")
        self.assertNotIn("createElement('iframe')", page)
        self.assertNotIn("_injectChartHtml", page)


class StaleFilesAreRemoved(unittest.TestCase):
    """An install that ran v4 or an early v5 has the files; nothing reads them now."""

    def test_setup_removes_them_and_keeps_everything_else(self):
        import engine
        d = tempfile.mkdtemp()
        for f in ("daily_usage.html", "net_heatmap.html", "blocks.db"):
            open(os.path.join(d, f), "w").write("x")
        with mock.patch.object(engine, "DATA_DIR", d):
            engine._remove_stale_chart_files()
            engine._remove_stale_chart_files()          # idempotent: nothing left, no error
        self.assertEqual(sorted(os.listdir(d)), ["blocks.db"])

    def test_setup_calls_it(self):
        fn = next(n for n in ast.walk(ast.parse(_src("engine.py")))
                  if isinstance(n, ast.FunctionDef) and n.name == "setup")
        calls = {getattr(c.func, "id", None) for c in ast.walk(fn) if isinstance(c, ast.Call)}
        self.assertIn("_remove_stale_chart_files", calls)


if __name__ == "__main__":
    unittest.main()
