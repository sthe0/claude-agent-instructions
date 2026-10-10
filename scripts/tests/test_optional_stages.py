"""Optional stages (R8): a stage a customer may decline at approval, and what "settled" means.

Difficulty removed: a plan that carries adjacent debt (a backlog item the work happens to
touch) had two bad choices — fold it into the order, so the customer pays for it unasked, or
leave it out and lose it. An OPTIONAL stage is the third: it traces to a backlog issue
instead of an order requirement and `approve --skip-optional <n>` declines it. The cost of
that choice is a fifth stage status, SKIPPED, and every decision that used to read "all
stages PASSED" has to learn that a declined stage is done too.

THE UNIVERSAL CLAIM AND HOW IT IS DISCHARGED. "No site that asks whether the stages are
done compares one stage's status to PASSED" is a universal claim over the codebase, and a
test of a few sites would not carry it. `test_every_single_stage_passed_comparison_is_accounted_for`
walks every script under scripts/ (AST for comparisons, a line grep as an independent net)
and requires each hit to be on a declared allowlist with a one-line reason. The allowlist is
the artefact under review: a new hit fails the test until someone reads it and decides
whether it asks "is this stage done?" (route it through `state.is_settled`) or a narrower
question (allowlist it, with the reason). A stale entry fails too, so the list cannot rot.

What no test here claims: that an optional stage is worth declining, or that the backlog
issue it names exists. The loader checks the SHAPE of the reference only.
"""
from __future__ import annotations

import ast
import importlib.util
import re
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, effort, gates, plugins_tracker
from agentctl.plan import (
    PlanError,
    load_plan,
    plan_content_digest,
    verify_command_reachability_blockers,
)
from agentctl.render import render_plan_md, render_stage_brief
from agentctl.state import InvariantError, Node, StageStatus, is_settled
from agentctl.submission import _element_traceability_violations
from conftest import SUBSTANTIVE_ORDER

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SUBSTANTIVE_FIXTURE = FIXTURES / "plan_two_stage_substantive.toml"
ISSUE = "owner/repo#12"


def ns(**kw):
    return Namespace(**kw)


# --- plan text -------------------------------------------------------------------

_META = """\
[meta]
weight_class = "small_change"
task_id = "optional-stages"
goal = "exercise the optional-stage rules"
done_criterion = "every required stage passed"
criterion_type = "measurable"
"""


def _stage(index, *, depends=(), optional=None, issue=None, extra=""):
    lines = [
        "[[stage]]",
        f"index = {index}",
        f'title = "Stage {index}"',
        'executor = "spawn:developer"',
        f'expected_result_image = "stage {index} result exists"',
        'criterion_type = "measurable"',
        f'done_criterion = "stage {index} result is on disk"',
        f"depends_on = {list(depends)}",
        f'output_artifacts = ["out{index}.txt"]',
    ]
    if optional is not None:
        lines.append(f"optional = {optional}")
    if issue is not None:
        lines.append(f'backlog_issue = "{issue}"')
    return "\n".join(lines) + "\n" + extra


def _plan_text(*stages, prelude="", tail=""):
    return _META + prelude + "\n" + "\n".join(stages) + tail


def _opt(index, depends=(), issue=ISSUE):
    return _stage(index, depends=depends, optional="true", issue=issue)


def _load(tmp_path, text):
    path = tmp_path / "plan.toml"
    path.write_text(text, encoding="utf-8")
    return load_plan(str(path))


_FINAL_CHECK_LANDED = """
[[final_check]]
kind = "landed"
label = "trunk contains the delivered commit"

[final_check.landed]
target = "main"
remote = "origin"
delivered_stage = {stage}
"""


def _order(coverage_controls):
    return SUBSTANTIVE_ORDER.replace(
        'R1 = ["stage 1 verify_command"]',
        "R1 = [" + ", ".join(f'"{c}"' for c in coverage_controls) + "]",
    )


# --- the loader: accept / reject table ---------------------------------------------

ACCEPTED = {
    "no optional stage at all": lambda: _plan_text(_stage(1), _stage(2, depends=[1])),
    "one optional leaf": lambda: _plan_text(_stage(1), _opt(2, [1])),
    "two optional leaves": lambda: _plan_text(_stage(1), _opt(2, [1]), _opt(3, [1])),
    "an optional chain": lambda: _plan_text(_stage(1), _opt(2, [1]), _opt(3, [2])),
    "a bare issue number with an owner": lambda: _plan_text(
        _stage(1), _opt(2, [1], issue="the-org/some.repo-name#7")),
    "a repo-less issue reference": lambda: _plan_text(_stage(1), _opt(2, [1], issue="repo#7")),
    "coverage with one control on a required stage": lambda: _plan_text(
        _stage(1, extra='verify_command = "true"\n'), _opt(2, [1]),
        prelude=_order(["stage 1 verify_command", "stage 2 verify_command"])),
}

REJECTED = {
    "three optional stages": (
        lambda: _plan_text(_stage(1), _opt(2, [1]), _opt(3, [1]), _opt(4, [1])),
        r"R8/O1.*at most 2",
    ),
    "optional without a backlog_issue": (
        lambda: _plan_text(_stage(1), _stage(2, depends=[1], optional="true")),
        r"optional stage has no backlog_issue \(R8/O2\)",
    ),
    "backlog_issue on a required stage": (
        lambda: _plan_text(_stage(1), _stage(2, depends=[1], issue=ISSUE)),
        r"backlog_issue is set but the stage is not optional \(R8/O2\)",
    ),
    "a required stage depending on an optional one": (
        lambda: _plan_text(_stage(1), _opt(2, [1]), _stage(3, depends=[2])),
        r"non-optional stage depends on optional stage\(s\) \[2\] \(R8/O3\)",
    ),
    "every stage optional": (
        lambda: _plan_text(_opt(1), _opt(2, [1])),
        r"R8/O4.*every stage is optional",
    ),
    "a backlog_issue that is not an issue reference": (
        lambda: _plan_text(_stage(1), _opt(2, [1], issue="fix the thing")),
        r"backlog_issue.*<repo>#<n>",
    ),
    "optional that is not a boolean": (
        lambda: _plan_text(_stage(1), _stage(2, depends=[1], optional='"yes"', issue=ISSUE)),
        r"optional must be a boolean",
    ),
    "a requirement covered only by an optional stage": (
        lambda: _plan_text(
            _stage(1), _stage(2, depends=[1], optional="true", issue=ISSUE,
                              extra='verify_command = "true"\n'),
            prelude=_order(["stage 2 verify_command"])),
        r"requirement 'R1' \(R8/O5\)",
    ),
    "a requirement whose only landed assertion is on an optional stage": (
        lambda: _plan_text(
            _stage(1), _opt(2, [1]),
            prelude=_order(["stage 2 landed assertion"])),
        r"requirement 'R1' \(R8/O5\)",
    ),
    "a final_check resting on an optional stage": (
        lambda: _plan_text(_stage(1), _opt(2, [1]), tail=_FINAL_CHECK_LANDED.format(stage=2)),
        r"final_check 1 \(R8/O5\).*optional stage 2",
    ),
}


@pytest.mark.parametrize("case", sorted(ACCEPTED))
def test_loader_accepts(case, tmp_path):
    doc = _load(tmp_path, ACCEPTED[case]())
    declared = [s.index for s in doc.stages if s.optional]
    assert all(s.backlog_issue for s in doc.stages if s.optional)
    assert len(declared) <= 2


@pytest.mark.parametrize("case", sorted(REJECTED))
def test_loader_rejects(case, tmp_path):
    build, message = REJECTED[case]
    with pytest.raises(PlanError, match=message):
        _load(tmp_path, build())


def test_a_plan_with_no_optional_stage_reads_the_new_fields_as_defaults(tmp_path):
    doc = _load(tmp_path, ACCEPTED["no optional stage at all"]())
    assert [(s.optional, s.backlog_issue) for s in doc.stages] == [(False, None), (False, None)]


def test_the_optional_marker_moves_the_plan_digest(tmp_path):
    plain = _load(tmp_path, _plan_text(_stage(1), _stage(2, depends=[1])))
    marked = _load(tmp_path, _plan_text(_stage(1), _opt(2, [1])))
    assert plan_content_digest(plain) != plan_content_digest(marked)


def test_the_backlog_issue_moves_the_plan_digest(tmp_path):
    first = _load(tmp_path, _plan_text(_stage(1), _opt(2, [1], issue="owner/repo#1")))
    second = _load(tmp_path, _plan_text(_stage(1), _opt(2, [1], issue="owner/repo#2")))
    assert plan_content_digest(first) != plan_content_digest(second)


# --- the consumers of "settled" ----------------------------------------------------

