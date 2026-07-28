#!/usr/bin/env bash
# Run the pytest suite that lives beside this machine's difficulty-channel
# plugins, if there is one.
#
# Why it exists: an adapter extracted out of this repo left the repo's test
# suite behind with it. Its tests can only live next to it, under
# <plugin dir>/tests/, and nothing in the repo enumerates that directory - so
# without a runner an extracted adapter is code nobody ever tests again.
#
# Default is fail-OPEN: no plugin tests reports so and exits 0, so a fresh clone
# with no overlay is unaffected. Two opt-ins tighten that:
#   --assert-tests-min N          fewer than N test files is a FAILURE. For a
#                                 caller that KNOWS tests must exist; without it
#                                 a suite that silently collected nothing would
#                                 pass as "no tests".
#   --require-if-plugin-installed same as --assert-tests-min 1, but only once
#                                 plugin code is actually installed. This is the
#                                 machine-agnostic form: it requires tests
#                                 exactly where extracted code exists, and stays
#                                 a no-op on a machine that has no plugin at all.
#
# How to run it:  bash scripts/verify-plugin-tests.sh [--assert-tests-min N]
# It also runs as part of scripts/verify-instructions-sync.sh.
set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MIN=""
REQUIRE_IF_INSTALLED=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --assert-tests-min) MIN="${2:-}"; shift 2 ;;
    --assert-tests-min=*) MIN="${1#*=}"; shift ;;
    --require-if-plugin-installed) REQUIRE_IF_INSTALLED=1; shift ;;
    *) echo "usage: verify-plugin-tests.sh [--assert-tests-min N] [--require-if-plugin-installed]" >&2; exit 2 ;;
  esac
done
if [[ -n "$MIN" && ! "$MIN" =~ ^[0-9]+$ ]]; then
  echo "FAIL: --assert-tests-min takes a non-negative integer, got '$MIN'" >&2
  exit 2
fi

# Resolve the plugin root through the same helper the adapter loader uses, so the
# runner can never look somewhere the loader does not.
PLUGIN_DIR="$(PYTHONPATH="$SCRIPTS_DIR${PYTHONPATH:+:$PYTHONPATH}" python3 - <<'PY'
from difficulty_channel.adapters import PLUGIN_DIR_ENV, PLUGIN_DIR_NAME
from lib.plugin_dir import resolve_plugin_dir

print(resolve_plugin_dir(PLUGIN_DIR_ENV, PLUGIN_DIR_NAME))
PY
)"
TESTS_DIR="$PLUGIN_DIR/tests"

count_files() {
  local dir="$1" pattern="$2"
  [[ -d "$dir" ]] || { echo 0; return; }
  find "$dir" -type f -name "$pattern" | wc -l | tr -d '[:space:]'
}

# Every plugin file except the tests themselves: an adapter, a detect hook, and
# whatever the seam grows next all left this repo's suite behind the same way.
count_plugin_sources() {
  [[ -d "$PLUGIN_DIR" ]] || { echo 0; return; }
  find "$PLUGIN_DIR" -type f -name '*.py' -not -path "$TESTS_DIR/*" | wc -l | tr -d '[:space:]'
}

TEST_COUNT="$(count_files "$TESTS_DIR" 'test_*.py')"

if [[ "$REQUIRE_IF_INSTALLED" -eq 1 && -z "$MIN" && "$(count_plugin_sources)" -gt 0 ]]; then
  MIN=1
fi

if [[ -n "$MIN" && "$TEST_COUNT" -lt "$MIN" ]]; then
  echo "FAIL: expected at least $MIN plugin test file(s) under $TESTS_DIR, found $TEST_COUNT."
  echo "An adapter extracted out of this repo carries its own tests there - see the dir's README."
  exit 1
fi

if [[ "$TEST_COUNT" -eq 0 ]]; then
  echo "OK: no plugin tests under $TESTS_DIR - nothing to run."
  exit 0
fi

echo "Running $TEST_COUNT plugin test file(s) under $TESTS_DIR"
PYTHONPATH="$SCRIPTS_DIR${PYTHONPATH:+:$PYTHONPATH}" python3 -m pytest "$TESTS_DIR" -q
