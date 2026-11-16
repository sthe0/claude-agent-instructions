#!/usr/bin/env python3
"""Hermetic tests for hook-guard-pgrep-wait.decide(). No process is spawned."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import importlib.util

import pytest

_HOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hook-guard-pgrep-wait.py")
_spec = importlib.util.spec_from_file_location("guard_pgrep_wait", _HOOK)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

DENY = [
    # The 2026-11-16/17 incident shapes.
    ('f=/x/thinker-r2.out; until ! pgrep -f "thinker-r2-brief.md" >/dev/null; do sleep 5; done; tail -c 6000 "$f"',
     "incident with output tail"),
    ('until ! pgrep -f "thinker-r3-brief.md" >/dev/null; do sleep 5; done', "incident r3 waiter"),
    ("while pgrep -f spawn-specialist >/dev/null; do sleep 10; done", "while loop, -f"),
    ("while pgrep -af job.py; do sleep 3; done", "combined -af"),
    ("until ! pgrep -u the0 -f myjob; do sleep 1; done", "-f after -u value"),
    ("while pgrep -fl job; do sleep 1; done", "combined -fl"),
    ("while pgrep --full job; do sleep 1; done", "long --full"),
]

ALLOW = [
    ("pgrep -f thinker-r3-brief.md", "pgrep -f without a loop"),
    ("while kill -0 $JOB; do sleep 5; done", "loop on kill -0 (pid wait)"),
    ("until ! pgrep python3; do sleep 2; done", "loop on pgrep without -f"),
    ("scripts/wait-for-pid.sh 12345", "the sanctioned wait"),
    ("for f in *.log; do grep -c ERROR $f; done", "loop with no pgrep"),
    ("pgrep -u the0 -l python; echo done", "pgrep without -f, no loop"),
    ("tail -f /var/log/x.log", "-f on a non-pgrep command"),
]


@pytest.mark.parametrize("command,label", DENY, ids=[c[1] for c in DENY])
def test_denies_pgrep_f_wait_loop(command, label):
    reason = guard.decide(command)
    assert reason is not None, label
    assert "wait-for-pid.sh" in reason
    assert "run_in_background" in reason


@pytest.mark.parametrize("command,label", ALLOW, ids=[c[1] for c in ALLOW])
def test_allows_benign_commands(command, label):
    assert guard.decide(command) is None, label
