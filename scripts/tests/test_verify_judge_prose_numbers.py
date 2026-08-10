"""Tests for verify-judge-prose-numbers.py — the allowlist that keeps a judge
duration written in prose from drifting away from the calibration it describes.

The synthetic cases below build their own domain + allowlist in tmp_path, so they
test the mechanism rather than the current repo's content; the last test runs the
real check over the real domain, which is what actually gates a commit.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "verify_judge_prose_numbers", SCRIPTS_DIR / "verify-judge-prose-numbers.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load()

_GOVERNED = "# the judge finishes inside 41s on the sample\n"


@pytest.fixture
def repo(tmp_path):
    """A miniature governed repo: one shell-style prose file with one number."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "hook.sh").write_text(
        "#!/bin/sh\n" + _GOVERNED + "TIMEOUT=41\n", encoding="utf-8"
    )
    domain = tmp_path / "domain.txt"
    domain.write_text("# comment\nscripts/hook.sh\n", encoding="utf-8")
    allow = tmp_path / "allow.txt"
    return {"root": tmp_path, "domain": domain, "allow": allow}


def _entry(repo, line=2, anchor=None, reason="# derived: last_resort_ceiling_s() = 41"):
    text = (repo["root"] / "scripts" / "hook.sh").read_text(encoding="utf-8").splitlines()[1]
    return f"scripts/hook.sh:{line}:{anchor or _mod.anchor_of(text)}  {reason}\n"


def _scan(repo) -> int:
    return _mod.scan(repo["root"], repo["allow"], repo["domain"])


def test_a_governed_number_with_a_pinned_reason_passes(repo):
    repo["allow"].write_text(_entry(repo), encoding="utf-8")
    assert _scan(repo) == 0


def test_a_governed_number_nobody_allowlisted_fails(repo, capsys):
    repo["allow"].write_text("# nothing here\n", encoding="utf-8")
    assert _scan(repo) == 1
    assert "unallowed: scripts/hook.sh:2" in capsys.readouterr().out


def test_an_entry_without_a_reason_is_rejected(repo, capsys):
    repo["allow"].write_text(_entry(repo, reason="").replace("  \n", "\n"), encoding="utf-8")
    assert _scan(repo) == 1
    assert "without a reason" in capsys.readouterr().out


def test_a_rewritten_sentence_goes_stale_even_at_the_same_line(repo, capsys):
    """The point of the anchor: the line still carries a number, but a DIFFERENT
    claim, so the reason on file was written about text that no longer exists."""
    repo["allow"].write_text(_entry(repo), encoding="utf-8")
    hook = repo["root"] / "scripts" / "hook.sh"
    hook.write_text(hook.read_text(encoding="utf-8").replace("41s", "20s"), encoding="utf-8")
    assert _scan(repo) == 1
    assert "stale entry" in capsys.readouterr().out


def test_a_moved_sentence_stays_covered_and_the_move_is_reported(repo, capsys):
    repo["allow"].write_text(_entry(repo), encoding="utf-8")
    hook = repo["root"] / "scripts" / "hook.sh"
    hook.write_text("# an inserted line\n" + hook.read_text(encoding="utf-8"), encoding="utf-8")
    assert _scan(repo) == 0
    assert "relocated scripts/hook.sh:2 -> :3" in capsys.readouterr().out


def test_two_entries_cannot_share_one_occurrence(repo, capsys):
    repo["allow"].write_text(_entry(repo) + _entry(repo), encoding="utf-8")
    assert _scan(repo) == 1
    assert "covered by several entries" in capsys.readouterr().out


def test_repin_updates_the_anchor_and_keeps_the_reason_verbatim(repo):
    reason = "# derived: last_resort_ceiling_s() = 41 — не трогать при repin"
    repo["allow"].write_text(_entry(repo, reason=reason), encoding="utf-8")
    hook = repo["root"] / "scripts" / "hook.sh"
    hook.write_text("# an inserted line\n" + hook.read_text(encoding="utf-8"), encoding="utf-8")
    assert _mod.repin(repo["root"], repo["allow"], repo["domain"]) == 0
    rewritten = repo["allow"].read_text(encoding="utf-8")
    assert rewritten.startswith("scripts/hook.sh:3:")
    assert reason in rewritten
    assert _scan(repo) == 0


def test_repin_refuses_to_guess_at_a_stale_entry(repo, capsys):
    repo["allow"].write_text(_entry(repo, anchor="deadbeef"), encoding="utf-8")
    assert _mod.repin(repo["root"], repo["allow"], repo["domain"]) == 0
    assert "left alone (stale" in capsys.readouterr().out
    assert "deadbeef" in repo["allow"].read_text(encoding="utf-8")


def test_python_prose_is_read_structurally_not_by_a_comment_prefix(tmp_path):
    """A duration inside a docstring is prose; the same digits in executable code
    are the constant itself, governed by the calibration tests instead."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "m.py").write_text(
        '"""A judge budget of 45s covers one call."""\n_BUDGET_S = 45\nx = 41\n',
        encoding="utf-8",
    )
    domain = tmp_path / "domain.txt"
    domain.write_text("scripts/m.py\n", encoding="utf-8")
    found = _mod.find_occurrences(tmp_path, domain)
    assert [(rel, ln) for rel, ln, _t in found] == [("scripts/m.py", 1)]


def test_a_domain_entry_pointing_nowhere_fails_loudly(repo, capsys):
    repo["domain"].write_text("scripts/gone.sh\n", encoding="utf-8")
    repo["allow"].write_text("# empty\n", encoding="utf-8")
    assert _scan(repo) == 1
    assert "domain names a missing file" in capsys.readouterr().out


def test_the_real_domain_is_fully_allowlisted():
    """The live gate. A failure here prints every unallowed occurrence with the
    spec to paste into the allowlist — read the sentence, then write the reason."""
    assert _mod.scan() == 0
