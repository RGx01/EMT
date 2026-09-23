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
_MODULES=(block_store energy_charts engine ha_client instance kraken_api_client
          kraken_ingester kraken_mini kraken_rates energy_engine_io main)
_cleanup_scaffolding() {
  for m in "${_MODULES[@]}"; do
    [ -L "tests/$m.py" ] && rm -f "tests/$m.py"
  done
  [ -L tests/web ]       && rm -f tests/web
  [ -L tests/server.py ] && rm -f tests/server.py
  [ -L tests/templates ] && rm -f tests/templates
  return 0
}
trap _cleanup_scaffolding EXIT INT TERM

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