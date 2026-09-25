"""Hermetic end-to-end rehearsal of the acceptance-review judge gate on a throwaway
session (own FileStateStore under tmp_path; the cheap judge stubbed via the runner
injection point advisor uses). Drives all four record-result outcomes and proves the
bypass ledger is surfaced by verify-final:

  1. passed with NO judge verdict (the judge CALL itself fails)  -> ADMITS unjudged
     and lands a fail_open JudgeBypass that verify-final prints (R4: fail-open in
     effect, not just in name — see test_convergence_r4_judge_after_check.py);
  2. a `revise` verdict                                          -> BLOCKED;
  3. a `pass` verdict bound to the observation sha256           -> ADMITS,
     and a one-character-different observation is then STALE     -> BLOCKED;
  4. a human `override` (reviewer + note)                        -> ADMITS and lands
     an override JudgeBypass that verify-final prints;
  5. the kill switch (AGENTCTL_STAGE_REVIEW=0) on a substantive session -> ADMITS
     unjudged and lands a killswitch JudgeBypass that verify-final prints.

Judge fails OPEN (no verdict on error) and the gate now honours that: only a genuine
`revise` verdict blocks; a judge call failure admits the pass via a recorded,
surfaced bypass. The rehearsal is the executable proof of that behaviour through the
real CLI recorders."""
from __future__ import annotations

import hashlib
from argparse import Namespace

import pytest

from agentctl import cli, gates
from agentctl.dispatch import RunResult
from agentctl.state import (
    Actor,
    Criterion,
    GateRecord,
    Means,
    Node,
    Outcome,
    Partition,
    Route,
    SessionState,
    Stage,
    StageStatus,
    Subject,
    WeightClass,
)


def ns(**kw):
    return Namespace(**kw)


# --- stubbed cheap judges (the advisor.subprocess_runner injection point) -----

def judge_fail_open(argv, *, timeout=None, stdin=""):
    # non-zero exit -> acceptance_judge returns (None, ...) -> no StageReview recorded.
    return RunResult(1, stdout="", stderr="boom")


def judge_no(argv, *, timeout=None, stdin=""):
    return RunResult(0, stdout="NO\nvague, a rephrase of the expected", stderr="")


def judge_yes(argv, *, timeout=None, stdin=""):
    return RunResult(0, stdout="YES\nconcrete and adequate", stderr="")


# --- a throwaway EXECUTING substantive session with one acceptance stage ------

def _exec_state(sid) -> SessionState:
    stage = _acceptance_stage()
    s = SessionState(
        session_id=sid, task_id="t",
        weight_class=WeightClass.SUBSTANTIVE.value, route=Route.SPAWN.value,
        node=Node.EXECUTING.value,
        approval=GateRecord("plan_approval", armed=True, passed=True),
        partition=Partition(m1=True, verdict="recommended"),
        stages=[stage],
    )
    s.current_stage = stage.index
    return s


def _acceptance_stage():
    return Stage(
        index=1, title="s1",
        subject=Subject(material="m", result="the expected image"),
        means=Means(means="Read", method="observe"),
        actor=Actor(executor="in_thread"),
        criterion=Criterion(criterion_type="acceptance_review", done_criterion="c"),
        outcome=Outcome(status=StageStatus.ACTIVE.value),
    )


def _record(store, sid, obs, runner):
    return cli.cmd_record_result(
        ns(session=sid, status="passed", actual="ok", control=None, observation=obs),
        store=store, runner=runner,
    )


OBS = "I ran the module against fixtures/three_rows.json and saw exactly 3 rows echoed"


# --- path 1: a judge call failure fails open -> admits via a recorded bypass -