@pytest.fixture
def optional_plan(tmp_path):
    """The three-stage substantive fixture with its last stage optional."""
    text = SUBSTANTIVE_FIXTURE.read_text(encoding="utf-8")
    text += f'optional = true\nbacklog_issue = "{ISSUE}"\n'
    path = tmp_path / "plan_optional.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _start_to_plan_ready(store, sid, plan_path):
    cli.cmd_start(ns(session=sid, task="demo", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    return cli.cmd_submit_plan(ns(session=sid, plan=str(plan_path)), store=store)


def _approve(store, sid, *, skip=(), by="user"):
    return cli.cmd_approve(ns(session=sid, by=by, skip_optional=list(skip)), store=store)


def _partition(store, sid):
    return cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                                m3_severe=False, m4_severe=False), store=store)


def _pass_next(store, sid, observation):
    cli.cmd_next_stage(ns(session=sid), store=store)
    return cli.cmd_record_result(
        ns(session=sid, status="passed", actual="ok", control="reviewed: ok",
           observation=observation), store=store)


def _executing(store, sid, plan_path, *, skip=()):
    _start_to_plan_ready(store, sid, plan_path)
    d = _approve(store, sid, skip=skip)
    assert d.ok is True, d.detail
    _partition(store, sid)
    return store.load(sid)


def test_approve_with_skip_marks_the_stage_skipped_and_logs_the_decline(store, optional_plan):
    sid = "skip-1"
    state = _executing(store, sid, optional_plan, skip=[3])
    assert [s.outcome.status for s in state.stages] == ["PENDING", "PENDING", "SKIPPED"]
    assert "declined at approval" in state.stage(3).outcome.actual
    declines = [h for h in state.history if h.get("event") == "skip_optional"]
    assert len(declines) == 1 and declines[0]["stages"] == [3] and declines[0]["by"] == "user"


def test_approve_without_skip_keeps_the_optional_stage_in_the_plan(store, optional_plan):
    state = _executing(store, "skip-0", optional_plan)
    assert [s.outcome.status for s in state.stages] == ["PENDING"] * 3
    assert state.stage(3).optional is True and state.stage(3).backlog_issue == ISSUE


@pytest.mark.parametrize(
    ("skip", "by", "message"),
    [
        ([1], "user", "not optional"),
        ([9], "user", "stage 9 does not exist"),
        ([3], "agent", "customer"),
    ],
    ids=["a required stage", "an unknown stage", "by the agent"],
)
def test_approve_refuses_a_skip_it_cannot_honour(store, optional_plan, skip, by, message):
    sid = "skip-refused"
    _start_to_plan_ready(store, sid, optional_plan)
    d = _approve(store, sid, skip=skip, by=by)
    assert d.ok is False and d.action == "fix_plan"
    assert any(message in p for p in d.data["problems"])
    state = store.load(sid)
    assert state.node == Node.PLAN_READY.value
    assert all(s.outcome.status == "PENDING" for s in state.stages)


def test_approve_refuses_declining_a_stage_a_kept_stage_depends_on(store, tmp_path):
    chain = _plan_text(_stage(1), _opt(2, [1]), _opt(3, [2]))
    path = tmp_path / "chain.toml"
    path.write_text(chain, encoding="utf-8")
    sid = "skip-stranded"
    _start_to_plan_ready(store, sid, path)
    d = _approve(store, sid, skip=[2])
    assert d.ok is False and d.action == "fix_plan"
    assert any("stage 3 depends on declined stage(s) [2]" in p for p in d.data["problems"])
    assert _approve(store, sid, skip=[2, 3]).ok is True
    assert [s.outcome.status for s in store.load(sid).stages] == ["PENDING", "SKIPPED", "SKIPPED"]


def test_dispatch_never_picks_a_skipped_stage(store, optional_plan, fixtures_dir):
    from conftest import STAGE_OBSERVATIONS
    sid = "skip-dispatch"
    state = _executing(store, sid, optional_plan, skip=[3])
    assert [s.index for s in state.ready_stages()] == [1]

    started = []
    for observation in STAGE_OBSERVATIONS[:2]:
        d = cli.cmd_next_stage(ns(session=sid), store=store)
        started.append(store.load(sid).current_stage)
        assert d.ok is True
        cli.cmd_record_result(
            ns(session=sid, status="passed", actual="ok", control="reviewed: ok",
               observation=observation), store=store)

    after = store.load(sid)
    assert started == [1, 2]
    assert after.stage(3).outcome.status == StageStatus.SKIPPED.value
    assert after.ready_stages() == []
    assert after.node == Node.VERIFYING.value


def test_a_skipped_stage_resolves_the_plan(store, optional_plan):
    from conftest import STAGE_OBSERVATIONS
    sid = "skip-resolve"
    _executing(store, sid, optional_plan, skip=[3])
    for observation in STAGE_OBSERVATIONS[:2]:
        _pass_next(store, sid, observation)
    d = cli.cmd_verify_final(ns(session=sid), store=store)
    assert d.ok is True, d.detail
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="searched"), store=store)
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="recorded"), store=store)
    d = cli.cmd_resolve(ns(session=sid, by="user", quality=5, quality_by="user-confirmed",
                           quality_note=None), store=store)
    assert d.ok is True, d.detail
    assert store.load(sid).node == Node.RESOLVED.value


def _to_resolution(store, sid, plan_path, *, skip=()):
    from conftest import STAGE_OBSERVATIONS
    _executing(store, sid, plan_path, skip=skip)
    for observation in STAGE_OBSERVATIONS[:2]:
        _pass_next(store, sid, observation)
    d = cli.cmd_verify_final(ns(session=sid), store=store)
    assert d.ok is True, d.detail
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="searched"), store=store)
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="recorded"), store=store)
    return store.load(sid)


def _work_the_difficulty(store, sid):
    cli.cmd_declare(ns(session=sid, expected="e", actual="a", mismatch="m"), store=store)
    cli.cmd_investigate(ns(session=sid, localized_expectation="le", localized_actual="la",
                           hypotheses=["h1", "h2"]), store=store)
    cli.cmd_critique(ns(session=sid, functional_ground="fg", replanning_task="rt",
                        failure_address="нормативное"), store=store)
    cli.cmd_normalize(ns(session=sid, factor="reproducible cause", level="note"), store=store)


def test_reject_without_a_stage_never_reopens_a_declined_stage(store, optional_plan, tmp_path):
    sid = "reject-skipped"
    state = _to_resolution(store, sid, optional_plan, skip=[3])
    assert state.node == Node.RESOLUTION.value

    d = cli.cmd_reject(ns(session=sid, reason="not what was asked", stage=None), store=store)
    assert d.data["rejected_stages"] == [2]
    state = store.load(sid)
    assert [s.outcome.status for s in state.stages] == ["PASSED", "FAILED", "SKIPPED"]

    _work_the_difficulty(store, sid)
    refined = tmp_path / "refined.toml"
    refined.write_text(
        optional_plan.read_text(encoding="utf-8").replace('"Add tests"', '"Add the tests"'),
        encoding="utf-8")
    d = cli.cmd_replan(ns(session=sid, plan=str(refined)), store=store)
    assert d.ok is True, d.detail

    state = store.load(sid)
    assert state.stage(3).outcome.status == StageStatus.SKIPPED.value
    assert [s.index for s in state.ready_stages()] == [2]
    cli.cmd_next_stage(ns(session=sid), store=store)
    assert store.load(sid).current_stage == 2


def test_reject_refuses_to_name_a_declined_stage(store, optional_plan):
    sid = "reject-named-skipped"
    _to_resolution(store, sid, optional_plan, skip=[3])
    d = cli.cmd_reject(ns(session=sid, reason="no", stage=["3"]), store=store)
    assert d.ok is False and "SKIPPED" in d.detail
    state = store.load(sid)
    assert state.node == Node.RESOLUTION.value
    assert state.stage(3).outcome.status == StageStatus.SKIPPED.value


def test_a_replan_that_strands_a_stage_on_a_declined_one_is_refused_at_approve(store, tmp_path):
    first = tmp_path / "first.toml"
    first.write_text(_plan_text(_stage(1), _opt(2, [1]), _opt(3, [1])), encoding="utf-8")
    sid = "strand-replan"
    _executing(store, sid, first, skip=[2])
    cli.cmd_next_stage(ns(session=sid), store=store)

    second = tmp_path / "second.toml"
    second.write_text(_plan_text(_stage(1), _opt(2, [1]), _opt(3, [2])), encoding="utf-8")
    d = cli.cmd_replan(ns(session=sid, plan=str(second)), store=store)
    assert d.marker == "PLAN-READY"
    assert store.load(sid).stage(2).outcome.status == StageStatus.SKIPPED.value

    d = _approve(store, sid)
    assert d.ok is False and d.action == "fix_plan"
    assert any("stage 3 depends on declined stage(s) [2]" in p for p in d.data["problems"])

    assert _approve(store, sid, skip=[3]).ok is True
    assert [s.outcome.status for s in store.load(sid).stages] == ["PENDING", "SKIPPED", "SKIPPED"]


