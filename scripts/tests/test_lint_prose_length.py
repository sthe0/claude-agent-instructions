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
| `claude-md-max-bytes` | `1000` | . |
| `readme-max-lines` | `50` | . |
| `cursor-mirror-max-lines` | `50` | . |
| `skill-md-max-lines` | `50` | . |
| `policy-md-max-lines` | `50` | . |
"""


def _make_repo(tmp: Path, claude_lines: int, claude_line_width: int = 5) -> None:
    (tmp / "config.md").write_text(_CONFIG_TEMPLATE, encoding="utf-8")
    body = "\n".join("x" * claude_line_width for _ in range(claude_lines)) + "\n"
    (tmp / "CLAUDE.md").write_text(body, encoding="utf-8")
    (tmp / "README.md").write_text("readme\n", encoding="utf-8")
    (tmp / "cursor" / "rules").mkdir(parents=True)
    (tmp / "cursor" / "rules" / "claude-code-sync.mdc").write_text("m\n", encoding="utf-8")


def _run(tmp: Path, capsys):
    mod = _load_mod()
    mod.REPO_ROOT = tmp
    mod.CONFIG_MD = tmp / "config.md"
    rc = mod.main([])
    return rc, capsys.readouterr().out


def test_clean_tree_no_warn(tmp_path, capsys):
    _make_repo(tmp_path, claude_lines=50)  # 50% of lines, 300/1000 bytes
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


def test_byte_warn_at_90_percent(tmp_path, capsys):
    # 50 lines x 18 chars + newline = 950 bytes -> 95% of the 1000-byte ceiling.
    _make_repo(tmp_path, claude_lines=50, claude_line_width=18)
    rc, out = _run(tmp_path, capsys)
    assert rc == 0
    assert "WARN — CLAUDE.md: 950 bytes, 95% of limit 1000" in out


def test_fail_above_ceiling_still_fatal(tmp_path, capsys):
    _make_repo(tmp_path, claude_lines=101)
    rc, out = _run(tmp_path, capsys)
    assert rc == 1
    assert "FAIL" in out
    assert "CLAUDE.md: 101 lines, limit 100" in out
