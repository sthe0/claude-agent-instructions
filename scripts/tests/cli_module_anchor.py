"""Locate the agentctl cli module that holds a mutation anchor.

Difficulty removed: the mutation catalogues name `agentctl/cli.py` as the file holding a
production line, but cli.py is split into cli_<group>.py modules and a line lives in
whichever module its function was moved to. A catalogue entry keeps naming cli.py and this
resolves the module at apply time; an anchor held by no module or by several falls back to
the named path so the catalogue's own exactly-once check reports it.
"""
from __future__ import annotations

from pathlib import Path


def anchor_target(subject_scripts: Path, relpath: str, anchor: str) -> Path:
    named = subject_scripts / relpath
    if named.name != "cli.py":
        return named
    holders = [p for p in sorted(named.parent.glob("cli*.py")) if anchor in p.read_text(encoding="utf-8")]
    return holders[0] if len(holders) == 1 else named
