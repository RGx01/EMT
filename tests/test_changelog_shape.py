"""
The changelog's shape at a release (21 Nov 2026, the user: "make sure this doesn't escape quality in the future").

The b30 bump (#192) dated its entries under `## [2026.11.0b30]` but kept an empty `## [Unreleased]` heading above
it. The add-on store shows the top of CHANGELOG.md, so the released b30 read as unreleased until #194/#195.

- An `[Unreleased]` heading is never empty: entries make it, the release renames it.
- `config.yaml`'s version has its own dated heading, `## [<version>] — YYYY-MM-DD`.
- On a pull request into `main` (a release) there is no `[Unreleased]` heading at all: everything on it is in
  that release. CI sets GITHUB_BASE_REF on a pull request; elsewhere this one is skipped.

Discriminating: the first fails on the tree tagged v2026.11.0b30.
"""
import os
import re
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*p):
    return open(os.path.join(_ROOT, *p), encoding="utf-8").read()


def sections(text):
    """[(heading, body)] for each `## [` heading, in order."""
    parts = re.split(r"^(## \[[^\n]*)\n", text, flags=re.M)
    return [(parts[i], parts[i + 1]) for i in range(1, len(parts) - 1, 2)]


def version():
    return re.search(r'^version:\s*"([^"]+)"', _read("config.yaml"), re.M).group(1)


class TheChangelog(unittest.TestCase):

    def setUp(self):
        self.secs = sections(_read("CHANGELOG.md"))

    def test_an_unreleased_heading_is_never_empty(self):
        for head, body in self.secs:
            if head.startswith("## [Unreleased]"):
                self.assertTrue(body.strip(), "an empty [Unreleased] heading: the release renames it, "
                                              "it does not leave it above the new version")

    def test_the_version_has_its_dated_heading(self):
        v = version()
        heads = [h for h, _ in self.secs if h.startswith("## [%s]" % v)]
        self.assertEqual(len(heads), 1, "CHANGELOG.md needs one heading for %s" % v)
        self.assertRegex(heads[0], r"^## \[%s\] — \d{4}-\d{2}-\d{2}$" % re.escape(v))

    @unittest.skipUnless(os.environ.get("GITHUB_BASE_REF") == "main", "a pull request into main only")
    def test_a_release_has_no_unreleased_heading(self):
        self.assertFalse([h for h, _ in self.secs if h.startswith("## [Unreleased]")],
                         "a release PR into main still has an [Unreleased] heading")


class TheShapeCheck(unittest.TestCase):
    """The checks on b30's own mistake and on a good release."""

    def test_the_b30_mistake_is_seen(self):
        bad = "# Changelog\n\n## [Unreleased]\n\n## [2026.11.0b30] — 2026-11-21\n\n- a fix\n"
        s = sections(bad)
        self.assertEqual(s[0][0], "## [Unreleased]")
        self.assertFalse(s[0][1].strip())

    def test_entries_under_unreleased_are_not_empty(self):
        good = "# Changelog\n\n## [Unreleased]\n\n- a fix\n\n## [2026.11.0b30] — 2026-11-21\n\n- another\n"
        self.assertTrue(sections(good)[0][1].strip())


if __name__ == "__main__":
    unittest.main()
