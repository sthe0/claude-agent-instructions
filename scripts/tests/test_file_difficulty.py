"""Tests for the file-difficulty.py CLI (ADR-0001 S3 stage 5).

Covers: dry-run output, null-channel submit, bad-severity rejection, --functional-ground alias,
default severity, --channel override.
"""
from __future__ import annotations

import importlib.util
import sys
from io import StringIO
from pathlib import Path

import pytest

import difficulty_channel as dc

# Load file-difficulty.py by path (hyphenated module name is not importable directly).
_SCRIPTS = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "file_difficulty", _SCRIPTS / "file-difficulty.py"
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
main = _mod.main

FIXED_TS = "2026-06-27T00:00:00+00:00"


def _run(*args, **kw):
    """Run main() with FIXED_TS so timestamps are deterministic in output assertions."""
    return main(list(args), _ts=FIXED_TS, **kw)


# ── dry-run ───────────────────────────────────────────────────────────────────

def test_dry_run_prints_record_and_exits_0(capsys):
    rc = _run("--target", "CLAUDE.md", "--ground", "gate wording ambiguous",
              "--severity", "high", "--dry-run")
    assert rc == 0
    out = capsys.readouterr().out
    assert "DifficultyRecord:" in out
    assert "gate wording ambiguous" in out
    assert "CLAUDE.md" in out
    assert "high" in out


def test_dry_run_functional_ground_alias(capsys):
    rc = _run("--target", "README.md", "--functional-ground", "readme too long",
              "--dry-run")
    assert rc == 0
    out = capsys.readouterr().out
    assert "readme too long" in out


def test_dry_run_default_severity_is_medium(capsys):
    rc = _run("--target", "CLAUDE.md", "--ground", "ambiguous gate", "--dry-run")
    assert rc == 0
    assert "medium" in capsys.readouterr().out


def test_dry_run_default_layer_is_core(capsys):
    rc = _run("--target", "CLAUDE.md", "--ground", "something", "--dry-run")
    assert rc == 0
    assert "core" in capsys.readouterr().out


def test_dry_run_includes_evidence_when_provided(capsys):
    rc = _run("--target", "CLAUDE.md", "--ground", "something", "--evidence",
              "log line xyz", "--dry-run")
    assert rc == 0
    assert "log line xyz" in capsys.readouterr().out


def test_dry_run_omits_evidence_field_when_empty(capsys):
    rc = _run("--target", "CLAUDE.md", "--ground", "something", "--dry-run")
    assert rc == 0
    assert "evidence" not in capsys.readouterr().out


# ── submit via null channel ───────────────────────────────────────────────────

def test_submit_via_null_channel_returns_handle(capsys):
    dc.register_channel("null-test", dc.NullChannel)
    rc = _run("--target", "CLAUDE.md", "--ground", "gate wording ambiguous",
              "--severity", "high", "--channel", "null-test")
    assert rc == 0
    # NullChannel returns mem-<n>; the handle is printed to stdout
    assert "mem-" in capsys.readouterr().out


def test_submit_via_null_channel_record_survives_round_trip():
    ch = dc.NullChannel()
    dc.register_channel("null-rt", lambda: ch)
    _run("--target", "docs/x.md", "--ground", "missing example",
         "--severity", "low", "--reporter", "testbot", "--channel", "null-rt")
    recs = ch.pull()
    assert len(recs) == 1
    r = recs[0]
    assert r.target == "docs/x.md"
    assert r.functional_ground == "missing example"
    assert r.severity is dc.Severity.LOW
    assert r.reporter == "testbot"
    assert r.layer == "core"


# ── bad severity ──────────────────────────────────────────────────────────────

def test_bad_severity_exits_nonzero(capsys):
    with pytest.raises(SystemExit) as exc:
        # argparse choices validation fires before main() logic
        main(["--target", "x", "--ground", "y", "--severity", "extreme"])
    assert exc.value.code != 0


# ── missing required args ─────────────────────────────────────────────────────

def test_missing_target_exits_nonzero():
    with pytest.raises(SystemExit) as exc:
        main(["--ground", "something"])
    assert exc.value.code != 0


def test_missing_ground_exits_nonzero():
    with pytest.raises(SystemExit) as exc:
        main(["--target", "CLAUDE.md"])
    assert exc.value.code != 0
