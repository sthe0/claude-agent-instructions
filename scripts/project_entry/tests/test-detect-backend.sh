#!/usr/bin/env bash
# Hermetic detector-test entry point for the project-entry subsystem.
#
# The substance lives in the pytest suite scripts/tests/test_detect_backend.py
# (pure-function rows + enter-task selection-precedence rows via subprocess) —
# co-located with its sibling scripts/tests/test_difficulty_channel.py so the
# repo's pytest run collects it. This thin wrapper is the bash entry point the
# plan names (and the other project_entry/tests/*.sh hermetic tests use), so a
# bash-driven check reaches the same assertions.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
exec python3 -m pytest "$REPO/scripts/tests/test_detect_backend.py" -q
