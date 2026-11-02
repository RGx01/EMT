"""
The repository ready to be public (2 Nov 2026, the user: "the repo preparedness areas such as security, ci to raise
issues around deprecations").

- The weekly GraphQL check opens one issue per deprecated FIELD, listing every type it is on. It read the open
  issues once, before its loop, so findings sharing a field each opened one: `provider`, on eight SmartFlex types,
  would have opened eight.
- The field watch opens (or comments on) one `graphql-schema-watch` issue for a schema change, once per change; it
  only reddened the run, though its script's docstring said it opened an issue.
- Every action is pinned to a commit SHA; Dependabot raises its PRs against `dev` and keeps the actions current.
- SECURITY.md, the issue templates (no blank issues; a private route for security), and `.claude/` ignored.

Discriminating: every test fails on the parent tree.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*p):
    return open(os.path.join(_ROOT, *p), encoding="utf-8").read()


def _step_run(workflow, name):
    """The `run:` block of the step named `name`, de-indented (the workflow's own text)."""
    src = _read(".github", "workflows", workflow)
    i = src.index("- name: " + name)
    j = src.index("        run: |\n", i) + len("        run: |\n")
    k = src.find("\n      - ", j)
    block = src[j:k if k != -1 else len(src)]
    return "\n".join(line[10:] if line.startswith("          ") else line.strip() for line in block.splitlines())


FINDINGS = [{"type": t, "field": "provider", "removal": "2027-04-15", "reason": "deprecated"}
            for t in ("SmartFlexBattery", "SmartFlexVehicle", "SmartFlexChargePoint")] + \
           [{"type": "TerminationFeeType", "field": "amount", "removal": "2027-06-01", "reason": "deprecated"}]


@unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "jq and bash needed")
class TheDeprecationIssues(unittest.TestCase):

    def test_one_issue_a_field_listing_its_types(self):
        d = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(d, "bin"))
            log = os.path.join(d, "gh.log")
            with open(os.path.join(d, "bin", "gh"), "w") as fh:
                fh.write('#!/bin/bash\ncase "$1 $2" in\n  "issue list") ;;\n  "issue create") shift 2; '
                         'printf "%%s\\n" "$@" >> %s; echo "---" >> %s;;\nesac\n' % (log, log))
            os.chmod(os.path.join(d, "bin", "gh"), 0o755)
            with open(os.path.join(d, "findings.json"), "w") as fh:
                json.dump(FINDINGS, fh)
            env = dict(os.environ, PATH=os.path.join(d, "bin") + os.pathsep + os.environ["PATH"])
            res = subprocess.run(["bash", "-c", _step_run("graphql-deprecations.yml",
                                                          "Open or update deprecation issues")],
                                 cwd=d, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(res.returncode, 0, res.stderr)
            issues = open(log).read().split("---\n")[:-1]
            titles = sorted(re.search(r"\[GraphQL Deprecation\][^\n]*", x).group(0) for x in issues)
            self.assertEqual(titles, ["[GraphQL Deprecation] amount — 2027-06-01",
                                      "[GraphQL Deprecation] provider — 2027-04-15"])
            prov = [x for x in issues if "provider —" in x][0]
            for t in ("SmartFlexBattery", "SmartFlexChargePoint", "SmartFlexVehicle"):
                self.assertIn("`%s.provider`" % t, prov)
        finally:
            shutil.rmtree(d)


class TheWatch(unittest.TestCase):

    def test_a_schema_change_opens_an_issue_once(self):
        run = _step_run("graphql-deprecations.yml", "Watch for new/changed fields on IOG reconstruction types")
        self.assertIn('gh issue create --title "[GraphQL schema] watched types changed" --label graphql-schema-watch', run)
        self.assertIn('grep -qF -- "diff ${digest}"', run)
        self.assertNotIn("--fail-on-change", run)


class TheRepository(unittest.TestCase):

    def test_every_action_is_pinned_to_a_sha(self):
        for wf in os.listdir(os.path.join(_ROOT, ".github", "workflows")):
            for use in re.findall(r"uses:\s*(\S+)", _read(".github", "workflows", wf)):
                self.assertRegex(use, r"@[0-9a-f]{40}$", wf)

    def test_dependabot_raises_against_dev_and_keeps_the_actions(self):
        d = _read(".github", "dependabot.yml")
        self.assertEqual(d.count('target-branch: "dev"'), 2)
        self.assertIn('package-ecosystem: "github-actions"', d)

    def test_security_and_issue_templates(self):
        self.assertIn("security/advisories/new", _read("SECURITY.md"))
        self.assertIn("no login", _read("SECURITY.md"))
        self.assertIn("blank_issues_enabled: false", _read(".github", "ISSUE_TEMPLATE", "config.yml"))
        for t in ("bug_report.yml", "v4_import.yml", "beta_feedback.yml"):
            self.assertTrue(os.path.exists(os.path.join(_ROOT, ".github", "ISSUE_TEMPLATE", t)), t)
        self.assertIn("\n.claude/\n", _read(".gitignore"))


if __name__ == "__main__":
    unittest.main()
