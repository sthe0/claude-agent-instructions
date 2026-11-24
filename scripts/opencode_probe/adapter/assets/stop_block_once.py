#!/usr/bin/env python3
"""Stand-in Stop row of the adapter probe: blocks the first turn end, then lets later ones through.

The marker file named by ``ADAPTER_STOP_MARK`` records that the block was issued.
"""
import json
import os
import sys

sys.stdin.read()
mark = os.environ.get("ADAPTER_STOP_MARK", "")
if mark and not os.path.exists(mark):
    with open(mark, "w", encoding="utf-8") as handle:
        handle.write("blocked\n")
    print(json.dumps({"decision": "block", "reason": os.environ.get("ADAPTER_STOP_REASON", "adapter-stop-hold")}))
sys.exit(0)
