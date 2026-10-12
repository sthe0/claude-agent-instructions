"""Tests for the memory-retrieval hit/miss ledger: the hook's surface rows, record-experience's
extend/new/search rows, spelling-independent leaf keys, single recording of a routed write, the
fail-open contract, and the report.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = SCRIPTS_DIR / "hook-memory-retrieval.py"
REPORT_SCRIPT = SCRIPTS_DIR / "memory-retrieval-report.py"
FIXTURE_LEDGER = SCRIPTS_DIR / "tests" / "fixtures" / "memory_retrieval" / "ledger.jsonl"

sys.path.insert(0, str(SCRIPTS_DIR))
from lib import memory_retrieval_ledger as ledger  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


re_mod = _load("record_experience_ledger", SCRIPTS_DIR / "record-experience.py")

TARGET_TERMS = "pelican migration schedule lighthouse harbour tides"


def _rows() -> list[dict]:
    return ledger.read_rows()


def _leaf(root: Path, name: str, desc: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text(
        f"---\nname: {name[:-3]}\ndescription: {desc}\nschema: difficulty/v1\n"
        "created: 2026-01-01\nlast_verified: 2026-01-01\n---\n\n"
        f"# {desc}\n\n## Difficulty\n{desc}\n\n## Contexts\n\n### 2026-01-01 — first\n",
        encoding="utf-8")
    return path


def _seed_surface(session: str, *paths: Path) -> None:
    ledger.append("surface", session=session, query="seed",
                  surfaced=[ledger.leaf_key(p) for p in paths])


@pytest.fixture
def memroot(tmp_path):
    return tmp_path / "memory-global" / "leaves" / "experience"


@pytest.fixture(autouse=True)
def _edit_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCTL_EDIT_LEDGER", str(tmp_path / "edit-log.jsonl"))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv(re_mod.ROUTED_ENV, raising=False)


def _extend(leaf: Path, session: str | None = "s1") -> int:
    argv = ["extend", "--leaf", str(leaf), "--date", "2026-02-02",
            "--context-label", "second", "--context-where", "W", "--plan", "P"]
    if session:
        argv += ["--session", session]
    return re_mod.main(argv)


def test_ledger_redirect_autouse_(tmp_path):
    assert ledger.ledger_path().parent == tmp_path
    assert ledger.ledger_path() != Path.home() / ".local" / "log" / "claude-memory-retrieval.jsonl"


def test_leaf_key_(tmp_path):
    real = tmp_path / "canon" / "memory-global" / "leaves" / "a.md"
    real.parent.mkdir(parents=True)
    real.write_text("x")
    link = tmp_path / "home"
    link.symlink_to(tmp_path / "canon" / "memory-global")
    assert ledger.leaf_key(real) == "global:leaves/a.md"
    assert ledger.leaf_key(link / "leaves" / "a.md") == "global:leaves/a.md"
    proj = tmp_path / "proj" / ".claude" / "agent-memory" / "experience" / "b.md"
    proj.parent.mkdir(parents=True)
    proj.write_text("x")
    assert ledger.leaf_key(proj) == "project:experience/b.md"
    other = tmp_path / "elsewhere" / "c.md"
    other.parent.mkdir()
    other.write_text("x")
    assert ledger.leaf_key(other) == os.path.realpath(other)


def test_surface_row_(tmp_path):
    root = tmp_path / "memory-global" / "leaves"
    root.mkdir(parents=True)
    (root / "hit.md").write_text(
        f"---\nname: hit\ndescription: {TARGET_TERMS}\n---\n\nbody\n", encoding="utf-8")
    for i in range(30):
        (root / f"f{i}.md").write_text(
            f"---\nname: f{i}\ndescription: unrelatedword{i} nothingelse{i} otherthing{i}\n---\n\nb\n",
            encoding="utf-8")
    env = {**os.environ, "CLAUDE_MEMORY_RETRIEVAL_GLOBAL_ROOT": str(root),
           "CLAUDE_MEMORY_RETRIEVAL_STATE_DIR": str(tmp_path / "state")}
    env.pop("AGENTCTL_JUDGE_CHILD", None)

    def fire(prompt: str):
        subprocess.run([sys.executable, str(HOOK_SCRIPT)], env=env, text=True,
                       input=json.dumps({"prompt": prompt, "session_id": "sess-9", "cwd": ""}),
                       capture_output=True, check=True, timeout=60)

    fire(f"{TARGET_TERMS} please run-nonce: abc123")
    fire("ok thanks")
    fire("completely different question about zebras and xylophones today")
    rows = _rows()
    assert len(rows) == 2, "the short prompt must not log; the unmatched prompt must"
    first, second = rows
    assert first["event"] == "surface" and first["session"] == "sess-9"
    assert first["surfaced"] == ["global:leaves/hit.md"]
    assert first["nonce"] == "abc123" and first["outcome"] is None
    assert second["surfaced"] == [] and second["nonce"] is None


def test_extend_hit_(tmp_path, memroot):
    leaf = _leaf(memroot, "2026-01-01-alpha.md", "pelican migration")
    link = tmp_path / "linked"
    link.symlink_to(tmp_path / "memory-global")
    _seed_surface("s1", link / "leaves" / "experience" / "2026-01-01-alpha.md")
    assert _extend(leaf) == 0
    row = _rows()[-1]
    assert (row["event"], row["outcome"]) == ("extend", "hit")
    assert row["target"] == "global:leaves/experience/2026-01-01-alpha.md"
    assert row["session"] == "s1"


def test_extend_miss_(memroot):
    leaf = _leaf(memroot, "2026-01-01-alpha.md", "pelican migration")
    other = _leaf(memroot, "2026-01-01-other.md", "different thing")
    _seed_surface("s1", other)
    assert _extend(leaf) == 0
    row = _rows()[-1]
    assert (row["event"], row["outcome"]) == ("extend", "miss")
    assert row["surfaced"] == ["global:leaves/experience/2026-01-01-other.md"]
    # a surfaced set from another session must not turn this into a hit
    _seed_surface("s2", leaf)
    assert _extend(leaf) == 0
    assert _rows()[-1]["outcome"] == "miss"


def test_extend_without_session_is_unattributed_(memroot):
    leaf = _leaf(memroot, "2026-01-01-alpha.md", "pelican migration")
    assert _extend(leaf, session=None) == 0
    row = _rows()[-1]
    assert row["session"] is None and row["outcome"] is None


def test_search_miss_(tmp_path, memroot, monkeypatch, capsys):
    _leaf(memroot, "2026-01-01-alpha.md", "pelican migration schedule")
    other = _leaf(memroot, "2026-01-01-other.md", "lighthouse keeper")
    monkeypatch.setattr(re_mod, "REPO_ROOT", tmp_path)
    _seed_surface("s1", other)
    assert re_mod.main(["search", "--session", "s1", "pelican migration"]) == 0
    row = _rows()[-1]
    assert (row["event"], row["outcome"]) == ("search", "miss")
    assert row["target"] == "global:leaves/experience/2026-01-01-alpha.md"
    assert row["query"] == "pelican migration"
    # hit when any of the top results was surfaced
    _seed_surface("s3", memroot / "2026-01-01-alpha.md")
    assert re_mod.main(["search", "--session", "s3", "pelican migration"]) == 0
    assert _rows()[-1]["outcome"] == "hit"
    capsys.readouterr()


def test_new_event_(tmp_path):
    proj = tmp_path / "proj"
    _seed_surface("s1")
    argv = ["new", "--scope", "project", "--project-dir", str(proj), "--date", "2026-02-02",
            "--slug", "fresh", "--title", "T", "--description", "d", "--confirmed-by", "user",
            "--difficulty", "an unrelated difficulty", "--order", "o", "--criterion", "c",
            "--context-where", "W", "--plan", "P", "--session", "s1"]
    assert re_mod.main(argv) == 0
    row = _rows()[-1]
    assert row["event"] == "new" and row["outcome"] is None
    assert row["target"] == "project:experience/2026-02-02-fresh.md"


def test_routed_once_(memroot, monkeypatch):
    leaf = _leaf(memroot, "2026-01-01-alpha.md", "pelican migration")
    argv = ["extend", "--leaf", str(leaf), "--date", "2026-02-02", "--context-label", "second",
            "--context-where", "W", "--plan", "P", "--session", "s1"]
    original_needs_worktree = re_mod.needs_worktree
    monkeypatch.setattr(re_mod, "needs_worktree", lambda args: True)
    monkeypatch.setattr(re_mod.worktree_route, "should_route", lambda repo: True)
    monkeypatch.setattr(re_mod, "write_via_worktree", lambda args, argv: 0)
    assert re_mod.main(argv) == 0
    assert len(_rows()) == 1, "the parent records the routed write once"
    # the routed child (guard env set) writes the leaf and records nothing
    monkeypatch.setattr(re_mod, "needs_worktree", original_needs_worktree)
    monkeypatch.setenv(re_mod.ROUTED_ENV, "1")
    before = len(_rows())
    assert re_mod.main(argv) == 0
    assert len(_rows()) == before


def test_routed_failure_records_nothing_(memroot, monkeypatch):
    leaf = _leaf(memroot, "2026-01-01-alpha.md", "pelican migration")
    monkeypatch.setattr(re_mod, "needs_worktree", lambda args: True)
    monkeypatch.setattr(re_mod.worktree_route, "should_route", lambda repo: True)
    monkeypatch.setattr(re_mod, "write_via_worktree", lambda args, argv: 1)
    code = re_mod.main(["extend", "--leaf", str(leaf), "--date", "2026-02-02",
                        "--context-label", "s", "--context-where", "W", "--plan", "P",
                        "--session", "s1"])
    assert code == 1 and _rows() == []


def test_ledger_fail_open_(memroot, tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    monkeypatch.setenv("CLAUDE_MEMORY_RETRIEVAL_LEDGER", str(blocker / "ledger.jsonl"))
    assert ledger.append("surface", session="s1") is False
    leaf = _leaf(memroot, "2026-01-01-alpha.md", "pelican migration")
    assert _extend(leaf) == 0
    assert "### 2026-02-02" in leaf.read_text(encoding="utf-8")
    # a classifier that blows up is swallowed too
    monkeypatch.setenv("CLAUDE_MEMORY_RETRIEVAL_LEDGER", str(tmp_path / "ok.jsonl"))
    monkeypatch.setattr(ledger, "classify", lambda *a, **k: 1 / 0)
    assert _extend(leaf) == 0


def test_report_(tmp_path):
    proc = subprocess.run([sys.executable, str(REPORT_SCRIPT), "--ledger", str(FIXTURE_LEDGER)],
                          capture_output=True, text=True, check=True)
    lines = proc.stdout.splitlines()
    assert lines[:5] == ["surfaces: 1", "hits: 1", "misses: 1", "unclassified: 1",
                         "unattributed: 1"]
    assert any("fixture-target.md" in ln and "harbour tides" in ln for ln in lines[5:])
    empty = subprocess.run(
        [sys.executable, str(REPORT_SCRIPT), "--ledger", str(tmp_path / "none.jsonl")],
        capture_output=True, text=True, check=True)
    assert "misses: 0" in empty.stdout and "miss [" not in empty.stdout
