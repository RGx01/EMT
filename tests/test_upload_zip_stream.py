"""
The routes that take an uploaded backup zip open it from a file, not the upload
stream (4 Oct 2026).

On Python 3.10 werkzeug's upload stream is a SpooledTemporaryFile with no seekable()
(added in 3.11), which zipfile needs, so `/api/import/from-zip` and
`/api/import/extract-zip` answered 500 to every zip. CI's 3.10 leg found the first
(#82); 3.10 has since left the matrix (#83), so these tests simulate it on the running
Python by making access to SpooledTemporaryFile.seekable raise AttributeError, as
3.10's missing attribute does.

`extract-zip` failed on the parent tree (it still read the stream; the route went on 21 Oct
2026, with no caller left); `from-zip` was a
GUARD here (fixed in #82, before this test existed — it failed there on CI 3.10). Since 11 Oct
2026 a restore zip goes to `/api/restore/stage`, which saves it the same way: a GUARD.
"""
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from block_store import BlockStore                # noqa: E402


def _server():
    spec = importlib.util.spec_from_file_location("server_uz", os.path.join(_ROOT, "web", "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    return srv


def _missing(self):
    raise AttributeError("'SpooledTemporaryFile' object has no attribute 'seekable'")


# A property, not a method: zipfile reads `file.seekable` as an attribute when it opens
# a member (`_SharedFile.__init__`), so only a failing ACCESS reproduces 3.10.
_NO_SEEKABLE = property(_missing)


class UploadedZips(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.srv = _server()

    def setUp(self):
        self.data = tempfile.mkdtemp()
        st = BlockStore(os.path.join(self.data, "src.db"))
        st.set_meta("written_by", "4.5.23")
        st.close()
        self.zip = io.BytesIO()
        with zipfile.ZipFile(self.zip, "w") as zf:
            zf.write(os.path.join(self.data, "src.db"), "blocks.db")
        os.remove(os.path.join(self.data, "src.db"))
        self.p = [mock.patch.object(self.srv, "DATA_DIR", self.data),
                  mock.patch.object(tempfile.SpooledTemporaryFile, "seekable", _NO_SEEKABLE,
                                    create=True)]       # 3.10 has no attribute to replace
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        shutil.rmtree(self.data, ignore_errors=True)

    def _post(self, route, field="zipfile"):
        self.zip.seek(0)
        return self.srv.app.test_client().post(route, data={field: (self.zip, "b.zip")},
                                               content_type="multipart/form-data")

    def test_the_simulation_breaks_the_stream(self):
        """Without this the tests below would pass vacuously on 3.11+."""
        spool = tempfile.SpooledTemporaryFile()
        spool.write(self.zip.getvalue())
        spool.seek(0)
        with self.assertRaises(AttributeError):
            zipfile.ZipFile(spool).read("blocks.db")

    def test_restore_stage(self):
        """GUARD: the restore zip is saved to a file and its blocks.db extracted from that."""
        r = self._post("/api/restore/stage", field="file")
        self.assertEqual(r.status_code, 200, r.get_json())
        left = os.listdir(self.data)
        self.assertEqual(left, [r.get_json()["token"]], "only the staged database is kept")


if __name__ == "__main__":
    unittest.main()
