"""Directive contract for the key commands: every command returns a Directive with
ok/node/action set, and the gate commands carry their return markers (submit-plan ->
PLAN-READY hard gate; resolve -> COMPLETED; loop guard -> ESCALATE)."""
import json as _json
from argparse import Namespace

import pytest

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


def test_substantive_markdown_plan_refused(store, tmp_path):
    """A substantive plan submitted as .md is refused; session stays at PLANNING."""
    sid = "subst-md"
    _start(store, sid)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)

    md_plan = tmp_path / "plan.md"
    md_plan.write_text(
        "## Problem and done criteria\nFix.\n\n"
        "## Stages\nExpected result image: done\n\n"
        "## Final verification\nrun.\n\n"
        "## Risks\nnone\n",
        encoding="utf-8",
    )
    d = cli.cmd_submit_plan(ns(session=sid, plan=str(md_plan)), store=store)

    assert d.ok is False
    assert d.action == "fix_plan"
    assert "toml" in d.detail.lower() or "toml" in str(d.data).lower()
    state = store.load(sid)
    assert state.node == Node.PLANNING.value   # NOT stranded at PLAN_READY


def test_substantive_toml_plan_still_accepted(store, fixtures_dir):
    """A substantive session with a .toml plan still advances to PLAN_READY (regression guard)."""
    d = _to_plan_ready(store, "subst-toml", str(fixtures_dir / "plan_two_stage.toml"))
    assert d.ok is True
    assert d.marker == "PLAN-READY"


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


def test_measurable_record_result_unchanged_without_observation(store, fixtures_dir):
    """Regression: measurable record-result passes without --observation (unchanged behaviour)."""
    sid = "meas-obs"
    _to_plan_ready(store, sid, str(fixtures_dir / "plan_two_stage.toml"))
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)
    d = cli.cmd_record_result(ns(session=sid, status="passed", actual="ok",
                               control="reviewed: ok"), store=store)
    assert d.ok is True


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


def test_final_check_carried_to_state_at_submit(store, tmp_path):
    """[[final_check]] tables in a TOML plan are parsed and carried to session state."""
    plan = tmp_path / "plan_with_fc.toml"
    plan.write_text(
        '[meta]\ntask_id = "fc-test"\ngoal = "g"\ndone_criterion = "dc"\n'
        'criterion_type = "measurable"\n\n'
        '[[stage]]\nindex = 1\ntitle = "s"\nexecutor = "in_thread"\n'
        'expected_result_image = "img"\ndone_criterion = "dc"\n\n'
        '[[final_check]]\ncommand = "pytest -q"\nexpected_exit = 0\nlabel = "suite"\n',
        encoding="utf-8",
    )
    sid = "fc-carry"
    _start(store, sid)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    d = cli.cmd_submit_plan(ns(session=sid, plan=str(plan)), store=store)
    assert d.ok is True
    state = store.load(sid)
    assert len(state.final_check) == 1
    assert state.final_check[0].command == "pytest -q"
    assert state.final_check[0].expected_exit == 0
    assert state.final_check[0].label == "suite"


# --- cost attribution via --cost-log -----------------------------------------

def _to_executing_spawn(store, sid, fixtures_dir):
    """Drive a session to EXECUTING with a spawn:developer stage 1 active."""
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_plan_ready(store, sid, plan)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)
    return plan


def test_record_result_spawn_stage_attributes_cost(store, fixtures_dir, tmp_path):
    """record-result with --cost-log populates Outcome cost fields on spawn stages."""
    sid = "cost-attr"
    plan = _to_executing_spawn(store, sid, fixtures_dir)

    cost_log = tmp_path / "costs.jsonl"
    cost_log.write_text(
        _json.dumps({"plan_path": plan, "stage_index": 1,
                     "cost_usd": 0.75, "duration_ms": 1200}) + "\n",
        encoding="utf-8",
    )

    d = cli.cmd_record_result(
        ns(session=sid, status="passed", actual="ok", control="reviewed: ok",
           cost_log=str(cost_log)),
        store=store,
    )
    assert d.ok is True
    state = store.load(sid)
    stage1 = state.stage(1)
    assert stage1.outcome.status == "PASSED"
    assert stage1.outcome.cost_usd == pytest.approx(0.75)
    assert stage1.outcome.duration_ms == 1200
    assert stage1.outcome.spawn_count == 1


def test_record_result_spawn_stage_sums_multiple_log_rows(store, fixtures_dir, tmp_path):
    """Multiple log rows for the same stage are summed (retries)."""
    sid = "cost-sum"
    plan = _to_executing_spawn(store, sid, fixtures_dir)

    cost_log = tmp_path / "costs.jsonl"
    cost_log.write_text(
        _json.dumps({"plan_path": plan, "stage_index": 1,
                     "cost_usd": 0.5, "duration_ms": 800}) + "\n" +
        _json.dumps({"plan_path": plan, "stage_index": 1,
                     "cost_usd": 0.3, "duration_ms": 400}) + "\n",
        encoding="utf-8",
    )

    d = cli.cmd_record_result(
        ns(session=sid, status="passed", actual="ok", control="reviewed: ok",
           cost_log=str(cost_log)),
        store=store,
    )
    assert d.ok is True
    state = store.load(sid)
    stage1 = state.stage(1)
    assert stage1.outcome.cost_usd == pytest.approx(0.8)
    assert stage1.outcome.duration_ms == 1200
    assert stage1.outcome.spawn_count == 2


