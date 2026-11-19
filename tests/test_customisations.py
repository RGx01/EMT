"""
Eras and chart colours to a file, and from one (19 Nov 2026, the user: "imported eras should replace whatever
eras are currently configured. Line colours and styles should replace devices that match the device type (not
id). Where there are duplicated devices, the natural order dictates the colour/style map. This could allow
users to share themes to import").

`customisations.py` reads the home's eras and every chart style as drawn — the house, its export, each device by
its role, the EV without a device — from this database or a kept backup's blocks.db, and applies such a file:
the eras replaced, each style to the meter of the same role, two of one role in config order. Data Management's
Backup & restore carries the buttons, and a .json dropped there is taken as one of these files, not a restore.

Checked in the local preview on 19 Nov 2026: the file downloaded from a copy of one install's database and from
a backup made of it (the same content), then imported on a copy of another install whose meters have other ids —
its 0 eras replaced by the file's, the house, export, EV charger and battery colours on its own meters, a heat
pump in the file reported as not used.

Discriminating: every test fails on the parent tree, where none of it existed.
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import home_eras                                   # noqa: E402
from block_store import BlockStore                # noqa: E402

DM = open(os.path.join(_ROOT, "web", "templates", "data_management.html"), encoding="utf-8").read()

ERA = {"id": "a1b2c3d4e5f6", "label": "Added Solar", "kind": "solar", "start": "2024-07-14", "kw": 5.6}


def _build(st, devices, colours=None):
    """A config period: the main meter, then `devices` [(meter_id, meter_type, label)] in order; `colours`
    {(meter_id, channel): (bar, line)}."""
    c = st._conn
    c.execute("INSERT OR IGNORE INTO config_periods (id, effective_from, billing_day, block_minutes, timezone) "
              "VALUES (1,'2020-01-01T00:00:00',1,30,'UTC')")
    rows = [("electricity_main", None, None, 0)] + [(m, t, lab, 1) for m, t, lab in devices]
    for mid, mtype, label, sub in rows:
        c.execute("INSERT INTO meters (config_period_id, meter_id, is_sub_meter, device_label, meter_type) "
                  "VALUES (1, ?, ?, ?, ?)", (mid, sub, label, mtype))
        pk = c.execute("SELECT id FROM meters WHERE meter_id = ?", (mid,)).fetchone()[0]
        for ch in (("import", "export") if not sub else ("import",)):
            bar, line = (colours or {}).get((mid, ch), (None, None))
            c.execute("INSERT INTO meter_channels (meter_id, channel, read_sensor, bar_color, rate_line) "
                      "VALUES (?, ?, 'sensor.x', ?, ?)", (pk, ch, bar, line))
    c.commit()
    return st


def _style(st, meter, ch):
    return tuple(st._conn.execute(
        "SELECT mc.bar_color, mc.rate_line FROM meter_channels mc JOIN meters m ON m.id = mc.meter_id "
        "WHERE m.meter_id = ? AND mc.channel = ?", (meter, ch)).fetchone())


SOURCE_DEVICES = [("ev_charger", "ev", "Zappi EV Charger"), ("house_battery", "battery", "Solax Battery"),
                  ("garage_battery", "battery", "Garage Battery")]
SOURCE_COLOURS = {("electricity_main", "import"): ("#ff8c82", None), ("ev_charger", "import"): ("#9467bd", "solid"),
                  ("house_battery", "import"): ("#96d35f", "dashed"), ("garage_battery", "import"): ("#123456", None)}


def _source():
    st = _build(BlockStore(":memory:"), SOURCE_DEVICES, SOURCE_COLOURS)
    home_eras.save(st, home_eras.clean([ERA]))
    return st


class TheFile(unittest.TestCase):

    def test_collect_holds_the_eras_and_every_style_as_drawn_by_role(self):
        import customisations as cz
        st = _source()
        d = cz.collect(st, st.config_from_db(1), False)
        self.assertEqual((d["format"], d["version"]), ("emt-customisations", 1))
        self.assertEqual([e["label"] for e in d["eras"]], ["Added Solar"])
        self.assertEqual([(s["role"], s["label"], s["bar_color"], s["rate_line"]) for s in d["styles"]], [
            ("house", "Direct import (the house)", "#ff8c82", "solid"),
            ("export", "Export", "#8c564b", "dashed"),           # unchosen: the palette's, and export's dashed line
            ("ev", "Zappi EV Charger", "#9467bd", "solid"),
            ("battery", "Solax Battery", "#96d35f", "dashed"),
            ("battery", "Garage Battery", "#123456", "solid")])
        self.assertNotIn("meter_id", json.dumps(d))           # by role, never an id

    def test_collect_from_a_backup_zip_reads_its_blocks_db(self):
        import customisations as cz
        tmp = tempfile.mkdtemp()
        try:
            db = os.path.join(tmp, "blocks.db")
            st = _build(BlockStore(db), SOURCE_DEVICES, SOURCE_COLOURS)
            home_eras.save(st, home_eras.clean([ERA]))
            st._conn.close()
            zp = os.path.join(tmp, "20261119T090000_manual.zip")
            with zipfile.ZipFile(zp, "w") as zf:
                zf.write(db, "blocks.db")
            d = cz.collect_from_backup(zp, lambda s, c: False)
            self.assertEqual(d["source"], "backup 20261119T090000_manual.zip")
            self.assertEqual([e["label"] for e in d["eras"]], ["Added Solar"])
            self.assertEqual([s["bar_color"] for s in d["styles"]][2:], ["#9467bd", "#96d35f", "#123456"])
            empty = os.path.join(tmp, "empty.zip")
            with zipfile.ZipFile(empty, "w") as zf:
                zf.writestr("readme.txt", "x")
            with self.assertRaises(cz.ThemeError):
                cz.collect_from_backup(empty, lambda s, c: False)
        finally:
            shutil.rmtree(tmp)


class TheImport(unittest.TestCase):

    def _file(self):
        import customisations as cz
        st = _source()
        return json.loads(json.dumps(cz.collect(st, st.config_from_db(1), False)))

    def test_styles_go_by_role_and_two_of_a_role_in_config_order(self):
        import customisations as cz
        # another install: other ids, the batteries listed the other way round from the source's labels
        tgt = _build(BlockStore(":memory:"), [("sub_meter_2", "battery", "Solax"), ("sub_meter_1", "ev", "Zappi"),
                                              ("sub_meter_3", "battery", "Powerwall")])
        parsed = cz.parse(self._file())
        p = cz.plan(parsed, cz.targets(tgt.config_from_db(1), False))
        cz.apply(tgt, parsed, p, 1)
        self.assertEqual(_style(tgt, "electricity_main", "import"), ("#ff8c82", "solid"))
        self.assertEqual(_style(tgt, "electricity_main", "export"), ("#8c564b", "dashed"))
        self.assertEqual(_style(tgt, "sub_meter_1", "import"), ("#9467bd", "solid"))
        self.assertEqual(_style(tgt, "sub_meter_2", "import"), ("#96d35f", "dashed"))     # the first battery's
        self.assertEqual(_style(tgt, "sub_meter_3", "import"), ("#123456", "solid"))      # the second's

    def test_unmatched_styles_are_skipped_and_unmatched_meters_kept(self):
        import customisations as cz
        tgt = _build(BlockStore(":memory:"), [("hp", "heat_pump", "Vaillant"), ("b", "battery", "Solax")],
                     {("hp", "import"): ("#00ff00", "solid")})
        parsed = cz.parse(self._file())
        p = cz.plan(parsed, cz.targets(tgt.config_from_db(1), False))
        s = cz.summary(parsed, p, 0)
        self.assertEqual(s["skipped"], ["Zappi EV Charger", "Garage Battery"])
        self.assertEqual(s["kept"], ["Vaillant"])
        cz.apply(tgt, parsed, p, 1)
        self.assertEqual(_style(tgt, "hp", "import"), ("#00ff00", "solid"))

    def test_the_eras_replace_the_ones_here(self):
        import customisations as cz
        tgt = _build(BlockStore(":memory:"), [])
        home_eras.save(tgt, home_eras.clean([{"label": "Old", "kind": "other", "start": "2021-01-01"},
                                             {"label": "Older", "kind": "ev", "start": "2020-01-01"}]))
        parsed = cz.parse(self._file())
        cz.apply(tgt, parsed, cz.plan(parsed, cz.targets(tgt.config_from_db(1), False)), 1)
        self.assertEqual([e["label"] for e in home_eras.load(tgt)], ["Added Solar"])
        # a file with no eras key leaves them; an empty list clears them
        f = self._file()
        del f["eras"]
        self.assertIsNone(cz.parse(f)["eras"])
        f["eras"] = []
        self.assertEqual(cz.parse(f)["eras"], [])

    def test_the_car_without_a_device_takes_the_ev_s_style(self):
        import customisations as cz
        tgt = _build(BlockStore(":memory:"), [])
        parsed = cz.parse(self._file())
        p = cz.plan(parsed, cz.targets(tgt.config_from_db(1), True))
        cz.apply(tgt, parsed, p, 1)
        self.assertEqual(tgt.get_meta("ev_style"), {"bar_color": "#9467bd", "rate_line": "solid"})

    def test_not_one_of_these_files_or_from_a_newer_emt(self):
        import customisations as cz
        for bad in (None, [], {"format": "something"}, {"format": "emt-customisations", "version": 99},
                    {"format": "emt-customisations", "version": 1, "eras": [{"label": "x", "kind": "nope", "start": "2024-01-01"}]}):
            with self.assertRaises(cz.ThemeError):
                cz.parse(bad)
        # junk colours are dropped, as everywhere a colour reaches a page
        p = cz.parse({"format": "emt-customisations", "version": 1,
                      "styles": [{"role": "house", "bar_color": "red\"><script>", "rate_line": "dotted"},
                                 {"role": "ev", "bar_color": "#ABCDEF"}]})
        self.assertEqual(p["styles"], [{"role": "ev", "label": "ev", "bar_color": "#abcdef", "rate_line": None}])


class TheRoutes(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("server_theme", os.path.join(_ROOT, "web", "server.py"))
        cls.srv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.srv)

    def setUp(self):
        self.st = _source()
        self.cfg = self.st.config_from_db(1)
        import energy_charts
        self.p = [mock.patch.object(self.srv, "load_config", lambda: self.cfg),
                  mock.patch.object(self.srv.store_access, "store", lambda: self.st),
                  mock.patch.object(self.srv, "_engine_write", lambda op: op(self.st)),
                  mock.patch.object(energy_charts, "has_had_ev", lambda s: False)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_download_is_a_file(self):
        r = self.srv.app.test_client().get("/api/customisations")
        self.assertEqual(r.status_code, 200)
        self.assertRegex(r.headers["Content-Disposition"], r'^attachment; filename="emt-eras-and-colours-\d{8}\.json"$')
        self.assertEqual(json.loads(r.data)["format"], "emt-customisations")
        self.assertEqual(self.srv.app.test_client().get("/api/customisations?backup=x.txt").status_code, 400)

    def test_preview_writes_nothing_and_apply_writes(self):
        c = self.srv.app.test_client()
        f = json.loads(c.get("/api/customisations").data)
        f["eras"] = []
        f["styles"][0]["bar_color"] = "#010203"
        d = c.post("/api/customisations/import", json={"file": f}).get_json()
        self.assertEqual((d["ok"], d["applied"], d["eras"], d["eras_now"]), (True, False, 0, 1))
        self.assertEqual(len(home_eras.load(self.st)), 1)
        self.assertEqual(_style(self.st, "electricity_main", "import"), ("#ff8c82", None))
        d = c.post("/api/customisations/import", json={"file": f, "apply": True}).get_json()
        self.assertTrue(d["applied"])
        self.assertEqual(home_eras.load(self.st), [])
        self.assertEqual(_style(self.st, "electricity_main", "import"), ("#010203", "solid"))

    def test_a_bad_file_is_refused_and_writes_nothing(self):
        r = self.srv.app.test_client().post("/api/customisations/import", json={"file": {"format": "x"}, "apply": True})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(len(home_eras.load(self.st)), 1)


class ThePage(unittest.TestCase):

    def test_download_and_import_in_backup_and_restore(self):
        self.assertIn("onclick=\"themeDownload('')\"", DM)
        self.assertIn("onclick=\"themeDownload(\\'' + zq + '\\')\">🎨</button>", DM)
        self.assertIn("accept=\".json,application/json\"", DM)

    def test_a_dropped_json_is_imported_not_restored(self):
        i = DM.index("function onFileSelect(")
        f = DM[i:DM.index("\n}\n", i)]
        self.assertIn("if (j) { themeImport(j); return; }", f)
        self.assertLess(f.index("themeImport"), f.index("showZipConfirm"))

    def test_the_import_shows_what_it_does_before_it_writes(self):
        i = DM.index("async function themeImport(")
        f = DM[i:DM.index("\n}\n", i)]
        self.assertLess(f.index("_themePost(file, false)"), f.index("showConfirm("))
        self.assertLess(f.index("showConfirm("), f.index("_themePost(file, true)"))


if __name__ == "__main__":
    unittest.main()