def test_a_changed_declined_stage_is_offered_again_on_both_carry_paths(
        store, optional_plan, tmp_path):
    text = optional_plan.read_text(encoding="utf-8")
    sid = "carry-rule"
    _executing(store, sid, optional_plan, skip=[3])
    cli.cmd_next_stage(ns(session=sid), store=store)

    bigger = tmp_path / "bigger.toml"
    bigger.write_text(text + (
        '\n[[stage]]\nindex = 4\ntitle = "Document"\nexecutor = "spawn:developer"\n'
        'expected_result_image = "docs exist"\ncriterion_type = "measurable"\n'
        'done_criterion = "docs on disk"\ndepends_on = [2]\noutput_artifacts = ["docs.md"]\n'),
        encoding="utf-8")
    d = cli.cmd_replan(ns(session=sid, plan=str(bigger)), store=store)
    assert d.marker == "PLAN-READY"
    assert store.load(sid).stage(3).outcome.status == StageStatus.SKIPPED.value  # unchanged: kept

    bigger.write_text(bigger.read_text(encoding="utf-8").replace(
        "CI config references test_mod", "CI config references test_mod and lint"),
        encoding="utf-8")
    d = _approve(store, sid)
    assert d.ok is True, d.detail
    state = store.load(sid)
    assert state.stage(3).optional is True
    assert state.stage(3).outcome.status == StageStatus.PENDING.value  # changed: offered again


def test_push_subplan_refuses_a_declined_originating_stage(store, optional_plan, tmp_path):
    sid = "push-skipped"
    _executing(store, sid, optional_plan, skip=[3])
    cli.cmd_next_stage(ns(session=sid), store=store)
    d = cli.cmd_push_subplan(
        ns(session=sid, plan=str(tmp_path / "child.toml"), task="child-task",
           originating_stage=3), store=store)
    assert d.ok is False and "SKIPPED" in d.detail
    state = store.load(sid)
    assert state.plan_stack == [] and state.node == Node.EXECUTING.value
    assert state.stage(3).outcome.status == StageStatus.SKIPPED.value


def test_popping_a_subplan_never_revives_a_declined_originating_stage(
        store, optional_plan, tmp_path):
    from agentctl.state import Criterion, Means, Outcome, Stage, Subject, Actor, GateRecord
    sid = "pop-skipped"
    _executing(store, sid, optional_plan, skip=[3])
    cli.cmd_next_stage(ns(session=sid), store=store)
    d = cli.cmd_push_subplan(
        ns(session=sid, plan=str(tmp_path / "child.toml"), task="child-task",
           originating_stage=1), store=store)
    assert d.ok is True, d.detail
    state = store.load(sid)
    # a frame that already holds the originating stage as declined (a crafted or older
    # frame): the pop guard is the second line behind the push refusal
    state.plan_stack[-1].stages[0].outcome.status = StageStatus.SKIPPED.value
    state.stages = [Stage(
        index=1, title="child", subject=Subject(material="m", result="r"),
        means=Means(means="Edit", method="do"), actor=Actor(executor="in_thread"),
        criterion=Criterion(criterion_type="measurable", done_criterion="done"),
        outcome=Outcome(status=StageStatus.PASSED.value))]
    state.resolution = GateRecord("resolution", armed=True, passed=True, by="tester")
    state.node = Node.RESOLVED.value
    state.current_stage = None
    store.save(state)

    cli.cmd_pop_subplan(ns(session=sid), store=store)
    assert store.load(sid).stage(1).outcome.status == StageStatus.SKIPPED.value


def test_an_optional_stage_not_declined_still_has_to_pass(store, optional_plan):
    from conftest import STAGE_OBSERVATIONS
    sid = "kept-optional"
    _executing(store, sid, optional_plan)
    for observation in STAGE_OBSERVATIONS[:2]:
        _pass_next(store, sid, observation)
    state = store.load(sid)
    assert state.all_stages_settled() is False
    assert [s.index for s in state.ready_stages()] == [3]
    assert gates.resolution_blockers(state)[0].startswith("stages not PASSED: [3]")


def test_resolution_gate_counts_a_skipped_stage_as_settled(store, optional_plan):
    state = _executing(store, "gate", optional_plan, skip=[3])
    state.stage(1).outcome.status = "PASSED"
    state.stage(2).outcome.status = "PASSED"
    assert not any(b.startswith("stages not PASSED") for b in gates.resolution_blockers(state))
    state.stage(2).outcome.status = "PENDING"
    assert any("stages not PASSED: [2]" in b for b in gates.resolution_blockers(state))


def test_resolved_invariant_accepts_skipped_and_refuses_pending(store, optional_plan):
    state = _executing(store, "inv", optional_plan, skip=[3])
    state.stage(1).outcome.status = "PASSED"
    state.stage(2).outcome.status = "PASSED"
    state.resolution.passed = True
    state.node = Node.RESOLVED.value
    state.check_invariants()
    state.stage(2).outcome.status = "PENDING"
    with pytest.raises(InvariantError):
        state.check_invariants()


def test_the_settled_helper_is_exactly_passed_or_skipped():
    seen = {}
    for status in StageStatus:
        seen[status.value] = is_settled(Namespace(outcome=Namespace(status=status.value)))
    assert seen == {"PENDING": False, "ACTIVE": False, "PASSED": True, "FAILED": False,
                    "SKIPPED": True}


def test_the_effort_estimate_does_not_price_a_declined_stage(store, optional_plan, fixtures_dir):
    kept = _executing(store, "eff-kept", optional_plan)
    declined = _executing(store, "eff-declined", optional_plan, skip=[3])
    two_stage = _executing(store, "eff-two", fixtures_dir / "plan_two_stage.toml")

    assert effort.estimate(declined) == effort.estimate(two_stage)
    assert effort.estimate(declined)[effort.SCALE_SPEND] < effort.estimate(kept)[effort.SCALE_SPEND]
    assert (effort.estimate(declined)[effort.SCALE_WALL_CLOCK]
            < effort.estimate(kept)[effort.SCALE_WALL_CLOCK])


def test_a_declined_stage_owes_no_ticket_progress_entry(store, optional_plan):
    state = _executing(store, "tracker", optional_plan, skip=[3])
    state.stage(3).output_artifacts = ["out3.txt"]
    assert plugins_tracker._passed_artifact_stages(state) == []
    state.stage(3).outcome.status = "PASSED"
    assert [s.index for s in plugins_tracker._passed_artifact_stages(state)] == [3]


def test_the_plan_render_marks_the_optional_stage(tmp_path, optional_plan):
    doc = load_plan(str(optional_plan))
    full = render_plan_md(doc)
    assert "Optional" in full and ISSUE in full and "--skip-optional 3" in full
    assert "Optional" not in render_stage_brief(doc, 1)
    assert ISSUE in render_stage_brief(doc, 3)
    plain = render_plan_md(load_plan(str(SUBSTANTIVE_FIXTURE)))
    assert "Optional" not in plain


def test_an_optional_stage_is_exempt_from_requirement_traceability(tmp_path):
    prelude = SUBSTANTIVE_ORDER.replace(
        "functional_place =", "requires_traceability = true\nfunctional_place =")
    plain = _load(tmp_path, _plan_text(_stage(1), _stage(2, depends=[1]), prelude=prelude))
    optional = _load(tmp_path, _plan_text(_stage(1), _opt(2, [1]), prelude=prelude))
    assert [v.split()[1] for v in _element_traceability_violations(plain)] == ["1", "2"]
    assert [v.split()[1] for v in _element_traceability_violations(optional)] == ["1"]


def test_reachability_counts_only_non_optional_producers(tmp_path):
    consumer = _stage(1, extra='verify_command = "test -f gen/made.txt"\n')

    def blockers(producer):
        doc = _load(tmp_path, _plan_text(consumer, producer))
        return verify_command_reachability_blockers(doc.stages, doc.meta.final_check, str(tmp_path))

    required_producer = _stage(2, depends=[1]).replace("out2.txt", "gen/made.txt")
    optional_producer = _opt(2, [1]).replace("out2.txt", "gen/made.txt")
    assert blockers(required_producer) == []
    assert any("gen/made.txt" in b for b in blockers(optional_producer))


def test_an_optional_stages_own_control_may_rest_on_its_own_output(tmp_path):
    own = _opt(2, [1]).replace("out2.txt", "gen/made.txt") + 'verify_command = "test -f gen/made.txt"\n'
    doc = _load(tmp_path, _plan_text(_stage(1), own))
    assert verify_command_reachability_blockers(doc.stages, doc.meta.final_check, str(tmp_path)) == []


def test_the_coverage_check_script_reports_the_optional_rule(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "check_order_coverage", SCRIPTS_DIR / "check-order-coverage.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    doc = _load(tmp_path, _plan_text(
        _stage(1), _stage(2, depends=[1], extra='verify_command = "true"\n'),
        prelude=_order(["stage 2 verify_command"])))
    assert module.coverage_violations(doc) == []
    # the loader already refuses the optional form; the script's own check is the second
    # net for a caller holding a hand-built PlanDoc
    doc.stages[1].optional = True
    doc.stages[1].backlog_issue = ISSUE
    assert any("R8/O5" in v for v in module.coverage_violations(doc))


