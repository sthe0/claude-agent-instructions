"""Tests for verify-canon-writers.py: every script that can write files has a
reviewed entry in scripts/canon_writers.toml.

The synthetic cases build a throwaway git repo (the verifier's domain is
`git ls-files`); the last two run against the real tree.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO / "scripts"
sys.path.insert(0, str(_SCRIPTS))

ROUTED_SCRIPTS = [
    "scripts/record-experience.py",
    "scripts/stamp-memory-dates.py",
    "scripts/gen_crutch_registry.py",
    "scripts/permissions-cli.py",
    "scripts/verify-readme.py",
]

WRITER = 'from pathlib import Path\nPath("out.txt").write_text("x")\n'
ROUTED_WRITER = "from lib import worktree_route\n" + WRITER


def _load_mod():
    spec = importlib.util.spec_from_file_location(
        "verify_canon_writers", _SCRIPTS / "verify-canon-writers.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_mod()


def _make_repo(tmp: Path, files: dict[str, str]) -> Path:
    root = tmp / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    return root


def _run(root: Path, entries: str, capsys) -> tuple[int, str]:
    registry = root.parent / "canon_writers.toml"
    registry.write_text("[writers]\n" + entries, encoding="utf-8")
    code = _mod.main(["--root", str(root), "--registry", str(registry)])
    return code, capsys.readouterr().out


def _entry(path: str, disposition: str, category: str, reason: str = "because") -> str:
    return (f'"{path}" = {{ disposition = "{disposition}", category = "{category}", '
            f'reason = "{reason}" }}\n')


def test_unclassified_candidate_fails(tmp_path, capsys):
    root = _make_repo(tmp_path, {"scripts/w.py": WRITER, "scripts/ok.py": "print(1)\n"})
    code, out = _run(root, "", capsys)
    assert code == 1
    assert "scripts/w.py" in out and "no entry" in out
    assert "scripts/ok.py" not in out

    code, out = _run(root, _entry("scripts/w.py", "not-canon", "not-canon"), capsys)
    assert code == 0, out


def test_dangling_entry_fails(tmp_path, capsys):
    root = _make_repo(tmp_path, {"scripts/ok.py": "print(1)\n"})
    code, out = _run(root, _entry("scripts/gone.py", "not-canon", "not-canon"), capsys)
    assert code == 1
    assert "scripts/gone.py" in out and "dangling" in out


def test_bad_disposition_fails(tmp_path, capsys):
    root = _make_repo(tmp_path, {"scripts/w.py": WRITER})
    code, out = _run(root, _entry("scripts/w.py", "maybe", "not-canon"), capsys)
    assert code == 1
    assert "disposition 'maybe'" in out


def test_bad_category_fails(tmp_path, capsys):
    root = _make_repo(tmp_path, {"scripts/w.py": WRITER})
    code, out = _run(root, _entry("scripts/w.py", "exempt", "whatever"), capsys)
    assert code == 1
    assert "category 'whatever'" in out

    code, out = _run(root, _entry("scripts/w.py", "exempt", "not-canon"), capsys)
    assert code == 1
    assert "does not belong" in out

    code, out = _run(root, _entry("scripts/w.py", "not-canon", "not-canon", reason=" "), capsys)
    assert code == 1
    assert "empty reason" in out


def test_routed_entry_without_import_fails(tmp_path, capsys):
    root = _make_repo(tmp_path, {"scripts/w.py": WRITER})
    code, out = _run(root, _entry("scripts/w.py", "routed", "routed"), capsys)
    assert code == 1
    assert "does not import lib.worktree_route" in out

    (tmp_path / "ok").mkdir()
    root = _make_repo(tmp_path / "ok", {"scripts/w.py": ROUTED_WRITER})
    code, out = _run(root, _entry("scripts/w.py", "routed", "routed"), capsys)
    assert code == 0, out


def test_known_writers_are_candidates():
    for rel in ROUTED_SCRIPTS:
        text = (_REPO / rel).read_text(encoding="utf-8")
        assert _mod.candidate_hits(rel, text), f"picker misses known writer {rel}"


def test_real_repo_passes(capsys):
    assert _mod.main([]) == 0, capsys.readouterr().out
