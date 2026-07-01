#!/usr/bin/env python3
"""Hermetic tests for hook-guard-destructive-rm.decide().

Runs decide() directly with a fixed HOME so no real filesystem is touched.
Exit 0 on all-pass.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import importlib.util

_HOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hook-guard-destructive-rm.py")
_spec = importlib.util.spec_from_file_location("guard_rm", _HOOK)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

HOME = "/Users/the0"

# (command, should_deny, label)
CASES = [
    # The incident: empty $HASH collapses under ~/.claude/projects.
    ('rm -rf "$HOME/.claude/projects/$HASH"', True, "empty-var collapse under ~/.claude"),
    ('rm -rf "$DIR" "$BASE" "$HOME/.claude/projects/$HASH"', True, "incident multi-arg"),
    # Direct protected targets.
    ("rm -rf ~/.claude", True, "delete ~/.claude"),
    ("rm -rf ~/.claude/projects", True, "delete inside ~/.claude"),
    ("rm -rf /", True, "delete root"),
    ('rm -rf "$HOME"', True, "delete home"),
    ("rm -rf /Users", True, "ancestor of home"),
    ("rm -rf ~/claude-agent-instructions", True, "delete instruction repo"),
    ("rm -rf ~/claude-agent-instructions/scripts", True, "inside instruction repo"),
    ("rm -fr ~/.claude", True, "-fr flag order"),
    ("rm -r -f ~/.claude", True, "separate -r -f"),
    ("rm --recursive --force ~/.claude", True, "long flags"),
    # Legitimate deletes — must NOT be blocked.
    ("rm -rf /tmp/foo", False, "temp path"),
    ("rm -rf ~/projects/marmaris-2025-04-11", False, "legit project under home"),
    ("rm -rf ./build", False, "cwd-relative build"),
    ('rm -rf "$TMPDIR/scratch"', False, "TMPDIR scratch"),
    ("rm -f ~/.claude/settings.json", False, "non-recursive rm (single file)"),
    ("ls -la ~/.claude", False, "not an rm"),
    ("rm -rf /var/folders/xx/demo", False, "mktemp-style abs path"),
]


def main() -> int:
    os.environ["HOME"] = HOME
    passed = failed = 0
    for command, should_deny, label in CASES:
        reason = guard.decide(command, HOME)
        denied = reason is not None
        if denied == should_deny:
            passed += 1
        else:
            failed += 1
            print(f"FAIL [{label}]: expected deny={should_deny}, got deny={denied}\n  cmd: {command}")
    print(f"guard-destructive-rm tests: {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
