"""Tests for scripts/escape-hatch-report.py.

Two halves, mirroring the script's own split.

The USAGE half is asserted STRUCTURALLY, against a fixture state dir — never
against the live population. A test that asserted "the report prints the known
N overrides" would be self-invalidating: this very session writes delivery
stamps into the real state dir while it runs, so N is different by the time the
assertion executes, and the only way to keep such a test green is to keep
weakening it.

The INVENTORY half is checked in both directions, the shape lib/hook_wiring.py's
gate-bearing registry uses: every declared entry is a live parser argument, and
every hatch-shaped parser argument is either declared or explicitly exempted
with a reason. The forward direction alone would let a new `--force` flag exist
uncounted, which is the exact failure the report exists to prevent.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

from agentctl import cli, delivery  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location(
        "escape_hatch_report", SCRIPTS / "escape-hatch-report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


report = _load()


def _write_stamp(state_dir: Path, sid: str, **kw) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    base = {
        "plan_path": "/p.toml", "plan_sha256": "a", "rendering_sha256": "b",
        "verified_ts": 1.0, "source": delivery.SOURCE_HOOK,
    }
    base.update(kw)
    (state_dir / f"{sid}.delivery.json").write_text(
        json.dumps(base), encoding="utf-8")


# --- usage: measured from a fixture population, never the live one -------------

def test_counts_group_by_source_and_reason(tmp_path):
    d = tmp_path / "state"
    _write_stamp(d, "s1", source=delivery.SOURCE_HOOK)
    _write_stamp(d, "s2", source=delivery.SOURCE_OVERRIDE,
                 escape_reason=delivery.ESCAPE_HOOK_NOT_INSTALLED)
    _write_stamp(d, "s3", source=delivery.SOURCE_OVERRIDE,
                 escape_reason=delivery.ESCAPE_HOOK_NOT_INSTALLED)
    _write_stamp(d, "s4", source=delivery.SOURCE_OVERRIDE,
                 escape_reason=delivery.ESCAPE_OTHER)
    stamps, unreadable = report.load_stamps(report.stamp_files([d]))
    assert unreadable == []
    assert report.tally(stamps) == {
        delivery.SOURCE_HOOK: {"": 1},
        delivery.SOURCE_OVERRIDE: {
            delivery.ESCAPE_HOOK_NOT_INSTALLED: 2, delivery.ESCAPE_OTHER: 1},
    }


def test_reasonless_override_is_counted_not_hidden(tmp_path):
    """The stamps that predate the field are the population that motivated it.
    Dropping them would make the report flatter the very ratio it measures."""
    d = tmp_path / "state"
    _write_stamp(d, "old", source=delivery.SOURCE_OVERRIDE, by="fedor", note="n")
    stamps, _ = report.load_stamps(report.stamp_files([d]))
    assert report.tally(stamps) == {delivery.SOURCE_OVERRIDE: {"": 1}}
    text = "\n".join(report.format_usage(report.tally(stamps), 1, []))
    assert "predates --escape-reason" in text


def test_empty_reason_reads_differently_for_hook_and_override(tmp_path):
    """An absent reason means "not an escape" on a hook stamp and "written
    before the field existed" on an override; one label for both would invent a
    backlog of missing data that is not missing."""
    d = tmp_path / "state"
    _write_stamp(d, "h", source=delivery.SOURCE_HOOK)
    stamps, _ = report.load_stamps(report.stamp_files([d]))
    text = "\n".join(report.format_usage(report.tally(stamps), 1, []))
    assert "not an escape" in text
    assert "predates --escape-reason" not in text


def test_unreadable_sidecar_is_reported_not_dropped(tmp_path):
    d = tmp_path / "state"
    _write_stamp(d, "ok", source=delivery.SOURCE_HOOK)
    d.mkdir(parents=True, exist_ok=True)
    (d / "broken.delivery.json").write_text("{not json", encoding="utf-8")
    stamps, unreadable = report.load_stamps(report.stamp_files([d]))
    assert len(stamps) == 1 and [p.name for p in unreadable] == ["broken.delivery.json"]
    text = "\n".join(report.format_usage(report.tally(stamps), 1, unreadable))
    assert "UNREADABLE" in text


def test_missing_state_dir_is_silence_not_an_error(tmp_path):
    assert report.stamp_files([tmp_path / "never-existed"]) == []


def test_main_prints_a_total_and_the_inventory(tmp_path, capsys):
    d = tmp_path / "state"
    _write_stamp(d, "s1", source=delivery.SOURCE_OVERRIDE,
                 escape_reason=delivery.ESCAPE_DELIVERED_OUT_OF_BAND)
    assert report.main(["--state-dir", str(d)]) == 0
    out = capsys.readouterr().out
    assert "delivery stamps: 1" in out
    assert delivery.ESCAPE_DELIVERED_OUT_OF_BAND in out
    assert "escape hatches in the engine:" in out


def test_main_on_an_empty_population_still_reports(tmp_path, capsys):
    assert report.main(["--state-dir", str(tmp_path / "none")]) == 0
    out = capsys.readouterr().out
    assert "delivery stamps: 0" in out
    assert "escape hatches in the engine:" in out


# --- inventory: exhaustive in both directions ----------------------------------

# High-recall net for a hatch-shaped argument. Deliberately name-based and
# deliberately NOT the source of the inventory: it is the tripwire, narrowed by
# the exemption list, so over-matching costs a review and under-matching costs a
# silently uncounted escape.
_HATCH_NAME_RE = re.compile(r"force|waiver|override|escape")

# Hatch-shaped arguments that are not escape hatches, each with its reason.
_NOT_A_HATCH: "dict[tuple[str, str], str]" = {}


def _declared() -> set:
    return {(command, dest) for command, dest, _, _ in report._ESCAPE_HATCH_ARGS}


def _parser_arguments() -> "dict[tuple[str, str], argparse.Action]":
    parser = cli.build_parser()
    found: "dict[tuple[str, str], argparse.Action]" = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                for a in sub._actions:
                    found[(name, a.dest)] = a
        else:
            found[(cli._ROOT, action.dest)] = action
    return found


def test_every_declared_hatch_is_a_live_parser_argument():
    """A declaration that outlives its flag is folklore: it keeps asserting
    coverage of a hatch nobody can take."""
    stale = sorted(_declared() - set(_parser_arguments()))
    assert not stale, f"declared in _ESCAPE_HATCH_ARGS but not declared by the parser: {stale}"


def test_every_hatch_shaped_argument_is_declared_or_exempted():
    """The reverse direction — the one that catches the NEXT hatch."""
    shaped = {
        key for key, action in _parser_arguments().items()
        if _HATCH_NAME_RE.search(key[1])
        or any(_HATCH_NAME_RE.search(o) for o in action.option_strings)
    }
    missing = sorted(shaped - _declared() - set(_NOT_A_HATCH))
    assert not missing, (
        "hatch-shaped parser arguments absent from _ESCAPE_HATCH_ARGS in "
        f"escape-hatch-report.py (declare them, or exempt with a reason): {missing}")
    stale_exemptions = sorted(set(_NOT_A_HATCH) - shaped)
    assert not stale_exemptions, (
        f"exemptions for arguments that no longer match the net: {stale_exemptions}")


def test_every_declared_hatch_states_what_it_records():
    for command, dest, past, recorded in report._ESCAPE_HATCH_ARGS:
        assert past.strip(), f"{command} {dest}: no statement of what it gets past"
        assert recorded.strip(), f"{command} {dest}: no statement of what is recorded"


def test_the_delivery_hatch_is_the_typed_one():
    """The one hatch this task typed. Pinned so a later change cannot quietly
    downgrade it to the free-text tier the others sit in."""
    entry = [e for e in report._ESCAPE_HATCH_ARGS if e[0] == "confirm-delivery"]
    assert len(entry) == 1
    assert entry[0][3].startswith("typed:")
    for reason in delivery.DELIVERY_ESCAPE_REASONS:
        assert reason in entry[0][3]
