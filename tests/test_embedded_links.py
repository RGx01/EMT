"""
A page opened as a modal (?embed=1) doesn't navigate its own frame (21 Oct 2026, staging).

After a PDF bill import, the done step's "← Data Management" loaded Data Management inside the
modal: a page within the page, sidebar and all. In embed mode a link back to Data Management asks
the modal to close (`emt-embed-close`, to the parent), and a link to any other EMT page navigates the
page around the modal. The heatmap's modal listens for the message, from its own frame only.

Discriminating: every test here fails on the parent.
"""
import os
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _t(name):
    with open(os.path.join(_ROOT, "web", "templates", name), encoding="utf-8") as f:
        return f.read()


class TheEmbeddedPage(unittest.TestCase):

    def setUp(self):
        b = _t("base.html")
        i = b.index("{% if request.args.get('embed') %}\ndocument.addEventListener('click'")
        self.js = b[i:b.index("{% endif %}", i)]

    def test_only_in_embed_mode(self):
        self.assertTrue(self.js.startswith("{% if request.args.get('embed') %}"))

    def test_data_management_closes_the_modal(self):
        self.assertIn("/\\/data-management\\/?$/.test(u.pathname)", self.js)
        self.assertIn("window.parent.postMessage({type: 'emt-embed-close'}, location.origin)", self.js)

    def test_another_page_opens_around_it(self):
        self.assertIn("window.parent.location.href = a.href", self.js)

    def test_leaves_downloads_targets_and_anchors_alone(self):
        self.assertIn("a.target || a.hasAttribute('download')", self.js)
        self.assertIn("u.hash && u.pathname === location.pathname", self.js)


class TheModal(unittest.TestCase):

    def test_closes_when_its_frame_asks(self):
        hm = _t("heatmap_module.html")
        self.assertIn("e.source === f.contentWindow && e.data && e.data.type === 'emt-embed-close'", hm)
        self.assertIn("window.removeEventListener('message', fromFrame);", hm)


if __name__ == "__main__":
    unittest.main()
