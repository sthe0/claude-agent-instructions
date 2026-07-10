#!/usr/bin/env bash
# Hermetic entry point for the opening-dialogue brief (opening.py).
#
# The substance lives in the pytest suite scripts/tests/test_opening.py
# (pure-function probe/brief tests + CLI/subprocess tests for the emit
# contract) — co-located with its sibling test_detect_backend.py so the
# repo's pytest run collects it. This thin wrapper is the bash entry point
# the plan names (and the other project_entry/tests/*.sh hermetic tests use),
# so a bash-driven check reaches the same assertions.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
exec python3 -m pytest "$REPO/scripts/tests/test_opening.py" -q
