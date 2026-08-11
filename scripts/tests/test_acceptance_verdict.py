"""Defect 2's ACCEPTANCE half: control compares an in-progress RESULT with the stage's
own GOAL, at every stage, throughout execution; acceptance compares the delivered
PRODUCT with the ORDER, once, and the comparison is RECORDED (AcceptanceReview) rather
than inferred from "all stages passed". Before this stage the engine only had the
control half (per-stage judge/observation gate) — a session could reach resolution with
every stage green and nobody had compared the whole against what the customer ordered.

Ten cases, each pinned to the exact wording of stage 8's done_criterion:

  1. a measurable substantive stage refused a pass with no observation
  2. refused with an observation echoing subject.result
  3. passed with a genuine observation and a green check
  4. all stages passed but no AcceptanceReview blocked at resolution
  5. an AcceptanceReview omitting a declared requirement id blocked
  6. an AcceptanceReview carrying a negative verdict on any requirement blocked
  7. an AcceptanceReview whose author != [meta.order].customer_id refused at write time
  8. an unreachable judge -> AcceptanceReview + AcceptanceBypass -> resolution PASSES,
     bypass surfaced in verify-final's output
  9. a bypass written without an accompanying AcceptanceReview refused
  10. a stale plan_sha256 (accept, then replace the plan through approve) treated as
      absent

Five more pin the boundaries of those ten — each the negative or the whole-path form
the first ten leave to inference:

  11. a NON-substantive measurable pass still needs no observation (the control half's
      activation floor: defect 2 widened the gate within substantive sessions, it did
      not widen which sessions pay for it)
  12. the ordinary, judge-corroborated path end to end: accept with no --bypass lands a
      review, records NO bypass, and clears resolution — cases 4-7 all end in a
      blocker and case 8 only reaches resolution through a bypass, so without this one
      nothing pins that acceptance can be satisfied at all
  13. acceptance_active's scoping trio (small-change inactive, AGENTCTL_ACCEPTANCE=0
      disables, =1 forces on), the same trio stage_review/code_review each carry
  14. a verdictless accept refused on the ORDINARY path too, not just under --bypass:
      resolution's completeness check cannot catch it, since a review omits nothing
      when the order declares nothing
  15. an unreadable plan blocks resolution instead of passing vacuously, and refuses at
      write time rather than raising PlanError out of the CLI

Cases 1-3 and 11 exercise cmd_record_result's observation gate directly on a throwaway
in-memory EXECUTING session (no plan file needed — the gate reads only the stage and
the session's weight_class). Cases 4-10 and 12-15 need a real order-bearing plan on
disk, since gates.resolution_blockers re-reads [meta.order] fresh via load_plan rather
than trusting anything cached at write time (see AcceptanceReview's docstring)."""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from agentctl import cli, gates
from agentctl.dispatch import RunResult
from agentctl.state import (
    Actor,
    AcceptanceReview,
    Criterion,
    GateRecord,
    Means,
    Node,
    Outcome,
    Partition,
    RequirementVerdict,
    Route,
    SessionState,
    Stage,
    StageStatus,
    Subject,
    WeightClass,
)


def ns(**kw):
    return Namespace(**kw)


# --- stubbed cheap judges (the advisor.subprocess_runner injection point) ----

def judge_fail_open(argv, *, timeout=None):
    return RunResult(1, stdout="", stderr="boom")


def judge_yes(argv, *, timeout=None):
    return RunResult(0, stdout="YES\nconcrete and adequate", stderr="")


# --- cases 1-3: the per-stage control gate, on a throwaway in-memory session -

def _measurable_stage() -> Stage:
    return Stage(
        index=1, title="s1",
        subject=Subject(material="m", result="the expected result image"),
        means=Means(means="bash", method="run"),
        actor=Actor(executor="in_thread"),
        criterion=Criterion(criterion_type="measurable", done_criterion="c",
                            verify_command="true", expected_exit=0),
        outcome=Outcome(status=StageStatus.ACTIVE.value),
    )


