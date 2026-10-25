"""Risk-shaped planning, phase A, stage 4: lazy elaboration at dispatch and the bet-raising replan.

A bound stage whose bet defers elaboration to launch is refused at dispatch until the fields
that elaboration calls for exist and its depth review stands. After a difficulty, a bound
replan raises the failed stage's bet or states why it stands, and owes that stage a
neighbours-depth review. An unbound session sees none of it.
"""
from __future__ import annotations

import json

import pytest

from agentctl import cli, gates, risk
from agentctl.config import RISK_PLANNING_ENV
from risk_planning_scenario import SESSION, Scenario, skeleton_text
from test_replan_autonomy_boundary import plan_text
from test_risk_bet import QUIET_BET, TWO_STAGES, bet_toml, freeze, stage_review, with_bets

LIKELY_BET = bet_toml(likelihood="medium")
RAISED_BET = bet_toml(likelihood="high", damage="medium")
KNOWLEDGE = {1: 'knowledge = "how the module is laid out"\n'}
STATED = "the failure was an environment fault, the stage's risk is as rated"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(RISK_PLANNING_ENV, "1")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")


@pytest.fixture
def sc(tmp_path):
    return Scenario(tmp_path)


def lazy_text(bet: str = LIKELY_BET, extras: dict[int, str] | None = None) -> str:
    return with_bets(TWO_STAGES, {1: bet, 2: QUIET_BET}, extras)


def launched(sc: Scenario, text: str | None = None) -> str:
    """An approved bound session whose stage 1 (an at-launch stage) is the active stage."""
    freeze(sc)
    path = sc.write(text or lazy_text(), "full.toml")
    assert sc.submit(path)["marker"] == "PLAN-READY"
    assert sc.eng.run("approve", session=SESSION, by="user")["ok"] is True
    sc.eng.execute(SESSION)
    return path


def dispatch(sc: Scenario) -> dict:
    return sc.eng.run("dispatch", session=SESSION, dry_run=True)


def test_dispatch_refuses_unelaborated_lazy_stage(sc):
    launched(sc)
    d = dispatch(sc)
    assert d["ok"] is False and "dispatch refused" in d["detail"]
    assert any("stage 1" in b and "'knowledge' is still missing" in b for b in d["data"]["blockers"])


def test_dispatch_refuses_missing_depth_review(sc):
    launched(sc, lazy_text(extras=KNOWLEDGE))
    d = dispatch(sc)
    assert d["ok"] is False
    assert d["data"]["blockers"] == [
        "stage 1: its risk bet calls for a stage-depth review before launch "
        "(no stage-scoped review is recorded) — run: plan-review --scope stage:1"
    ]


def test_dispatch_admits_after_elaboration_and_review(sc):
    path = launched(sc, lazy_text(extras=KNOWLEDGE))
    assert stage_review(sc, path, 1)["ok"] is True
    d = dispatch(sc)
    assert d["ok"] is True, d
    assert gates.lazy_stage_blockers(sc.state(), 1) == []


def test_a_quiet_stage_is_admitted_without_review(sc):
    launched(sc, with_bets(TWO_STAGES, {1: QUIET_BET, 2: QUIET_BET}, {1: 'verify_command = "true"\n'}))
    assert dispatch(sc)["ok"] is True


def failed_and_diagnosed(sc: Scenario, path: str) -> None:
    sc.eng.run("record_result", session=SESSION, status="failed", actual="boom")
    assert sc.state().node == "DIAGNOSING"
    sc.eng.diagnose(SESSION)


def corrected(sc: Scenario, bet: str, *, renormed: bool = True) -> str:
    """The corrected plan: stage 1 elaborated and re-rated. `renormed` also rewrites the
    stage's result image, which the difficulty's norm target asks the replan to change."""
    text = lazy_text(bet, KNOWLEDGE)
    if renormed:
        text = text.replace('expected_result_image = "', 'expected_result_image = "Revised: ', 1)
    return sc.write(text, "corrected.toml")


def test_replan_after_difficulty_needs_raised_bet(sc):
    failed_and_diagnosed(sc, launched(sc))
    d = sc.eng.run("replan", session=SESSION, plan=corrected(sc, LIKELY_BET))
    assert d["ok"] is False and d["action"] == "replan"
    assert d["data"]["blockers"] == [
        "stage 1 failed and its risk bet is not raised in the corrected plan — "
        "raise the bet or state why it stands with --reason"
    ]
    raised = sc.eng.run("replan", session=SESSION, plan=corrected(sc, RAISED_BET))
    assert raised["action"] == "plan_review", raised
    assert not any("risk bet is not raised" in b for b in raised["data"]["blockers"])


