"""Telemetry-ground dedup in improvement-scan.py: a lexical search proposes
candidate experience leaves, a model judge alone decides "same difficulty?".

Loaded by path like test_improvement_scan_telemetry.py (the module's filename
carries a dash). Every judge call goes through a stub runner; no test spawns a
real model.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))


def _load_scan():
    spec = importlib.util.spec_from_file_location(
        "improvement_scan", SCRIPTS_DIR / "improvement-scan.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


scan = _load_scan()

GROUND = "opus inherited by default for a delegatable haiku-shaped task"
LEAF_DESC = "delegation misses: a haiku-shaped job runs on the inherited opus model"


def _search_output(*hits: "tuple[str, str]") -> str:
    lines = ["analogous experience leafs (extend one instead of duplicating):"]
    for i, (name, desc) in enumerate(hits):
        lines.append(f"  [{100 - i:>3}] {name}\n        {desc}")
        lines.append(f"        extend --leaf /x/{name}")
    return "\n".join(lines)


class _StubJudge:
    """A runner double: answers are consumed in order; an Exception instance is
    raised, a string is returned as the model's stdout, 'TIMEOUT' simulates a
    timed-out call."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, argv, **kwargs):
        from agentctl.dispatch import RunResult

        self.calls.append(kwargs.get("stdin", ""))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if answer == "TIMEOUT":
            return RunResult(1, stdout="", stderr="advisor timed out after 1s", timed_out=True)
        return RunResult(0, stdout=answer, stderr="")


def _ground():
    return [{"detector": "delegation-misses", "functional_ground": GROUND, "title": "t"}]


@pytest.fixture
def judge_on(monkeypatch):
    monkeypatch.delenv("AGENTCTL_ADVISOR", raising=False)


def _stub_search(monkeypatch, output, ok=True, found=True):
    monkeypatch.setattr(
        scan.shell, "search_experience",
        lambda keywords, scope="global": (ok, found, output),
    )


def test_multiword_ground_is_not_search_failed():
    import improvement_scan_shell as shell

    ok, _found, output = shell.search_experience(GROUND.split(), scope="global")
    assert ok is True, output


def test_judge_yes_is_dedup_match(monkeypatch, judge_on):
    _stub_search(monkeypatch, _search_output(("leaf-a.md", LEAF_DESC)))
    judge = _StubJudge("YES")
    findings, log = scan.build_findings_from_grounds(_ground(), judge_runner=judge)
    assert findings == []
    assert [e["outcome"] for e in log] == ["dedup-match"]
    assert "leaf-a.md" in log[0]["detail"]
    assert len(judge.calls) == 1
    assert GROUND in judge.calls[0] and LEAF_DESC in judge.calls[0]


def test_judge_no_is_no_match_despite_full_overlap(monkeypatch, judge_on):
    _stub_search(monkeypatch, _search_output(("leaf-a.md", GROUND)))
    judge = _StubJudge("NO")
    findings, log = scan.build_findings_from_grounds(_ground(), judge_runner=judge)
    assert len(findings) == 1
    assert [e["outcome"] for e in log] == ["no-match"]


def test_first_yes_stops_the_walk_over_candidates(monkeypatch, judge_on):
    _stub_search(monkeypatch, _search_output(("a.md", "da"), ("b.md", "db"), ("c.md", "dc")))
    judge = _StubJudge("NO", "YES", "NO")
    _findings, log = scan.build_findings_from_grounds(_ground(), judge_runner=judge)
    assert log[0]["outcome"] == "dedup-match"
    assert "b.md" in log[0]["detail"]
    assert len(judge.calls) == 2


@pytest.mark.parametrize("answer", ["TIMEOUT", RuntimeError("boom"), "maybe"])
def test_judge_unavailable_keeps_finding(monkeypatch, judge_on, answer):
    _stub_search(monkeypatch, _search_output(("leaf-a.md", LEAF_DESC)))
    judge = _StubJudge(answer)
    findings, log = scan.build_findings_from_grounds(_ground(), judge_runner=judge)
    assert len(findings) == 1
    assert [e["outcome"] for e in log] == ["judge-unavailable"]


