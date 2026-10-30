#!/usr/bin/env python3
"""
The pypdf bump gate (ROADMAP BL-64): does a new pypdf parse your bills the way the pinned one does?

WHY THIS EXISTS
    `bill_parser` reads a PDF bill through pypdf in one place (`_read_pages`) and everything after
    it is regular expressions over the extracted text. A pypdf release that changes how text comes
    out — spacing, line order, a ligature — can change or drop a parsed figure without an error.
    The parser's tests run on placeholder text, not PDFs, so they cannot see it.

    The bills themselves never enter the repo (29 Oct 2026, the user): not as fixtures, not
    redacted, and not their snapshots. So this runs locally, against a folder of bills you keep
    elsewhere, and the snapshots it writes live in that folder.

WHAT IT PRINTS
    A bill is named by the first 12 characters of its PDF's sha256, never its file name (a file
    name can carry an account number). A difference is reported as the field's path and how many
    times it differs — never a value. Nothing from a bill reaches the terminal or a log.

USAGE
    # with the pinned pypdf (the repo's venv):
    python3 .build/pypdf_gate.py snapshot <bills dir>
    # in a throwaway venv outside the repo, with the candidate pypdf installed:
    python3 .build/pypdf_gate.py compare <bills dir> --against 6.18.0

    `compare` exits 0 when every bill parses the same, 1 when any differs, 2 on a usage error
    (no snapshot for that version, no bills, a folder inside the repo). Extracted text that
    changed but parsed the same is reported, and does not fail the gate.
"""
import argparse
import dataclasses
import hashlib
import json
import os
import re
import sys
from datetime import date

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP_DIR = ".pypdf-snapshots"


class GateError(Exception):
    """A usage error: exit 2."""


def _inside_repo(path: str) -> bool:
    p, root = os.path.realpath(path), os.path.realpath(_ROOT)
    return p == root or p.startswith(root + os.sep)


def _bills(bills_dir: str) -> list:
    if _inside_repo(bills_dir):
        raise GateError("the bills folder is inside the repo — bills stay outside it")
    if not os.path.isdir(bills_dir):
        raise GateError("no such folder")
    out = []
    for dirpath, dirnames, files in os.walk(bills_dir):
        dirnames[:] = [d for d in dirnames if d != SNAP_DIR]
        out += [os.path.join(dirpath, f) for f in files if f.lower().endswith(".pdf")]
    if not out:
        raise GateError("no PDFs in that folder")
    return sorted(out)


def _key(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def _plain(v):
    """A parsed bill as JSON-able data: dataclasses to dicts, dates to ISO, tuples to lists."""
    if dataclasses.is_dataclass(v):
        return {f.name: _plain(getattr(v, f.name)) for f in dataclasses.fields(v)}
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return v


def parse_one(data: bytes) -> dict:
    """What the gate compares for one bill: the parsed bill and the CSV rows built from it, plus a
    hash of the extracted text (information only)."""
    sys.path.insert(0, _ROOT)
    import bill_parser as bp
    import pypdf
    try:
        pages = bp._read_pages(data)
    except Exception as e:               # parse_bill records it as a warning; keep its type here
        pages = [f"unreadable: {type(e).__name__}"]
    bill = bp.parse_bill(data, source_name="bill.pdf")      # never the file's own name
    parsed = _plain(bill)
    parsed.pop("source", None)
    return {"pypdf": pypdf.__version__,
            "text_sha": hashlib.sha256("\f".join(pages).encode()).hexdigest(),
            "parsed": {"bill": parsed, "csv": _plain(bp.build_csv_rows(bill))}}


_INDEX = re.compile(r"\[\d+\]")


def diff_paths(a, b, path: str = "") -> dict:
    """{path: count} of where `a` and `b` differ — list indices folded to [*], so 48 changed
    half-hours read as one path counted 48 times. Paths and counts only: no value is returned."""
    out = {}

    def add(p):
        k = _INDEX.sub("[*]", p) or "(root)"
        out[k] = out.get(k, 0) + 1

    def walk(x, y, p):
        if isinstance(x, dict) and isinstance(y, dict):
            for k in sorted(set(x) | set(y), key=str):
                q = f"{p}.{k}" if p else str(k)
                if k not in x or k not in y:
                    add(q + " (present in one only)")
                else:
                    walk(x[k], y[k], q)
        elif isinstance(x, list) and isinstance(y, list):
            if len(x) != len(y):
                add(p + " (length)")
            for i, (u, v) in enumerate(zip(x, y)):
                walk(u, v, f"{p}[{i}]")
        elif x != y:
            add(p)

    walk(a, b, path)
    return out


def snapshot(bills_dir: str) -> int:
    import pypdf
    dest = os.path.join(bills_dir, SNAP_DIR, pypdf.__version__)
    files = _bills(bills_dir)
    os.makedirs(dest, exist_ok=True)
    for f in files:
        with open(f, "rb") as fh:
            data = fh.read()
        with open(os.path.join(dest, _key(data) + ".json"), "w", encoding="utf-8") as fh:
            json.dump(parse_one(data), fh, indent=1, sort_keys=True)
    print(f"snapshot: {len(files)} bill(s) with pypdf {pypdf.__version__}")
    return 0


def compare(bills_dir: str, against: str) -> int:
    import pypdf
    src = os.path.join(bills_dir, SNAP_DIR, against)
    files = _bills(bills_dir)
    if not os.path.isdir(src):
        raise GateError(f"no snapshot for pypdf {against} — run `snapshot` with it first")
    print(f"compare: pypdf {pypdf.__version__} against {against}, {len(files)} bill(s)")
    failed = 0
    for f in files:
        with open(f, "rb") as fh:
            data = fh.read()
        k = _key(data)
        try:
            with open(os.path.join(src, k + ".json"), encoding="utf-8") as fh:
                old = json.load(fh)
        except FileNotFoundError:
            print(f"  {k}  no snapshot (a bill added since) — not compared")
            continue
        new = parse_one(data)
        d = diff_paths(old["parsed"], new["parsed"])
        if d:
            failed += 1
            print(f"  {k}  DIFFERS")
            for p, n in sorted(d.items()):
                print(f"      {p}  ×{n}")
        elif old["text_sha"] != new["text_sha"]:
            print(f"  {k}  same parse (the extracted text changed)")
        else:
            print(f"  {k}  same")
    print("FAIL: the bump changes how bills parse" if failed else "OK: every bill parses the same")
    return 1 if failed else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot", help="parse every bill with the installed pypdf and save it")
    s.add_argument("bills_dir")
    c = sub.add_parser("compare", help="parse again and compare with a saved snapshot")
    c.add_argument("bills_dir")
    c.add_argument("--against", required=True, help="the pypdf version the snapshot was taken with")
    a = ap.parse_args(argv)
    try:
        return snapshot(a.bills_dir) if a.cmd == "snapshot" else compare(a.bills_dir, a.against)
    except GateError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
