"""Tests for verify-onboarding-entrypoint.py.

Covers:
  - FAIL: fenced `cd ~/x && claude` (the original README regression shape)
  - FAIL: fenced standalone `claude`
  - FAIL: fenced `cd ~/x && claude   # start your first task` (this repo's
    commented house style — a naive `claude\\s*$` regex would miss this)
  - OK: `claude-agent`, `claude-task ... # comment`, `CLAUDE_CONFIG_DIR=...
    claude auth login`, `claude --version`
  - OK: inline prose "bare `claude`" outside any fence is not flagged
  - --root override and default two-target scan
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))


def _load_mod():
    path = _SCRIPTS / "verify-onboarding-entrypoint.py"
    spec = importlib.util.spec_from_file_location("verify_onboarding_entrypoint", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_mod()
main = _mod.main


def _make_tree(tmp_path: Path, readme_body: str, setup_body: str | None = None) -> Path:
    (tmp_path / "README.md").write_text(readme_body, encoding="utf-8")
    ops_dir = tmp_path / "docs" / "operations"
    ops_dir.mkdir(parents=True)
    ops_dir_body = setup_body if setup_body is not None else "no fences here\n"
    (ops_dir / "setup.md").write_text(ops_dir_body, encoding="utf-8")
    return tmp_path


class TestFailCases:
    def test_cd_and_bare_claude(self, tmp_path, capsys):
        tree = _make_tree(
            tmp_path,
            "```bash\ncd ~/x && claude\n```\n",
        )
        rc = main(["--root", str(tree)])
        out = capsys.readouterr().out
        assert rc == 1
        assert "FAIL" in out
        assert "README.md:2" in out

    def test_standalone_bare_claude(self, tmp_path, capsys):
        tree = _make_tree(
            tmp_path,
            "```\nclaude\n```\n",
        )
        rc = main(["--root", str(tree)])
        assert rc == 1
        assert "README.md:2" in capsys.readouterr().out

    def test_commented_house_style(self, tmp_path, capsys):
        tree = _make_tree(
            tmp_path,
            "```bash\ncd ~/x && claude   # start your first task\n```\n",
        )
        rc = main(["--root", str(tree)])
        out = capsys.readouterr().out
        assert rc == 1
        assert "README.md:2" in out


class TestAllowCases:
    def test_claude_agent(self, tmp_path):
        tree = _make_tree(
            tmp_path,
            "```bash\ncd ~/x && claude-agent\n```\n",
        )
        assert main(["--root", str(tree)]) == 0

    def test_claude_task_with_comment(self, tmp_path):
        tree = _make_tree(
            tmp_path,
            "```\nclaude-task ABC-1        # comment\n```\n",
        )
        assert main(["--root", str(tree)]) == 0

    def test_claude_auth_login(self, tmp_path):
        tree = _make_tree(
            tmp_path,
            "```bash\nCLAUDE_CONFIG_DIR=~/.claude-agent claude auth login\n```\n",
        )
        assert main(["--root", str(tree)]) == 0

    def test_claude_version(self, tmp_path):
        tree = _make_tree(
            tmp_path,
            "```bash\nclaude --version\n```\n",
        )
        assert main(["--root", str(tree)]) == 0

    def test_inline_prose_not_flagged(self, tmp_path):
        tree = _make_tree(
            tmp_path,
            "Bare `claude` is your untouched personal install, not the system.\n",
        )
        assert main(["--root", str(tree)]) == 0


class TestScanCount:
    def test_ok_message_reports_both_targets(self, tmp_path, capsys):
        tree = _make_tree(
            tmp_path,
            "```bash\ncd ~/x && claude-agent\n```\n",
            "```bash\nclaude-task DEEPAGENT-1\n```\n",
        )
        rc = main(["--root", str(tree)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "2 onboarding doc(s) scanned" in out


class TestRealTree:
    def test_current_repo_tree_passes(self):
        repo_root = _SCRIPTS.parent
        assert main(["--root", str(repo_root)]) == 0
