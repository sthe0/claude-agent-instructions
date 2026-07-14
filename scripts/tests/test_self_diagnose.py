"""Unit tests for self-diagnose.py's three scans plus scan()/main() wiring.

oversized-index and dangling-pointer are exercised against crafted MEMORY.md
fixtures on disk (no mocking needed — they're pure filesystem reads).
ceiling-proximity is exercised against a stubbed lint-prose-length module
(monkeypatched onto _load_lint_prose_length) so the test never depends on the
real repo's byte/line counts drifting over time.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_SD_PATH = Path(__file__).resolve().parent.parent / "self-diagnose.py"
_spec = importlib.util.spec_from_file_location("self_diagnose", _SD_PATH)
sd = importlib.util.module_from_spec(_spec)
sys.modules["self_diagnose"] = sd
_spec.loader.exec_module(sd)


# ── scan_oversized_indexes ──────────────────────────────────────────────────

def test_oversized_index_flagged_over_threshold(tmp_path):
    (tmp_path / "MEMORY.md").write_text("\n".join(["line"] * 5), encoding="utf-8")
    findings = sd.scan_oversized_indexes(tmp_path, threshold=3)
    assert len(findings) == 1
    assert findings[0].kind == "oversized-index"
    assert findings[0].path == "MEMORY.md"


def test_index_under_threshold_not_flagged(tmp_path):
    (tmp_path / "MEMORY.md").write_text("\n".join(["line"] * 2), encoding="utf-8")
    assert sd.scan_oversized_indexes(tmp_path, threshold=3) == []


def test_oversized_index_missing_root_is_empty(tmp_path):
    assert sd.scan_oversized_indexes(tmp_path / "does-not-exist", threshold=3) == []


# ── scan_dangling_pointers ───────────────────────────────────────────────────

def test_dangling_pointer_flags_missing_local_target(tmp_path):
    (tmp_path / "MEMORY.md").write_text("[dead](leaves/missing.md)\n", encoding="utf-8")
    findings = sd.scan_dangling_pointers(tmp_path)
    assert len(findings) == 1
    assert findings[0].kind == "dangling-pointer"
    assert findings[0].detail == "leaves/missing.md"


def test_valid_pointer_not_flagged(tmp_path):
    (tmp_path / "leaves").mkdir()
    (tmp_path / "leaves" / "real.md").write_text("x", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("[ok](leaves/real.md)\n", encoding="utf-8")
    assert sd.scan_dangling_pointers(tmp_path) == []


def test_external_link_not_flagged(tmp_path):
    (tmp_path / "MEMORY.md").write_text("[site](https://example.com/missing.md)\n", encoding="utf-8")
    assert sd.scan_dangling_pointers(tmp_path) == []


def test_anchor_only_fragment_not_flagged(tmp_path):
    (tmp_path / "MEMORY.md").write_text("[section](#some-heading)\n", encoding="utf-8")
    assert sd.scan_dangling_pointers(tmp_path) == []


# ── scan_ceiling_proximity (lint-prose-length.py stubbed) ───────────────────

def _stub_lint_prose_length(monkeypatch, *, config, governed):
    fake_mod = types.SimpleNamespace(
        parse_config_md=lambda: config,
        check_level=lambda value, limit: (
            "fail" if value > limit else ("warn" if value >= limit * 0.9 else "ok")
        ),
        GOVERNED=governed,
    )
    monkeypatch.setattr(sd, "_load_lint_prose_length", lambda repo_root: fake_mod)


def test_ceiling_proximity_flags_claude_md_and_governed_file(tmp_path, monkeypatch):
    (tmp_path / "CLAUDE.md").write_text("x" * 95, encoding="utf-8")
    (tmp_path / "README.md").write_text("\n".join(["l"] * 5), encoding="utf-8")
    _stub_lint_prose_length(
        monkeypatch,
        config={"claude-md-max-bytes": "100", "readme-max-lines": "5"},
        governed=[("README.md", "readme-max-lines")],
    )
    findings = sd.scan_ceiling_proximity(tmp_path)
    paths = {f.path for f in findings}
    assert "CLAUDE.md" in paths
    assert "README.md" in paths
    assert all(f.kind == "ceiling-proximity" for f in findings)


def test_ceiling_proximity_clean_when_well_under_limits(tmp_path, monkeypatch):
    (tmp_path / "CLAUDE.md").write_text("x" * 10, encoding="utf-8")
    (tmp_path / "README.md").write_text("l\n", encoding="utf-8")
    _stub_lint_prose_length(
        monkeypatch,
        config={"claude-md-max-bytes": "10000", "readme-max-lines": "140"},
        governed=[("README.md", "readme-max-lines")],
    )
    assert sd.scan_ceiling_proximity(tmp_path) == []


def test_ceiling_proximity_missing_lint_module_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(sd, "_load_lint_prose_length", lambda repo_root: None)
    assert sd.scan_ceiling_proximity(tmp_path) == []


# ── scan() combined ─────────────────────────────────────────────────────────

def test_scan_combines_memory_roots_and_repo(tmp_path, monkeypatch):
    root = tmp_path / "mem"
    root.mkdir()
    (root / "MEMORY.md").write_text("\n".join(["line"] * 5), encoding="utf-8")
    (root / "MEMORY.md").write_text("[dead](missing.md)\n" + "\n".join(["l"] * 5), encoding="utf-8")

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(sd, "_load_lint_prose_length", lambda repo_root: None)

    findings = sd.scan([root], repo, threshold=3)
    kinds = {f.kind for f in findings}
    assert "oversized-index" in kinds
    assert "dangling-pointer" in kinds


def test_scan_no_repo_skips_ceiling_scan(tmp_path):
    findings = sd.scan([], None, scan_hooks=False)
    assert findings == []


# ── main() CLI ───────────────────────────────────────────────────────────────

def test_main_clean_tree_returns_zero(tmp_path, capsys):
    memory_root = tmp_path / "mem"
    memory_root.mkdir()
    rc = sd.main(["--memory-root", str(memory_root), "--no-repo", "--no-hooks"])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_main_dirty_tree_returns_one_and_prints(tmp_path, capsys):
    memory_root = tmp_path / "mem"
    memory_root.mkdir()
    (memory_root / "MEMORY.md").write_text("[dead](missing.md)\n", encoding="utf-8")
    rc = sd.main(["--memory-root", str(memory_root), "--no-repo", "--no-hooks"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "dangling-pointer" in out


def test_main_json_mode(tmp_path, capsys):
    memory_root = tmp_path / "mem"
    memory_root.mkdir()
    (memory_root / "MEMORY.md").write_text("[dead](missing.md)\n", encoding="utf-8")
    rc = sd.main(["--memory-root", str(memory_root), "--no-repo", "--no-hooks", "--json"])
    assert rc == 1
    out = capsys.readouterr().out
    assert '"kind": "dangling-pointer"' in out


# ── scan_dangling_pointers false-positive fixes ─────────────────────────────

def test_link_inside_html_comment_not_flagged(tmp_path):
    (tmp_path / "MEMORY.md").write_text(
        "real content\n<!-- [old](leaves/removed.md) was here -->\n", encoding="utf-8"
    )
    assert sd.scan_dangling_pointers(tmp_path) == []


def test_placeholder_slug_target_not_flagged(tmp_path):
    (tmp_path / "MEMORY.md").write_text(
        "add a leaf in `leaves/` — see [template](leaves/<slug>.md)\n", encoding="utf-8"
    )
    assert sd.scan_dangling_pointers(tmp_path) == []


def test_real_dangling_still_flagged_alongside_false_positive_shapes(tmp_path):
    (tmp_path / "MEMORY.md").write_text(
        "<!-- [c](leaves/x.md) -->\n[t](leaves/<slug>.md)\n[dead](leaves/gone.md)\n",
        encoding="utf-8",
    )
    findings = sd.scan_dangling_pointers(tmp_path)
    assert [f.detail for f in findings] == ["leaves/gone.md"]


# ── scan_broken_hooks ───────────────────────────────────────────────────────

def _write_settings(path, commands):
    hooks = {"SessionStart": [{"hooks": [{"type": "command", "command": c} for c in commands]}]}
    path.write_text(__import__("json").dumps({"hooks": hooks}), encoding="utf-8")


def test_broken_hook_flags_missing_absolute_script(tmp_path):
    settings = tmp_path / "settings.json"
    _write_settings(settings, [str(tmp_path / "missing-hook.py")])
    findings = sd.scan_broken_hooks([settings])
    assert len(findings) == 1
    assert findings[0].kind == "broken-hook-registration"
    assert "missing-hook.py not found" in findings[0].detail


def test_broken_hook_clean_when_script_exists(tmp_path):
    script = tmp_path / "present-hook.py"
    script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    settings = tmp_path / "settings.json"
    _write_settings(settings, [str(script)])
    assert sd.scan_broken_hooks([settings]) == []


def test_bare_command_hook_not_flagged(tmp_path):
    settings = tmp_path / "settings.json"
    _write_settings(settings, ["jq '{hookSpecificOutput: 1}'", "bash -c 'echo hi'"])
    assert sd.scan_broken_hooks([settings]) == []


def test_absolute_interpreter_with_module_not_flagged(tmp_path):
    # Leading token is an existing interpreter path; the -m module is not a file.
    interp = tmp_path / "python"
    interp.write_text("", encoding="utf-8")
    settings = tmp_path / "settings.json"
    _write_settings(settings, [f"{interp} -m ccgram.main hook"])
    assert sd.scan_broken_hooks([settings]) == []


def test_broken_hook_missing_or_malformed_settings_skipped(tmp_path):
    missing = tmp_path / "nope.json"
    malformed = tmp_path / "bad.json"
    malformed.write_text("{ not json", encoding="utf-8")
    assert sd.scan_broken_hooks([missing, malformed]) == []


def test_default_settings_paths_dedups_by_resolved(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    real = home / ".claude" / "settings.json"
    real.write_text("{}", encoding="utf-8")
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    # Project settings.json symlinks to the user file → must be scanned once.
    (proj / ".claude" / "settings.json").symlink_to(real)
    monkeypatch.setattr(sd.Path, "home", staticmethod(lambda: home))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    paths = sd.default_settings_paths()
    resolved = {p.resolve() for p in paths}
    assert resolved == {real.resolve()}


# ── scan_near_duplicates ─────────────────────────────────────────────────────

def _leaf(path, name, description):
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nbody\n", encoding="utf-8"
    )


def test_near_duplicate_flags_high_overlap_pair(tmp_path):
    _leaf(tmp_path / "a.md", "ssh-agent-hang", "stale forwarded ssh agent socket hangs git auth")
    _leaf(tmp_path / "b.md", "ssh-agent-hang-dup", "stale forwarded ssh agent socket hangs git auth again")
    findings = sd.scan_near_duplicates(tmp_path, threshold=0.6)
    assert len(findings) == 1
    assert findings[0].kind == "near-duplicate"


def test_distinct_leaves_not_flagged(tmp_path):
    _leaf(tmp_path / "a.md", "token-economy", "context engineering and cache aware spend reduction")
    _leaf(tmp_path / "b.md", "robot-acl", "yt nirvana robot identity read acl for humans")
    assert sd.scan_near_duplicates(tmp_path, threshold=0.6) == []


def test_near_duplicate_ignores_files_without_frontmatter(tmp_path):
    (tmp_path / "MEMORY.md").write_text("# index\n- pointer\n", encoding="utf-8")
    _leaf(tmp_path / "a.md", "solo-leaf", "a single leaf with no duplicate")
    assert sd.scan_near_duplicates(tmp_path, threshold=0.6) == []


# ── scan_orphans ─────────────────────────────────────────────────────────────

def test_orphan_leaf_flagged_when_unlinked(tmp_path):
    (tmp_path / "MEMORY.md").write_text("- [linked](leaves/kept.md)\n", encoding="utf-8")
    (tmp_path / "leaves").mkdir()
    (tmp_path / "leaves" / "kept.md").write_text("x", encoding="utf-8")
    (tmp_path / "leaves" / "orphan.md").write_text("y", encoding="utf-8")
    findings = sd.scan_orphans(tmp_path)
    assert len(findings) == 1
    assert findings[0].kind == "orphan-leaf"
    assert findings[0].path == "leaves/orphan.md"


def test_reachable_via_subindex_not_flagged(tmp_path):
    (tmp_path / "MEMORY.md").write_text("- [sub](sub/MEMORY.md)\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "MEMORY.md").write_text("- [leaf](deep.md)\n", encoding="utf-8")
    (tmp_path / "sub" / "deep.md").write_text("z", encoding="utf-8")
    assert sd.scan_orphans(tmp_path) == []


def test_wikilink_reachable_leaf_not_flagged(tmp_path):
    (tmp_path / "MEMORY.md").write_text("- [[topic-slug]]\n", encoding="utf-8")
    (tmp_path / "leaves").mkdir()
    (tmp_path / "leaves" / "topic.md").write_text(
        "---\nname: topic-slug\ndescription: d\n---\nbody\n", encoding="utf-8"
    )
    assert sd.scan_orphans(tmp_path) == []


def test_wikilink_alias_and_anchor_resolve(tmp_path):
    (tmp_path / "MEMORY.md").write_text("- [[topic-slug|подпись]]\n", encoding="utf-8")
    (tmp_path / "leaves").mkdir()
    (tmp_path / "leaves" / "topic.md").write_text(
        "---\nname: topic-slug\ndescription: d\n---\nbody\n", encoding="utf-8"
    )
    assert sd.scan_orphans(tmp_path) == []


def test_wikilink_unknown_slug_ignored_real_orphan_still_flagged(tmp_path):
    (tmp_path / "MEMORY.md").write_text("- [[nope]]\n", encoding="utf-8")
    (tmp_path / "leaves").mkdir()
    (tmp_path / "leaves" / "orphan.md").write_text(
        "---\nname: real-orphan\ndescription: d\n---\nbody\n", encoding="utf-8"
    )
    findings = sd.scan_orphans(tmp_path)
    assert len(findings) == 1
    assert findings[0].kind == "orphan-leaf"
    assert findings[0].path == "leaves/orphan.md"


def test_orphan_index_kind_for_unlinked_memory_md(tmp_path):
    (tmp_path / "MEMORY.md").write_text("no links here\n", encoding="utf-8")
    (tmp_path / "island").mkdir()
    (tmp_path / "island" / "MEMORY.md").write_text("stranded sub-index\n", encoding="utf-8")
    findings = sd.scan_orphans(tmp_path)
    kinds = {f.kind for f in findings}
    assert "orphan-index" in kinds


def test_orphans_no_root_index_emits_single_finding(tmp_path):
    (tmp_path / "leaves").mkdir()
    (tmp_path / "leaves" / "a.md").write_text("x", encoding="utf-8")
    (tmp_path / "leaves" / "b.md").write_text("y", encoding="utf-8")
    findings = sd.scan_orphans(tmp_path)
    assert len(findings) == 1
    assert findings[0].kind == "no-root-index"


def test_orphans_missing_root_dir_is_empty(tmp_path):
    assert sd.scan_orphans(tmp_path / "does-not-exist") == []


# ── scan() wiring: all four classes present in a dirty tree, absent in clean ──

def test_scan_includes_new_classes_in_dirty_tree(tmp_path):
    root = tmp_path / "mem"
    (root / "leaves").mkdir(parents=True)
    (root / "MEMORY.md").write_text(
        "- [dup-a](leaves/dup_a.md)\n<!-- [x](leaves/commented.md) -->\n[t](leaves/<slug>.md)\n",
        encoding="utf-8",
    )
    _leaf(root / "leaves" / "dup_a.md", "dup-topic", "identical description tokens here")
    _leaf(root / "leaves" / "dup_b.md", "dup-topic-two", "identical description tokens here")

    settings = tmp_path / "settings.json"
    _write_settings(settings, [str(tmp_path / "gone-hook.py")])

    findings = sd.scan([root], None, settings_paths=[settings], near_dup_threshold=0.6)
    kinds = {f.kind for f in findings}
    assert "broken-hook-registration" in kinds
    assert "near-duplicate" in kinds
    assert "orphan-leaf" in kinds  # dup_b.md is unlinked
    # False-positive shapes suppressed: no dangling-pointer from comment/placeholder.
    assert "dangling-pointer" not in kinds


def test_scan_clean_tree_empty(tmp_path):
    root = tmp_path / "mem"
    (root / "leaves").mkdir(parents=True)
    (root / "MEMORY.md").write_text("- [only](leaves/only.md)\n", encoding="utf-8")
    _leaf(root / "leaves" / "only.md", "sole-topic", "the only leaf, reachable and unique")
    empty_settings = tmp_path / "settings.json"
    empty_settings.write_text("{}", encoding="utf-8")
    findings = sd.scan([root], None, settings_paths=[empty_settings], near_dup_threshold=0.6)
    assert findings == []