def test_path1_no_verdict_fails_open_and_admits(store, monkeypatch):
    monkeypatch.delenv("AGENTCTL_STAGE_REVIEW", raising=False)
    store.save(_exec_state("p1"))
    d = _record(store, "p1", OBS, judge_fail_open)
    assert d.ok is True
    after = store.load("p1")
    assert after.stage(1).outcome.status == StageStatus.PASSED.value
    assert after.stage_reviews == []  # fail-open judge recorded no StageReview
    assert [b.kind for b in after.judge_bypassed] == ["fail_open"]

    vf = cli.cmd_verify_final(ns(session="p1"), store=store, runner=judge_fail_open)
    assert vf.data.get("judge_bypassed")
    assert vf.data["judge_bypassed"][0]["kind"] == "fail_open"
    assert "bypass" in vf.detail.lower()


# --- path 2: a revise verdict blocks ----------------------------------------

def test_path2_revise_blocks(store, monkeypatch):
    monkeypatch.delenv("AGENTCTL_STAGE_REVIEW", raising=False)
    store.save(_exec_state("p2"))
    d = _record(store, "p2", OBS, judge_no)
    assert d.ok is False
    assert d.action == "attest_observation"
    after = store.load("p2")
    assert after.stage(1).outcome.status == StageStatus.ACTIVE.value
    assert after.stage_reviews[-1].verdict == "revise"


# --- path 3: a pass bound to the observation admits; drift is stale ----------

def test_path3_pass_admits_then_drift_is_stale(store, monkeypatch):
    monkeypatch.delenv("AGENTCTL_STAGE_REVIEW", raising=False)
    store.save(_exec_state("p3"))
    d = _record(store, "p3", OBS, judge_yes)
    assert d.ok is True
    after = store.load("p3")
    assert after.stage(1).outcome.status == StageStatus.PASSED.value
    assert after.stage(1).criterion.observation == OBS
    review = after.stage_reviews[-1]
    assert review.verdict == "pass"
    assert review.observation_sha256 == hashlib.sha256(OBS.encode()).hexdigest()

    # A one-character-different observation is no longer bound by that verdict:
    # the pure gate recomputes the sha and rejects the drift.
    after.stage(1).criterion.observation = OBS + "!"
    stale = gates.acceptance_review_blockers(after, after.stage(1))
    assert stale and "stale" in stale[0]


# --- path 4: a human override admits and is recorded as a bypass -------------

def test_path4_override_admits_and_records_bypass(store, monkeypatch):
    monkeypatch.delenv("AGENTCTL_STAGE_REVIEW", raising=False)
    store.save(_exec_state("p4"))
    # Human authors the override, bound to the same observation.
    cli.cmd_stage_review(
        ns(session="p4", verdict="override", reviewer="fedor", concerns=[],
           note="judge stuck; manually accepted after live check", observation=OBS),
        store=store,
    )
    d = _record(store, "p4", OBS, judge_yes)  # judge cannot clobber a manual override
    assert d.ok is True
    after = store.load("p4")
    assert after.stage(1).outcome.status == StageStatus.PASSED.value
    assert [b.kind for b in after.judge_bypassed] == ["override"]
    assert after.judge_bypassed[0].reviewer == "fedor"
    assert after.judge_bypassed[0].note

    # verify-final surfaces the bypass verbatim rather than a clean bill.
    vf = cli.cmd_verify_final(ns(session="p4"), store=store, runner=judge_yes)
    assert vf.data.get("judge_bypassed")
    assert vf.data["judge_bypassed"][0]["kind"] == "override"
    assert "bypass" in vf.detail.lower()


# --- path 5: the kill switch admits unjudged and records a bypass ------------

def test_path5_killswitch_admits_unjudged_and_records_bypass(store, monkeypatch):
    monkeypatch.setenv("AGENTCTL_STAGE_REVIEW", "0")
    store.save(_exec_state("p5"))
    d = _record(store, "p5", OBS, judge_fail_open)  # runner never consulted (gate off)
    assert d.ok is True
    after = store.load("p5")
    assert after.stage(1).outcome.status == StageStatus.PASSED.value
    assert [b.kind for b in after.judge_bypassed] == ["killswitch"]

    vf = cli.cmd_verify_final(ns(session="p5"), store=store, runner=judge_fail_open)
    assert vf.data.get("judge_bypassed")
    assert vf.data["judge_bypassed"][0]["kind"] == "killswitch"
    assert "bypass" in vf.detail.lower()
