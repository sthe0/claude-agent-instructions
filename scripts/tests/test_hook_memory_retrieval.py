"""Tests for hook-memory-retrieval.py — lexical pointers to memory leaves on UserPromptSubmit.

Covers: a hit surfaces the leaf by real path; the cutoff, the short-prompt floor, per-session
dedupe, the top-K cap and the line format; project corpus present/absent; the recursive corpus
and index exclusion; fail-open on malformed stdin; the judge-child guard; latency at the real
corpus's size; and that the hook's constants are the calibrated ones.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent
HOOK_SCRIPT = SCRIPTS_DIR / "hook-memory-retrieval.py"
CALIBRATION_JSON = REPO_ROOT / "docs" / "operations" / "memory-retrieval-calibration.json"

sys.path.insert(0, str(SCRIPTS_DIR))
from lib import bm25f  # noqa: E402


def _load_hook():
    spec = importlib.util.spec_from_file_location("hook_memory_retrieval", HOOK_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_hook()

POINTER_RE = re.compile(r"^\[memory-retrieval\] (\S+) — (.{0,160}) — (/\S.*)$")
TARGET_TERMS = "pelican migration schedule lighthouse harbour tides"


def _write_leaf(root: Path, rel: str, name: str, description: str, body: str = "plain body") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n", encoding="utf-8")
    return path


def _filler(root: Path, count: int = 40) -> None:
    for i in range(count):
        _write_leaf(root, f"filler/f{i}.md", f"filler-{i}", f"unrelatedword{i} nothingelse{i} otherthing{i}")


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "leaves"
    root.mkdir()
    monkeypatch.setenv(_mod.GLOBAL_ROOT_ENV, str(root))
    monkeypatch.setenv(_mod.STATE_DIR_ENV, str(tmp_path / "state"))
    monkeypatch.delenv(_mod.JUDGE_CHILD_ENV_VAR, raising=False)
    return root


def _payload(prompt: str, session: str = "s1", cwd: Path | None = None) -> dict:
    return {"prompt": prompt, "session_id": session, "cwd": str(cwd) if cwd else ""}


def _target(root: Path) -> Path:
    return _write_leaf(root, "target.md", "pelican-rota", TARGET_TERMS)


def test_hit_surfaces_matching_leaf_by_real_path(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    _filler(real)
    target = _target(real)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv(_mod.GLOBAL_ROOT_ENV, str(link))
    monkeypatch.setenv(_mod.STATE_DIR_ENV, str(tmp_path / "state"))
    monkeypatch.delenv(_mod.JUDGE_CHILD_ENV_VAR, raising=False)

    lines = _mod.run(_payload(TARGET_TERMS)).splitlines()

    assert len(lines) == 2
    assert lines[0] == _mod.HEADER
    match = POINTER_RE.match(lines[1])
    assert match and match.group(1) == "pelican-rota"
    assert match.group(3) == os.path.realpath(target)


def test_hit_ranks_through_the_bm25f_module_attribute(env, monkeypatch):
    _filler(env)
    _target(env)
    monkeypatch.setattr(bm25f, "bm25f_scores", lambda q, docs, *a, **k: [0.0] * len(docs))

    assert _mod.run(_payload(TARGET_TERMS)) == ""


def test_below_cutoff_prompt_is_not_surfaced(env):
    _filler(env)
    _target(env)
    prompt = "pelican nonexistentaaa nonexistentbbb"
    corpus = _mod.load_corpus([env])
    terms = _mod.query_terms(prompt)

    assert _mod.rank(terms, corpus, min_score=0.0), "the prompt shares a term, so only the cutoff can drop it"
    assert _mod.run(_payload(prompt)) == ""


@pytest.mark.parametrize("prompt", ["ok", "thanks", "yes do it", "", "a b c d e f"])
def test_short_prompt_never_reaches_the_scorer(env, monkeypatch, prompt):
    _filler(env)
    _target(env)

    def boom(*args, **kwargs):
        raise AssertionError("scorer called for a prompt below MIN_QUERY_TOKENS")

    monkeypatch.setattr(bm25f, "bm25f_scores", boom)

    assert _mod.run(_payload(prompt)) == ""


def test_dedupe_surfaces_a_leaf_once_per_session(env):
    _filler(env)
    _target(env)

    first = _mod.run(_payload(TARGET_TERMS, session="a"))
    again = _mod.run(_payload(TARGET_TERMS, session="a"))
    other = _mod.run(_payload(TARGET_TERMS, session="b"))

    assert first and other == first
    assert again == ""


def test_project_corpus_present_is_searched(env, tmp_path):
    _filler(env)
    project = tmp_path / "project"
    leaf = _write_leaf(project / ".claude" / "agent-memory", "local.md", "project-rota", TARGET_TERMS)

    lines = _mod.run(_payload(TARGET_TERMS, cwd=project)).splitlines()

    assert [POINTER_RE.match(ln).group(3) for ln in lines[1:]] == [os.path.realpath(leaf)]


def test_project_corpus_absent_leaves_the_global_corpus_working(env, tmp_path):
    _filler(env)
    target = _target(env)
    project = tmp_path / "project"
    project.mkdir()

    lines = _mod.run(_payload(TARGET_TERMS, cwd=project)).splitlines()

    assert [POINTER_RE.match(ln).group(3) for ln in lines[1:]] == [os.path.realpath(target)]


@pytest.mark.parametrize("stdin", ["not json", "", "[]", '{"prompt": 7}', "null"])
def test_malformed_stdin_exits_zero_with_empty_stdout(env, stdin):
    proc = subprocess.run([sys.executable, str(HOOK_SCRIPT)], input=stdin, text=True,
                          capture_output=True, env=os.environ.copy(), timeout=30)

    assert proc.returncode == 0
    assert proc.stdout == ""


def test_timing_at_the_real_corpus_size(env):
    for i in range(216):
        body = " ".join(f"word{(i * 7 + j) % 5000}" for j in range(1500))
        _write_leaf(env, f"experience/leaf{i}.md", f"synthetic-leaf-{i}", f"synthetic description {i}", body)
    _target(env)

    start = time.perf_counter()
    out = _mod.run(_payload(TARGET_TERMS))
    elapsed = time.perf_counter() - start

    assert out
    assert elapsed < 1.0, f"{elapsed:.2f}s in-process; the hook's budget is 1s including interpreter start"


def test_judge_child_gets_nothing(env, monkeypatch, capsys):
    _filler(env)
    _target(env)
    payload = json.dumps(_payload(TARGET_TERMS))

    monkeypatch.setenv(_mod.JUDGE_CHILD_ENV_VAR, "1")
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert _mod.main() == 0
    assert capsys.readouterr().out == ""

    monkeypatch.delenv(_mod.JUDGE_CHILD_ENV_VAR)
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert _mod.main() == 0
    assert _mod.HEADER in capsys.readouterr().out


def test_judge_child_env_name_is_the_one_host_llm_sets():
    from lib import host_llm

    assert _mod.JUDGE_CHILD_ENV_VAR == host_llm.JUDGE_CHILD_ENV_VAR


def test_calibration_json_matches_the_hook_constants():
    data = json.loads(CALIBRATION_JSON.read_text(encoding="utf-8"))

    assert data["min_score"] == _mod.MIN_SCORE
    assert data["body_prefix_chars"] == _mod.BODY_PREFIX_CHARS
    assert data["top_k"] == _mod.TOP_K
    assert data["sample_source"] == "memory-corpus-leave-one-out"
    assert data["n_positive_pairs"] >= 20
    assert "queries" not in data


def test_top_k_caps_pointers_and_keeps_the_line_format(env):
    _filler(env)
    for i in range(5):
        _write_leaf(env, f"many/m{i}.md", f"pelican-rota-{i}", TARGET_TERMS + " " + "x" * 300)

    lines = _mod.run(_payload(TARGET_TERMS)).splitlines()

    assert lines[0] == _mod.HEADER
    assert len(lines) == 1 + _mod.TOP_K
    for line in lines[1:]:
        assert POINTER_RE.match(line), line
        assert len(line.split(" — ")[1]) <= _mod.DESCRIPTION_MAX_CHARS


def test_recursive_corpus_reaches_subdirectories_and_skips_indexes(env):
    _filler(env)
    target = _write_leaf(env, "experience/deep/leaf.md", "deep-pelican-rota", TARGET_TERMS)
    _write_leaf(env, "MEMORY.md", "index", TARGET_TERMS)
    _write_leaf(env, "experience/MEMORY.md", "experience-index", TARGET_TERMS)

    lines = _mod.run(_payload(TARGET_TERMS)).splitlines()

    assert [POINTER_RE.match(ln).group(3) for ln in lines[1:]] == [os.path.realpath(target)]


def test_exhausted_load_budget_reads_no_further_leaves(env, monkeypatch):
    _filler(env, 10)
    monkeypatch.setattr(_mod, "LOAD_BUDGET_S", -1.0)

    assert _mod.load_corpus([env]) == []


def test_broken_lib_import_still_exits_zero_with_empty_stdout(tmp_path):
    scripts = tmp_path / "scripts"
    (scripts / "lib").mkdir(parents=True)
    (scripts / "hook-memory-retrieval.py").write_text(HOOK_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    (scripts / "lib" / "__init__.py").write_text("raise RuntimeError('broken lib')\n", encoding="utf-8")

    done = subprocess.run(
        [sys.executable, str(scripts / "hook-memory-retrieval.py")],
        input=json.dumps(_payload(TARGET_TERMS)),
        capture_output=True,
        text=True,
    )

    assert (done.returncode, done.stdout) == (0, "")


def test_run_raising_exits_zero_with_empty_stdout(env, monkeypatch, capsys):
    def boom(payload):
        raise RuntimeError("boom")

    monkeypatch.setattr(_mod, "run", boom)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_payload(TARGET_TERMS))))

    assert _mod.main() == 0
    assert capsys.readouterr().out == ""
