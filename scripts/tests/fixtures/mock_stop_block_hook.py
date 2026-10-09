#!/usr/bin/env python3
"""Test fixture: emulate a Claude Stop hook that blocks the turn."""
import json
import sys

print(json.dumps({"decision": "block", "reason": "mock stop obligation unmet"}))