def _exec_state(sid: str, weight: str = WeightClass.SUBSTANTIVE.value) -> SessionState:
    stage = _measurable_stage()
    s = SessionState(
        session_id=sid, task_id="t",
        weight_class=weight,
        route=Route.SPAWN.value if weight == WeightClass.SUBSTANTIVE.value else Route.IN_THREAD.value,
        node=Node.EXECUTING.value,
        approval=GateRecord("plan_approval", armed=True, passed=True),
        partition=Partition(m1=True, verdict="recommended"),
        stages=[stage],
    )
    s.current_stage = stage.index
    return s


def _record(store, sid, obs, runner=None):
    return cli.cmd_record_result(
        ns(session=sid, status="passed", actual="ran it", control=None, observation=obs),
        store=store, runner=runner,
    )


OBS_GENUINE = "ran `true`; exit 0, nothing printed — matches the declared image"


def test_case1_measurable_substantive_pass_refused_with_no_observation(store, monkeypatch):
    monkeypatch.delenv("AGENTCTL_STAGE_REVIEW", raising=False)
    store.save(_exec_state("c1"))
    d = _record(store, "c1", "", runner=judge_fail_open)
    assert d.ok is False
    assert d.action == "attest_observation"
    after = store.load("c1")
    assert after.stage(1).outcome.status == StageStatus.ACTIVE.value


def test_case2_measurable_substantive_pass_refused_echoing_result(store, monkeypatch):
    monkeypatch.delenv("AGENTCTL_STAGE_REVIEW", raising=False)
    store.save(_exec_state("c2"))
    d = _record(store, "c2", "the expected result image", runner=judge_fail_open)
    assert d.ok is False
    assert d.action == "attest_observation"
    assert "echoing" in d.detail
    after = store.load("c2")
    assert after.stage(1).outcome.status == StageStatus.ACTIVE.value


def test_case3_measurable_substantive_pass_with_genuine_observation_and_green_check(
    store, monkeypatch
):
    monkeypatch.delenv("AGENTCTL_STAGE_REVIEW", raising=False)
    store.save(_exec_state("c3"))
    d = _record(store, "c3", OBS_GENUINE, runner=judge_yes)
    assert d.ok is True
    after = store.load("c3")
    assert after.stage(1).outcome.status == StageStatus.PASSED.value
    assert after.stage(1).criterion.observation == OBS_GENUINE


# --- cases 4-10: the plan-level acceptance gate, against a real order table --

_ORDER = """
[meta.order]
customer_id = "user"
customer = "the position that posed the critique task"
functional_place = "the norm that governs an act of activity in this engine"

[[meta.order.requirements]]
id = "R1"
text = "control compares result with goal at every stage"

[[meta.order.requirements]]
id = "R2"
text = "acceptance compares product with order once, and is recorded"

[meta.order.coverage]
R1 = ["stage 1 verify_command"]
R2 = ["agentctl accept"]
"""

_FINAL_CHECK = '[[final_check]]\ncommand = "true"\nexpected_exit = 0\n'

_PLAN = """
[meta]
task_id = "acc"
goal = "prove the acceptance half of Defect 2"
done_criterion = "the ten cases in stage 8's done_criterion hold"
criterion_type = "measurable"
weight_class = "substantive"
external_research = "read the corpus audit; no prior art applies"

{order}{final_check}
[[stage]]
index = 1
title = "the stage under test"
executor = "in_thread"
expected_result_image = "the expected image the stage's observation must differ from"
criterion_type = "measurable"
done_criterion = "d1"
verify_command = "true"
material = "m1"
means = "bash"
method = "run"
conditions = "none"
preconditions = "none"
invariants = "none"
capability_required = "cap"
material_refs = ["scripts/agentctl/cli.py"]
knowledge_refs = ["scripts/agentctl/gates.py"]
knowledge = "where acceptance binds"
[stage.principle]
statement = "s"
source = "src"
derivation = "der"
confidence = "high"
refutation = "r"
"""


def _write_plan(path: Path, *, order: str = _ORDER) -> str:
    path.write_text(_PLAN.format(order=order, final_check=_FINAL_CHECK), encoding="utf-8")
    return str(path)


