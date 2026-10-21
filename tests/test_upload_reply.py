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
def _template(name):
    return open(os.path.join(_ROOT, "web", "templates", name), encoding="utf-8").read()


# The upload helpers live in a partial both upload pages include (11 Oct 2026); the page under
# test is Data Management with it inlined, as Jinja renders it.
HELPERS = _template("_upload_js.html")
PAGE = _template("data_management.html").replace('{% include "_upload_js.html" %}', HELPERS)
WELCOME = _template("welcome.html")
# 14 Oct 2026: a restore goes up in pieces (stageInPieces); its replies are read inside the
# helper, through uploadReply. 21 Oct 2026: the page's last single-fetch upload (extract-zip, the
# legacy zip extraction) went with the file list (data management design §1 (5)).
UPLOADS = ()


class ThePage(unittest.TestCase):

    def _reply_line(self, url):
        # a plain fetch(): the call, then the line that reads its reply
        for call in ("fetch(apiUrl('%s'), { method: 'POST', body: form });" % url,):
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
        starts = [m.start() for m in re.finditer(r"body: form \}\);", PAGE)]
        # none on the page since 21 Oct 2026 — every restore goes up in pieces — so this guards
        # one added later
        for i in starts:
            m = re.compile(r"const \w+ = await ([\w.]+)\(").search(PAGE, i, i + 400)
            self.assertIsNotNone(m, PAGE[i:i + 200])
            self.assertEqual(m.group(1), "uploadReply", PAGE[i:m.end()])

    def test_a_restore_upload_says_how_far_it_has_got(self):
        """10 Oct 2026: an 80 MB upload took about two minutes to reach a Pi, and the page sat
        silent with its button live. A restore reports progress. Since 21 Oct 2026 a dropped zip
        and a dropped blocks.db both go through doZipRestore (data management design §1 (5)).
        STRUCTURAL."""
        for fn in ("async function doZipRestore()",):
            with self.subTest(fn=fn):
                j = PAGE.index(fn)
                self.assertIn("restoreUpload(", PAGE[j:PAGE.index("\n}\n", j)])
        self.assertIn("stageInPieces(file, onStage)", PAGE)
        body = PAGE[PAGE.index("async function stageInPieces("):]
        body = body[:body.index("\n}\n")]
        self.assertIn("Uploading… ' + Math.floor(100 * off / file.size)", body)
        self.assertIn("EMT is checking the file", body)
        j = PAGE.index("async function doZipRestore()")
        self.assertIn("restoreUpload(zipFile, function(t) { statusEl.textContent = '⏳ ' + t; });",
                      PAGE[j:PAGE.index("\n}\n", j)])

    def test_a_restore_goes_up_in_pieces(self):
        """14 Oct 2026: through Home Assistant's ingress a whole backup in one request was
        refused before it reached EMT — "the server answered HTTP 413 without a result". Each
        piece is small enough for the proxy, every reply goes through uploadReply, and a failed
        piece is retried. STRUCTURAL; fails on the parent."""
        body = PAGE[PAGE.index("async function stageInPieces("):]
        body = body[:body.index("\n}\n")]
        for needle in ("/api/restore/upload/begin", "/api/restore/upload/chunk?id=",
                       "/api/restore/upload/finish", ".then(uploadReply)", "file.slice(off",
                       "if (++tries > 2) throw e;", "await uploadReply(await sendPiece("):
            self.assertIn(needle, body)

    def test_a_piece_goes_by_xhr_as_bytes_with_a_timeout(self):
        """14 Oct 2026: fetch() with a Blob body never left Safari (no headers, no status; the
        upload sat at 0%). Each piece is read into memory and sent by XMLHttpRequest — what sent
        the whole file in the same Safari — with a timeout, so a stall is retried. STRUCTURAL;
        fails on the parent."""
        i = PAGE.index("function sendPiece(url, piece)")
        body = PAGE[i:PAGE.index("\n}\n", i)]
        for needle in ("piece.arrayBuffer()", "new XMLHttpRequest()", "xhr.timeout = 60000",
                       "xhr.ontimeout", "xhr.send(buf)"):
            self.assertIn(needle, body)
        for page in (PAGE, WELCOME):
            self.assertNotIn("/api/restore/stage", page, "a page still sends the whole backup "
                                                          "in one request")

    def test_the_helper_checks_the_type_and_names_the_way_round(self):
        i = PAGE.index("async function uploadReply(r)")
        body = PAGE[i:PAGE.index("\n}\n", i)]
        self.assertIn("content-type", body)
        self.assertIn("application/json", body)
        self.assertIn("r.status", body)
        self.assertIn("directly", body)


class TheWelcomePage(unittest.TestCase):
    """The first-run fork's restore (11 Oct 2026). STRUCTURAL; new page, so it fails on the parent."""

    def test_it_takes_the_shared_helpers(self):
        self.assertIn('{% include "_upload_js.html" %}', WELCOME)
        self.assertNotIn("function uploadReply(", WELCOME, "a second copy of the helper")

    def test_its_upload_and_its_confirm_read_their_replies_through_uploadReply(self):
        self.assertIn("await stageInPieces(file, showBusy)", WELCOME)     # replies read in the helper
        j = WELCOME.index("fetch(apiUrl('/api/restore/confirm')")
        self.assertIn("await uploadReply(r)", WELCOME[j:j + 400])

    def test_a_mismatch_offers_to_import_unconnected_or_rekey(self):
        """First-run design §2.4."""
        self.assertIn("r.status === 409 && data.mismatch", WELCOME)
        self.assertIn("accept_mismatch = true", WELCOME)
        self.assertIn("Import without connecting", WELCOME)
        self.assertIn("Enter a different key", WELCOME)

    def test_a_skipped_key_hands_off_to_settings(self):
        """First-run design §3: credentials missing on an API mode -> Settings and its banner."""
        self.assertIn("m.credentials_missing", WELCOME)


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
