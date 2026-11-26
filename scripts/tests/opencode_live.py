"""Reads the probe record the live suite produced, for ``live_opencode_probe_record.py``.

A check that depends on a record fails when there is none: a skip would report a green run that
checked nothing. The record is the latest one of the phase that matches the current tree's suite
hash and the pin; ``scripts/opencode-probe.py run --phase <phase> --require-live`` makes it.
"""
import pytest

from opencode_probe import core, record


def latest_record(phase_name):
    """The latest record of the phase for the current tree as ``(path, record)``; fails the
    calling test when there is none."""
    phase = core.get_phase(phase_name)
    found = record.latest_record(core.state_dir(), phase_name, core.read_pin()["version"], core.phase_hash(phase))
    if not found:
        pytest.fail(f"no record of phase {phase_name} matches the current tree under {core.state_dir()}; "
                    f"run `scripts/opencode-probe.py run --phase {phase_name} --require-live`")
    return found
