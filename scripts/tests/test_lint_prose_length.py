"""Tests for lint-prose-length.py WARN threshold and fail-at-ceiling behavior.

Builds a throwaway repo root with a config.md and governed files, then points
the module's REPO_ROOT/CONFIG_MD globals at it.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))


def _load_mod():
    path = _SCRIPTS / "lint-prose-length.py"
    spec = importlib.util.spec_from_file_location("lint_prose_length", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_CONFIG_TEMPLATE = """\
| Key | Value | Meaning |
|---|---|---|
| `claude-md-max-lines` | `100` | . |
| `claude-md-max-chars` | `1000` | . |
| `readme-max-lines` | `50` | . |
| `cursor-mirror-max-lines` | `50` | . |
| `skill-md-max-lines` | `50` | . |
| `policy-md-max-lines` | `50` | . |
| `skill-description-max-chars` | `850` | . |
| `always-loaded-surface-advisory-chars` | `100000` | . |
"""


def _make_repo(tmp: Path, claude_lines: int, claude_line_width: int = 5) -> None:
    (tmp / "config.md").write_text(_CONFIG_TEMPLATE, encoding="utf-8")
    body = "\n".join("x" * claude_line_width for _ in range(claude_lines)) + "\n"
    (tmp / "CLAUDE.md").write_text(body, encoding="utf-8")
    (tmp / "README.md").write_text("readme\n", encoding="utf-8")
    (tmp / "cursor" / "rules").mkdir(parents=True)
    (tmp / "cursor" / "rules" / "claude-code-sync.mdc").write_text("m\n", encoding="utf-8")


def _write_skill(tmp: Path, name: str, description: str) -> None:
    skill_dir = tmp / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\nbody\n",
        encoding="utf-8",
    )


def _run(tmp: Path, capsys):
    mod = _load_mod()
    mod.REPO_ROOT = tmp
    mod.CONFIG_MD = tmp / "config.md"
    rc = mod.main([])
    return rc, capsys.readouterr().out


def test_clean_tree_no_warn(tmp_path, capsys):
    _make_repo(tmp_path, claude_lines=50)  # 50% of lines, 300/1000 chars
    rc, out = _run(tmp_path, capsys)
    assert rc == 0
    assert "WARN" not in out
    assert "OK" in out


def test_warn_at_90_percent_exits_zero(tmp_path, capsys):
    _make_repo(tmp_path, claude_lines=92)  # 92% of the 100-line ceiling
    rc, out = _run(tmp_path, capsys)
    assert rc == 0
    assert "lint-prose-length: WARN — CLAUDE.md: 92 lines, 92% of limit 100" in out
    assert "OK" in out


def test_char_warn_at_90_percent(tmp_path, capsys):
    # 50 lines x 18 chars + newline = 950 chars -> 95% of the 1000-char ceiling.
    _make_repo(tmp_path, claude_lines=50, claude_line_width=18)
    rc, out = _run(tmp_path, capsys)
    assert rc == 0
    assert "WARN — CLAUDE.md: 950 chars, 95% of limit 1000" in out


def test_char_unit_not_byte_unit_cyrillic(tmp_path, capsys):
    # 950 Cyrillic chars = 951 chars total (95% of the 1000-char ceiling,
    # WARN) but 1901 UTF-8 bytes -- over the 1000-byte ceiling the OLD
    # byte-based model would have FAILED on. Proves the linter measures
    # chars, not bytes.
    (tmp_path / "config.md").write_text(_CONFIG_TEMPLATE, encoding="utf-8")
    body = "б" * 950 + "\n"
    (tmp_path / "CLAUDE.md").write_text(body, encoding="utf-8")
    (tmp_path / "README.md").write_text("readme\n", encoding="utf-8")
    (tmp_path / "cursor" / "rules").mkdir(parents=True)
    (tmp_path / "cursor" / "rules" / "claude-code-sync.mdc").write_text("m\n", encoding="utf-8")
    assert len(body.encode("utf-8")) > 1000  # sanity: would FAIL as bytes
    rc, out = _run(tmp_path, capsys)
    assert rc == 0
    assert "WARN — CLAUDE.md: 951 chars, 95% of limit 1000" in out


def test_fail_above_ceiling_still_fatal(tmp_path, capsys):
    _make_repo(tmp_path, claude_lines=101)
    rc, out = _run(tmp_path, capsys)
    assert rc == 1
    assert "FAIL" in out
    assert "CLAUDE.md: 101 lines, limit 100" in out


def test_skill_description_over_cap_fails(tmp_path, capsys):
    _make_repo(tmp_path, claude_lines=50)
    _write_skill(tmp_path, "toolong", "x" * 900)
    rc, out = _run(tmp_path, capsys)
    assert rc == 1
    assert "FAIL" in out
    assert "skills/toolong/SKILL.md: 900 chars description, limit 850" in out


def test_skill_description_under_cap_passes(tmp_path, capsys):
    _make_repo(tmp_path, claude_lines=50)
    _write_skill(tmp_path, "fine", "x" * 800)
    rc, out = _run(tmp_path, capsys)
    assert rc == 0


def test_surface_report_consistency(tmp_path, capsys):
    _make_repo(tmp_path, claude_lines=50)
    (tmp_path / "memory-global").mkdir()
    (tmp_path / "memory-global" / "MEMORY.md").write_text("m" * 40 + "\n", encoding="utf-8")
    _write_skill(tmp_path, "a", "d" * 100)
    _write_skill(tmp_path, "b", "e" * 200)

    mod = _load_mod()
    mod.REPO_ROOT = tmp_path
    mod.CONFIG_MD = tmp_path / "config.md"
    rc = mod.main(["--surface-report"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "DYNAMIC" not in out

    lines = [l.strip() for l in out.splitlines() if l.strip().endswith("chars")]
    breakdown_total = sum(int(l.rsplit(" ", 2)[-2]) for l in lines if "TOTAL" not in l)
    total_line = next(l for l in lines if l.startswith("TOTAL"))
    reported_total = int(total_line.rsplit(" ", 2)[-2])
    assert reported_total == breakdown_total


def test_surface_report_no_transcript_io_without_include_dynamic(tmp_path, capsys):
    _make_repo(tmp_path, claude_lines=50)

    mod = _load_mod()
    mod.REPO_ROOT = tmp_path
    mod.CONFIG_MD = tmp_path / "config.md"

    def _boom(*args, **kwargs):
        raise AssertionError("scan_dynamic_injection must not run without --include-dynamic")

    mod.scan_dynamic_injection = _boom
    rc = mod.main(["--surface-report"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "DYNAMIC" not in out
