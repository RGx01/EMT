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
UPLOADS = ("/api/import/from-zip", "/api/import/extract-zip", "/api/import")


class ThePage(unittest.TestCase):

    def _reply_line(self, url):
        m = re.search(r"fetch\(apiUrl\('%s'\), \{ method: 'POST', body: form \}\);\s*\n"
                      r"\s*const data = await (\w+)\(r\);" % re.escape(url), PAGE)
        self.assertIsNotNone(m, "the %s upload is not where the test expects it" % url)
        return m.group(1)

    def test_every_upload_reads_its_reply_through_uploadReply(self):
        for url in UPLOADS:
            with self.subTest(url=url):
                self.assertEqual(self._reply_line(url), "uploadReply")

    def test_no_upload_parses_a_reply_blind(self):
        """Any POST of a FormData body — including one added later."""
        for m in re.finditer(r"body: form \}\);\s*\n\s*const \w+ = await ([\w.]+)\(", PAGE):
            self.assertEqual(m.group(1), "uploadReply", PAGE[m.start() - 80:m.end()])

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
        for url, field in (("/api/import", "blocks"), ("/api/import/from-zip", "zipfile"),
                           ("/api/import/extract-zip", "zipfile")):
            with self.subTest(url=url):
                r = self.client.post(url, data={}, content_type="multipart/form-data")
                self.assertGreaterEqual(r.status_code, 400)
                self.assertIn("application/json", r.headers.get("content-type", ""))


if __name__ == "__main__":
    unittest.main()
