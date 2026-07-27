"""verify-terms.py: the CLI check that enumerates a tracked tree and enforces
every discovered term ruleset (C1 mechanism).

Runs the script as a real subprocess against a synthetic temp git repo (via
--repo-root, added specifically so this script is testable without touching
the real Core checkout) with $CLAUDE_TERM_RULESET_DIR pointed at a synthetic
ruleset — never a real org term, and never the real machine's ruleset dir.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "verify-terms.py"

DENY_ZORBLEX = """
[[deny]]
pattern = 'zorblex'
label = "codename"
"""

DENY_ZORBLEX_WITH_GRANDFATHER = """
[[deny]]
pattern = 'zorblex'

[[grandfather]]
path = "legacy/*"
reason = "TODO(owner): migrate off zorblex by next quarter"
"""


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)


def _commit_all(path: Path) -> None:
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "snapshot", "--allow-empty"],
        cwd=path, check=True, capture_output=True,
    )


def _write(path: Path, relpath: str, content: str) -> None:
    p = path / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def run(repo_root: Path, ruleset_dir: Path | None, *extra_args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if ruleset_dir is not None:
        env["CLAUDE_TERM_RULESET_DIR"] = str(ruleset_dir)
    else:
        env["CLAUDE_TERM_RULESET_DIR"] = str(repo_root / "does-not-exist")  # force zero rulesets
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo-root", str(repo_root), *extra_args],
        capture_output=True, text=True, env=env,
    )


def _ruleset_dir(tmp_path: Path, content: str = DENY_ZORBLEX) -> Path:
    d = tmp_path / "rulesets"
    d.mkdir(exist_ok=True)
    (d / "synthetic.toml").write_text(content, encoding="utf-8")
    return d


def test_zero_rulesets_is_a_reported_noop(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "clean.md", "nothing to see here\n")
    _commit_all(repo)

    r = run(repo, None)
    assert r.returncode == 0
    assert "rulesets discovered: 0" in r.stdout
    assert "no-op" in r.stdout


def test_content_hit_fails(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "doc.md", "mentions zorblex right here\n")
    _commit_all(repo)

    r = run(repo, _ruleset_dir(tmp_path))
    assert r.returncode == 1
    assert "FAIL" in r.stdout


def test_path_hit_fails_even_with_clean_content(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "zorblex-notes.md", "perfectly clean content\n")
    _commit_all(repo)

    r = run(repo, _ruleset_dir(tmp_path))
    assert r.returncode == 1
    assert "(path)" in r.stdout


def test_clean_tree_passes_with_ruleset_installed(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "doc.md", "nothing suspicious\n")
    _commit_all(repo)

    r = run(repo, _ruleset_dir(tmp_path))
    assert r.returncode == 0
    assert "OK" in r.stdout


def test_expect_rulesets_mismatch_fails(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit_all(repo)

    r = run(repo, _ruleset_dir(tmp_path), "--expect-rulesets", "0")
    assert r.returncode == 1


def test_expect_rulesets_match_passes(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "doc.md", "clean\n")
    _commit_all(repo)

    r = run(repo, _ruleset_dir(tmp_path), "--expect-rulesets", "1")
    assert r.returncode == 0


def test_assert_grandfather_empty_fails_when_entries_remain(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit_all(repo)

    r = run(repo, _ruleset_dir(tmp_path, DENY_ZORBLEX_WITH_GRANDFATHER), "--assert-grandfather-empty")
    assert r.returncode == 1


def test_assert_grandfather_empty_passes_when_no_grandfather(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit_all(repo)

    r = run(repo, _ruleset_dir(tmp_path, DENY_ZORBLEX), "--assert-grandfather-empty")
    assert r.returncode == 0


def test_grandfathered_path_passes_by_default_but_fails_with_ignore_grandfather(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "legacy/old.md", "still says zorblex\n")
    _commit_all(repo)

    ruleset_dir = _ruleset_dir(tmp_path, DENY_ZORBLEX_WITH_GRANDFATHER)
    ok = run(repo, ruleset_dir)
    assert ok.returncode == 0

    forced = run(repo, ruleset_dir, "--ignore-grandfather")
    assert forced.returncode == 1


def test_staged_scoping_only_flags_staged_changes(tmp_path):
    """--staged (git diff --cached) is narrower than default (git ls-files +
    live working-tree read): a tracked file edited but not `git add`-ed shows
    up in default mode (it reads the live file) but not in --staged mode
    (its staged/index content still matches HEAD, so it never appears in the
    staged diff at all)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "modified_unstaged.md", "clean at commit time\n")
    _commit_all(repo)
    _write(repo, "modified_unstaged.md", "now mentions zorblex, but unstaged\n")
    _write(repo, "staged_new.md", "mentions zorblex, staged\n")
    subprocess.run(["git", "add", "staged_new.md"], cwd=repo, check=True, capture_output=True)

    ruleset_dir = _ruleset_dir(tmp_path)

    staged = run(repo, ruleset_dir, "--staged")
    assert staged.returncode == 1
    assert "staged_new.md" in staged.stdout
    assert "modified_unstaged.md" not in staged.stdout

    all_tracked = run(repo, ruleset_dir)
    assert all_tracked.returncode == 1
    assert "modified_unstaged.md" in all_tracked.stdout


def test_require_clean_path_both_directions(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "guarded/leaks.md", "mentions zorblex\n")
    _write(repo, "elsewhere/leaks.md", "mentions zorblex\n")
    _commit_all(repo)

    ruleset_dir = _ruleset_dir(tmp_path)

    in_scope = run(repo, ruleset_dir, "--require-clean-path", "guarded/")
    assert in_scope.returncode == 1
    assert "guarded/leaks.md" in in_scope.stdout
    assert "elsewhere/leaks.md" not in in_scope.stdout

    out_of_scope = run(repo, ruleset_dir, "--require-clean-path", "only-this-prefix/")
    assert out_of_scope.returncode == 0


def test_list_rulesets(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit_all(repo)

    r = run(repo, _ruleset_dir(tmp_path), "--list-rulesets")
    assert r.returncode == 0
    assert "synthetic" in r.stdout
