"""
BL-101 PR 1 (1 Nov 2026, home eras design) — the home's eras: kept, edited, shown in a modal.

An era is a dated note about the home (*Added solar*, *Moved house*) for display only — never read
for pricing, attribution, settlement or a bill. Kept in the store's meta (`home_eras`, one JSON list),
edited through `GET` / `POST /api/home-eras`, which replaces the list as `home_eras.clean` keeps it:
a label, a kind, a start; an end not before it; the optional text and numbers bounded; anything else
dropped. A house move takes the site name — no address is kept (the user). The modal sits behind a
⚙︎ on the Charts page.

Checked in the local preview on 1 Nov 2026 through the page: the ⚙︎'s menu, the modal, an era added
through its form and read back, an end before its start refused by the page and the server, a house
move's site name filled in; the scratch store's eras cleared after.

Discriminating: every test fails on the parent tree, where none of it existed.
"""
import importlib.util
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import home_eras as H                            # noqa: E402

PAGE = open(os.path.join(_ROOT, "web", "templates", "charts.html"), encoding="utf-8").read()


def _era(**kw):
    e = {"label": "Added solar", "kind": "solar", "start": "2025-03-14"}
    e.update(kw)
    return e


class TheRule(unittest.TestCase):

    def test_kept_with_what_it_holds(self):
        e = H.clean_one(_era(kw="4.2", manufacturer="  Acme   Solar ", notes="", junk="x"))
        self.assertEqual(set(e), {"id", "label", "kind", "start", "kw", "manufacturer"})
        self.assertEqual((e["kw"], e["manufacturer"]), (4.2, "Acme Solar"))
        self.assertRegex(e["id"], r"^[0-9a-f]{12}$")

    def test_its_id_is_kept(self):
        self.assertEqual(H.clean_one(_era(id="55a6ad6d1ce1"))["id"], "55a6ad6d1ce1")

    def test_what_is_refused(self):
        for bad, why in ((_era(label=" "), "a name"), (_era(kind="pool"), "unknown kind"),
                         (_era(start="14/03/2025"), "start date"), (_era(end="2025-01-01"), "ends before"),
                         (_era(kwh=-1), "between 0"), (_era(kw="lots"), "a number")):
            with self.assertRaises(H.EraError) as c:
                H.clean_one(bad)
            self.assertIn(why, str(c.exception))

    def test_the_new_kinds(self):
        self.assertEqual(H.clean_one(_era(kind="house_move", site="Home"))["site"], "Home")
        self.assertEqual(H.clean_one(_era(kind="meter_change"))["kind"], "meter_change")

    def test_no_address_field(self):
        self.assertNotIn("address", H.clean_one(_era(kind="house_move", address="1 High Street")))

    def test_the_list_oldest_first_ids_unique(self):
        out = H.clean([_era(id="aaaaaaaa", start="2026-01-01"), _era(id="aaaaaaaa", label="Battery", kind="battery")])
        self.assertEqual([e["start"] for e in out], ["2025-03-14", "2026-01-01"])
        self.assertEqual(len({e["id"] for e in out}), 2)

    def test_at_most_a_hundred(self):
        with self.assertRaises(H.EraError):
            H.clean([_era()] * 101)

    def test_load_tolerates_junk(self):
        st = mock.Mock(get_meta=lambda k: "not a list")
        self.assertEqual(H.load(st), [])


class TheRoute(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("server_bl101", os.path.join(_ROOT, "web", "server.py"))
        cls.srv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.srv)

    def test_a_post_keeps_the_cleaned_list(self):
        kept = {}

        def _write(op):
            st = mock.Mock()
            st.set_meta.side_effect = lambda k, v: kept.update({k: v})
            return op(st)
        with mock.patch.object(self.srv, "_engine_write", _write):
            r = self.srv.app.test_client().post("/api/home-eras", json={"eras": [_era(kw=4.2)]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(kept["home_eras"][0]["label"], "Added solar")
        self.assertEqual(r.get_json()["eras"], kept["home_eras"])

    def test_a_bad_era_is_refused_with_why(self):
        with mock.patch.object(self.srv, "_engine_write", lambda op: self.fail("nothing written")):
            r = self.srv.app.test_client().post("/api/home-eras", json={"eras": [_era(end="2024-01-01")]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("ends before it starts", r.get_json()["error"])


class ThePage(unittest.TestCase):

    def test_the_cog_and_its_menu(self):
        self.assertIn('onclick="chartsCogMenu(this)"', PAGE)
        i = PAGE.index("function chartsCogMenu(")
        self.assertIn("Home eras\u2026', erasOpen]", PAGE[i:i + 600])

    def test_the_modal_saves_the_whole_list(self):
        i = PAGE.index("function _erasPost(")
        self.assertIn("body: JSON.stringify({eras: list})", PAGE[i:i + 400])

    def test_every_kind_the_server_keeps(self):
        i = PAGE.index("var ERA_KINDS = {")
        block = PAGE[i:PAGE.index("};", i)]
        for k in H.KINDS:
            self.assertIn("  " + k + ":", block)

    def test_a_house_move_takes_the_site_name(self):
        self.assertIn("house_move:   {icon: '\U0001f3e0', name: 'House move',     site: true}", PAGE)
        self.assertIn("placeholder=\"The site\u2019s name, not its address\"", PAGE)


if __name__ == "__main__":
    unittest.main()