# --- the enumerator: no unaccounted single-stage PASSED comparison -------------------

_COMPARE_OPS = (ast.Eq, ast.NotEq, ast.In, ast.NotIn, ast.Is, ast.IsNot)
_GREP = re.compile(
    r"""(==|!=|\bin\b|\bnot in\b|\bis\b).*["']passed["']|["']passed["'].*(==|!=)""",
    re.IGNORECASE,
)


def _is_passed(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and node.value.upper() == "PASSED"
    if isinstance(node, ast.Attribute):
        if node.attr == "PASSED":
            return True
        if node.attr == "value":
            return _is_passed(node.value)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return any(_is_passed(e) for e in node.elts)
    return False


def _owner_resolver(tree: ast.AST):
    """Map a line number to the qualified name of its innermost enclosing function/class."""
    spans: "list[tuple[int, int, str]]" = []

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                spans.append((child.lineno, child.end_lineno or child.lineno, name))
                walk(child, name + ".")
            else:
                walk(child, prefix)

    walk(tree, "")

    def owner(line: int) -> str:
        enclosing = [s for s in spans if s[0] <= line <= s[1]]
        return max(enclosing, key=lambda s: s[0])[2] if enclosing else "<module>"

    return owner


def single_stage_sites(source: str) -> "set[str]":
    """Qualified names of the functions in `source` that compare something to PASSED.

    Two nets over the same text so one cannot hide a site from the other: the AST finds
    every comparison node whatever its spelling; the line grep finds a comparison written
    in a shape the AST helper does not model, and is mapped to its function by line."""
    tree = ast.parse(source)
    owner = _owner_resolver(tree)
    found: "set[str]" = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and any(isinstance(op, _COMPARE_OPS) for op in node.ops):
            if any(_is_passed(part) for part in [node.left, *node.comparators]):
                found.add(owner(node.lineno))
    for number, line in enumerate(source.splitlines(), 1):
        if _GREP.search(line):
            found.add(owner(number))
    return found


def _mentions_stage_status(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Name) and n.id == "StageStatus" for n in ast.walk(node))


def _targets_of(node: ast.AST) -> "list[ast.AST]":
    if isinstance(node, ast.Assign):
        return list(node.targets)
    if isinstance(node, (ast.AugAssign, ast.AnnAssign)):
        return [node.target]
    return []


def _attribute_chain(node: ast.AST) -> "set[str]":
    names: "set[str]" = set()
    while isinstance(node, ast.Attribute):
        names.add(node.attr)
        node = node.value
    return names


_STATUS_LITERALS = frozenset(s.value for s in StageStatus)


def _names_a_stage_status(value: "ast.AST | None") -> bool:
    """The right side is a stage status: `StageStatus...` anywhere in it, or an exact
    (upper-case) status literal; a lower-case "failed" is some other record's status."""
    if value is None:
        return False
    if _mentions_stage_status(value):
        return True
    return (isinstance(value, ast.Constant) and isinstance(value.value, str)
            and value.value in _STATUS_LITERALS)


def _subscript_key(node: ast.AST) -> "str | None":
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        return node.slice.value if isinstance(node.slice.value, str) else None
    return None


def stage_status_write_sites(source: str) -> "set[str]":
    """Qualified names of the functions in `source` that WRITE a stage's status or outcome.

    The twin of `single_stage_sites` on the write side: a write is where a SKIPPED stage
    could be turned into FAILED, ACTIVE or PENDING behind the customer's back. Shapes:
      * `<...>.outcome = ...`, and `<...>.status = ...` through an `outcome` chain or with a
        right side that is a stage status (`StageStatus...` or a status literal, so an
        aliased `o = s.outcome; o.status = "FAILED"` is found);
      * a JSON state: `d["outcome"]["status"] = ...` whatever the right side, and
        `d["status"] = ...` with a stage-status right side (an `d["outcome"] = ...` is NOT
        a hit: "outcome" is also the key of unrelated records, e.g. a gate's audit row);
      * `setattr(<x>, "status" | "outcome", ...)`;
      * an `Outcome(status=...)` construction."""
    tree = ast.parse(source)
    owner = _owner_resolver(tree)
    found: "set[str]" = set()
    for node in ast.walk(tree):
        value = getattr(node, "value", None)
        for target in _targets_of(node):
            if isinstance(target, ast.Attribute):
                if target.attr == "outcome":
                    found.add(owner(node.lineno))
                elif target.attr == "status" and (
                    "outcome" in _attribute_chain(target.value)
                    or _names_a_stage_status(value)
                ):
                    found.add(owner(node.lineno))
            elif _subscript_key(target) == "status":
                if _subscript_key(target.value) == "outcome" or _names_a_stage_status(value):
                    found.add(owner(node.lineno))
        if isinstance(node, ast.Call):
            callee = getattr(node.func, "id", None)
            if callee == "Outcome" and any(kw.arg == "status" for kw in node.keywords):
                found.add(owner(node.lineno))
            elif (callee == "setattr" and len(node.args) == 3
                  and isinstance(node.args[1], ast.Constant)
                  and node.args[1].value in ("status", "outcome")):
                found.add(owner(node.lineno))
    return found


def _scripts_under_review() -> "list[Path]":
    return sorted(
        p for p in SCRIPTS_DIR.rglob("*.py")
        if "tests" not in p.relative_to(SCRIPTS_DIR).parts
        and "__pycache__" not in p.parts
    )


def _sites(finder) -> "set[tuple[str, str]]":
    out: "set[tuple[str, str]]" = set()
    for path in _scripts_under_review():
        for name in finder(path.read_text(encoding="utf-8")):
            out.add((path.relative_to(SCRIPTS_DIR).as_posix(), name))
    return out


def _live_sites() -> "set[tuple[str, str]]":
    return _sites(single_stage_sites)


# (file under scripts/, qualified function) -> why this site asks a question narrower
# than "is the stage done?". A site that asks whether the PLAN may resolve belongs on
# `state.is_settled`, not here.
ALLOWED_SINGLE_STAGE_SITES: "dict[tuple[str, str], str]" = {
    ("agentctl/state.py", "SessionState.ready_stages"): (
        "dependency satisfaction, not plan completion: a stage is ready only when every "
        "dependency PASSED; approve refuses declining a stage something live depends on, so "
        "a dependency of a runnable stage is never SKIPPED"
    ),
    ("agentctl/cli.py", "_refresh_caches_from_plan_path"): (
        "asks whether THIS stage's earlier PASS survives an edit of its own definition; a "
        "SKIPPED stage has no pass to invalidate and is reset by _apply_refined_stage_fields"
    ),
    ("agentctl/cli.py", "_cmd_replan"): (
        "re-attest stash and substantive-replan carry: both ask whether a stage has a PASS "
        "outcome to reuse, and the carry arm names SKIPPED beside PASSED explicitly"
    ),
    ("agentctl/plugins_tracker.py", "_passed_artifact_stages"): (
        "asks which stages produced artifacts a ticket reader must be told about; a declined "
        "stage produced none, so PASSED is the exact set"
    ),
    ("hook-resolution-reminder.py", "landing_pending"): (
        "KNOWN GAP, not a legitimate narrower question: it asks 'is every stage done?' of a "
        "state file read as JSON and so cannot import is_settled; the file is outside this "
        "change's edit grant. With a SKIPPED stage at VERIFYING it fails to fire its nudge "
        "AND its result switches off the PreToolUse landing-discipline judge (`decide()`), "
        "which can then let an AskUserQuestion proposing a PR on a direct-push repo through; "
        "at RESOLUTION `resolution_gate_open` takes over both paths. Fix is the one-line "
        "`in (\"PASSED\", \"SKIPPED\")`; remove this entry with it"
    ),
    ("agentctl/cli.py", "cmd_record_result"): (
        "`args.status == \"passed\"` is the incoming result's own verdict on the one stage "
        "being recorded (--status passed|failed), not a read of the plan's stage statuses"
    ),
    ("agentctl/plugins_tracker.py", "_last_passed_stage_index"): (
        "reads the plugin's own event history for the last record_result logged as passed; a "
        "history row of one recorded stage, never a plan-wide completeness question"
    ),
    ("agentctl/plugins_tracker.py", "_terminal"): (
        "lowercase hit on `getattr(state.resolution, \"passed\")`: the resolution gate's own "
        "flag, which resolve sets only after resolution_blockers (settled-aware) clear"
    ),
    ("agentctl/plugins_experience.py", "_terminal"): (
        "lowercase hit on `getattr(state.resolution, \"passed\")`: the resolution gate's own "
        "flag, not a stage status"
    ),
    ("agentctl/plugins_ledger.py", "_terminal"): (
        "lowercase hit on `getattr(state.resolution, \"passed\")`: the resolution gate's own "
        "flag, not a stage status"
    ),
    ("hook-turn-end-gate.py", "resolution_turn_blockers"): (
        "lowercase hit on `getattr(resolution, \"passed\")`: the resolution gate's own flag; "
        "the stage-completeness question in this function goes through all_stages_passed, "
        "which is the settled predicate under its legacy name"
    ),
}


