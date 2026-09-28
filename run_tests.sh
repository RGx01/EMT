#!/usr/bin/env bash
# Run the EMT unit-test suite.
#
# The suite is designed to run FILE-ISOLATED: several test modules install
# sys.modules stubs at import time, so importing them all into one process
# (plain `pytest tests/`) collides at collection. This script runs each test
# file in its own process and aggregates the result — the invocation the suite
# expects. Works on Python 3.10+ (energy_charts.py is dual-compatible).
#
# It also (idempotently) creates the source symlinks the tests load by path
# (tests/<module>.py, tests/web, tests/server.py, tests/templates), so a fresh
# checkout can run the suite without any manual setup.
#
# Usage:  ./run_tests.sh            # whole suite
#         ./run_tests.sh -k rate    # pass extra args through to pytest
set -u
cd "$(dirname "$0")"

# ── Scaffolding: tests load sibling modules by absolute path ─────────────────
# Created here and removed again on exit. Leaving them behind means every later
# `git status`, `git add -A`, diff or zip of the tree carries a dozen stray symlinks
# and two directories that are pure build scaffolding — which is how they end up
# inside patches. Only SYMLINKS are removed, so a real file of the same name (if
# anyone ever adds one) is never touched.
_MODULES=(billing block_store energy_charts engine ha_client instance kraken_api_client
          kraken_ingester kraken_mini kraken_rates energy_engine_io main)

# ── Temp databases: scoped to this run, removed with it ──────────────────────
# 42 call sites across 23 test files build a store with `tempfile.mktemp()` and
# never close or unlink it. MEASURED: one full run leaves **344 files, 170 MB**
# behind in the system temp directory, and it is cumulative — a handful of runs
# fills a small disk, which on a Pi is the disk the database lives on.
#
# Fixed here rather than in 42 places. `tempfile` honours TMPDIR, so pointing it
# at a run-scoped directory makes every one of those sites self-cleaning without
# a single test edit, and a test that DOES clean up after itself is unaffected.
# Exported before pytest runs so each child process inherits it.
#
# It also isolates runs from each other: a leftover database from a previous run
# can no longer be picked up by a later one, which is the kind of coincidence
# that produces a test that passes alone and fails in the suite.
# Try the inherited TMPDIR first, then /tmp — a CI box or container can have a
# TMPDIR pointing somewhere small or full, and the first version of this exported
# an EMPTY TMPDIR when mktemp failed there, which silently restored the leak it
# was added to fix. Only export once a directory actually exists.
# Sweep any run directory a PREVIOUS run could not clean. The trap below handles
# exit, interrupt and terminate — but not SIGKILL, and not a power cut, so a
# killed run leaves its databases behind with nothing to remove them. Observed:
# one 66 MB directory after a harness timeout killed the runner outright.
for _cand in "${TMPDIR:-/tmp}" /tmp; do
  [ -d "$_cand" ] && rm -rf "$_cand"/emt-tests-* 2>/dev/null
done

_TMPROOT=""
for _cand in "${TMPDIR:-/tmp}" /tmp; do
  _TMPROOT="$(mktemp -d "$_cand/emt-tests-XXXXXX" 2>/dev/null)" && break
  _TMPROOT=""
done
if [ -n "$_TMPROOT" ] && [ -d "$_TMPROOT" ]; then
  export TMPDIR="$_TMPROOT"
else
  echo "run_tests.sh: WARNING — could not create a run-scoped temp dir;" >&2
  echo "              test databases will be left in ${TMPDIR:-/tmp}" >&2
fi