def _approved_state(sid: str, plan_path: str) -> SessionState:
    """A SessionState carrying a real order-bearing plan_path with the digest
    already stamped at its accepted seam (cli._stamp_accepted_plan_digest, the exact
    helper `approve` and `replan` both call) — the shape gates.resolution_blockers
    sees once a session reaches VERIFYING. Built directly rather than driven through
    the full submit/approve/partition/dispatch sequence, since only the acceptance
    half is under test here; that sequence is covered elsewhere (test_meta_order.py,
    test_cli_directives.py)."""
    stage = _measurable_stage()
    stage.outcome.status = StageStatus.PASSED.value
    s = SessionState(
        session_id=sid, task_id="t", weight_class=WeightClass.SUBSTANTIVE.value,
        route=Route.SPAWN.value, node=Node.VERIFYING.value,
        approval=GateRecord("plan_approval", armed=True, passed=True),
        partition=Partition(m1=True, verdict="recommended"),
        plan_path=plan_path, stages=[stage],
    )
    cli._stamp_accepted_plan_digest(s, plan_path)
    return s


def test_case4_all_stages_passed_no_review_blocks_at_resolution(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTCTL_ACCEPTANCE", raising=False)
    plan = _write_plan(tmp_path / "p4.toml")
    s = _approved_state("c4", plan)
    blockers = gates.resolution_blockers(s)
    assert blockers and "no AcceptanceReview recorded" in blockers[0]


def test_case5_review_omitting_a_declared_requirement_id_blocks(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTCTL_ACCEPTANCE", raising=False)
    plan = _write_plan(tmp_path / "p5.toml")
    s = _approved_state("c5", plan)
    s.acceptance_review = AcceptanceReview(
        author="user", plan_sha256=s.accepted_plan_digest,
        verdicts=[RequirementVerdict("R1", "pass")],
    )
    blockers = gates.resolution_blockers(s)
    assert blockers
    assert "omits declared requirement id" in blockers[0]
    assert "R2" in blockers[0]


def test_case6_review_with_a_negative_verdict_blocks(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTCTL_ACCEPTANCE", raising=False)
    plan = _write_plan(tmp_path / "p6.toml")
    s = _approved_state("c6", plan)
    s.acceptance_review = AcceptanceReview(
        author="user", plan_sha256=s.accepted_plan_digest,
        verdicts=[RequirementVerdict("R1", "pass"), RequirementVerdict("R2", "fail")],
    )
    blockers = gates.resolution_blockers(s)
    assert blockers
    assert "non-pass verdict" in blockers[0]
    assert "R2" in blockers[0]


def test_case7_author_mismatch_refused_at_write_time(store, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTCTL_ACCEPTANCE", raising=False)
    plan = _write_plan(tmp_path / "p7.toml")
    store.save(_approved_state("c7", plan))
    d = cli.cmd_accept(
        ns(session="c7", author="mallory", verdict=["R1|pass", "R2|pass"],
           note="", bypass=False, bypass_reason=""),
        store=store, runner=judge_yes,
    )
    assert d.ok is False
    assert "customer_id" in d.detail
    after = store.load("c7")
    assert after.acceptance_review is None


def test_case8_unreachable_judge_then_bypass_and_resolution_passes(
    store, tmp_path, monkeypatch
):
    monkeypatch.delenv("AGENTCTL_ACCEPTANCE", raising=False)
    plan = _write_plan(tmp_path / "p8.toml")
    store.save(_approved_state("c8", plan))

    # An unreachable judge refuses the write outright — no silent pass-through.
    d1 = cli.cmd_accept(
        ns(session="c8", author="user", verdict=["R1|pass", "R2|pass"],
           note="", bypass=False, bypass_reason=""),
        store=store, runner=judge_fail_open,
    )
    assert d1.ok is False
    assert store.load("c8").acceptance_review is None

    # Re-run with an explicit, reasoned bypass: the judge is never consulted, and
    # both an AcceptanceReview and an AcceptanceBypass land together.
    d2 = cli.cmd_accept(
        ns(session="c8", author="user", verdict=["R1|pass", "R2|pass"],
           note="", bypass=True,
           bypass_reason="judge endpoint unreachable; manually verified against the order"),
        store=store, runner=judge_fail_open,
    )
    assert d2.ok is True
    after = store.load("c8")
    assert after.acceptance_review is not None
    assert after.acceptance_bypass is not None
    assert after.acceptance_bypass.reason

    vf = cli.cmd_verify_final(ns(session="c8"), store=store, runner=judge_yes)
    assert vf.ok is True
    assert vf.data.get("acceptance_bypass")
    assert "bypass" in vf.detail.lower()


def test_case9_bypass_without_verdicts_refused(store, tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTCTL_ACCEPTANCE", raising=False)
    plan = _write_plan(tmp_path / "p9.toml")
    store.save(_approved_state("c9", plan))
    d = cli.cmd_accept(
        ns(session="c9", author="user", verdict=[], note="",
           bypass=True, bypass_reason="reason given but nothing to bypass onto"),
        store=store, runner=judge_fail_open,
    )
    assert d.ok is False
    assert "accompanying AcceptanceReview" in d.detail
    after = store.load("c9")
    assert after.acceptance_review is None
    assert after.acceptance_bypass is None


def test_case10_stale_digest_after_plan_replaced_through_approve_is_absent(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("AGENTCTL_ACCEPTANCE", raising=False)
    plan_v1 = _write_plan(tmp_path / "v1.toml")
    s = _approved_state("c10", plan_v1)
    s.acceptance_review = AcceptanceReview(
        author="user", plan_sha256=s.accepted_plan_digest,
        verdicts=[RequirementVerdict("R1", "pass"), RequirementVerdict("R2", "pass")],
    )
    # Sanity: fresh, complete, all-pass -> no blocker yet.
    assert gates.resolution_blockers(s) == []

    # Replace the plan's bytes at the SAME path and re-stamp the digest exactly as
    # `approve` (and `replan`) do at their seam — cli._stamp_accepted_plan_digest is
    # the one place either command names. The review, written against the old
    # bytes, now points at superseded content.
    path = Path(plan_v1)
    path.write_text(path.read_text(encoding="utf-8") + "\n# replanned\n", encoding="utf-8")
    cli._stamp_accepted_plan_digest(s, plan_v1)

    blockers = gates.resolution_blockers(s)
    assert blockers
    assert "stale" in blockers[0]
    assert "treated as absent" in blockers[0]


# --- cases 11-15: the boundaries of the ten above ----------------------------

def test_case11_non_substantive_measurable_pass_needs_no_observation(store, monkeypatch):
    """The control half's activation floor. Defect 2 widened the observation gate from
    acceptance_review stages to EVERY stage OF A SUBSTANTIVE SESSION; it did not widen
    which sessions pay for it. Case 1 is this test's mirror, and without the pair, a
    later change scoping the gate on stage shape alone would pass case 1 unchanged."""
    monkeypatch.delenv("AGENTCTL_STAGE_REVIEW", raising=False)
    store.save(_exec_state("c11", weight=WeightClass.SMALL_CHANGE.value))
    d = _record(store, "c11", "", runner=judge_yes)
    assert d.ok is True
    assert store.load("c11").stage(1).outcome.status == StageStatus.PASSED.value


def test_case12_ordinary_judge_corroborated_accept_clears_resolution(
    store, tmp_path, monkeypatch
):
    """The whole non-bypass path, end to end: a corroborated accept lands a review with
    no bypass beside it, and the resolution gate the review was written for clears."""
    monkeypatch.delenv("AGENTCTL_ACCEPTANCE", raising=False)
    plan = _write_plan(tmp_path / "p12.toml")
    store.save(_approved_state("c12", plan))

    # Blocked before the accept — the same session, one command apart.
    assert gates.resolution_blockers(store.load("c12"))

    d = cli.cmd_accept(
        ns(session="c12", author="user", verdict=["R1|pass", "R2|pass"],
           note="compared the delivered engine against both requirements of the order",
           bypass=False, bypass_reason=""),
        store=store, runner=judge_yes,
    )
    assert d.ok is True, d.detail

    after = store.load("c12")
    assert after.acceptance_review is not None
    assert after.acceptance_bypass is None  # the ordinary path records no bypass
    assert after.acceptance_review.plan_sha256 == after.accepted_plan_digest
    assert gates.resolution_blockers(after) == []


def _no_review(sid: str, tmp_path: Path, *, weight=WeightClass.SUBSTANTIVE.value):
    """An otherwise resolvable session (stage PASSED, plan fresh) whose ONLY outstanding
    blocker could be the missing acceptance — so `resolution_blockers` reads as the
    acceptance gate's consequence and nothing else's."""
    s = _approved_state(sid, _write_plan(tmp_path / f"{sid}.toml"))
    s.weight_class = weight
    return s


# Case 13 is the trio test_acceptance_review_gate.py and test_code_review.py each carry
# for their own gate, written the same way: the predicate AND the consequence, since a
# gate can read the env correctly and still not be wired to the predicate.

def test_case13a_inactive_on_small_change_is_vacuous(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTCTL_ACCEPTANCE", raising=False)
    s = _no_review("c13a", tmp_path, weight=WeightClass.SMALL_CHANGE.value)
    assert gates.acceptance_active(s) is False
    assert gates.resolution_blockers(s) == []


def test_case13b_force_off_env_makes_gate_vacuous(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCTL_ACCEPTANCE", "0")
    s = _no_review("c13b", tmp_path)  # substantive, no review recorded
    assert gates.acceptance_active(s) is False
    assert gates.resolution_blockers(s) == []


def test_case13c_force_on_env_activates_small_change(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCTL_ACCEPTANCE", "1")
    s = _no_review("c13c", tmp_path, weight=WeightClass.SMALL_CHANGE.value)
    assert gates.acceptance_active(s) is True
    blockers = gates.resolution_blockers(s)
    assert blockers and "no AcceptanceReview recorded" in blockers[0]


def test_case14_verdictless_accept_refused_on_the_ordinary_path_too(
    store, tmp_path, monkeypatch
):
    """Case 9's non-bypass twin. Resolution's completeness check cannot catch this one:
    a review omits no declared id when the order declares none, so an empty review would
    sail through a gate whose whole job is comparing the product with the order."""
    monkeypatch.delenv("AGENTCTL_ACCEPTANCE", raising=False)
    plan = _write_plan(tmp_path / "p14.toml")
    store.save(_approved_state("c14", plan))
    d = cli.cmd_accept(
        ns(session="c14", author="user", verdict=[], note="looks good to me",
           bypass=False, bypass_reason=""),
        store=store, runner=judge_yes,
    )
    assert d.ok is False
    assert "at least one --verdict" in d.detail
    after = store.load("c14")
    assert after.acceptance_review is None


def test_case15_unreadable_plan_blocks_resolution_and_refuses_accept(
    store, tmp_path, monkeypatch
):
    """The vacuous path: with the plan gone, [meta.order] declares nothing the gate can
    see, so completeness and verdict checks would both pass over an empty list. The gate
    refuses what it cannot check, and accept refuses to write against a plan it cannot
    read — as a Directive, not a PlanError out of the CLI."""
    monkeypatch.delenv("AGENTCTL_ACCEPTANCE", raising=False)
    plan = _write_plan(tmp_path / "p15.toml")
    s = _approved_state("c15", plan)
    s.acceptance_review = AcceptanceReview(
        author="user", plan_sha256=s.accepted_plan_digest,
        verdicts=[RequirementVerdict("R1", "pass"), RequirementVerdict("R2", "pass")],
    )
    assert gates.resolution_blockers(s) == []  # complete and fresh, with the plan present

    Path(plan).unlink()
    blockers = gates.resolution_blockers(s)
    assert blockers
    assert "cannot be read" in blockers[0]

    store.save(s)
    d = cli.cmd_accept(
        ns(session="c15", author="user", verdict=["R1|pass", "R2|pass"], note="",
           bypass=False, bypass_reason=""),
        store=store, runner=judge_yes,
    )
    assert d.ok is False
    assert "cannot read the plan" in d.detail