def test_every_single_stage_passed_comparison_is_accounted_for():
    live = _live_sites()
    unaccounted = sorted(live - set(ALLOWED_SINGLE_STAGE_SITES))
    stale = sorted(set(ALLOWED_SINGLE_STAGE_SITES) - live)
    assert not unaccounted, (
        "a comparison to PASSED outside the allowlist: if it asks 'is this stage done?' use "
        f"state.is_settled, otherwise allowlist it with a reason: {unaccounted}"
    )
    assert not stale, f"allowlist entries that no longer match a site: {stale}"


def test_every_allowlist_entry_states_a_reason():
    assert all(len(reason.strip()) >= 20 for reason in ALLOWED_SINGLE_STAGE_SITES.values())


# --- the write-side twin: every place a stage's status is set -----------------------

# (file under scripts/, qualified function) -> how a SKIPPED stage is kept from being
# re-opened here ("guarded") or why the stage written is never a declined one ("n/a").
# Reads are universally quantified (the PASSED enumerator above); so are writes: a
# declined stage written FAILED/PENDING/ACTIVE is dispatched again, which no read-side
# net can see.
ALLOWED_STATUS_WRITE_SITES: "dict[tuple[str, str], str]" = {
    ("agentctl/cli.py", "cmd_approve"): (
        "n/a: the ONLY writer of SKIPPED, and only for stages the plan declares optional "
        "and the customer named in --skip-optional"
    ),
    ("agentctl/cli.py", "cmd_reject"): (
        "guarded: the default target is the last stage that is not SKIPPED, and an explicit "
        "--stage naming a SKIPPED stage is refused; FAILED is never written to a declined one"
    ),
    ("agentctl/cli.py", "cmd_next_stage"): (
        "n/a: writes ACTIVE to ready[0], taken from ready_stages(), which lists PENDING "
        "stages only"
    ),
    ("agentctl/cli.py", "cmd_record_result"): (
        "n/a: writes PASSED/FAILED to state.active_stage() (current_stage), which only "
        "next-stage sets, from ready_stages()"
    ),
    ("agentctl/cli.py", "_try_reattest"): (
        "n/a: re-attests the stage under dispatch (current_stage), never a SKIPPED one; the "
        "stash it reads holds PASSED prior outcomes only"
    ),
    ("agentctl/cli.py", "_diagnose_materialization_defect"): (
        "n/a: receives the stage a spawn just ran (ACTIVE) from its caller"
    ),
    ("agentctl/cli.py", "cmd_pop_subplan"): (
        "guarded: marks the stage that opened the sub-plan PASSED unless it is SKIPPED, behind "
        "push-subplan, which refuses a SKIPPED originating stage (--originating-stage can "
        "name any stage)"
    ),
    ("agentctl/cli.py", "_apply_refined_stage_fields"): (
        "guarded: its only status write moves a SKIPPED stage whose refined definition is "
        "no longer optional back to PENDING (a required stage cannot be declined)"
    ),
    ("agentctl/cli.py", "_refresh_caches_from_plan_path"): (
        "guarded: resets only a CHANGED stage (PASSED or SKIPPED) to PENDING and keeps its "
        "`declined` mark, which the agent's approve refuses; an unchanged declined stage "
        "keeps SKIPPED"
    ),
    ("agentctl/cli.py", "_cmd_replan"): (
        "guarded: FAILED stages go back to PENDING (a SKIPPED one is never FAILED, see "
        "cmd_reject); the substantive carry copies the outcome of an unchanged PASSED or "
        "SKIPPED stage, and a changed one is left PENDING with `declined` set so only the "
        "customer's approve can re-offer it"
    ),
    ("agentctl/plan.py", "parse_plan"): (
        "n/a: constructs every stage PENDING at load; SKIPPED exists only in session state"
    ),
    ("agentctl/state.py", "Stage.from_dict"): (
        "n/a: deserializes the persisted outcome verbatim, SKIPPED included"
    ),
    ("file-difficulty.py", "_Dedup.__init__"): (
        "n/a: an unrelated `outcome` attribute on a difficulty-filing dedup record"
    ),
    ("verify-agentctl.py", "check_code_review_precondition._dev_stage"): (
        "n/a: the verifier's own synthetic stage, built ACTIVE for one check"
    ),
    ("verify-agentctl.py", "check_control_precondition._dev_stage"): (
        "n/a: the verifier's own synthetic stage, built ACTIVE for one check"
    ),
    ("verify-agentctl.py", "check_review_dispatch"): (
        "n/a: the verifier's own synthetic stage, built ACTIVE for one check"
    ),
    ("verify-agentctl.py", "check_state_roundtrip"): (
        "n/a: the verifier's own synthetic stage, built ACTIVE for one round-trip"
    ),
}


def test_every_stage_status_write_is_accounted_for():
    live = _sites(stage_status_write_sites)
    allowed = set(ALLOWED_STATUS_WRITE_SITES)
    unaccounted = sorted(live - allowed)
    stale = sorted(allowed - live)
    assert not unaccounted, (
        "a write of a stage status outside the allowlist: make sure a SKIPPED stage cannot "
        f"reach it (guard it) or say why it cannot, then list it: {unaccounted}"
    )
    assert not stale, f"write-allowlist entries that no longer match a site: {stale}"
    assert all(len(r.strip()) >= 20 for r in ALLOWED_STATUS_WRITE_SITES.values())


PLANTED = {
    "attribute compare": (
        "def done(stage):\n    return stage.outcome.status == StageStatus.PASSED.value\n",
        {"done"},
    ),
    "string literal compare in a method": (
        "class Gate:\n    def ok(self, s):\n        return s['status'] != 'PASSED'\n",
        {"Gate.ok"},
    ),
    "membership against a tuple": (
        "def f(s):\n    return s in ('PASSED', 'FAILED')\n",
        {"f"},
    ),
    "all() over a generator": (
        "def g(stages):\n"
        "    return all(s.outcome.status == StageStatus.PASSED.value for s in stages)\n",
        {"g"},
    ),
    "the settled helper is not a hit": (
        "def h(stages):\n    return all(is_settled(s) for s in stages)\n",
        set(),
    ),
    "an assignment is not a question": (
        "def k(stage):\n    stage.outcome.status = StageStatus.PASSED.value\n",
        set(),
    ),
    "a lower-case literal compare": (
        "def m(s):\n    return s.status == 'passed'\n",
        {"m"},
    ),
    "an identity test against the member": (
        "def n(x):\n    return x is StageStatus.PASSED\n",
        {"n"},
    ),
}


@pytest.mark.parametrize("case", sorted(PLANTED))
def test_the_enumerator_finds_planted_sites_and_only_those(case):
    source, expected = PLANTED[case]
    assert single_stage_sites(source) == expected


PLANTED_WRITES = {
    "a direct status write": (
        "def a(s):\n    s.outcome.status = StageStatus.FAILED.value\n",
        {"a"},
    ),
    "a write through an alias of the outcome": (
        "def b(s):\n    o = s.outcome\n    o.status = 'FAILED'\n",
        {"b"},
    ),
    "a replaced outcome": (
        "def c(s, fresh):\n    s.outcome = fresh\n",
        {"c"},
    ),
    "a JSON state's nested status without the enum": (
        "def d(doc):\n    doc['stages'][0]['outcome']['status'] = 'PENDING'\n",
        {"d"},
    ),
    "a JSON stage's status from the enum": (
        "def e(stage):\n    stage['status'] = StageStatus.ACTIVE.value\n",
        {"e"},
    ),
    "setattr on the outcome": (
        "def f(s):\n    setattr(s.outcome, 'status', 'PENDING')\n",
        {"f"},
    ),
    "a constructed outcome": (
        "def g(s):\n    s.attempt = Outcome(status='ACTIVE')\n",
        {"g"},
    ),
    "a method write": (
        "class Box:\n    def h(self, s):\n        s.outcome.status = 'SKIPPED'\n",
        {"Box.h"},
    ),
    "an unrelated record's lower-case status is not a hit": (
        "def i(job):\n    job.status = 'failed'\n    job['status'] = 'skipped'\n",
        set(),
    ),
    "a read is not a write": (
        "def j(s):\n    return s.outcome.status\n",
        set(),
    ),
}


@pytest.mark.parametrize("case", sorted(PLANTED_WRITES))
def test_the_write_enumerator_finds_planted_writes_and_only_those(case):
    source, expected = PLANTED_WRITES[case]
    assert stage_status_write_sites(source) == expected


def test_the_enumerator_walks_real_files_and_finds_the_known_setters_free_of_noise(tmp_path):
    scripts = _scripts_under_review()
    assert SCRIPTS_DIR / "agentctl" / "cli.py" in scripts
    assert SCRIPTS_DIR / "hook-resolution-reminder.py" in scripts
    assert not any("tests" in p.parts for p in scripts)


