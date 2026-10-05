"""improvement-scan grounds path: a telemetry ground is judged against the board's open items.

Hermetic: the judge runner is injected, the experience search is stubbed, and every cached
verdict is written straight into a tmp verdict file.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

from agentctl import advisor

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

_SPEC = importlib.util.spec_from_file_location("improvement_scan", SCRIPTS_DIR / "improvement-scan.py")
scan = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = scan
_SPEC.loader.exec_module(scan)

GROUND = "opus inherited by default for a delegatable haiku-shaped task"
BOARD_GROUND = "delegation misses: a haiku-shaped job runs on the inherited opus model"


class _Runner:
    def __init__(self, answer: str = "YES"):
        self.answer = answer
        self.calls = []

    def __call__(self, argv, **kwargs):
        from agentctl.dispatch import RunResult

        self.calls.append(kwargs.get("stdin", ""))
        return RunResult(0, stdout=self.answer, stderr="")


def _salt() -> str:
    probe = _Runner()
    advisor.judge_same_difficulty("fixed ground one", "fixed ground two", probe)
    return "same-difficulty@" + hashlib.sha256(probe.calls[0].encode("utf-8")).hexdigest() + ":confirmed-yes"


def _key(a: str, b: str) -> str:
    joined = "\x00".join([_salt()] + sorted(" ".join(t.split()) for t in (a, b)))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _board() -> "scan.PriorBoard":
    item = scan.PriorBoardItem(
        classification="needs-work", score=1.0, rank=1, source_digest="d",
        title="t", functional_ground=BOARD_GROUND,
    )
    return scan.PriorBoard(schema=1, generated_at="2026-01-01T00:00:00+00:00", items={"CORE#9": item})


def _setup(monkeypatch, tmp_path: Path, verdicts: dict) -> list:
    cache = tmp_path / "verdicts.json"
    if verdicts:
        cache.write_text(json.dumps(verdicts), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_SAME_DIFFICULTY_VERDICTS", str(cache))
    monkeypatch.setenv("CLAUDE_SAME_DIFFICULTY_JUDGE_BUDGET_S", "300")
    monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
    searches: list = []

    def _search(keywords, scope="global"):
        searches.append(keywords)
        return True, False, ""

    monkeypatch.setattr(scan.shell, "search_experience", _search)
    return searches


def _ground():
    return [{"detector": "delegation-misses", "functional_ground": GROUND, "title": "t"}]


def test_paraphrased_board_ground_is_board_match(monkeypatch, tmp_path):
    searches = _setup(monkeypatch, tmp_path, {_key(GROUND, BOARD_GROUND): True})
    runner = _Runner()
    findings, log = scan.build_findings_from_grounds(_ground(), board=_board(), judge_runner=runner)
    assert [e["outcome"] for e in log] == ["board-match"]
    assert searches == []
    assert findings == []


def test_board_judge_no_falls_through(monkeypatch, tmp_path):
    searches = _setup(monkeypatch, tmp_path, {})
    runner = _Runner("NO")
    findings, log = scan.build_findings_from_grounds(_ground(), board=_board(), judge_runner=runner)
    assert len(runner.calls) == 1
    assert len(searches) == 1
    assert [e["outcome"] for e in log] == ["no-match"]
    assert len(findings) == 1


def test_unjudged_board_ground_reports_judge_unavailable(monkeypatch, tmp_path):
    searches = _setup(monkeypatch, tmp_path, {})
    monkeypatch.setenv("AGENTCTL_ADVISOR", "0")
    runner = _Runner("NO")
    findings, log = scan.build_findings_from_grounds(_ground(), board=_board(), judge_runner=runner)
    assert runner.calls == []
    assert len(searches) == 1
    assert [e["outcome"] for e in log] == ["judge-unavailable"]
    assert len(findings) == 1


def test_one_judge_budget_covers_every_ground(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, {})
    from lib.judge_budget import JudgeBudget

    now = [0.0]
    created = []

    def _budget():
        created.append(JudgeBudget(100, 1, clock=lambda: now[0]))
        return created[-1]

    class _SlowRunner(_Runner):
        def __call__(self, argv, **kwargs):
            now[0] += 100
            return super().__call__(argv, **kwargs)

    monkeypatch.setattr(scan.semantic_join, "env_budget", _budget)
    runner = _SlowRunner("NO")
    grounds = _ground() + [{
        "detector": "other", "title": "t",
        "functional_ground": "a haiku-shaped job on the inherited opus model wastes delegation",
    }]
    _findings, log = scan.build_findings_from_grounds(grounds, board=_board(), judge_runner=runner)
    assert len(created) == 1
    assert len(runner.calls) == 1
    assert [e["outcome"] for e in log] == ["no-match", "judge-unavailable"]
