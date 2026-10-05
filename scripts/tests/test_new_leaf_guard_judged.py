"""record-experience `new`: the fragmentation guard follows a judge, not wording overlap.

A model judge decides "same difficulty?" for the leaves nominated by word overlap; a
cached verdict is seeded straight into the verdict cache file, so no test runs a model.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentctl import advisor

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("record_experience", SCRIPTS_DIR / "record-experience.py")
rec = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rec)

LEAF_DIFFICULTY = "the resolution gate is skipped after the user says thanks"
REWORDED = "closing handshake bypassed once gratitude is expressed by the human gate"


class _CapturingRunner:
    def __init__(self):
        self.prompt = ""

    def __call__(self, argv, **kwargs):
        from agentctl.dispatch import RunResult

        self.prompt = kwargs.get("stdin", "")
        return RunResult(0, stdout="NO", stderr="")


def _salt() -> str:
    runner = _CapturingRunner()
    advisor.judge_same_difficulty("fixed ground one", "fixed ground two", runner)
    return "same-difficulty@" + hashlib.sha256(runner.prompt.encode("utf-8")).hexdigest()


def _key(a: str, b: str) -> str:
    joined = "\x00".join([_salt()] + sorted(" ".join(t.split()) for t in (a, b)))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _isolate(monkeypatch, tmp_path: Path, verdict: bool | None = None, *, pair=None) -> None:
    cache = tmp_path / "verdicts.json"
    monkeypatch.setenv("CLAUDE_SAME_DIFFICULTY_VERDICTS", str(cache))
    monkeypatch.setenv("CLAUDE_SAME_DIFFICULTY_JUDGE_BUDGET_S", "0")
    monkeypatch.setenv("AGENTCTL_ADVISOR", "0")
    if verdict is not None:
        cache.write_text(json.dumps({_key(*pair): verdict}), encoding="utf-8")


def _exp_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".claude" / "agent-memory" / "experience"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _leaf(path: Path, difficulty: str) -> str:
    desc = difficulty[:80]
    text = (
        f"---\nname: {path.stem}\ndescription: {desc}\ntype: reference\nschema: difficulty/v1\n"
        f'resolution_confirmed_by_user: "tester"\n---\n\n# Test Leaf\n\n'
        f"## Difficulty\n{difficulty}\n\n## Order & criterion\norder\n\n**Acceptance check:** check\n"
        f"\n## Contexts\n\n### 2026-01-01 — ctx-1\n- Where it arose: test\n- Working plan: p\n\n## Cost\nfree\n"
    )
    path.write_text(text, encoding="utf-8")
    return _ground_of(text)


def _ground_of(text: str) -> str:
    fm = rec.FRONTMATTER.match(text)
    desc = [ln for ln in fm.group(1).splitlines() if ln.startswith("description:")][0].split(":", 1)[1].strip()
    start, end = rec.section_span(text, "Difficulty")
    return desc + " " + text[start:end]


def _new(tmp_path: Path, slug: str, difficulty: str, justify_new=None) -> int:
    args = SimpleNamespace(
        scope="project", project_dir=str(tmp_path), date="2026-06-27", slug=slug,
        title="Test Title", description=f"desc: {difficulty[:60]}", confirmed_by="tester",
        difficulty=difficulty, order="order criterion", criterion="acceptance check",
        context_where="test env", plan="test plan", context_label="initial",
        plan_file=None, cost=None, self_critique=None, refs=[], justify_new=justify_new,
    )
    try:
        return rec.cmd_new(args)
    except SystemExit as exc:
        return 1 if isinstance(exc.code, str) else int(exc.code or 0)


def test_new_guard_follows_judge_not_overlap(tmp_path, monkeypatch):
    ground = _leaf(_exp_dir(tmp_path) / "existing.md", LEAF_DIFFICULTY)
    _isolate(monkeypatch, tmp_path, False, pair=(ground, LEAF_DIFFICULTY))
    rc = _new(tmp_path, "overlapping", LEAF_DIFFICULTY)
    assert rc == 0
    assert (_exp_dir(tmp_path) / "2026-06-27-overlapping.md").exists()


def test_new_guard_refuses_on_judged_yes(tmp_path, monkeypatch, capsys):
    ground = _leaf(_exp_dir(tmp_path) / "existing.md", LEAF_DIFFICULTY)
    _isolate(monkeypatch, tmp_path, True, pair=(ground, REWORDED))
    rc = _new(tmp_path, "reworded", REWORDED)
    assert rc != 0
    assert not (_exp_dir(tmp_path) / "2026-06-27-reworded.md").exists()


def test_new_guard_justify_new_overrides_judged_yes(tmp_path, monkeypatch):
    ground = _leaf(_exp_dir(tmp_path) / "existing.md", LEAF_DIFFICULTY)
    _isolate(monkeypatch, tmp_path, True, pair=(ground, REWORDED))
    rc = _new(tmp_path, "reworded", REWORDED, justify_new="distinct trigger")
    assert rc == 0
    assert (_exp_dir(tmp_path) / "2026-06-27-reworded.md").exists()


def test_new_guard_unjudged_creates_with_note(tmp_path, monkeypatch, capsys):
    _leaf(_exp_dir(tmp_path) / "existing.md", LEAF_DIFFICULTY)
    _isolate(monkeypatch, tmp_path)
    rc = _new(tmp_path, "unjudged", LEAF_DIFFICULTY)
    assert rc == 0
    assert (_exp_dir(tmp_path) / "2026-06-27-unjudged.md").exists()
    assert "existing.md" in capsys.readouterr().err
