#!/usr/bin/env python3
"""Test fixture: child hook that prints non-JSON stdout."""
import sys

sys.stdout.write("not-json-output\n")
