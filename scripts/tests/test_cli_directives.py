"""Directive contract for the key commands: every command returns a Directive with
ok/node/action set, and the gate commands carry their return markers (submit-plan ->
PLAN-READY hard gate; resolve -> COMPLETED; loop guard -> ESCALATE)."""
from argparse import Namespace

from agentctl import cli
from agentctl.directive import Directive
from agentctl.state import Node


def ns(**kw):
    return Namespace(**kw)


def _start(store, sid):
    return cli.cmd_start(ns(session=sid, task="demo", goal="g", done_criterion="dc",
                            criterion_type="measurable", recursion_depth=0), store=store)


def _to_plan_ready(store, sid, plan):
    _start(store, sid)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    return cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)


def test_start_directive_shape(store):
    d = _start(store, "d1")
    assert isinstance(d, Directive)
    assert d.ok is True
    assert d.node == Node.CLASSIFIED.value
    assert d.action == "classify"
    assert d.marker is None


def test_classify_substantive_directive(store):
    _start(store, "d2")
    d = cli.cmd_classify(ns(session="d2", chat=False, changed_lines=200, files=5,
                            wall_clock_min=60, tracker_key=None, architectural=True,
                            external_effect=False, new_dependency=False,
                            public_api_change=False), store=store)
    assert d.ok is True
    assert d.node == Node.ROUTED.value
    assert d.action == "plan"
    assert "reasons" in d.data


def test_submit_plan_is_hard_gate_directive(store, fixtures_dir):
    d = _to_plan_ready(store, "d3", str(fixtures_dir / "plan_two_stage.toml"))
    assert d.ok is True
    assert d.node == Node.PLAN_READY.value
    assert d.action == "await_user_approval"
    assert d.marker == "PLAN-READY"


def test_failed_submit_plan_does_not_strand(store, fixtures_dir, tmp_path):
    """Regression: a failed plan verification must NOT advance to PLAN_READY.

    The strand bug (recovered by hand on 2026-06-25 via `reset --force`):
    cmd_submit_plan transitioned to PLAN_READY and armed the approval gate
    *unconditionally*, so a structure-check failure left the session parked at
    PLAN_READY with an armed gate and no edge back to PLANNING — every retry
    bounced. The fix keeps a failed submit at PLANNING so the agent can fix the
    plan and re-submit in place.
    """
    sid = "strand"
    _start(store, sid)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)

    bad = tmp_path / "bad_plan.md"
    bad.write_text("this is not a structurally valid plan\n", encoding="utf-8")
    d = cli.cmd_submit_plan(ns(session=sid, plan=str(bad)), store=store)

    assert d.ok is False
    assert d.action == "fix_plan"
    state = store.load(sid)
    assert state.node == Node.PLANNING.value          # NOT stranded at PLAN_READY
    assert not state.plan_verified
    # gate must not be armed-and-passed on a failed plan
    assert not (state.approval and state.approval.passed)

    # recovery: a corrected plan submitted from PLANNING advances normally
    d2 = cli.cmd_submit_plan(ns(session=sid, plan=str(fixtures_dir / "plan_two_stage.toml")),
                             store=store)
    assert d2.ok is True
    assert d2.marker == "PLAN-READY"
    assert store.load(sid).node == Node.PLAN_READY.value


def test_resolve_completed_marker(store, fixtures_dir):
    sid = "d4"
    _to_plan_ready(store, sid, str(fixtures_dir / "plan_two_stage.toml"))
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    for _ in range(2):
        cli.cmd_next_stage(ns(session=sid), store=store)
        cli.cmd_record_result(ns(session=sid, status="passed", actual="ok",
                               control="reviewed: ok"), store=store)
    cli.cmd_verify_final(ns(session=sid), store=store)
    # experience auto-activates for substantive sessions and gates resolution
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="searched"), store=store)
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="recorded"), store=store)
    d = cli.cmd_resolve(ns(session=sid, by="user"), store=store)
    assert d.ok is True
    assert d.node == Node.RESOLVED.value
    assert d.marker == "COMPLETED"


def test_record_result_loop_guard_escalate_marker(store, fixtures_dir):
    sid = "d5"
    _to_plan_ready(store, sid, str(fixtures_dir / "plan_two_stage.toml"))
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)
    d = cli.cmd_record_result(ns(session=sid, status="failed", actual="boom"), store=store)
    assert d.ok is False
    assert d.action == "declare"  # FAILED enters the DIAGNOSING sub-spine, not straight to replan
    assert d.node == Node.DIAGNOSING.value
    assert d.marker == "OVERCOME-DIFFICULTY"

    # restart the same stage, fail identically -> ESCALATE marker (loop guard wins
    # before the diagnose transition)
    state = store.load(sid)
    from agentctl.state import StageStatus
    state.stage(1).outcome.status = StageStatus.ACTIVE.value
    state.current_stage = 1
    state.node = Node.EXECUTING.value
    store.save(state)
    d = cli.cmd_record_result(ns(session=sid, status="failed", actual="boom"), store=store)
    assert d.ok is False
    assert d.marker == "ESCALATE"


