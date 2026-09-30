#!/usr/bin/env python3
"""
Workstream C, rule 2: a function whose output depends on tariff context takes that
context as a REQUIRED argument, with no default.

WHY (C §2). The 4.5.13 `/api/charts/regenerate` episode: the scheduled renderer
passed `cap_from` and `import_schedule`; the on-demand endpoint silently did not,
so `capped=False` and the same function drew different charts depending on who
called it. Two days went into the rendering layer, because the divergence was
invisible at the call site. `cap_from=None` meaning "render as if uncapped" is what
made the omission silent. *The default is the defect.*

A timezone is tariff context too: a day, a billing period and an Economy-7 band are
all wall-clock questions, and `tz_name='UTC'` answers them wrongly for every UK
summer day without an error. (30 Sep 2026: `get_day_rollups` grouped by UTC date
for exactly that reason.)

WHAT IT COUNTS. A parameter is tariff context by NAME, from the vocabulary below
— explicit rather than pattern-matched, so `cap=20000` (a row limit) and
`hold_rate` (a mode flag, #475) are not swept in. Scope is the pricing and serving
modules. A defaulted parameter in that vocabulary is a finding.

A RATCHET, not an assertion of the finished state (C §0, 30 Sep 2026: ~20 exist).
The committed baseline is today's set; the set may shrink and must never grow. A
new function that takes `tz_name=None` fails the build on the commit that adds it.
When the set is empty the baseline is deleted and the test becomes a plain
assertion.

USAGE
    python3 .build/check_tariff_defaults.py [--json]
    python3 .build/check_tariff_defaults.py --baseline .build/tariff_defaults_baseline.json
    python3 .build/check_tariff_defaults.py --update-baseline <path>
"""
import argparse
import ast
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MODULES = ["energy_charts.py", "billing.py", "chart_emit.py", "engine.py",
           "web/server.py", "block_store.py", "pricing_segments.py", "iog_cap.py",
           "reprice.py", "carbon.py", "vat_calendar.py", "kraken_rates.py"]

# Tariff context, by parameter name. Add to this list deliberately.
CONTEXT = {
    "cap_from", "capped", "over_cap", "cap_hours", "cap_state",
    "import_schedule", "export_schedule", "rate_schedule", "schedules", "sched",
    "rate_segs", "tariff", "tariff_code", "agreement", "agreements",
    "tz", "tz_name", "timezone", "timezone_name", "site_tz",
    "vat_rate", "vat",
}


def findings(root=ROOT):
    out = []
    for rel in MODULES:
        path = os.path.join(root, rel)
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=rel)
        for n in ast.walk(tree):
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            a = n.args
            pos = a.posonlyargs + a.args
            pairs = list(zip(pos[len(pos) - len(a.defaults):], a.defaults))
            pairs += [(k, d) for k, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None]
            for arg, default in pairs:
                if arg.arg in CONTEXT:
                    out.append("%s:%s(%s=%s)" % (rel, n.name, arg.arg, ast.unparse(default)))
    return sorted(set(out))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--baseline")
    ap.add_argument("--update-baseline")
    a = ap.parse_args(argv)
    f = findings()
    if a.update_baseline:
        with open(a.update_baseline, "w") as fh:
            json.dump({"defaulted_context": f}, fh, indent=2)
            fh.write("\n")
        print("baseline written: %d defaulted tariff-context parameter(s)" % len(f))
        return 0
    if a.baseline:
        base = set(json.load(open(a.baseline))["defaulted_context"])
        new = sorted(set(f) - base)
        gone = sorted(base - set(f))
        for x in new:
            print("NEW  " + x)
        for x in gone:
            print("gone " + x + "   (shrink the baseline)")
        return 1 if new else 0
    if a.json:
        print(json.dumps({"count": len(f), "defaulted_context": f}, indent=2))
    else:
        print("%d defaulted tariff-context parameter(s)" % len(f))
        for x in f:
            print("  " + x)
    return 0


if __name__ == "__main__":
    sys.exit(main())
