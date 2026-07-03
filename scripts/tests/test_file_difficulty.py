"""Tests for the file-difficulty.py CLI (ADR-0001 S3 stage 5).

Covers: dry-run output, null-channel submit, bad-severity rejection, --functional-ground alias,
default severity, --channel override, --queue/--stream routing.
"""
from __future__ import annotations

import importlib.util
import json
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


@pytest.fixture(autouse=True)
def _non_author(monkeypatch):
    """Hermetic default: the real is_author() probes `git push --dry-run` over
    the network (and its answer depends on the machine). Tests that exercise
    the author path override with their own monkeypatch."""
    monkeypatch.setattr(_mod.authority, "is_author", lambda: False)


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

def test_submit_via_null_channel_returns_handle(monkeypatch, capsys):
    monkeypatch.setattr(_mod.authority, "is_author", lambda: False)
    dc.register_channel("null-test", dc.NullChannel)
    rc = _run("--target", "CLAUDE.md", "--ground", "gate wording ambiguous",
              "--severity", "high", "--channel", "null-test")
    assert rc == 0
    # NullChannel returns mem-<n>; the handle is printed to stdout
    assert "mem-" in capsys.readouterr().out


def test_submit_via_null_channel_record_survives_round_trip(monkeypatch):
    monkeypatch.setattr(_mod.authority, "is_author", lambda: False)
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


# ── routing: --queue / --stream dry-run output ───────────────────────────────

def test_dry_run_queue_override_shows_queue(capsys):
    rc = _run("--target", "CLAUDE.md", "--ground", "x",
              "--channel", "startrek", "--queue", "OOSEVEN", "--dry-run")
    assert rc == 0
    out = capsys.readouterr().out
    assert "queue: OOSEVEN" in out


def test_dry_run_startrek_default_shows_oosevenreport(capsys):
    rc = _run("--target", "CLAUDE.md", "--ground", "x",
              "--channel", "startrek", "--dry-run")
    assert rc == 0
    out = capsys.readouterr().out
    assert "queue: OOSEVENREPORT" in out


def test_dry_run_startrek_backlog_stream_shows_ooseven(capsys):
    rc = _run("--target", "CLAUDE.md", "--ground", "x",
              "--channel", "startrek", "--stream", "backlog", "--dry-run")
    assert rc == 0
    out = capsys.readouterr().out
    assert "queue: OOSEVEN" in out


def test_dry_run_github_default_shows_difficulty_label(capsys):
    rc = _run("--target", "CLAUDE.md", "--ground", "x",
              "--channel", "github", "--dry-run")
    assert rc == 0
    out = capsys.readouterr().out
    assert "label: difficulty" in out


def test_dry_run_github_backlog_stream_shows_backlog_label(capsys):
    rc = _run("--target", "CLAUDE.md", "--ground", "x",
              "--channel", "github", "--stream", "backlog", "--dry-run")
    assert rc == 0
    out = capsys.readouterr().out
    assert "label: backlog" in out
    assert "difficulty" not in out.split("label:")[1]


def test_dry_run_project_target_resolves_queue(tmp_path, capsys):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "agent-project.json").write_text(
        json.dumps({"instruction_queue": "DEEPAGENT"}), encoding="utf-8"
    )
    target_file = tmp_path / "CLAUDE.md"
    target_file.write_text("# test", encoding="utf-8")
    rc = _run("--target", str(target_file), "--ground", "x",
              "--channel", "startrek", "--dry-run")
    assert rc == 0
    out = capsys.readouterr().out
    assert "queue: DEEPAGENT" in out


# ── authority gate (author machine refuses the non-author route) ────────────

def test_author_machine_refuses_without_force_report(monkeypatch, capsys):
    monkeypatch.setattr(_mod.authority, "is_author", lambda: True)
    dc.register_channel("null-authority-1", dc.NullChannel)
    rc = _run("--target", "CLAUDE.md", "--ground", "x", "--channel", "null-authority-1")
    assert rc != 0
    assert "force-report" in capsys.readouterr().err


def test_author_machine_force_report_proceeds(monkeypatch, capsys):
    monkeypatch.setattr(_mod.authority, "is_author", lambda: True)
    ch = dc.NullChannel()
    dc.register_channel("null-authority-2", lambda: ch)
    rc = _run("--target", "CLAUDE.md", "--ground", "x", "--channel", "null-authority-2",
              "--force-report")
    assert rc == 0
    assert len(ch.pull()) == 1


def test_non_author_machine_proceeds_without_force_report(monkeypatch, capsys):
    monkeypatch.setattr(_mod.authority, "is_author", lambda: False)
    ch = dc.NullChannel()
    dc.register_channel("null-authority-3", lambda: ch)
    rc = _run("--target", "CLAUDE.md", "--ground", "x", "--channel", "null-authority-3")
    assert rc == 0
    assert len(ch.pull()) == 1


# ── fix-first guard (author + core-tier startrek filing refused) ─────────────

def test_author_startrek_core_target_refused_fix_first(monkeypatch, capsys):
    monkeypatch.setattr(_mod.authority, "is_author", lambda: True)
    rc = _run("--target", "CLAUDE.md", "--ground", "x",
              "--channel", "startrek", "--dry-run")
    assert rc == 2
    assert "fix-first" in capsys.readouterr().err


def test_author_startrek_explicit_queue_bypasses_guard(monkeypatch, capsys):
    monkeypatch.setattr(_mod.authority, "is_author", lambda: True)
    rc = _run("--target", "CLAUDE.md", "--ground", "x",
              "--channel", "startrek", "--queue", "OOSEVEN", "--dry-run")
    assert rc == 0
    assert "queue: OOSEVEN" in capsys.readouterr().out


def test_author_startrek_force_report_bypasses_guard(monkeypatch, capsys):
    monkeypatch.setattr(_mod.authority, "is_author", lambda: True)
    rc = _run("--target", "CLAUDE.md", "--ground", "x",
              "--channel", "startrek", "--force-report", "--dry-run")
    assert rc == 0
    assert "queue: OOSEVENREPORT" in capsys.readouterr().out


def test_author_startrek_project_target_bypasses_guard(monkeypatch, tmp_path, capsys):
    # a project-queue resolution means the filing is project-tier work, not a
    # deferred Core edit — the guard must not fire
    monkeypatch.setattr(_mod.authority, "is_author", lambda: True)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "agent-project.json").write_text(
        json.dumps({"instruction_queue": "DEEPAGENT"}), encoding="utf-8"
    )
    target_file = tmp_path / "CLAUDE.md"
    target_file.write_text("# test", encoding="utf-8")
    rc = _run("--target", str(target_file), "--ground", "x",
              "--channel", "startrek", "--dry-run")
    assert rc == 0
    assert "queue: DEEPAGENT" in capsys.readouterr().out


def test_dry_run_explicit_queue_overrides_project_field(tmp_path, capsys):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "agent-project.json").write_text(
        json.dumps({"instruction_queue": "DEEPAGENT"}), encoding="utf-8"
    )
    target_file = tmp_path / "CLAUDE.md"
    target_file.write_text("# test", encoding="utf-8")
    rc = _run("--target", str(target_file), "--ground", "x",
              "--channel", "startrek", "--queue", "MYQUEUE", "--dry-run")
    assert rc == 0
    out = capsys.readouterr().out
    assert "queue: MYQUEUE" in out
