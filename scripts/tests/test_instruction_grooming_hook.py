"""Tests for hook-instruction-grooming-due.py — WARN-triggered grooming OFFER.

Hermetic: builds a throwaway "repo" (a copy of the real lint-prose-length.py
under scripts/ + a fixture config.md + governed files, mirroring the fixture
shape in test_lint_prose_length.py) and points CLAUDE_INSTRUCTIONS_REPO at
it, so the linter subprocess the hook shells out to resolves its own
REPO_ROOT to the fixture, never the real repo.

Covers: fires-on-WARN (offer names file + pct, instructs an AskUserQuestion
OFFER), silent-when-clean (no WARN lines), and per-file debounce (a second
run within the window is silent; a file due again after the window fires).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = SCRIPTS_DIR / "hook-instruction-grooming-due.py"
LINTER_SCRIPT = SCRIPTS_DIR / "lint-prose-length.py"

_CONFIG_TEMPLATE = """\
| Key | Value | Meaning |
|---|---|---|
| `claude-md-max-lines` | `100` | . |
| `claude-md-max-bytes` | `1000` | . |
| `readme-max-lines` | `50` | . |
| `cursor-mirror-max-lines` | `50` | . |
| `skill-md-max-lines` | `50` | . |
| `policy-md-max-lines` | `50` | . |
"""


def _make_fake_repo(tmp_path: Path, readme_lines: int) -> Path:
    """Fixture repo with its own copy of the real linter, so the hook's
    subprocess call resolves REPO_ROOT to this tree, not the real one."""
    repo = tmp_path / "fake-repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(LINTER_SCRIPT, repo / "scripts" / "lint-prose-length.py")
    (repo / "config.md").write_text(_CONFIG_TEMPLATE, encoding="utf-8")
    (repo / "README.md").write_text("x\n" * readme_lines, encoding="utf-8")
    (repo / "CLAUDE.md").write_text("x\n", encoding="utf-8")
    (repo / "cursor" / "rules").mkdir(parents=True)
    (repo / "cursor" / "rules" / "claude-code-sync.mdc").write_text("m\n", encoding="utf-8")
    return repo


def run_hook(home: Path, repo: Path):
    env = {**os.environ, "HOME": str(home), "CLAUDE_INSTRUCTIONS_REPO": str(repo)}
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input="{}",
        env=env,
        capture_output=True,
        text=True,
    )


def stamp_path(home: Path) -> Path:
    return home / ".local" / "state" / "claude-instruction-grooming.stamp.json"


# ---------------------------------------------------------------------------
# fires-on-WARN
# ---------------------------------------------------------------------------


def test_warn_file_emits_offer(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo = _make_fake_repo(tmp_path, readme_lines=46)  # 92% of 50-line ceiling

    proc = run_hook(home, repo)

    assert proc.returncode == 0
    assert "[instruction-grooming]" in proc.stdout
    assert "README.md" in proc.stdout
    assert "92%" in proc.stdout
    assert "instruction-grooming" in proc.stdout
    assert "AskUserQuestion" in proc.stdout


# ---------------------------------------------------------------------------
# silent-when-clean
# ---------------------------------------------------------------------------


def test_clean_tree_silent(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo = _make_fake_repo(tmp_path, readme_lines=10)  # 20% of ceiling — no WARN

    proc = run_hook(home, repo)

    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_missing_repo_silent(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    missing = tmp_path / "does-not-exist"

    proc = run_hook(home, missing)

    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# ---------------------------------------------------------------------------
# per-file debounce
# ---------------------------------------------------------------------------


def test_second_run_within_window_silent(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo = _make_fake_repo(tmp_path, readme_lines=46)

    first = run_hook(home, repo)
    assert "[instruction-grooming]" in first.stdout
    assert stamp_path(home).exists()

    second = run_hook(home, repo)
    assert second.returncode == 0
    assert second.stdout.strip() == ""


def test_due_again_after_window_fires(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo = _make_fake_repo(tmp_path, readme_lines=46)

    stamp = stamp_path(home)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stale = dt.datetime.now() - dt.timedelta(days=8)
    stamp.write_text(json.dumps({"README.md": stale.isoformat()}), encoding="utf-8")

    proc = run_hook(home, repo)

    assert proc.returncode == 0
    assert "[instruction-grooming]" in proc.stdout
    assert "README.md" in proc.stdout


def test_only_newly_due_file_named(tmp_path):
    """A file offered recently stays out of the message; a fresh WARN on a
    second file is named — debounce is per-file, not a whole-hook throttle."""
    home = tmp_path / "home"
    home.mkdir()
    repo = _make_fake_repo(tmp_path, readme_lines=46)

    stamp = stamp_path(home)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    recent = dt.datetime.now()
    stamp.write_text(json.dumps({"README.md": recent.isoformat()}), encoding="utf-8")

    # CLAUDE.md now also crosses WARN — 92% of its 1000-byte ceiling.
    (repo / "CLAUDE.md").write_text("x" * 920, encoding="utf-8")

    proc = run_hook(home, repo)

    assert proc.returncode == 0
    assert "CLAUDE.md" in proc.stdout
    assert "README.md" not in proc.stdout
