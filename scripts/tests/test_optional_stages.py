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


def test_popping_a_subplan_never_revives_a_declined_originating_stage(store, optional_plan):
    from agentctl.state import Criterion, Means, Outcome, Stage, Subject, Actor, GateRecord
    sid = "pop-skipped"
    _executing(store, sid, optional_plan, skip=[3])
    cli.cmd_next_stage(ns(session=sid), store=store)
    cli.cmd_push_subplan(
        ns(session=sid, plan="/tmp/child.toml", task="child-task", originating_stage=3),
        store=store)
    state = store.load(sid)
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
    assert store.load(sid).stage(3).outcome.status == StageStatus.SKIPPED.value


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


def stage_status_write_sites(source: str) -> "set[str]":
    """Qualified names of the functions in `source` that WRITE a stage's status or outcome.

    The twin of `single_stage_sites` on the write side: a write is where a SKIPPED stage
    could be turned into FAILED, ACTIVE or PENDING behind the customer's back. Shapes: an
    assignment to `<...>.outcome.status` (or any `.status` whose right side names
    StageStatus), an assignment to `<...>.outcome`, an assignment to a `["status"]`
    subscript whose right side names StageStatus (a JSON state), and an `Outcome(status=...)`
    construction."""
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
                    or (value is not None and _mentions_stage_status(value))
                ):
                    found.add(owner(node.lineno))
            elif (isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant)
                  and target.slice.value == "status" and value is not None
                  and _mentions_stage_status(value)):
                found.add(owner(node.lineno))
        if (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Outcome"
                and any(kw.arg == "status" for kw in node.keywords)):
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
        "guarded: marks the stage that opened the sub-plan PASSED unless it is SKIPPED (an "
        "explicit --originating-stage can name any stage)"
    ),
    ("agentctl/cli.py", "_apply_refined_stage_fields"): (
        "guarded: its only status write moves a SKIPPED stage whose refined definition is "
        "no longer optional back to PENDING (a required stage cannot be declined)"
    ),
    ("agentctl/cli.py", "_refresh_caches_from_plan_path"): (
        "guarded: resets only a CHANGED stage (PASSED or SKIPPED) to PENDING; an unchanged "
        "declined stage keeps SKIPPED"
    ),
    ("agentctl/cli.py", "_cmd_replan"): (
        "guarded: FAILED stages go back to PENDING (a SKIPPED one is never FAILED, see "
        "cmd_reject); the substantive carry copies the outcome of an unchanged PASSED or "
        "SKIPPED stage and leaves a changed one PENDING"
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
}


@pytest.mark.parametrize("case", sorted(PLANTED))
def test_the_enumerator_finds_planted_sites_and_only_those(case):
    source, expected = PLANTED[case]
    assert single_stage_sites(source) == expected


def test_the_enumerator_walks_real_files_and_finds_the_known_setters_free_of_noise(tmp_path):
    scripts = _scripts_under_review()
    assert SCRIPTS_DIR / "agentctl" / "cli.py" in scripts
    assert SCRIPTS_DIR / "hook-resolution-reminder.py" in scripts
    assert not any("tests" in p.parts for p in scripts)
