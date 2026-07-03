"""Tests for verify-config-root-refs.py — the doc-side legacy `~/.claude` /
`$HOME/.claude` reference enumerator (complement of the code-side S2
enumerator covered by scripts/tests/test_config_root.py).
"""
from __future__ import annotations

import importlib.util
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
