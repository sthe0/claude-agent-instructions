"""Checks of the live mechanisms record; not part of the default run.

The file name carries no ``test_`` prefix, so ``pytest scripts/tests`` does not collect it; run it
by path after a live pass: ``python3 -m pytest -q -p no:xdist scripts/tests/live_opencode_probe_record.py``.
Each test fails when the current tree has no record, it never skips.
"""
import opencode_live
from opencode_probe import core, record


def test_live_record_passes_the_structural_check():
    path, rec = opencode_live.latest_record("mechanisms")
    assert record.structure_problems(core.get_phase("mechanisms"), path, rec, None) == []


def test_pinned_record_is_acceptable():
    phase = core.get_phase("mechanisms")
    opencode_live.latest_record("mechanisms")
    assert record.check_phase(core.state_dir(), phase, require_pass=True, case_ids=None) == []
