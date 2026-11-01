"""
BL-101 PR 3 (1 Nov 2026, home eras design §2) — eras EMT finds for itself.

- **A house move:** a supply point (MPAN) after the first in the stored import agreements, dated at
  its first agreement's start; or discovery reporting a new supply point, dated the day it was seen.
- **A meter change:** discovery reporting the same supply point with a new meter serial — seen at
  start-up, or by the daily rediscovery, which refuses the change until a reconnect — dated the day
  EMT noticed (EMT kept no serial history before).

Points are kept only as fingerprints (16 hex characters of a SHA-256), never the MPAN or serial; the
first point seen is the baseline. A found era carries `found` and a `found_key` (`move:` / `meter:` +
a fingerprint); one already there is not added again, and one the user deleted is kept in
`home_eras_dismissed` and not found again. A house move is named by the site name — no address.

Discriminating: every test fails on the parent tree.
"""
import importlib.util
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import home_eras as H                            # noqa: E402

M1, M2, S1, S2 = "2600000000001", "2600000000002", "99Z0000001", "99Z0000002"


class ThePoints(unittest.TestCase):

    def test_the_first_is_the_baseline(self):
        seen, ch = H.note_point([], M1, S1, "2026-11-01")
        self.assertEqual(len(seen), 1)
        self.assertIsNone(ch)

    def test_the_same_again_nothing(self):
        seen, _ = H.note_point([], M1, S1, "2026-11-01")
        self.assertEqual(H.note_point(seen, M1, S1, "2026-11-02"), (seen, None))

    def test_a_new_serial_a_meter_change(self):
        seen, _ = H.note_point([], M1, S1, "2026-11-01")
        seen, ch = H.note_point(seen, M1, S2, "2026-12-05")
        self.assertEqual((ch["kind"], ch["date"]), ("meter_change", "2026-12-05"))
        self.assertRegex(ch["key"], r"^meter:[0-9a-f]{16}$")

    def test_a_new_supply_point_a_move(self):
        seen, _ = H.note_point([], M1, S1, "2026-11-01")
        _, ch = H.note_point(seen, M2, S2, "2027-01-10")
        self.assertEqual(ch["kind"], "house_move")
        self.assertEqual(ch["key"], "move:" + H.fingerprint("mpan", M2))

    def test_only_fingerprints_are_kept(self):
        seen, _ = H.note_point([], M1, S1, "2026-11-01")
        text = repr(seen)
        self.assertNotIn(M1, text)
        self.assertNotIn(S1, text)


class TheAgreements(unittest.TestCase):

    ROWS = [{"direction": "import", "mpan": M1, "valid_from": "2023-06-02T23:00:00"},
            {"direction": "import", "mpan": M1, "valid_from": "2024-01-01T00:00:00"},
            {"direction": "import", "mpan": M2, "valid_from": "2025-04-30T23:00:00"},
            {"direction": "export", "mpan": "2600000000009", "valid_from": "2023-01-01T00:00:00"}]

    def test_a_move_per_supply_point_after_the_first(self):
        out = H.moves_from_agreements(self.ROWS, lambda iso: iso[:10] if iso[11:13] != "23" else "2025-05-01")
        self.assertEqual(out, [{"key": "move:" + H.fingerprint("mpan", M2), "date": "2025-05-01"}])


class TheEras(unittest.TestCase):

    CH = [{"kind": "house_move", "key": "move:" + "a" * 16, "date": "2025-05-01"}]

    def test_named_by_the_site(self):
        e = H.found_eras(self.CH, [], [], site="Home")[0]
        self.assertEqual((e["label"], e["kind"], e["start"], e["site"], e["found"], e["found_key"]),
                         ("Moved house", "house_move", "2025-05-01", "Home", True, "move:" + "a" * 16))

    def test_not_twice_and_not_when_deleted(self):
        e = H.found_eras(self.CH, [], [])
        self.assertEqual(H.found_eras(self.CH, e, []), [])
        self.assertEqual(H.found_eras(self.CH, [], ["move:" + "a" * 16]), [])

    def test_a_found_key_survives_clean_and_junk_does_not(self):
        self.assertEqual(H.clean_one({"label": "X", "kind": "other", "start": "2025-01-01", "found": True,
                                      "found_key": "meter:" + "b" * 16})["found_key"], "meter:" + "b" * 16)
        self.assertNotIn("found_key", H.clean_one({"label": "X", "kind": "other", "start": "2025-01-01",
                                                    "found": True, "found_key": M1}))


class TheEngine(unittest.TestCase):

    def setUp(self):
        import engine
        from block_store import BlockStore
        self.engine = engine
        self.st = BlockStore(":memory:")
        self.st._conn.execute("INSERT OR IGNORE INTO config_periods (id, effective_from, billing_day, block_minutes, "
                              "timezone, site_name) VALUES (1,'2020-01-01T00:00:00',1,30,'Europe/London','Home')")
        for m, vf in ((M1, "2023-06-02T23:00:00"), (M2, "2025-04-30T23:00:00")):
            self.st._conn.execute("INSERT INTO tariff_agreements (account, mpan, direction, tariff_code, valid_from, "
                                  "fetched_at) VALUES ('A-0000ABC7', ?, 'import', 'E-1R-VAR-22-11-01-C', ?, ?)",
                                  (m, vf, vf))
        self.st._conn.commit()
        self.p = [mock.patch.object(engine, "_store", self.st),
                  mock.patch.object(engine, "_kraken_account_number", "A-0000ABC7")]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        self.st.close()

    def test_a_past_move_from_the_agreements(self):
        self.assertEqual(self.engine._find_home_eras({"import": {"mpan": M2, "serial": S2}}), 1)
        eras = H.load(self.st)
        self.assertEqual([(e["label"], e["start"], e.get("site")) for e in eras],
                         [("Moved house", "2025-05-01", "Home")])          # 23:00 UTC is the next local day

    def test_found_once_and_a_meter_change_later(self):
        self.engine._find_home_eras({"import": {"mpan": M2, "serial": S2}})
        self.assertEqual(self.engine._find_home_eras({"import": {"mpan": M2, "serial": S2}}), 0)
        self.assertEqual(self.engine._find_home_eras({"import": {"mpan": M2, "serial": "99Z0000003"}}), 1)
        self.assertEqual(sorted(e["kind"] for e in H.load(self.st)), ["house_move", "meter_change"])

    def test_nothing_stored_holds_an_mpan_or_serial(self):
        self.engine._find_home_eras({"import": {"mpan": M2, "serial": S2}})
        for key in (H.META_KEY, H.POINTS_KEY):
            text = repr(self.st.get_meta(key))
            self.assertNotIn(M2, text)
            self.assertNotIn(S2, text)


class TheDelete(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("server_bl101b", os.path.join(_ROOT, "web", "server.py"))
        cls.srv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.srv)

    def test_a_deleted_found_era_is_not_found_again(self):
        meta = {H.META_KEY: [{"id": "aaaaaaaa", "label": "Moved house", "kind": "house_move", "start": "2025-05-01",
                              "found": True, "found_key": "move:" + "c" * 16}]}
        st = mock.Mock()
        st.get_meta.side_effect = lambda k, d=None: meta.get(k, d)
        st.set_meta.side_effect = lambda k, v: meta.update({k: v})
        with mock.patch.object(self.srv, "_engine_write", lambda op: op(st)):
            r = self.srv.app.test_client().post("/api/home-eras", json={"eras": []})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(meta[H.DISMISSED_KEY], ["move:" + "c" * 16])
        self.assertEqual(meta[H.META_KEY], [])


if __name__ == "__main__":
    unittest.main()
