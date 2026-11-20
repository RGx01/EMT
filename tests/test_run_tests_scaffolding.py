"""run_tests.sh symlinks the modules in its _MODULES into tests/ and removes them on exit. A
symlink that is also tracked is deleted by every run, so the tree shows a deletion afterwards:
tests/billing.py went in with an unrelated commit on 20 Nov 2026 (e70b9cf), because billing had
joined _MODULES without joining .gitignore. Each scaffolding path must be ignored and untracked."""
import os
import re
import subprocess
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _scaffolding():
    src = open(os.path.join(_ROOT, "run_tests.sh"), encoding="utf-8").read()
    m = re.search(r"^_MODULES=\(([^)]*)\)", src, re.M)
    assert m, "run_tests.sh: _MODULES not found"
    paths = ["tests/%s.py" % n for n in m.group(1).split()]
    paths += re.findall(r"^\[ -e (tests/\S+) \]\s+\|\| ln ", src, re.M)
    return paths


def _git(*args):
    return subprocess.run(["git", "-C", _ROOT, *args], capture_output=True, text=True)


@unittest.skipUnless(_git("rev-parse", "--git-dir").returncode == 0, "not a git checkout")
class TestRunTestsScaffolding(unittest.TestCase):

    def test_the_list_is_read(self):   # guard: passes before and after
        paths = _scaffolding()
        self.assertIn("tests/billing.py", paths)
        self.assertIn("tests/templates", paths)

    def test_no_scaffolding_path_is_tracked(self):
        tracked = _git("ls-files", "--", *_scaffolding()).stdout.split()
        self.assertEqual(tracked, [], "run_tests.sh deletes these on exit; untrack them")

    def test_every_scaffolding_path_is_ignored(self):
        # --no-index: ask about the rules alone, whether or not a path is tracked.
        out = _git("check-ignore", "--no-index", "--", *_scaffolding()).stdout.split()
        self.assertEqual(sorted(set(_scaffolding()) - set(out)), [],
                         "add these to .gitignore beside the other run_tests.sh symlinks")


if __name__ == "__main__":
    unittest.main()
