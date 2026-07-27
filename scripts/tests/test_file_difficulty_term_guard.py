"""file-difficulty.py: hard-blocking term-ruleset gate on the record body
before it leaves the machine for a PUBLIC channel (C1 mechanism, sub-item 9).

Mirrors test_file_difficulty.py's module-loading and NullChannel-injection
patterns but stays in its own file (that file is owned by a different plan
stage and must not be touched here). $CLAUDE_TERM_RULESET_DIR replaces
discovery, so these tests never depend on / interfere with a real installed
ruleset. All terms used are synthetic (zorblex).
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

import difficulty_channel as dc

_SCRIPTS = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "file_difficulty_term_guard", _SCRIPTS / "file-difficulty.py"
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
main = _mod.main

FIXED_TS = "2026-06-27T00:00:00+00:00"

DENY_ZORBLEX = """
[[deny]]
pattern = 'zorblex'
label = "codename"
"""


@pytest.fixture(autouse=True)
def _non_author(monkeypatch):
    """Hermetic default: the real is_author() probes `git push --dry-run` over
    the network. The term-guard gate under test only runs after that check,
    so every test here must be on the non-author (report) path."""
    monkeypatch.setattr(_mod.authority, "is_author", lambda: False)


@pytest.fixture
def no_plugin_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_DIFFICULTY_PLUGIN_DIR", str(tmp_path / "no-plugins"))


def _ruleset_dir(tmp_path: Path) -> Path:
    d = tmp_path / "rulesets"
    d.mkdir(exist_ok=True)
    (d / "synthetic.toml").write_text(DENY_ZORBLEX, encoding="utf-8")
    return d


def _point_at_ruleset_dir(monkeypatch, path: Path) -> None:
    monkeypatch.setenv("CLAUDE_TERM_RULESET_DIR", str(path))


def _run(*args, **kw):
    return main(list(args), _ts=FIXED_TS, **kw)


def test_denied_term_in_ground_is_blocked(no_plugin_dir, monkeypatch, tmp_path, capsys):
    _point_at_ruleset_dir(monkeypatch, _ruleset_dir(tmp_path))
    ch = dc.NullChannel()
    dc.register_channel("null-guard-1", lambda: ch)

    rc = _run("--target", "CLAUDE.md", "--ground", "mentions zorblex right here",
              "--channel", "null-guard-1")

    assert rc == 1
    out = capsys.readouterr()
    assert "zorblex" in out.err
    assert ch.pull() == []  # never submitted


def test_denied_term_in_evidence_is_blocked(no_plugin_dir, monkeypatch, tmp_path, capsys):
    _point_at_ruleset_dir(monkeypatch, _ruleset_dir(tmp_path))
    ch = dc.NullChannel()
    dc.register_channel("null-guard-2", lambda: ch)

    rc = _run("--target", "CLAUDE.md", "--ground", "something",
              "--evidence", "log line mentions zorblex", "--channel", "null-guard-2")

    assert rc == 1
    assert ch.pull() == []


def test_clean_body_files_successfully(no_plugin_dir, monkeypatch, tmp_path, capsys):
    _point_at_ruleset_dir(monkeypatch, _ruleset_dir(tmp_path))
    ch = dc.NullChannel()
    dc.register_channel("null-guard-3", lambda: ch)

    rc = _run("--target", "CLAUDE.md", "--ground", "gate wording ambiguous",
              "--channel", "null-guard-3")

    assert rc == 0
    assert "mem-" in capsys.readouterr().out
    assert len(ch.pull()) == 1


def test_zero_rulesets_prints_unchecked_and_still_files(no_plugin_dir, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("CLAUDE_TERM_RULESET_DIR", str(tmp_path / "does-not-exist"))
    ch = dc.NullChannel()
    dc.register_channel("null-guard-4", lambda: ch)

    rc = _run("--target", "CLAUDE.md", "--ground", "mentions zorblex right here",
              "--channel", "null-guard-4")

    assert rc == 0
    out = capsys.readouterr().out
    assert "UNCHECKED: no term ruleset installed" in out
    assert len(ch.pull()) == 1
