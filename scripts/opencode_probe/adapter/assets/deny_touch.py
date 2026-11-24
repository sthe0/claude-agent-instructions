#!/usr/bin/env python3
"""A stand-in denying gate that refuses only a Bash command starting with ``touch ``: the nested
process's call, while the outer shell call that launches the nested process passes."""
import json
import sys

command = (json.load(sys.stdin).get("tool_input") or {}).get("command") or ""
if command.startswith("touch "):
    print("adapter-probe-deny", file=sys.stderr)
    sys.exit(2)
