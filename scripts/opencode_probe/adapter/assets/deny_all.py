#!/usr/bin/env python3
"""Stand-in denying PreToolUse row of the adapter probe: refuses every call it sees.

Core's own denying gates deny only on engine state, which a stub-driven session never has, so
the adapter probe adds this row through ``AGENT_GATE_EXTRA_ROWS`` to make the verdict path carry
a real deny for each probed tool.
"""
import sys

sys.stdin.read()
print("adapter-probe-deny", file=sys.stderr)
sys.exit(2)
