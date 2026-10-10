"""The mutation catalogue of scripts/claude-recover.py, run from the suite.

A catalogue that lives outside pytest drifts silently: an anchor that no longer matches makes
its control a no-op. The anchor and coverage checks are cheap and always run; the kill check
runs every mutant through its own inner pytest (about 25 s each), so it is opt-in:

    CLAUDE_RECOVER_MUTANTS=1 scripts/cap-run.sh -- python3 -m pytest -q \
        scripts/tests/test_claude_recover_mutants.py

The inner runs are one process each (`-p no:xdist`).
"""
from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _load_catalogue():
    spec = importlib.util.spec_from_file_location("claude_recover_mutants", HERE / "claude_recover_mutants.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CATALOGUE = _load_catalogue()
SOURCE = CATALOGUE.TARGET.read_text()
KILL_CHECK_ON = os.environ.get("CLAUDE_RECOVER_MUTANTS") == "1"


@pytest.mark.parametrize("name", sorted(CATALOGUE.MUTANTS))
def test_anchor_present(name):
    anchor = CATALOGUE.MUTANTS[name][1]
    assert SOURCE.count(anchor) == 1, f"{name}: anchor occurs {SOURCE.count(anchor)} times"


def test_exempt_fragments_unique():
    for fragment, why in CATALOGUE.DRY_RUN_GUARD_EXEMPT.items():
        assert why.strip()
        assert SOURCE.count(fragment) == 1, f"{fragment!r} occurs {SOURCE.count(fragment)} times"


def _covered_spans() -> list[tuple[int, int]]:
    spans = []
    for fragment in [*(a for _w, a, _r in CATALOGUE.MUTANTS.values()), *CATALOGUE.DRY_RUN_GUARD_EXEMPT]:
        start = SOURCE.find(fragment)
        if start >= 0:
            spans.append((start, start + len(fragment)))
    return spans


def test_dry_run_sites_covered():
    spans = _covered_spans()
    uncovered = []
    for m in re.finditer(r"\bdry_run\b", SOURCE):
        if not any(start <= m.start() and m.end() <= end for start, end in spans):
            line = SOURCE.count("\n", 0, m.start()) + 1
            uncovered.append(f"line {line}: {SOURCE.splitlines()[line - 1].strip()}")
    assert not uncovered, "dry_run occurrences with neither a mutant nor an exemption:\n" + "\n".join(uncovered)


@pytest.mark.skipif(not KILL_CHECK_ON, reason="set CLAUDE_RECOVER_MUTANTS=1 (about 25 s per mutant)")
@pytest.mark.parametrize("name", sorted(CATALOGUE.MUTANTS))
def test_mutant_killed(name):
    assert CATALOGUE.run_mutant(name) == CATALOGUE.KILLED