def test_unavailable_candidate_then_no_is_still_judge_unavailable(monkeypatch, judge_on):
    _stub_search(monkeypatch, _search_output(("a.md", "da"), ("b.md", "db")))
    judge = _StubJudge("TIMEOUT", "NO")
    findings, log = scan.build_findings_from_grounds(_ground(), judge_runner=judge)
    assert len(findings) == 1
    assert log[0]["outcome"] == "judge-unavailable"


def test_no_candidates_skips_judge(monkeypatch, judge_on):
    _stub_search(monkeypatch, "no analogous experience leaf found — record a NEW leaf", found=False)
    judge = _StubJudge()
    findings, log = scan.build_findings_from_grounds(_ground(), judge_runner=judge)
    assert len(findings) == 1
    assert [e["outcome"] for e in log] == ["no-match"]
    assert judge.calls == []


def test_search_subprocess_failure_stays_search_failed(monkeypatch, judge_on):
    _stub_search(monkeypatch, "record-experience search exited 1", ok=False, found=False)
    judge = _StubJudge()
    findings, log = scan.build_findings_from_grounds(_ground(), judge_runner=judge)
    assert len(findings) == 1
    assert log[0]["outcome"] == "search-failed"
    assert judge.calls == []


def test_killswitch_gives_judge_unavailable(monkeypatch):
    monkeypatch.setenv("AGENTCTL_ADVISOR", "0")
    _stub_search(monkeypatch, _search_output(("leaf-a.md", LEAF_DESC)))
    judge = _StubJudge("YES")
    findings, log = scan.build_findings_from_grounds(_ground(), judge_runner=judge)
    assert len(findings) == 1
    assert log[0]["outcome"] == "judge-unavailable"
    assert judge.calls == []


def test_summary_line_counts_judge_unavailable(monkeypatch, judge_on, tmp_path, capsys):
    import argparse
    import json

    _stub_search(monkeypatch, _search_output(("leaf-a.md", LEAF_DESC)))
    monkeypatch.setattr(scan, "_default_judge_runner", lambda: _StubJudge("TIMEOUT"))
    grounds = tmp_path / "grounds.json"
    grounds.write_text(json.dumps(_ground()), encoding="utf-8")
    args = argparse.Namespace(
        grounds=str(grounds), board=None, dry_run=True, store=str(tmp_path / "s.jsonl"),
    )
    assert scan._run_telemetry_grounds(args) == 0
    out = capsys.readouterr().out
    assert (
        "dedup outcomes: no-match=0 dedup-match=0 board-match=0 "
        "search-failed=0 judge-unavailable=1"
    ) in out


def test_summary_line_counts_every_outcome(monkeypatch, judge_on, tmp_path, capsys):
    import argparse
    import json

    _stub_search(monkeypatch, "no analogous experience leaf found", found=False)
    grounds = tmp_path / "grounds.json"
    grounds.write_text(json.dumps(_ground() + _ground()), encoding="utf-8")
    args = argparse.Namespace(
        grounds=str(grounds), board=None, dry_run=True, store=str(tmp_path / "s.jsonl"),
    )
    assert scan._run_telemetry_grounds(args) == 0
    assert "dedup outcomes: no-match=2 dedup-match=0 board-match=0 search-failed=0 judge-unavailable=0" in (
        capsys.readouterr().out
    )


def test_parse_search_hits_reads_rank_ordered_name_and_description():
    out = _search_output(("a.md", "first desc"), ("b.md", "second desc"))
    assert scan._parse_search_hits(out) == [("a.md", "first desc"), ("b.md", "second desc")]
    assert scan._parse_search_hits("no analogous experience leaf found") == []


def test_real_search_output_parses_to_candidates():
    import improvement_scan_shell as shell

    ok, found, output = shell.search_experience(["delegation", "haiku", "opus"], scope="global")
    assert ok and found
    hits = scan._parse_search_hits(output)
    assert hits and all(name.endswith(".md") for name, _ in hits)