def test_record_result_missing_cost_log_leaves_none(store, fixtures_dir, tmp_path):
    """A non-existent cost log degrades gracefully — cost fields stay None."""
    sid = "cost-none"
    _to_executing_spawn(store, sid, fixtures_dir)

    missing = tmp_path / "no_such_file.jsonl"

    d = cli.cmd_record_result(
        ns(session=sid, status="passed", actual="ok", control="reviewed: ok",
           cost_log=str(missing)),
        store=store,
    )
    assert d.ok is True
    state = store.load(sid)
    stage1 = state.stage(1)
    assert stage1.outcome.cost_usd is None
    assert stage1.outcome.duration_ms is None
    assert stage1.outcome.spawn_count == 0


def test_verify_final_populates_state_cost(store, fixtures_dir, tmp_path):
    """verify-final computes a CostRollup and stores it on state.cost."""
    sid = "cost-vf"
    plan = _to_executing_spawn(store, sid, fixtures_dir)

    cost_log = tmp_path / "costs.jsonl"
    cost_log.write_text(
        _json.dumps({"plan_path": plan, "stage_index": 1,
                     "cost_usd": 1.0, "duration_ms": 2000}) + "\n",
        encoding="utf-8",
    )

    # Pass stage 1
    cli.cmd_record_result(
        ns(session=sid, status="passed", actual="ok", control="reviewed: ok",
           cost_log=str(cost_log)),
        store=store,
    )
    # Pass stage 2 (no cost log for this one)
    cli.cmd_next_stage(ns(session=sid), store=store)
    cli.cmd_record_result(
        ns(session=sid, status="passed", actual="ok", control="reviewed: ok",
           cost_log=str(tmp_path / "empty.jsonl")),
        store=store,
    )

    d = cli.cmd_verify_final(ns(session=sid), store=store)
    assert d.ok is True
    state = store.load(sid)
    assert state.cost is not None
    assert state.cost.total_cost_usd == pytest.approx(1.0)
    assert state.cost.total_duration_ms == 2000
    assert state.cost.spawn_count == 1
    assert state.cost.attributed_stages == 1
    assert state.cost.note != ""
    # Directive carries the rollup
    assert "cost" in d.data
    assert d.data["cost"]["total_cost_usd"] == pytest.approx(1.0)
    assert d.data["cost"]["attributed_stages"] == 1


def test_resolve_directive_carries_cost(store, fixtures_dir, tmp_path):
    """resolve Directive.data includes the plan cost total."""
    sid = "cost-res"
    plan = _to_executing_spawn(store, sid, fixtures_dir)

    cost_log = tmp_path / "costs.jsonl"
    cost_log.write_text(
        _json.dumps({"plan_path": plan, "stage_index": 1,
                     "cost_usd": 0.5, "duration_ms": 1000}) + "\n" +
        _json.dumps({"plan_path": plan, "stage_index": 2,
                     "cost_usd": 0.25, "duration_ms": 500}) + "\n",
        encoding="utf-8",
    )

    for _ in range(2):
        cli.cmd_record_result(
            ns(session=sid, status="passed", actual="ok", control="reviewed: ok",
               cost_log=str(cost_log)),
            store=store,
        )
        cli.cmd_next_stage(ns(session=sid), store=store)

    # All stages passed; verify-final arms the resolution gate
    d_vf = cli.cmd_verify_final(ns(session=sid), store=store)
    assert d_vf.ok is True

    # Satisfy experience plugin gate
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="searched"), store=store)
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="recorded"), store=store)

    d = cli.cmd_resolve(ns(session=sid, by="user"), store=store)
    assert d.ok is True
    assert "cost" in d.data
    assert d.data["cost"]["total_cost_usd"] == pytest.approx(0.75)
    assert d.data["cost"]["total_duration_ms"] == 1500
    assert d.data["cost"]["spawn_count"] == 2
    assert d.data["cost"]["attributed_stages"] == 2


def test_resolve_without_attributed_cost_has_empty_cost_dict(store, fixtures_dir):
    """When no cost was attributed, resolve Directive still carries cost key (empty)."""
    sid = "cost-empty"
    _to_executing_spawn(store, sid, fixtures_dir)

    for _ in range(2):
        cli.cmd_record_result(
            ns(session=sid, status="passed", actual="ok", control="reviewed: ok"),
            store=store,
        )
        cli.cmd_next_stage(ns(session=sid), store=store)

    cli.cmd_verify_final(ns(session=sid), store=store)
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="searched"), store=store)
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="recorded"), store=store)

    d = cli.cmd_resolve(ns(session=sid, by="user"), store=store)
    assert d.ok is True
    # cost key is present but values are None/zero
    assert "cost" in d.data
    assert d.data["cost"].get("total_cost_usd") is None
    assert d.data["cost"].get("spawn_count") == 0


def test_record_result_inthread_stage_leaves_cost_none(store, tmp_path):
    """An in-thread (small-change) stage always leaves cost fields None."""
    sid = "cost-inthread"
    _start(store, sid)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=5, files=1,
                        wall_clock_min=5, tracker_key=None, architectural=False,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)

    # write a log that has a matching row — it must be ignored for in-thread stages
    cost_log = tmp_path / "costs.jsonl"
    cost_log.write_text(
        _json.dumps({"plan_path": "whatever", "stage_index": 1,
                     "cost_usd": 1.0, "duration_ms": 500}) + "\n",
        encoding="utf-8",
    )

    state = store.load(sid)
    assert not state.active_stage().is_spawn()

    d = cli.cmd_record_result(
        ns(session=sid, status="passed", actual="ok", control=None,
           cost_log=str(cost_log)),
        store=store,
    )
    assert d.ok is True
    state = store.load(sid)
    s = state.stage(1)
    assert s.outcome.cost_usd is None
    assert s.outcome.duration_ms is None
    assert s.outcome.spawn_count == 0