# --- the declined stage and the agent's approval --------------------------------------

_OPTIONAL_STAGE_3 = f'optional = true\nbacklog_issue = "{ISSUE}"\n'


_STAGE_4 = (
    '\n[[stage]]\nindex = 4\ntitle = "Document"\nexecutor = "spawn:developer"\n'
    'expected_result_image = "docs exist"\ncriterion_type = "measurable"\n'
    'done_criterion = "docs on disk"\ndepends_on = [2]\n'
)


def _optional_third_stage_plan(
        *, changed=False, required=False, retitled=False, fourth=False) -> str:
    """The autonomy-boundary harness's three-stage plan with stage 3 optional, one axis varied."""
    from test_replan_autonomy_boundary import plan_text

    text = plan_text(stage3=True)
    if changed:
        text = text.replace("the CI config names test_mod", "the CI config names test_mod and lint")
    if retitled:
        text = text.replace('title = "Wire CI"', 'title = "Wire CI up"')
    if not required:
        text = text.rstrip("\n") + "\n" + _OPTIONAL_STAGE_3
    return text + _STAGE_4 if fourth else text


@pytest.fixture
def eng(tmp_path, monkeypatch):
    from test_replan_autonomy_boundary import Eng

    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return Eng(tmp_path)


@pytest.mark.parametrize(
    ("variant", "replanned", "user_skips", "final_status"),
    [
        ("replan-changes-stage", dict(changed=True), [3], "SKIPPED"),
        ("replan-changes-stage", dict(changed=True), [], "PENDING"),
        ("replan-makes-stage-required", dict(required=True), [], "PENDING"),
        ("in-place-edit-at-plan-ready", dict(retitled=True, fourth=True), [3], "SKIPPED"),
    ],
    ids=["changed-user-keeps-declined", "changed-user-takes-it", "made-required-user-takes-it",
         "edited-in-place-user-keeps-declined"],
)
def test_the_agent_cannot_approve_a_declined_stage_back_in(
        eng, variant, replanned, user_skips, final_status):
    sid = "revive"
    plan = eng.write(_optional_third_stage_plan(), "first.toml")
    assert eng.open(sid, plan)["marker"] == "PLAN-READY"
    assert eng.run("approve", session=sid, by="user", skip_optional=[3])["ok"] is True
    assert eng.state(sid).stage(3).outcome.status == "SKIPPED"
    eng.fail_stage(sid)
    in_place = variant == "in-place-edit-at-plan-ready"
    eng.diagnose(sid, difference=in_place)

    if in_place:
        second = eng.write(_optional_third_stage_plan(fourth=True), "second.toml")
        d = eng.run("replan", session=sid, plan=second)
        assert d["data"]["autonomy"]["action"] == "self_approve", d
        assert eng.state(sid).stage(3).outcome.status == "SKIPPED"
        Path(second).write_text(_optional_third_stage_plan(**replanned), encoding="utf-8")
    else:
        second = eng.write(_optional_third_stage_plan(**replanned), "second.toml")
        d = eng.run("replan", session=sid, plan=second)
        assert d["data"]["autonomy"]["action"] == "self_approve", d

    for _ in range(2):  # a second try must not slip through whatever the first one persisted
        refused = eng.run("approve", session=sid, by="agent")
        assert refused["ok"] is False
        assert any("declined by the customer" in b for b in refused["data"]["blockers"]), refused
        state = eng.state(sid)
        assert state.stage(3).outcome.declined is True
        assert state.node == Node.PLAN_READY.value
        if not in_place:  # the replan's carry already reset it; an in-place edit is only read at approve
            assert state.stage(3).outcome.status == "PENDING"

    accepted = eng.run("approve", session=sid, by="user", skip_optional=user_skips)
    assert accepted["ok"] is True, accepted
    state = eng.state(sid)
    assert state.stage(3).outcome.status == final_status
    assert state.stage(3).outcome.declined is (final_status == "SKIPPED")


# --- the decline follows the stage's issue, not its index ------------------------------

def _block(index, title, depends, *, image, done, outputs=(), optional=False):
    lines = ["", "[[stage]]", f"index = {index}", f'title = "{title}"',
             'executor = "spawn:developer"', f'expected_result_image = "{image}"',
             'criterion_type = "measurable"', f'done_criterion = "{done}"',
             f"depends_on = {list(depends)}"]
    if outputs:
        lines.append("output_artifacts = [" + ", ".join(f'"{o}"' for o in outputs) + "]")
    if optional:
        lines += ["optional = true", f'backlog_issue = "{ISSUE}"']
    return "\n".join(lines) + "\n"


def _scaffold():
    return _block(1, "Scaffold module", [], image="module file exists and imports cleanly",
                  done="python -c 'import mod' exits 0", outputs=["mod.py"])


def _add_tests():
    return _block(2, "Add tests", [1], image="pytest green for the new module",
                  done="pytest tests/test_mod.py green", outputs=["tests/test_mod.py"])


def _wire_ci(index, depends, *, done="the CI config names test_mod"):
    return _block(index, "Wire CI", depends, image="CI config runs the suite",
                  done=done, optional=True)


def _document(index, depends):
    return _block(index, "Document", depends, image="docs exist", done="docs on disk")


def _plan_of(*blocks):
    from test_replan_autonomy_boundary import plan_text

    return plan_text().partition("\n[[stage]]")[0] + "".join(blocks)


def _declined_then_diagnosing(eng, sid, *, difference=True):
    """Stage 3 (optional "Wire CI") declined by the customer, then stage 1 fails.

    `difference=False` is for a replan that edits prose only: a named difference to remove
    must be matched by a change of the plan's operative surface."""
    plan = eng.write(_plan_of(_scaffold(), _add_tests(), _wire_ci(3, [2])), "first.toml")
    assert eng.open(sid, plan)["marker"] == "PLAN-READY"
    assert eng.run("approve", session=sid, by="user", skip_optional=[3])["ok"] is True
    state = eng.state(sid)
    assert state.stage(3).outcome.status == "SKIPPED"
    assert state.declined_issues == [ISSUE]
    eng.fail_stage(sid)
    eng.diagnose(sid, difference=difference)


def _replan_to(eng, sid, text, name):
    d = eng.run("replan", session=sid, plan=eng.write(text, name))
    assert d["ok"] is True, d
    assert eng.state(sid).node == Node.PLAN_READY.value
    return d


def _refused_for_the_decline(eng, sid):
    refused = eng.run("approve", session=sid, by="agent")
    assert refused["ok"] is False
    assert any("declined by the customer" in b for b in refused["data"]["blockers"]), refused
    state = eng.state(sid)
    assert state.node == Node.PLAN_READY.value
    return state


def test_a_renumbering_replan_keeps_the_decline_on_the_stage_by_its_issue(eng):
    sid = "renumber"
    _declined_then_diagnosing(eng, sid)
    _replan_to(eng, sid, _plan_of(_scaffold(), _wire_ci(2, [1])), "second.toml")  # 3 -> 2

    state = _refused_for_the_decline(eng, sid)
    assert [s.index for s in state.stages] == [1, 2]
    assert state.stage(2).outcome.declined is True
    assert state.stage(2).outcome.status == "PENDING"
    assert state.declined_issues == [ISSUE]

    accepted = eng.run("approve", session=sid, by="user", skip_optional=[2])
    assert accepted["ok"] is True, accepted
    state = eng.state(sid)
    assert state.stage(2).outcome.status == "SKIPPED"
    assert state.declined_issues == [ISSUE]


def test_the_customer_choosing_the_renumbered_stage_clears_the_recorded_decline(eng):
    sid = "choose"
    _declined_then_diagnosing(eng, sid)
    _replan_to(eng, sid, _plan_of(_scaffold(), _wire_ci(2, [1])), "second.toml")
    _refused_for_the_decline(eng, sid)

    accepted = eng.run("approve", session=sid, by="user")
    assert accepted["ok"] is True, accepted
    state = eng.state(sid)
    assert state.stage(2).outcome.status == "PENDING"
    assert state.stage(2).outcome.declined is False
    assert state.declined_issues == []


def test_dropping_then_readding_a_declined_stage_across_two_replans_stays_declined(eng):
    sid = "readd"
    _declined_then_diagnosing(eng, sid)
    _replan_to(eng, sid, _plan_of(_scaffold(), _add_tests()), "second.toml")
    assert eng.state(sid).declined_issues == [ISSUE]
    approved = eng.run("approve", session=sid, by="agent")
    assert approved["ok"] is True, approved
    assert eng.state(sid).declined_issues == [ISSUE]  # an agent approval never writes it

    eng.fail_stage(sid)
    eng.diagnose(sid, "b")
    _replan_to(eng, sid, _plan_of(_scaffold(), _add_tests(), _wire_ci(3, [2])), "third.toml")

    state = _refused_for_the_decline(eng, sid)
    assert state.stage(3).outcome.declined is True
    assert state.stage(3).outcome.status == "PENDING"


