"""
The pypdf bump gate (`.build/pypdf_gate.py`, ROADMAP BL-64): its own logic, on stand-in bills.

The gate's real run needs real bills, which never enter the repo; these tests replace the parse with
a stand-in so they need no PDF. What they pin:

- **A changed field fails the gate**, reported as its path and a count, with list indices folded.
- **No value and no file name is printed** — a bill is its hash prefix, a difference its path.
- **Text that changed but parsed the same passes**, and says so.
- **A bills folder inside the repo is refused**, and so is a compare with no snapshot to compare to.
- **A real PDF parses through the real path** (`parse_one`) — pypdf's own blank page, no bill text.

New tool, new tests: there is no parent for them to fail on.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, ".build"))
import pypdf_gate as G                              # noqa: E402

SECRET = "A-0000ABC7"                               # stands for anything a bill could print


def _fake(rate, text="page text"):
    def parse_one(data):
        return {"pypdf": "x", "text_sha": text,
                "parsed": {"bill": {"mpan_import": SECRET, "warnings": []},
                           "csv": {"import": [{"Unit Rate (p/kWh)": rate, "Start": s} for s in range(3)]}}}
    return parse_one


def _run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = G.main(list(argv))
    return code, out.getvalue() + err.getvalue()


class TheGate(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        with open(os.path.join(self.dir, f"bill {SECRET}.pdf"), "wb") as fh:
            fh.write(b"%PDF-stand-in")
        self.p = mock.patch.object(G, "parse_one", _fake(6.67))
        self.p.start()
        self.ver = mock.patch.dict(sys.modules, {"pypdf": mock.Mock(__version__="6.18.0")})
        self.ver.start()
        self.assertEqual(_run("snapshot", self.dir)[0], 0)

    def tearDown(self):
        self.p.stop()
        self.ver.stop()
        self.tmp.cleanup()

    def test_the_same_parse_passes(self):
        code, out = _run("compare", self.dir, "--against", "6.18.0")
        self.assertEqual(code, 0, out)
        self.assertIn("OK", out)

    def test_a_changed_field_fails_by_path_without_values(self):
        with mock.patch.object(G, "parse_one", _fake(7.01)):
            code, out = _run("compare", self.dir, "--against", "6.18.0")
        self.assertEqual(code, 1, out)
        self.assertIn("csv.import[*].Unit Rate (p/kWh)  ×3", out)
        for leaked in ("6.67", "7.01", SECRET):
            self.assertNotIn(leaked, out)

    def test_changed_text_with_the_same_parse_passes(self):
        with mock.patch.object(G, "parse_one", _fake(6.67, text="other")):
            code, out = _run("compare", self.dir, "--against", "6.18.0")
        self.assertEqual(code, 0, out)
        self.assertIn("same parse (the extracted text changed)", out)

    def test_no_snapshot_for_the_version_is_a_usage_error(self):
        self.assertEqual(_run("compare", self.dir, "--against", "5.0.0")[0], 2)

    def test_the_snapshot_lives_in_the_bills_folder(self):
        snaps = os.listdir(os.path.join(self.dir, G.SNAP_DIR, "6.18.0"))
        self.assertEqual(len(snaps), 1)
        self.assertNotIn(SECRET, snaps[0])


class TheRepo(unittest.TestCase):

    def test_a_folder_inside_the_repo_is_refused(self):
        code, out = _run("snapshot", os.path.join(_ROOT, "tests"))
        self.assertEqual(code, 2)
        self.assertIn("inside the repo", out)

    def test_diff_paths_folds_indices_and_counts(self):
        d = G.diff_paths({"a": [1, 2, 3], "b": {"c": 1}}, {"a": [1, 9, 9, 4], "b": {}})
        self.assertEqual(d, {"a (length)": 1, "a[*]": 2, "b.c (present in one only)": 1})


class TheRealPath(unittest.TestCase):

    def test_a_blank_pdf_parses_without_raising(self):
        import pypdf
        w = pypdf.PdfWriter()
        w.add_blank_page(width=200, height=200)
        buf = io.BytesIO()
        w.write(buf)
        got = G.parse_one(buf.getvalue())
        self.assertEqual(got["pypdf"], pypdf.__version__)
        self.assertIn("csv", got["parsed"])
        self.assertNotIn("source", got["parsed"]["bill"])
        json.dumps(got)                              # what the snapshot writes


if __name__ == "__main__":
    unittest.main()