def test_replan_after_difficulty_accepts_stated_reason(sc):
    failed_and_diagnosed(sc, launched(sc))
    d = sc.eng.run("replan", session=SESSION, plan=corrected(sc, LIKELY_BET), reason=STATED)
    assert d["action"] == "plan_review", d
    assert not any("risk bet is not raised" in b for b in d["data"]["blockers"])


def test_failed_stage_owes_neighbours_review(sc):
    failed_and_diagnosed(sc, launched(sc))
    path = corrected(sc, RAISED_BET, renormed=False)
    state = sc.state()
    doc = gates.load_plan(path)
    owed = gates.bound_review_blockers(state, path, doc)
    assert any("stage 1" in b and "neighbours-depth review" in b for b in owed), owed
    assert not any("stage 2" in b for b in owed)
    state.difficulty = None
    assert gates.bound_review_blockers(state, path, doc) == []
    assert risk.effective(risk.parse_bet(doc.bets[1])).depth == risk.STAGE


def test_bet_raised_axes():
    base = risk.parse_bet({"difficulty": "d", "likelihood": "low", "damage": "low",
                           "detection": "loud", "recovery": "cheap"})
    def with_(**kw):
        return risk.parse_bet({"difficulty": "d", "likelihood": "low", "damage": "low",
                               "detection": "loud", "recovery": "cheap", **kw})
    assert not risk.bet_raised(base, base)
    for changed in (with_(likelihood="medium"), with_(damage="high"), with_(recovery="costly"),
                    with_(detection="silent"), with_(review="neighbours"),
                    with_(elaboration=["staging"])):
        assert risk.bet_raised(base, changed)
    assert not risk.bet_raised(with_(likelihood="high"), base)


def test_launch_elaboration_through_replan_owes_only_the_stage_review(sc):
    launched(sc)
    elaborated = corrected(sc, LIKELY_BET, renormed=False)
    applied = sc.eng.run("replan", session=SESSION, plan=elaborated)
    assert applied["ok"] is True and applied["detail"].startswith("refinement applied"), applied
    refused = dispatch(sc)
    assert refused["ok"] is False and "no stage-scoped review is recorded" in refused["detail"]
    assert stage_review(sc, elaborated, 1)["ok"] is True
    assert dispatch(sc)["ok"] is True


def test_unbound_dispatch_and_replan_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv(RISK_PLANNING_ENV)
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "0")
    sc = Scenario(tmp_path)
    plan = sc.write(plan_text())
    sc.eng.approve_as_user(SESSION, plan)
    sc.eng.execute(SESSION)
    assert sc.state().risk_planning is False
    assert gates.lazy_stage_blockers(sc.state(), 1) == []
    assert dispatch(sc)["ok"] is True
    failed_and_diagnosed(sc, plan)
    d = sc.eng.run("replan", session=SESSION, plan=sc.write(plan_text(revised=True)))
    assert "risk bet" not in str(d.get("data", {}).get("blockers", "")), d
    assert gates.failed_stage_indices(sc.state()) == {1}


def _lazy_rows(log):
    return [r for r in map(json.loads, log.read_text().splitlines()) if r["gate"] == "lazy_stage"] if log.exists() else []


def test_lazy_stage_gate_logged_only_when_bound(tmp_path, monkeypatch):
    log = tmp_path / "gate-log.jsonl"
    monkeypatch.setattr(cli, "GATE_LOG", log)
    monkeypatch.delenv(RISK_PLANNING_ENV)
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "0")
    sc = Scenario(tmp_path)
    sc.eng.approve_as_user(SESSION, sc.write(plan_text()))
    sc.eng.execute(SESSION)
    assert dispatch(sc)["ok"] is True
    assert _lazy_rows(log) == []


def test_lazy_stage_gate_logged_when_bound(sc, tmp_path, monkeypatch):
    log = tmp_path / "gate-log.jsonl"
    monkeypatch.setattr(cli, "GATE_LOG", log)
    launched(sc)
    assert dispatch(sc)["ok"] is False
    assert [r["passed"] for r in _lazy_rows(log)] == [False]
