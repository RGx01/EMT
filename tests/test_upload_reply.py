"""
A restore upload that never reaches EMT says so (5 Oct 2026).

Through Home Assistant's ingress proxy a large upload can be refused before it reaches
the add-on, and the reply is the proxy's page, not JSON. The Data Management page called
`r.json()` on it and showed the browser's parse error — "The string did not match the
expected pattern." in Safari — which named nothing. `uploadReply` now reads JSON only
when the reply is JSON, and otherwise says what happened and where to restore instead.

STRUCTURAL (no JS runtime in the suite): every FormData upload on the page reads its reply
through `uploadReply`. Fails on the parent. The server half is a GUARD: EMT's own replies
on these routes are JSON even on error, so the helper never mistakes one for the proxy's.
"""
import importlib.util
import os
import re
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = open(os.path.join(_ROOT, "web", "templates", "data_management.html"),
            encoding="utf-8").read()
UPLOADS = ("/api/restore/stage", "/api/import/extract-zip")


class ThePage(unittest.TestCase):

    def _reply_line(self, url):
        # a plain fetch(), or (10 Oct 2026) uploadWithProgress(), whose reply has the same shape:
        # the call, then the line that reads its reply
        for call in ("fetch(apiUrl('%s'), { method: 'POST', body: form });" % url,
                     "uploadWithProgress(apiUrl('%s'), form," % url):
            i = PAGE.find(call)
            if i != -1:
                m = re.compile(r"const data = await (\w+)\(r\);").search(PAGE, i, i + 400)
                self.assertIsNotNone(m, "the %s upload's reply is not where expected" % url)
                return m.group(1)
        self.fail("the %s upload is not where the test expects it" % url)

    def test_every_upload_reads_its_reply_through_uploadReply(self):
        for url in UPLOADS:
            with self.subTest(url=url):
                self.assertEqual(self._reply_line(url), "uploadReply")

    def test_no_upload_parses_a_reply_blind(self):
        """Any POST of a FormData body — including one added later."""
        starts = [m.start() for m in re.finditer(r"body: form \}\);|uploadWithProgress\(apiUrl", PAGE)]
        self.assertTrue(starts)
        for i in starts:
            m = re.compile(r"const \w+ = await ([\w.]+)\(").search(PAGE, i, i + 400)
            self.assertIsNotNone(m, PAGE[i:i + 200])
            self.assertEqual(m.group(1), "uploadReply", PAGE[i:m.end()])

    def test_a_restore_upload_says_how_far_it_has_got(self):
        """10 Oct 2026: an 80 MB upload took about two minutes to reach a Pi, and fetch()
        reports nothing until the reply — the page sat silent with its button live, and a
        second click started a second upload. Both restore uploads now report progress and the
        import button is off while one runs. STRUCTURAL; fails on the parent."""
        # 11 Oct 2026: both restore uploads go through restoreUpload(), which stages the file
        # with uploadWithProgress (first-run design §2.5)
        self.assertIn("uploadWithProgress(apiUrl('/api/restore/stage')", PAGE)
        for fn in ("async function doImport()", "async function doZipRestore()"):
            with self.subTest(fn=fn):
                j = PAGE.index(fn)
                self.assertIn("restoreUpload(", PAGE[j:PAGE.index("\n}\n", j)])
        i = PAGE.index("function uploadWithProgress(")
        body = PAGE[i:PAGE.index("\n}\n", i)]
        self.assertIn("xhr.upload.onprogress", body)
        self.assertIn("xhr.upload.onload", body)          # "uploaded — EMT is checking it"
        j = PAGE.index("async function doImport()")
        self.assertIn("_uploadBusy(true", PAGE[j:PAGE.index("\n}\n", j)])

    def test_the_helper_checks_the_type_and_names_the_way_round(self):
        i = PAGE.index("async function uploadReply(r)")
        body = PAGE[i:PAGE.index("\n}\n", i)]
        self.assertIn("content-type", body)
        self.assertIn("application/json", body)
        self.assertIn("r.status", body)
        self.assertIn("directly", body)


def _server():
    spec = importlib.util.spec_from_file_location("server_ur", os.path.join(_ROOT, "web", "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    return srv


class TheServer(unittest.TestCase):
    """GUARD: EMT's own failures on these routes are JSON."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        srv = _server()
        srv.DATA_DIR = tempfile.mkdtemp()        # as server init sets it
        cls.client = srv.app.test_client()

    def test_a_refused_upload_is_still_json(self):
        for url, field in (("/api/restore/stage", "file"), ("/api/import/extract-zip", "zipfile")):
            with self.subTest(url=url):
                r = self.client.post(url, data={}, content_type="multipart/form-data")
                self.assertGreaterEqual(r.status_code, 400)
                self.assertIn("application/json", r.headers.get("content-type", ""))


if __name__ == "__main__":
    unittest.main()