_cleanup_scaffolding() {
  # Stop the in-flight pytest FIRST. On an interrupt the child outlives this
  # handler for a moment, and removing the temp root out from under it makes
  # Python's tempfile fall back to the system /tmp — so the interrupted run
  # leaks there instead. Measured: 228 files after a `timeout`-killed run, with
  # the run directory itself correctly cleaned.
  pkill -P $$ >/dev/null 2>&1 || true
  wait 2>/dev/null || true

  # Temp databases next: if the symlink removal below ever fails, the disk is
  # still reclaimed.
  case "$_TMPROOT" in
    */emt-tests-*) rm -rf "$_TMPROOT" ;;   # never rm -rf an unexpected path
  esac
  for m in "${_MODULES[@]}"; do
    [ -L "tests/$m.py" ] && rm -f "tests/$m.py"
  done
  [ -L tests/web ]       && rm -f tests/web
  [ -L tests/server.py ] && rm -f tests/server.py
  [ -L tests/templates ] && rm -f tests/templates
  return 0
}
# INT/TERM must EXIT after cleaning, not fall through. `trap handler EXIT INT TERM`
# runs the handler and then CARRIES ON with the loop — so an interrupted run
# removed its own temp root and then kept creating databases with TMPDIR pointing
# at a directory that no longer existed, which Python's tempfile silently resolves
# by falling back to the system /tmp. That is where the 228 leaked files came
# from, and the run directory looked correctly cleaned the whole time.
trap _cleanup_scaffolding EXIT
trap '_cleanup_scaffolding; exit 130' INT TERM

for m in "${_MODULES[@]}"; do
  [ -e "tests/$m.py" ] || ln -sf "../$m.py" "tests/$m.py"
done
[ -e tests/web ]        || ln -sfn ../web tests/web
[ -e tests/server.py ]  || ln -sf ../web/server.py tests/server.py
[ -e tests/templates ]  || ln -sfn ../web/templates tests/templates

# ── Run each test file in its own process ────────────────────────────────────
# Three of these files are named test_* and are not unit tests — manual tools
# that take arguments and do work. Each contributed nothing and said nothing to
# every green run until the no-result guard below named them:
#
#   test_harness.py         manual chart inspection
#   test_data_generator.py  `python3 test_data_generator.py --days 7 --output ...`
#   test_gap_automated.py   `python3 tests/test_gap_automated.py <blocks.db>`
_MANUAL=(test_harness.py test_data_generator.py test_gap_automated.py)
PYTEST="${PYTEST:-python3 -m pytest}"
total_pass=0 total_fail=0 total_err=0 failed_files=()
total_skip=0 silent_files=()
for f in tests/test_*.py; do
  _b=$(basename "$f")
  # `continue 2` — leave the inner loop AND skip the file; a bare `continue`
  # would only advance to the next name in _MANUAL.
  for _m in "${_MANUAL[@]}"; do [ "$_b" = "$_m" ] && continue 2; done
  # Capture the FULL output (with -rf short summary) so a failure surfaces the
  # test id + traceback — otherwise a CI failure is invisible (only the count).
  full=$($PYTEST "$f" -rf -p no:cacheprovider "$@" 2>&1)
  out=$(tail -1 <<<"$full")
  p=$(grep -oE '[0-9]+ passed' <<<"$out" | grep -oE '[0-9]+'); p=${p:-0}
  fl=$(grep -oE '[0-9]+ failed' <<<"$out" | grep -oE '[0-9]+'); fl=${fl:-0}
  er=$(grep -oE '[0-9]+ error'  <<<"$out" | grep -oE '[0-9]+'); er=${er:-0}
  sk=$(grep -oE '[0-9]+ skipped' <<<"$out" | grep -oE '[0-9]+'); sk=${sk:-0}
  total_skip=$((total_skip+sk))
  [ $((p+fl+er+sk)) -eq 0 ] && silent_files+=("$(basename "$f")")
  total_pass=$((total_pass+p)); total_fail=$((total_fail+fl)); total_err=$((total_err+er))
  if [ "$fl" != 0 ] || [ "$er" != 0 ]; then
    failed_files+=("$(basename "$f"): $out")
    printf '\n──────── output for %s ────────\n%s\n' "$f" "$full"
  fi
done

echo "═══════════════════════════════════════════════"
echo "  passed=$total_pass  failed=$total_fail  errors=$total_err  skipped=$total_skip"

if [ $# -eq 0 ] && [ "${#silent_files[@]}" -ne 0 ]; then
  echo "  NO RESULT PARSED (counted as nothing):"
  printf '    %s\n' "${silent_files[@]}"
fi

if [ "${#failed_files[@]}" -ne 0 ]; then
  echo "  files needing attention:"
  printf '    %s\n' "${failed_files[@]}"
fi
echo "═══════════════════════════════════════════════"
[ "$total_fail" = 0 ] && [ "$total_err" = 0 ] && { [ $# -ne 0 ] || [ "${#silent_files[@]}" -eq 0 ]; }