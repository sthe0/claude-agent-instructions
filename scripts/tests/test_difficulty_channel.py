"""DifficultyChannel port + record schema + registry (ADR-0001 S3 stage 7).

The port is transport-agnostic: these tests exercise the in-memory NullChannel double only,
never external I/O. They pin the record schema (the single join contract), the severity enum,
the submit/pull(since) round-trip, and config-routed registry resolution.
"""
import importlib.util
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parents[1] / "difficulty_channel"
_SPEC = importlib.util.spec_from_file_location(
    "difficulty_channel", _PKG / "__init__.py", submodule_search_locations=[str(_PKG)]
)
dc = importlib.util.module_from_spec(_SPEC)
import sys

sys.modules["difficulty_channel"] = dc
_SPEC.loader.exec_module(dc)


def _rec(ts="2026-06-26T00:00:00", ground="gate denies a legit write", sev=dc.Severity.HIGH):
    return dc.DifficultyRecord(
        ts=ts,
        layer="core",
        target="CLAUDE.md",
        functional_ground=ground,
        severity=sev,
        reporter="agent",
        evidence="quote from session",
    )


def test_record_round_trips_through_in_memory_channel():
    ch = dc.NullChannel()
    handle = ch.submit(_rec())
    assert isinstance(handle, str) and handle
    pulled = ch.pull()
    assert len(pulled) == 1
    assert pulled[0].functional_ground == "gate denies a legit write"
    assert pulled[0].severity is dc.Severity.HIGH


def test_submit_then_pull_since_filters():
    ch = dc.NullChannel()
    ch.submit(_rec(ts="2026-06-01T00:00:00"))
    ch.submit(_rec(ts="2026-06-20T00:00:00"))
    recent = ch.pull(since="2026-06-10T00:00:00")
    assert len(recent) == 1
    assert recent[0].ts == "2026-06-20T00:00:00"
    assert len(ch.pull()) == 2  # None == all


def test_severity_enum_validates_and_carries_mass():
    assert dc.Severity.parse("critical") is dc.Severity.CRITICAL
    # raw string in the record is normalised to the enum
    r = dc.DifficultyRecord(
        ts="t", layer="team", target="x", functional_ground="g",
        severity="medium", reporter="r",
    )
    assert r.severity is dc.Severity.MEDIUM
    assert dc.Severity.CRITICAL.mass > dc.Severity.LOW.mass
    with pytest.raises(ValueError):
        dc.Severity.parse("catastrophic")


def test_empty_functional_ground_rejected():
    with pytest.raises(ValueError):
        dc.DifficultyRecord(
            ts="t", layer="core", target="x", functional_ground="   ",
            severity=dc.Severity.LOW, reporter="r",
        )


def test_registry_resolves_name_to_channel():
    ch = dc.get_channel("null")
    assert isinstance(ch, dc.DifficultyChannel)
    assert isinstance(ch, dc.NullChannel)
    with pytest.raises(KeyError):
        dc.get_channel("does-not-exist")


def test_register_custom_channel():
    dc.register_channel("null2", dc.NullChannel)
    assert isinstance(dc.get_channel("null2"), dc.NullChannel)
