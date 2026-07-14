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
    findings = sd.scan([], None)
    assert findings == []


# ── main() CLI ───────────────────────────────────────────────────────────────

def test_main_clean_tree_returns_zero(tmp_path, capsys):
    memory_root = tmp_path / "mem"
    memory_root.mkdir()
    rc = sd.main(["--memory-root", str(memory_root), "--no-repo"])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_main_dirty_tree_returns_one_and_prints(tmp_path, capsys):
    memory_root = tmp_path / "mem"
    memory_root.mkdir()
    (memory_root / "MEMORY.md").write_text("[dead](missing.md)\n", encoding="utf-8")
    rc = sd.main(["--memory-root", str(memory_root), "--no-repo"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "dangling-pointer" in out


def test_main_json_mode(tmp_path, capsys):
    memory_root = tmp_path / "mem"
    memory_root.mkdir()
    (memory_root / "MEMORY.md").write_text("[dead](missing.md)\n", encoding="utf-8")
    rc = sd.main(["--memory-root", str(memory_root), "--no-repo", "--json"])
    assert rc == 1
    out = capsys.readouterr().out
    assert '"kind": "dangling-pointer"' in out