def test_an_in_place_renumbering_at_plan_ready_leaves_no_orphan_skipped_stage(eng):
    sid = "inplace"
    _declined_then_diagnosing(eng, sid)
    second = _plan_of(_scaffold(), _add_tests(), _wire_ci(3, [2]), _document(4, [2]))
    _replan_to(eng, sid, second, "second.toml")
    assert eng.state(sid).stage(3).outcome.status == "SKIPPED"  # unchanged stage: kept

    renumbered = _plan_of(_scaffold(), _wire_ci(2, [1]))
    Path(eng.tmp / "second.toml").write_text(renumbered, encoding="utf-8")
    _refused_for_the_decline(eng, sid)

    accepted = eng.run("approve", session=sid, by="user", skip_optional=[2])
    assert accepted["ok"] is True, accepted
    state = eng.state(sid)
    assert [s.index for s in state.stages] == [1, 2]
    assert [s.outcome.status for s in state.stages] == ["PENDING", "SKIPPED"]
    assert state.stage(2).title == "Wire CI"
    assert state.stage(2).outcome.declined is True
    assert state.declined_issues == [ISSUE]


def test_an_insertion_before_the_declined_stage_keeps_the_marker_on_the_right_stage(eng):
    sid = "insert"
    _declined_then_diagnosing(eng, sid)
    inserted = _plan_of(_scaffold(), _add_tests(), _document(3, [2]), _wire_ci(4, [2]))
    _replan_to(eng, sid, inserted, "second.toml")

    state = _refused_for_the_decline(eng, sid)
    assert state.stage(3).title == "Document"
    assert state.stage(3).outcome.declined is False
    assert state.stage(4).title == "Wire CI"
    assert state.stage(4).outcome.declined is True

    accepted = eng.run("approve", session=sid, by="user", skip_optional=[4])
    assert accepted["ok"] is True, accepted
    state = eng.state(sid)
    assert [s.outcome.status for s in state.stages] == ["PENDING", "PENDING", "PENDING", "SKIPPED"]


def test_the_agents_approval_reads_the_declined_issue_not_only_the_stage_marker(eng):
    sid = "marker"
    _declined_then_diagnosing(eng, sid, difference=False)
    _replan_to(eng, sid,
               _plan_of(_scaffold(), _add_tests(), _wire_ci(3, [2], done="the CI config names lint")),
               "second.toml")
    state = eng.state(sid)
    state.stage(3).outcome.declined = False  # a state that predates the per-stage marker
    eng.store.save(state)

    state = _refused_for_the_decline(eng, sid)
    assert state.declined_issues == [ISSUE]


def test_declined_live_optional_is_keyed_by_issue_and_ignores_skipped_stages(tmp_path):
    doc = _load(tmp_path, _plan_text(_stage(1), _opt(2, [1]), _opt(3, [1], issue="owner/repo#9")))
    assert gates.declined_live_optional(doc, [ISSUE]) == [2]
    assert gates.declined_live_optional(doc.stages, [ISSUE, "owner/repo#9"]) == [2, 3]
    assert gates.declined_live_optional(doc, []) == []
    doc.stages[1].outcome.status = StageStatus.SKIPPED.value
    assert gates.declined_live_optional(doc, [ISSUE, "owner/repo#9"]) == [3]


def test_a_stored_state_without_the_decline_fields_loads_as_not_declined(eng):
    from agentctl.state import SessionState

    sid = "legacy"
    _declined_then_diagnosing(eng, sid)
    stored = eng.state(sid).to_dict()
    assert stored["declined_issues"] == [ISSUE]
    assert stored["stages"][2]["outcome"]["declined"] is True
    del stored["declined_issues"]
    for stage in stored["stages"]:
        del stage["outcome"]["declined"]

    loaded = SessionState.from_dict(stored)
    assert loaded.declined_issues == []
    assert [s.outcome.declined for s in loaded.stages] == [False, False, False]


# --- the decline lives on the customer's approval record --------------------------------

def _declined_order(eng, *, skip=(3,)):
    """The customer approves the optional-third-stage plan in session `u1`, declining `skip`."""
    plan = eng.write(_optional_third_stage_plan(), "first.toml")
    assert eng.open("u1", plan)["marker"] == "PLAN-READY"
    accepted = eng.run("approve", session="u1", by="user", skip_optional=list(skip))
    assert accepted["ok"] is True, accepted
    return plan


def _boundary_refusal(directive) -> list[str]:
    return [b for b in directive["data"]["blockers"]
            if gates.AUTONOMY_REASON_DECLINED_STAGE in b]


def test_a_customer_approval_stamps_the_decline_on_the_ledger_and_the_session_agrees(eng):
    plan = _declined_order(eng)
    assert eng.ledger(plan)["records"][-1]["declined_issues"] == [ISSUE]
    assert eng.state("u1").declined_issues == [ISSUE]


@pytest.mark.parametrize("user_skips", [[3], []], ids=["keeps-it-declined", "takes-it"])
def test_a_new_session_cannot_self_approve_the_stage_the_customer_declined(eng, user_skips):
    plan = _declined_order(eng)

    opened = eng.open("a2", plan)
    assert eng.state("a2").declined_issues == []  # the session never saw the decline
    assert opened["data"]["autonomy"]["action"] == "await_user_approval", opened
    assert opened["data"]["autonomy"]["declined_live_optional"] == [3]
    refused = eng.run("approve", session="a2", by="agent")
    assert refused["ok"] is False
    assert _boundary_refusal(refused), refused
    assert "stage 3 (" + ISSUE + ")" in _boundary_refusal(refused)[0]
    assert eng.state("a2").node == Node.PLAN_READY.value

    accepted = eng.run("approve", session="a2", by="user", skip_optional=user_skips)
    assert accepted["ok"] is True, accepted
    state = eng.state("a2")
    assert state.stage(3).outcome.status == ("SKIPPED" if user_skips else "PENDING")
    stamped = eng.ledger(plan)["records"][-1]["declined_issues"]
    assert stamped == sorted(state.declined_issues) == ([ISSUE] if user_skips else [])


def test_a_reset_session_on_the_same_order_cannot_self_approve_the_declined_stage(eng):
    plan = _declined_order(eng)
    reset = eng.run("reset", session="u1", task="task-u1-again", goal="g", done_criterion="dc",
                    force=True)
    assert reset["ok"] is True, reset
    assert eng.state("u1").declined_issues == []
    eng.run("classify", session="u1", architectural=True, files=5, changed_lines=200,
            wall_clock_min=60)
    eng.run("plan", session="u1")
    assert eng.run("submit_plan", session="u1", plan=plan)["marker"] == "PLAN-READY"

    refused = eng.run("approve", session="u1", by="agent")
    assert refused["ok"] is False
    assert _boundary_refusal(refused), refused


def test_once_the_customer_chose_to_keep_the_stage_a_new_session_may_self_approve(eng):
    plan = _declined_order(eng)
    eng.open("a2", plan)
    assert eng.run("approve", session="a2", by="user", skip_optional=[])["ok"] is True
    assert eng.ledger(plan)["records"][-1]["declined_issues"] == []

    opened = eng.open("a3", plan)
    assert opened["data"]["autonomy"]["action"] == "self_approve", opened
    assert eng.run("approve", session="a3", by="agent")["ok"] is True


def test_a_ledger_record_without_the_field_declines_nothing(eng):
    import json

    from agentctl import order_approvals as oa
    from agentctl.plan import order_digest

    plan = _declined_order(eng)
    path = oa._path(order_digest(load_plan(plan, strict=False)), None)
    stored = json.loads(path.read_text(encoding="utf-8"))
    for record in stored["records"]:
        record.pop("declined_issues", None)
        record.pop("declined_stages", None)
    path.write_text(json.dumps(stored), encoding="utf-8")

    assert oa.declined_issues_of(eng.ledger(plan)["records"][-1]) == []
    assert oa.declined_titles_of(eng.ledger(plan)["records"][-1]) == []
    opened = eng.open("a2", plan)
    assert opened["data"]["autonomy"]["action"] == "self_approve", opened
    assert eng.run("approve", session="a2", by="agent")["ok"] is True


def test_a_replan_inside_the_session_that_holds_the_decline_still_self_approves(eng):
    _declined_order(eng)
    eng.fail_stage("u1")
    eng.diagnose("u1")
    second = eng.write(_optional_third_stage_plan(fourth=True), "second.toml")
    d = eng.run("replan", session="u1", plan=second)
    assert d["data"]["autonomy"]["action"] == "self_approve", d
    assert d["data"]["autonomy"]["declined_live_optional"] == []


# --- one identity rule: the ledger names a declined stage by title once it is required ----

def _required_variant(eng):
    return eng.write(_optional_third_stage_plan(required=True), "required.toml")


def _stage_3_title(eng, plan):
    return next(s.title for s in load_plan(plan, strict=False).stages if s.index == 3)


def test_a_customer_approval_stamps_the_declined_stage_by_issue_and_title(eng):
    plan = _declined_order(eng)
    record = eng.ledger(plan)["records"][-1]
    assert record["declined_stages"] == [{"issue": ISSUE, "title": _stage_3_title(eng, plan)}]
    assert record["declined_issues"] == [ISSUE]


