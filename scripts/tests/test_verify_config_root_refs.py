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


# ── content anchors: path:line:<sha8> ─────────────────────────────────────────

# The anchor of the line "See ~/.claude for config." — computed OUTSIDE this
# codebase so the test cannot pass by agreeing with a self-consistently broken
# implementation:
#     printf '%s' 'See ~/.claude for config.' | shasum -a 256
_SEE_ANCHOR = "d3b52e9e"


def test_anchor_definition_matches_independent_literal():
    """Outcome-independent anti-vacuity check: --repin writes anchors with the
    same function scan() reads them with, so a self-consistently wrong
    anchor_of would make every other anchor test green on a broken mechanism.
    Only an independently-derived constant can refute that."""
    assert vcr.anchor_of("See ~/.claude for config.") == _SEE_ANCHOR
    assert vcr.anchor_of("   See ~/.claude for config.  ") == _SEE_ANCHOR


def test_anchor_matches_at_pinned_line(tmp_path):
    """(i) Anchor agrees with the pin — covered, exactly as a bare pin is."""
    _write(tmp_path, "README.md", "See ~/.claude for config.\n")
    allowlist = _write(tmp_path, "allow.txt", f"README.md:1:{_SEE_ANCHOR}  # legacy note\n")
    assert vcr.scan(tmp_path, allowlist) == 0


def test_anchor_relocates_to_single_other_occurrence(tmp_path):
    """(ii) A line inserted above the reference must not redden the pin: the
    anchor finds the reference one line down and the entry follows it."""
    _write(tmp_path, "README.md", "filler\nSee ~/.claude for config.\n")
    allowlist = _write(tmp_path, "allow.txt", f"README.md:1:{_SEE_ANCHOR}  # legacy note\n")
    assert vcr.scan(tmp_path, allowlist) == 0

    entries = vcr.parse_allowlist(allowlist)
    relocations, ambiguities = vcr.resolve_entries(entries, vcr.find_occurrences(tmp_path))
    assert ambiguities == []
    assert [(r["old"], r["new"]) for r in relocations] == [(1, 2)]


def test_anchor_matching_no_occurrence_is_stale(tmp_path):
    """(iii) The silent hole a bare pin leaves open: the pinned line's own text
    was rewritten, so the anchor matches nothing and the entry goes stale."""
    _write(tmp_path, "README.md", "See ~/.claude for the OLD config.\n")
    allowlist = _write(tmp_path, "allow.txt", f"README.md:1:{_SEE_ANCHOR}  # legacy note\n")
    assert vcr.scan(tmp_path, allowlist) == 1

    entries = vcr.parse_allowlist(allowlist)
    occurrences = vcr.find_occurrences(tmp_path)
    vcr.resolve_entries(entries, occurrences)
    assert [e["raw"] for e in vcr.find_stale_entries(entries, occurrences)] == [
        f"README.md:1:{_SEE_ANCHOR}  # legacy note"
    ]


def test_anchor_matching_two_occurrences_is_ambiguous(tmp_path):
    """(iv) Two occurrences carry identical text and the pin matches neither —
    a hard failure naming both, never a silent pick."""
    _write(
        tmp_path, "README.md",
        "intro\nSee ~/.claude for config.\nmiddle\nSee ~/.claude for config.\n",
    )
    allowlist = _write(tmp_path, "allow.txt", f"README.md:1:{_SEE_ANCHOR}  # legacy note\n")
    assert vcr.scan(tmp_path, allowlist) == 1

    entries = vcr.parse_allowlist(allowlist)
    relocations, ambiguities = vcr.resolve_entries(entries, vcr.find_occurrences(tmp_path))
    assert relocations == []
    assert [a["candidates"] for a in ambiguities] == [[2, 4]]


def test_two_entries_resolving_to_same_occurrence_fail(tmp_path):
    """(v) Two entries collapsing onto one occurrence: without this check one
    silently double-covers while the occurrence the other named resurfaces as
    unallowed with no stated cause."""
    _write(tmp_path, "README.md", "See ~/.claude for config.\nLegacy note about ~/.claude here.\n")
    allowlist = _write(
        tmp_path, "allow.txt",
        f"README.md:1:{_SEE_ANCHOR}  # first entry\n"
        f"README.md:2:{_SEE_ANCHOR}  # second entry, same anchor\n",
    )
    assert vcr.scan(tmp_path, allowlist) == 1

    entries = vcr.parse_allowlist(allowlist)
    vcr.resolve_entries(entries, vcr.find_occurrences(tmp_path))
    duplicates = vcr.find_duplicate_coverage(entries)
    assert [(path, lineno, len(group)) for path, lineno, group in duplicates] == [
        ("README.md", 1, 2)
    ]


