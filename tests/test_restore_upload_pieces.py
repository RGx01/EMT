"""
A restore upload in pieces (14 Oct 2026): /api/restore/upload/{begin,chunk,finish}.

Through Home Assistant's ingress a whole backup in one request was refused before it reached EMT
("the server answered HTTP 413 without a result"), so a restore worked only on the add-on's own
port. The page now sends it in pieces; the server writes each at its offset and stages the whole
file exactly as /api/restore/stage does.

Discriminating: every test here fails on the parent (the routes are new), except the one
labelled GUARD.
"""
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(__file__))
from tests.test_server import make_client, _memory_uri     # noqa: E402
from block_store import BlockStore                        # noqa: E402
import server                                             # noqa: E402


def _backup_bytes():
    st = BlockStore(_memory_uri())
    st.set_meta("written_by", "4.5.23")
    p = os.path.join(tempfile.mkdtemp(), "blocks.db")
    try:
        st.backup(p)
        return open(p, "rb").read()
    finally:
        if os.path.exists(p):
            os.remove(p)


class TheUploadInPieces(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="emt_pieces_")
        server.DATA_DIR = self.tmp
        self.client = make_client()
        server.DATA_DIR = self.tmp
        server._restore_job = {"status": "idle"}

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        server.DATA_DIR = "/tmp/emt_test_data"
        server._restore_job = {"status": "idle"}

    def _send(self, data, step=1000, retry_one=False):
        b = self.client.post("/api/restore/upload/begin", json={"size": len(data)}).get_json()
        self.assertTrue(b["ok"], b)
        off = 0
        while off < len(data):
            piece = data[off:off + step]
            r = self.client.post("/api/restore/upload/chunk?id=%s&offset=%d" % (b["id"], off),
                                 data=piece, content_type="application/octet-stream")
            if retry_one and off == step:                   # the same piece again, as a retry
                r = self.client.post("/api/restore/upload/chunk?id=%s&offset=%d" % (b["id"], off),
                                     data=piece, content_type="application/octet-stream")
            self.assertEqual(r.status_code, 200, r.get_json())
            off = r.get_json()["received"]
        return self.client.post("/api/restore/upload/finish", json={"id": b["id"], "size": len(data)})

    def _left(self):
        return sorted(os.listdir(self.tmp))

    def test_a_db_in_pieces_stages_as_one_upload(self):
        r = self._send(_backup_bytes())
        self.assertEqual(r.status_code, 200, r.get_json())
        d = r.get_json()
        self.assertEqual(d["facts"]["written_by"], "4.5.23")
        self.assertEqual(self._left(), [d["token"]], "only the staged database is kept")

    def test_a_zip_in_pieces_stages_its_blocks_db(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("blocks.db", _backup_bytes())
        r = self._send(buf.getvalue())
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self._left(), [r.get_json()["token"]])

    def test_a_retried_piece_replaces_itself(self):
        r = self._send(_backup_bytes(), retry_one=True)
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_an_incomplete_upload_is_refused_and_removed(self):
        data = _backup_bytes()
        b = self.client.post("/api/restore/upload/begin", json={"size": len(data)}).get_json()
        self.client.post("/api/restore/upload/chunk?id=%s&offset=0" % b["id"], data=data[:1000],
                         content_type="application/octet-stream")
        r = self.client.post("/api/restore/upload/finish", json={"id": b["id"], "size": len(data)})
        self.assertEqual(r.status_code, 400)
        self.assertIn("incomplete", r.get_json()["error"])
        self.assertEqual(self._left(), [])

    def test_a_piece_out_of_order_is_409(self):
        b = self.client.post("/api/restore/upload/begin", json={"size": 5000}).get_json()
        r = self.client.post("/api/restore/upload/chunk?id=%s&offset=4000" % b["id"], data=b"x",
                             content_type="application/octet-stream")
        self.assertEqual(r.status_code, 409)

    def test_an_id_names_nothing_but_a_piece_file(self):
        """GUARD: passes on the parent too (no route, so nothing is written either way)."""
        open(os.path.join(self.tmp, "blocks.db"), "wb").write(b"live")
        for bad in ("blocks.db", "../blocks.db", "uploaded_restore-x.db", "uploaded_restore-gone.part"):
            with self.subTest(id=bad):
                r = self.client.post("/api/restore/upload/chunk?id=%s&offset=0" % bad, data=b"x",
                                     content_type="application/octet-stream")
                self.assertEqual(r.status_code, 400)
        self.assertEqual(open(os.path.join(self.tmp, "blocks.db"), "rb").read(), b"live")

    def test_begin_is_refused_while_a_restore_runs(self):
        server._restore_job = {"status": "running"}
        self.assertEqual(self.client.post("/api/restore/upload/begin", json={"size": 10}).status_code, 409)

    def test_a_not_a_database_is_refused_with_nothing_left(self):
        r = self._send(b"not a database" * 100)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._left(), [])

    def test_abandoned_pieces_go_with_stale_stages(self):
        p = os.path.join(self.tmp, "uploaded_restore-old.part")
        open(p, "wb").write(b"x")
        server._remove_stale_stages(max_age=0)
        self.assertFalse(os.path.exists(p))


if __name__ == "__main__":
    unittest.main()