def test_a_new_session_cannot_self_approve_a_declined_stage_that_became_required(eng):
    plan = _declined_order(eng)
    title = _stage_3_title(eng, plan)
    required = _required_variant(eng)

    opened = eng.open("a2", required)
    assert eng.state("a2").declined_issues == []  # the session never saw the decline
    assert opened["data"]["autonomy"]["action"] == "await_user_approval", opened
    assert opened["data"]["autonomy"]["declined_live_optional"] == [3]
    refused = eng.run("approve", session="a2", by="agent")
    assert refused["ok"] is False
    reason = _boundary_refusal(refused)
    assert reason, refused
    assert f"stage 3 ({title})" in reason[0]
    assert eng.state("a2").node == Node.PLAN_READY.value

    accepted = eng.run("approve", session="a2", by="user")
    assert accepted["ok"] is True, accepted
    assert eng.state("a2").stage(3).outcome.status == "PENDING"
    latest = eng.ledger(plan)["records"][-1]
    # The customer took the stage: its title-half lapses. The issue-half stays, as it does
    # inside a session, because no stage of this plan offers the issue to be chosen again.
    assert latest["declined_stages"] == []
    assert latest["declined_issues"] == [ISSUE]

    assert eng.open("a3", required)["data"]["autonomy"]["action"] == "self_approve"


def test_a_reset_session_cannot_self_approve_a_declined_stage_that_became_required(eng):
    plan = _declined_order(eng)
    required = _required_variant(eng)
    assert eng.run("reset", session="u1", task="task-u1-again", goal="g", done_criterion="dc",
                   force=True)["ok"] is True
    eng.run("classify", session="u1", architectural=True, files=5, changed_lines=200,
            wall_clock_min=60)
    eng.run("plan", session="u1")
    assert eng.run("submit_plan", session="u1", plan=required)["marker"] == "PLAN-READY"

    refused = eng.run("approve", session="u1", by="agent")
    assert refused["ok"] is False
    assert _boundary_refusal(refused), refused
    assert eng.ledger(plan)["records"][-1]["declined_stages"][0]["issue"] == ISSUE


def _submit_in_fresh_session(eng, sid, plan, *, reset, tag):
    """Open `plan` in a session that holds no decline: a new one, or `sid` after a `reset`."""
    if not reset:
        return eng.open(sid, plan)
    assert eng.run("reset", session=sid, task=f"task-{sid}-{tag}", goal="g",
                   done_criterion="dc", force=True)["ok"] is True
    eng.run("classify", session=sid, architectural=True, files=5, changed_lines=200,
            wall_clock_min=60)
    eng.run("plan", session=sid)
    return eng.run("submit_plan", session=sid, plan=plan)


@pytest.mark.parametrize("reset", [False, True], ids=["new-session", "after-reset"])
@pytest.mark.parametrize("readded_required", [False, True], ids=["as-optional", "as-required"])
def test_a_customer_approval_of_a_plan_without_the_stage_keeps_its_decline_across_sessions(
        eng, readded_required, reset):
    from test_replan_autonomy_boundary import plan_text

    plan = _declined_order(eng)
    title = _stage_3_title(eng, plan)
    dropped = eng.write(plan_text(), "dropped.toml")
    readded = _required_variant(eng) if readded_required else plan

    first = _submit_in_fresh_session(eng, "a2", dropped, reset=reset, tag="drop")
    assert eng.state("a2").node == Node.PLAN_READY.value, first
    assert eng.state("a2").declined_issues == []  # the session never saw the decline
    assert eng.run("approve", session="a2", by="user")["ok"] is True
    middle = eng.ledger(plan)["records"][-1]
    assert middle["declined_issues"] == [ISSUE]
    assert middle["declined_stages"] == [{"issue": ISSUE, "title": title}]
    assert eng.state("a2").declined_issues == [ISSUE]

    sid = "a2" if reset else "a3"
    opened = _submit_in_fresh_session(eng, sid, readded, reset=reset, tag="readd")
    assert opened["marker"] == "PLAN-READY", opened
    refused = eng.run("approve", session=sid, by="agent")
    assert refused["ok"] is False, refused
    assert f"stage 3 ({ISSUE if not readded_required else title})" in _boundary_refusal(refused)[0]
    assert eng.state(sid).node == Node.PLAN_READY.value

    assert eng.run("approve", session=sid, by="user")["ok"] is True


def test_a_declined_stage_is_matched_by_issue_while_optional_and_by_title_once_required(tmp_path):
    doc = _load(tmp_path, _plan_text(_stage(1), _opt(2, [1]), _stage(3, depends=[1])))
    optional, required = doc.stages[1], doc.stages[2]
    assert gates.is_declined_stage(optional, {ISSUE}, set())
    assert not gates.is_declined_stage(optional, set(), {optional.title})  # title never names an optional one
    assert gates.is_declined_stage(required, set(), {required.title})
    assert not gates.is_declined_stage(required, {ISSUE}, set())  # a required stage has no issue
    assert gates.declined_live_optional(doc, [ISSUE], [required.title]) == [2, 3]
    assert gates.declined_live_optional(doc, [], [required.title]) == [3]


def test_a_renamed_required_stage_is_not_matched_by_the_title_rule_the_accepted_limit(tmp_path):
    doc = _load(tmp_path, _plan_text(_stage(1), _stage(2, depends=[1])))
    assert gates.declined_live_optional(doc, [], ["Stage 2 renamed"]) == []


def test_the_decline_entries_follow_the_stage_the_customer_actually_left_declined(tmp_path):
    from agentctl.state import SessionState

    doc = _load(tmp_path, _plan_text(_stage(1), _opt(2, [1], issue="o/r#1"), _stage(3, depends=[1])))
    doc.stages[0].outcome.status = StageStatus.PASSED.value
    doc.stages[1].outcome.status = StageStatus.SKIPPED.value
    state = SessionState(session_id="s", task_id="t")
    state.declined_issues = ["o/r#1", "o/r#2", "o/r#3"]
    state.stages = doc.stages
    # "Stage 3" is live and required: the customer approved it, so its old decline lapses.
    prior = [{"issue": "o/r#2", "title": "Dropped since"}, {"issue": "o/r#3", "title": "Stage 3"}]
    assert gates.declined_stage_entries(state, prior) == [
        {"issue": "o/r#1", "title": "Stage 2"},
        {"issue": "o/r#2", "title": "Dropped since"},
    ]
    assert gates.declined_stage_entries(state) == [{"issue": "o/r#1", "title": "Stage 2"}]


def test_a_record_without_titles_or_with_malformed_ones_names_no_title():
    from agentctl import order_approvals as oa

    assert oa.declined_titles_of(None) == []
    assert oa.declined_titles_of({"declined_issues": [ISSUE]}) == []
    assert oa.declined_titles_of({"declined_stages": "Wire CI"}) == []
    assert oa.declined_titles_of({"declined_stages": [
        {"issue": ISSUE, "title": "B"}, {"issue": ISSUE, "title": "A"}, {"issue": ISSUE, "title": "A"},
        {"issue": ISSUE}, {"title": "no issue"}, "junk",
    ]}) == ["A", "B"]


def test_an_approval_with_an_empty_plan_digest_is_refused_not_stored_unreadable(tmp_path):
    from agentctl import order_approvals as oa

    with pytest.raises(ValueError, match="empty plan_sha256"):
        oa.record_approval(
            "o", plan_sha256="", resources=[], unresolved_identities=[], stage_effects=[],
            by="user", at="t", root=tmp_path,
        )
    assert not (tmp_path / "o.json").exists()


def test_the_boundary_reads_the_decline_from_the_record_by_issue(tmp_path):
    from agentctl.plan_resources import BoundaryView

    doc = _load(tmp_path, _plan_text(_stage(1), _opt(2, [1]), _opt(3, [1], issue="owner/repo#9")))
    view = BoundaryView(order_sha256="o")

    def ledger(**record_extra):
        return {"order_sha256": "o", "records": [
            {"plan_sha256": "p", "resources": [], "unresolved_identities": [], **record_extra}]}

    declined = gates.autonomy_boundary(ledger(declined_issues=[ISSUE]), view, live_optional=doc.stages)
    assert declined["eligible"] is False
    assert declined["declined_live_optional"] == [2]
    assert gates.AUTONOMY_REASON_DECLINED_STAGE in declined["reasons"][0]

    for snapshot, live in [
        (ledger(), doc.stages),                                  # a record that declines nothing
        (ledger(declined_issues=[ISSUE]), None),                 # nothing offered to the check
        (ledger(declined_issues=[ISSUE]), [doc.stages[0], doc.stages[2]]),  # the issue is not on offer
    ]:
        verdict = gates.autonomy_boundary(snapshot, view, live_optional=live)
        assert verdict["declined_live_optional"] == []
        assert verdict["eligible"] is True, verdict
