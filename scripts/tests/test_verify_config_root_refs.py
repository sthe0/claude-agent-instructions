"""Tests for verify-config-root-refs.py — the doc-side legacy `~/.claude` /
`$HOME/.claude` reference enumerator (complement of the code-side S2
enumerator covered by scripts/tests/test_config_root.py).
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "verify_config_root_refs",
    Path(__file__).resolve().parents[1] / "verify-config-root-refs.py",
)
vcr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(vcr)


def _write(repo: Path, rel: str, content: str) -> Path:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _init_git_repo(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")


def _full_report(repo_root: Path, allowlist_path: Path):
    """The complete verdict scan() reports on — unallowed, stale, ungoverned —
    as plain data, so two calls can be compared for exact equality."""
    entries = vcr.parse_allowlist(allowlist_path)
    occurrences = vcr.find_occurrences(repo_root)
    unallowed = vcr.find_unallowed(occurrences, entries)
    stale = [e["raw"] for e in vcr.find_stale_entries(entries, occurrences)]
    ungoverned = vcr.find_ungoverned(repo_root, occurrences)
    return (unallowed, stale, ungoverned)


def test_non_allowlisted_ref_fails(tmp_path):
    _write(tmp_path, "README.md", "See ~/.claude for config.\n")
    allowlist = _write(tmp_path, "allow.txt", "")
    assert vcr.scan(tmp_path, allowlist) == 1


def test_allowlisted_ref_passes(tmp_path):
    _write(tmp_path, "README.md", "See ~/.claude for config.\n")
    allowlist = _write(tmp_path, "allow.txt", "README.md:1  # legacy-fallback doc note\n")
    assert vcr.scan(tmp_path, allowlist) == 0


def test_stale_allowlist_entry_fails(tmp_path):
    _write(tmp_path, "README.md", "Nothing legacy here.\n")
    allowlist = _write(tmp_path, "allow.txt", "README.md:1  # no longer true\n")
    assert vcr.scan(tmp_path, allowlist) == 1


def test_code_scope_files_ignored(tmp_path):
    _write(tmp_path, "scripts/tool.py", "# see ~/.claude for legacy behavior\n")
    _write(tmp_path, "scripts/tool.sh", "# see ~/.claude for legacy behavior\n")
    allowlist = _write(tmp_path, "allow.txt", "")
    assert vcr.scan(tmp_path, allowlist) == 0


def test_target_excluding_regex_never_matches_new_root():
    assert vcr.TILDE_RE.search("~/.claude-agent/skills") is None
    assert vcr.TILDE_RE.search("~/.claude/skills") is not None
    assert vcr.HOME_RE.search("$HOME/.claude-agent/skills") is None
    assert vcr.HOME_RE.search("$HOME/.claude/skills") is not None


def test_new_root_path_ignored_in_scan(tmp_path):
    _write(tmp_path, "README.md", "Config lives at ~/.claude-agent now.\n")
    allowlist = _write(tmp_path, "allow.txt", "")
    assert vcr.scan(tmp_path, allowlist) == 0


def test_glob_allowlist_entry(tmp_path):
    _write(tmp_path, "docs/migrations/note.md", "Historically at ~/.claude.\n")
    allowlist = _write(
        tmp_path, "allow.txt",
        "docs/migrations/*.md  # migration docs quote the legacy path\n",
    )
    assert vcr.scan(tmp_path, allowlist) == 0


def test_malformed_allowlist_entry_missing_reason_fails(tmp_path):
    _write(tmp_path, "README.md", "clean\n")
    allowlist = _write(tmp_path, "allow.txt", "README.md:1\n")
    assert vcr.scan(tmp_path, allowlist) == 1


def test_git_dir_excluded(tmp_path):
    _write(tmp_path, ".git/COMMIT_EDITMSG", "mentions ~/.claude here\n")
    assert vcr.find_occurrences(tmp_path) == []


def test_allowlist_file_itself_excluded_from_domain(tmp_path):
    # The allowlist's own reason text routinely quotes ~/.claude (R4) — its
    # SELF_REF_EXCLUDED path must never appear as a scan target itself.
    _write(
        tmp_path, "scripts/config-root-refs-allowlist.txt",
        "# reason keeps ~/.claude.json harness-owned (no real entries here)\n",
    )
    assert vcr.find_occurrences(tmp_path) == []


def test_worklist_tsv_excluded_from_domain(tmp_path):
    _write(
        tmp_path, "docs/migrations/config-root-tails-worklist.tsv",
        "path\tline\tcategory\nfoo.md\t3\tkeep:harness-owned ~/.claude.json\n",
    )
    assert vcr.find_occurrences(tmp_path) == []


# ── exhaustiveness cross-check (Stage 5) ──────────────────────────────────────

def test_ungoverned_undecodable_file_fails(tmp_path):
    """A file find_occurrences silently drops on a UnicodeDecodeError (not
    *.py/*.sh, so the S2 code enumerator never looks at it either) must
    surface as ungoverned rather than disappearing from both enumerators."""
    p = tmp_path / "notes.md"
    p.write_bytes(b"See ~/.claude for config.\n\xff\xfe garbage\n")
    # Confirm the premise: find_occurrences really does drop it.
    assert vcr.find_occurrences(tmp_path) == []
    assert vcr.find_ungoverned(tmp_path) == ["notes.md"]
    allowlist = _write(tmp_path, "allow.txt", "")
    assert vcr.scan(tmp_path, allowlist) == 1


def test_partition_green_case_passes(tmp_path):
    """Doc-scope (allowlisted) + code-scope (*.py, ignored) together leave
    nothing ungoverned."""
    _write(tmp_path, "README.md", "See ~/.claude for config.\n")
    _write(tmp_path, "scripts/tool.py", "# see ~/.claude for legacy behavior\n")
    allowlist = _write(tmp_path, "allow.txt", "README.md:1  # legacy-fallback doc note\n")
    assert vcr.find_ungoverned(tmp_path) == []
    assert vcr.scan(tmp_path, allowlist) == 0


def test_ungoverned_ignores_self_ref_excluded_paths(tmp_path):
    """The two generated artifacts are domain-excluded outright, not
    ungoverned, even though they quote the legacy pattern by construction."""
    _write(
        tmp_path, "scripts/config-root-refs-allowlist.txt",
        "# reason keeps ~/.claude.json harness-owned (no real entries here)\n",
    )
    _write(
        tmp_path, "docs/migrations/config-root-tails-worklist.tsv",
        "path\tline\tcategory\nfoo.md\t3\tkeep:harness-owned ~/.claude.json\n",
    )
    assert vcr.find_ungoverned(tmp_path) == []


# ── domain must be the git index, not the working tree (verifier-reproducibility) ──

def test_iter_repo_files_skips_untracked(tmp_path):
    """A file created on disk but never `git add`ed must be absent from the
    enumerator's domain — the verdict must not depend on untracked scratch."""
    _init_git_repo(tmp_path)
    _write(tmp_path, "README.md", "tracked\n")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    _write(tmp_path, "scratch.md", "untracked\n")

    files = {p.as_posix() for p in vcr._iter_repo_files(tmp_path)}

    assert "README.md" in files
    assert "scratch.md" not in files


def test_untracked_file_does_not_change_verdict(tmp_path):
    """Reproduces the eca07be regression: an allowlist entry naming a path
    that is never committed must report identically (stale, both times)
    whether or not that path happens to exist untracked on disk — otherwise
    two checkouts of the same commit disagree."""
    _init_git_repo(tmp_path)
    _write(tmp_path, "README.md", "clean\n")
    allowlist = _write(
        tmp_path, "allow.txt",
        "scratch/legacy-note.md:1  # names a path that is never committed\n",
    )
    _git(tmp_path, "add", "README.md", "allow.txt")
    _git(tmp_path, "commit", "-q", "-m", "initial")

    before = _full_report(tmp_path, allowlist)
    assert before[1] == ["scratch/legacy-note.md:1  # names a path that is never committed"]

    _write(tmp_path, "scratch/legacy-note.md", "See ~/.claude for config.\n")
    after = _full_report(tmp_path, allowlist)

    assert before == after


# ── --staged: only NEWLY-INTRODUCED violations block (Stage 4, lib.baseline_diff) ──

def test_staged_tolerates_pre_existing_red(tmp_path):
    """The motivating case: a pre-existing legacy reference at HEAD, in a file
    an unrelated staged change never touches, must not block --staged."""
    _init_git_repo(tmp_path)
    _write(tmp_path, "legacy.md", "See ~/.claude for config.\n")
    _write(tmp_path, "clean.md", "nothing here\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed with pre-existing red")

    _write(tmp_path, "clean.md", "nothing here, now changed\n")

    assert vcr.scan_staged(tmp_path) == 0


def test_staged_reports_new_unallowed_reference(tmp_path):
    _init_git_repo(tmp_path)
    _write(tmp_path, "clean.md", "nothing here\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed clean")

    _write(tmp_path, "clean.md", "nothing here\nSee ~/.claude now.\n")

    assert vcr.scan_staged(tmp_path) == 1


def test_staged_reports_new_stale_allowlist_entry(tmp_path):
    """An allowlist entry that matched at HEAD but is orphaned by a staged fix
    becomes stale — that is a NEW stale entry, not a pre-existing one."""
    _init_git_repo(tmp_path)
    _write(tmp_path, "doc.md", "See ~/.claude for x.\n")
    _write(
        tmp_path, "scripts/config-root-refs-allowlist.txt",
        "doc.md:1  # legacy note\n",
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed with a matching allowlist entry")

    _write(tmp_path, "doc.md", "See ~/.claude-agent for x.\n")  # fixed, orphaning the entry

    assert vcr.scan_staged(tmp_path) == 1


def test_staged_reports_new_ungoverned_file(tmp_path):
    _init_git_repo(tmp_path)
    _write(tmp_path, "clean.md", "nothing here\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed clean")

    (tmp_path / "notes.dat").write_bytes(b"See ~/.claude for config.\n\xff\xfe garbage\n")
    _git(tmp_path, "add", "-A")

    assert vcr.scan_staged(tmp_path) == 1


def test_whole_repo_mode_unchanged_still_reports_pre_existing_red(tmp_path):
    """Whole-repo (no --staged) is byte-for-byte preserved: it still reports
    a red that --staged now tolerates."""
    _init_git_repo(tmp_path)
    _write(tmp_path, "legacy.md", "See ~/.claude for config.\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed with pre-existing red")

    allowlist = tmp_path / "scripts" / "config-root-refs-allowlist.txt"
    assert vcr.scan(tmp_path, allowlist) == 1
    assert vcr.scan_staged(tmp_path) == 0


def test_staged_degrades_to_whole_repo_when_baseline_unavailable(tmp_path):
    """Not a git work tree at all -> BaselineUnavailable -> --staged degrades
    to reporting everything, same as whole-repo scan()."""
    _write(tmp_path, "legacy.md", "See ~/.claude for config.\n")

    assert vcr.scan_staged(tmp_path) == 1