def test_unanchored_pin_behaves_as_before(tmp_path):
    """Back-compat: a bare path:line neither relocates nor gains an opinion —
    it covers its own line and goes stale when that line stops matching."""
    _write(tmp_path, "README.md", "filler\nSee ~/.claude for config.\n")
    allowlist = _write(tmp_path, "allow.txt", "README.md:1  # legacy note\n")
    assert vcr.scan(tmp_path, allowlist) == 1

    entries = vcr.parse_allowlist(allowlist)
    assert entries[0]["anchor"] is None
    relocations, ambiguities = vcr.resolve_entries(entries, vcr.find_occurrences(tmp_path))
    assert (relocations, ambiguities) == ([], [])
    assert entries[0]["resolved_line"] == 1

    _write(tmp_path, "README.md", "See ~/.claude for config.\n")
    assert vcr.scan(tmp_path, allowlist) == 0


# ── the migrated allowlist: the difficulty itself, end to end ─────────────────

_DOC = (
    "intro line\n"
    "See ~/.claude for config.\n"
    "prose\n"
    "Legacy note about ~/.claude here.\n"
)
_ALLOWLIST = (
    "# Allowlist header — kept verbatim by --repin.\n"
    "# Second header line.\n"
    "\n"
    "docs/note.md:2  # keep:legacy-fallback — описание старого расположения\n"
    "docs/note.md:4    # keep:migration-doc — migration doc quotes the legacy path\n"
)


def _repinned_fixture(tmp_path):
    _write(tmp_path, "docs/note.md", _DOC)
    allowlist = _write(tmp_path, "allow.txt", _ALLOWLIST)
    assert vcr.repin(tmp_path, allowlist) == 0
    assert vcr.scan(tmp_path, allowlist) == 0
    return allowlist


def test_pure_line_shift_stays_green_and_relocates(tmp_path, capsys):
    """The regression this whole change exists for: inserting a line ABOVE the
    references leaves every reference untouched, so the check must stay green
    and say where each pin travelled — not redden with nothing to re-read."""
    allowlist = _repinned_fixture(tmp_path)
    _write(tmp_path, "docs/note.md", "brand new first line\n" + _DOC)
    capsys.readouterr()

    assert vcr.scan(tmp_path, allowlist) == 0

    out = capsys.readouterr().out
    assert "docs/note.md:2 -> :3" in out
    assert "docs/note.md:4 -> :5" in out


def test_edited_pinned_line_still_fails(tmp_path):
    """The other half: rewriting the pinned line's own text must still fail,
    so a changed reference reaches a human. A bare pin would stay green here —
    that is the silent hole the anchors close, not a regression they cause."""
    allowlist = _repinned_fixture(tmp_path)
    _write(tmp_path, "docs/note.md", _DOC.replace(
        "See ~/.claude for config.", "See ~/.claude for the NEW config."))
    assert vcr.scan(tmp_path, allowlist) == 1


def test_repin_preserves_reasons_and_header(tmp_path):
    """--repin is a diff of line numbers and anchors and nothing else: comment
    lines, blank lines, per-entry spacing and reason text all survive byte for
    byte, so a whole-allowlist migration stays reviewable."""
    allowlist = _repinned_fixture(tmp_path)
    before = _ALLOWLIST.splitlines()
    after = allowlist.read_text(encoding="utf-8").splitlines()

    assert len(after) == len(before)
    for b, a in zip(before, after):
        assert b.partition("#")[2] == a.partition("#")[2]
    assert [line for line in after if not line.strip() or line.startswith("#")] == [
        line for line in before if not line.strip() or line.startswith("#")
    ]

    entries = vcr.parse_allowlist(allowlist)
    assert [e["anchor"] for e in entries] == [
        vcr.anchor_of("See ~/.claude for config."),
        vcr.anchor_of("Legacy note about ~/.claude here."),
    ]
    assert [e["line"] for e in entries] == [2, 4]


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