def test_critique_persists_the_structured_split(store, fixtures_dir):
    """A critique invocation carrying repeatable --invariant-to-preserve /
    --difference-to-remove persists both lists on the Critique."""
    sid = "csplit"
    _to_plan_ready(store, sid, str(fixtures_dir / "plan_two_stage.toml"))
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)
    cli.cmd_record_result(ns(session=sid, status="failed", actual="boom"), store=store)
    cli.cmd_declare(ns(session=sid, expected="e", actual="a", mismatch="m"), store=store)
    cli.cmd_investigate(ns(session=sid, localized_expectation="le", localized_actual="la",
                           hypotheses=["h1", "h2"]), store=store)
    d = cli.cmd_critique(ns(session=sid, functional_ground="fg", replanning_task="rt",
                            invariants_to_preserve=["stage 1 done criterion"],
                            differences_to_remove=["means: ad-hoc retry"]), store=store)
    assert d.action == "replan"
    crit = store.load(sid).difficulty.critique
    assert crit.invariants_to_preserve == ["stage 1 done criterion"]
    assert crit.differences_to_remove == ["means: ad-hoc retry"]


def test_status_directive_on_empty_session(store):
    d = cli.cmd_status(ns(session="nope"), store=store)
    assert d.ok is True
    assert d.node == "(none)"
    assert d.action == "start"


def test_directive_to_dict_is_json_safe(store):
    d = _start(store, "d6")
    payload = d.to_dict()
    assert set(payload) >= {"ok", "node", "action", "detail", "marker", "data"}


# --- start --if-absent / reset lifecycle ----------------------------------

def _reset_ns(sid, **kw):
    base = dict(session=sid, task="demo2", goal="g2", done_criterion="dc2",
                criterion_type="measurable", recursion_depth=0, force=False)
    base.update(kw)
    return ns(**base)


def _start_if_absent(store, sid, **kw):
    base = dict(session=sid, task="demo", goal="g", done_criterion="dc",
                criterion_type="measurable", recursion_depth=0, if_absent=True)
    base.update(kw)
    return cli.cmd_start(ns(**base), store=store)


def test_start_if_absent_no_prior_creates(store):
    d = _start_if_absent(store, "ia1")
    assert d.ok is True
    assert d.node == Node.CLASSIFIED.value
    assert d.action == "classify"
    assert store.load("ia1").node == Node.CLASSIFIED.value


def test_start_if_absent_on_live_session_is_noop(store, fixtures_dir):
    sid = "ia2"
    # drive to APPROVED
    _to_plan_ready(store, sid, str(fixtures_dir / "plan_two_stage.toml"))
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    assert store.load(sid).node == Node.APPROVED.value
    d = _start_if_absent(store, sid)
    assert d.ok is True
    assert d.action == "continue"
    # KEY anti-overwrite assertion: state is untouched
    assert store.load(sid).node == Node.APPROVED.value


def test_reset_from_resolved_rearms(store, fixtures_dir):
    sid = "rs1"
    _to_plan_ready(store, sid, str(fixtures_dir / "plan_two_stage.toml"))
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    for _ in range(2):
        cli.cmd_next_stage(ns(session=sid), store=store)
        cli.cmd_record_result(ns(session=sid, status="passed", actual="ok",
                               control="reviewed: ok"), store=store)
    cli.cmd_verify_final(ns(session=sid), store=store)
    # experience auto-activates for substantive sessions and gates resolution
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="searched"), store=store)
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="recorded"), store=store)
    cli.cmd_resolve(ns(session=sid, by="user"), store=store)
    assert store.load(sid).node == Node.RESOLVED.value
    d = cli.cmd_reset(_reset_ns(sid), store=store)
    assert d.ok is True
    assert d.action == "classify"
    fresh = store.load(sid)
    assert fresh.node == Node.CLASSIFIED.value
    assert fresh.task_id == "demo2"
    assert fresh.weight_class is None


def test_reset_from_routed_rearms(store):
    sid = "rs2"
    # chat classify lands at ROUTED (terminal)
    _start(store, sid)
    cli.cmd_classify(ns(session=sid, chat=True, changed_lines=0, files=1,
                        wall_clock_min=0, tracker_key=None, architectural=False,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    assert store.load(sid).node == Node.ROUTED.value
    d = cli.cmd_reset(_reset_ns(sid), store=store)
    assert d.ok is True
    assert store.load(sid).node == Node.CLASSIFIED.value


def test_reset_from_live_without_force_refuses(store, fixtures_dir):
    sid = "rs3"
    _to_plan_ready(store, sid, str(fixtures_dir / "plan_two_stage.toml"))
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    assert store.load(sid).node == Node.APPROVED.value
    d = cli.cmd_reset(_reset_ns(sid), store=store)
    assert d.ok is False
    assert d.action == "noop"
    # state unchanged
    after = store.load(sid)
    assert after.node == Node.APPROVED.value
    assert after.task_id == "demo"


def test_reset_from_live_with_force_rearms(store, fixtures_dir):
    sid = "rs4"
    _to_plan_ready(store, sid, str(fixtures_dir / "plan_two_stage.toml"))
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    d = cli.cmd_reset(_reset_ns(sid, force=True), store=store)
    assert d.ok is True
    fresh = store.load(sid)
    assert fresh.node == Node.CLASSIFIED.value
    assert fresh.task_id == "demo2"
