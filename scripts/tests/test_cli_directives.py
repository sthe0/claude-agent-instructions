"""Directive contract for the key commands: every command returns a Directive with
ok/node/action set, and the gate commands carry their return markers (submit-plan ->
PLAN-READY hard gate; resolve -> COMPLETED; loop guard -> ESCALATE)."""
import json as _json
from argparse import Namespace

import pytest

from agentctl import cli, plan, plugins
from agentctl import plugins_premise as pp
from agentctl.directive import Directive
from agentctl.state import Node, StageStatus


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


def test_small_change_markdown_plan_refused(store, tmp_path):
    """The TOML-only guard is a single all-weights check, not a substantive-only
    one: a SMALL_CHANGE session submitting a .md plan is refused with the same
    'plans are TOML-only' contract, not routed through the retired markdown
    validator (which only ever existed for the non-substantive case)."""
    sid = "small-md"
    _start(store, sid)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=5, files=1,
                        wall_clock_min=5, tracker_key=None, architectural=False,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    assert store.load(sid).weight_class == "SMALL_CHANGE"

    md_plan = tmp_path / "plan.md"
    md_plan.write_text("# a small-change plan\n", encoding="utf-8")
    d = cli.cmd_submit_plan(ns(session=sid, plan=str(md_plan)), store=store)

    assert d.ok is False
    assert d.action == "fix_plan"
    assert "toml" in d.detail.lower()


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
    d = cli.cmd_resolve(ns(session=sid, by="user", quality=5, quality_by="user-confirmed",
                           quality_note=None), store=store)
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


def _to_diagnosing(store, sid, fixtures_dir):
    _to_plan_ready(store, sid, str(fixtures_dir / "plan_two_stage.toml"))
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)
    cli.cmd_record_result(ns(session=sid, status="failed", actual="boom"), store=store)
    cli.cmd_declare(ns(session=sid, expected="e", actual="a", mismatch="m"), store=store)


def test_critique_does_not_announce_replan_while_blocked(store, fixtures_dir):
    """Regression: cmd_critique used to announce 'replan is now unblocked' as soon
    as the three difficulty sections existed, without reading the shape checks
    gates.difficulty_blockers enforces (>=2 distinct hypotheses). A one-hypothesis
    investigation leaves the gate blocked; the directive must say so, not lie —
    but the critique itself is still recorded, so a follow-up investigate+critique
    can proceed without redoing the declaration.
    """
    sid = "cblocked"
    _to_diagnosing(store, sid, fixtures_dir)
    cli.cmd_investigate(ns(session=sid, localized_expectation="le", localized_actual="la",
                           hypotheses=["h1"]), store=store)
    d = cli.cmd_critique(ns(session=sid, functional_ground="fg", replanning_task="rt",
                            invariants_to_preserve=[], differences_to_remove=[]), store=store)
    assert d.ok is False
    assert d.action != "replan"
    assert "investigation needs >=2 hypotheses" in d.detail
    assert store.load(sid).difficulty.critique is not None


def test_critique_announces_replan_when_record_is_complete(store, fixtures_dir):
    """Happy-path text must stay byte-identical: a well-shaped record (>=2 distinct
    hypotheses, non-placeholder declaration) still gets exactly today's directive."""
    sid = "ccomplete"
    _to_diagnosing(store, sid, fixtures_dir)
    cli.cmd_investigate(ns(session=sid, localized_expectation="le", localized_actual="la",
                           hypotheses=["h1", "h2"]), store=store)
    d = cli.cmd_critique(ns(session=sid, functional_ground="fg", replanning_task="rt",
                            invariants_to_preserve=[], differences_to_remove=[]), store=store)
    assert d.ok is True
    assert d.action == "replan"
    assert d.detail == "difficulty cycle complete; replan is now unblocked"


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
    cli.cmd_resolve(ns(session=sid, by="user", quality=5, quality_by="user-confirmed",
                       quality_note=None), store=store)
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

    d = cli.cmd_resolve(ns(session=sid, by="user", quality=5, quality_by="user-confirmed",
                           quality_note=None), store=store)
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

    d = cli.cmd_resolve(ns(session=sid, by="user", quality=5, quality_by="user-confirmed",
                           quality_note=None), store=store)
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


# --- verify-final failure routes into the difficulty cycle -------------------

def _to_verifying_all_passed_failing_finalcheck(store, sid, tmp_path):
    """Drive a session to VERIFYING with both stages PASSED but a final_check that
    always fails — a fresh minimal plan (stages carry no verify_command, mirroring
    plan_two_stage.toml) so the ONLY failure verify-final can surface is the
    final_check itself."""
    plan = tmp_path / "plan_failing_finalcheck.toml"
    plan.write_text(
        '[meta]\n'
        'task_id = "demo-failing-finalcheck"\n'
        'goal = "Pin verify-final routing a failing final_check into DIAGNOSING"\n'
        'done_criterion = "both stages PASSED and final_check green"\n'
        'criterion_type = "measurable"\n'
        '\n'
        '[[final_check]]\n'
        'label = "all green"\n'
        'command = "false"\n'
        'expected_exit = 0\n'
        '\n'
        '[[stage]]\n'
        'index = 1\n'
        'title = "Scaffold module"\n'
        'executor = "spawn:developer"\n'
        'expected_result_image = "module file exists"\n'
        'criterion_type = "measurable"\n'
        'done_criterion = "mod.py exists"\n'
        'depends_on = []\n'
        'output_artifacts = ["mod.py"]\n'
        '\n'
        '[[stage]]\n'
        'index = 2\n'
        'title = "Add tests"\n'
        'executor = "spawn:developer"\n'
        'expected_result_image = "tests exist"\n'
        'criterion_type = "measurable"\n'
        'done_criterion = "tests/test_mod.py exists"\n'
        'depends_on = [1]\n'
        'output_artifacts = ["tests/test_mod.py"]\n',
        encoding="utf-8",
    )

    _to_plan_ready(store, sid, str(plan))
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    for _ in range(2):
        cli.cmd_next_stage(ns(session=sid), store=store)
        cli.cmd_record_result(ns(session=sid, status="passed", actual="ok",
                               control="reviewed: ok"), store=store)


def test_verify_final_failure_routes_to_diagnosing(store, tmp_path):
    """A failing final_check is a difficulty like a failed stage: verify-final must
    not strand the session at VERIFYING (from which declare/investigate/critique all
    refuse) — it enters the DIAGNOSING cycle via the existing `diagnose` transition,
    exactly as a failed stage's record-result already does."""
    sid = "vf-diag"
    _to_verifying_all_passed_failing_finalcheck(store, sid, tmp_path)

    d = cli.cmd_verify_final(ns(session=sid), store=store)
    assert d.ok is False
    assert d.node == Node.DIAGNOSING.value
    assert d.action == "declare"
    assert d.data["failures"]
    assert any("final_check" in f for f in d.data["failures"])

    state = store.load(sid)
    assert state.node == Node.DIAGNOSING.value
    assert state.difficulty is not None

    # the cycle is now reachable: a follow-up declare is accepted, not refused
    d2 = cli.cmd_declare(
        ns(session=sid, expected="final_check passes", actual="final_check failed",
           mismatch="the 'all green' final_check exited nonzero"),
        store=store,
    )
    assert d2.ok is True
    assert d2.action == "investigate"


def test_verify_final_pending_stage_stays_fix_stages(store, fixtures_dir):
    """A merely-unfinished (not yet PASSED) stage is NOT a difficulty — the
    gates.blockers(state, "resolution") early-return must keep returning
    fix_stages at VERIFYING, untouched by the new failing-final_check routing."""
    sid = "vf-pending"
    plan = _to_executing_spawn(store, sid, fixtures_dir)
    # Only stage 1 is passed; stage 2 remains PENDING.
    cli.cmd_record_result(ns(session=sid, status="passed", actual="ok",
                           control="reviewed: ok"), store=store)

    d = cli.cmd_verify_final(ns(session=sid), store=store)
    assert d.ok is False
    assert d.action == "fix_stages"
    assert d.node == Node.VERIFYING.value
    state = store.load(sid)
    assert state.node == Node.VERIFYING.value
    assert state.difficulty is None


# --- approve/replan refresh caches from the plan file, not the submit-time copy ----

def _two_stage_plan_text(final_check_cmd="true", stage2_verify_cmd="true"):
    return (
        '[meta]\n'
        'task_id = "demo-refresh"\n'
        'goal = "Pin approve/no_change refreshing final_check/stage caches"\n'
        'done_criterion = "both stages PASSED and final_check green"\n'
        'criterion_type = "measurable"\n'
        '\n'
        '[[final_check]]\n'
        'label = "all green"\n'
        f'command = "{final_check_cmd}"\n'
        'expected_exit = 0\n'
        '\n'
        '[[stage]]\n'
        'index = 1\n'
        'title = "Scaffold module"\n'
        'executor = "spawn:developer"\n'
        'expected_result_image = "module file exists"\n'
        'criterion_type = "measurable"\n'
        'done_criterion = "mod.py exists"\n'
        'verify_command = "true"\n'
        'expected_exit = 0\n'
        'depends_on = []\n'
        'output_artifacts = ["mod.py"]\n'
        '\n'
        '[[stage]]\n'
        'index = 2\n'
        'title = "Add tests"\n'
        'executor = "spawn:developer"\n'
        'expected_result_image = "tests exist"\n'
        'criterion_type = "measurable"\n'
        'done_criterion = "tests/test_mod.py exists"\n'
        f'verify_command = "{stage2_verify_cmd}"\n'
        'expected_exit = 0\n'
        'depends_on = [1]\n'
        'output_artifacts = ["tests/test_mod.py"]\n'
    )


def test_approve_refreshes_caches_from_edited_plan(store, tmp_path):
    """approve snapshots+hashes plan_path but must also RELOAD it: a REVISE edit made
    while PLAN_READY (plan-mutable by design) must not leave state.final_check / a
    stage's verify_command pinned to the submit-time copy while plan_snapshot_hash
    attests to the edited bytes. Stage 1 is left untouched (unchanged carry key) and
    must keep its PASSED outcome; stage 2's verify_command is edited (changed carry
    key) and its PASSED outcome must be invalidated back to PENDING, since it no
    longer attests to the stage's current criterion."""
    sid = "refresh-approve"
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text(_two_stage_plan_text())
    _to_plan_ready(store, sid, str(plan_path))

    # Simulate both stages already PASSED from a prior (pre-review) approve/execute
    # pass, as if this were a re-approve after a REVISE-driven in-place edit.
    state = store.load(sid)
    state.stage(1).outcome.status = StageStatus.PASSED.value
    state.stage(2).outcome.status = StageStatus.PASSED.value
    store.save(state)

    # Edit the plan file in place (PLAN_READY is plan-mutable): final_check and
    # stage 2's verify_command change; stage 1 is left byte-for-byte identical.
    plan_path.write_text(_two_stage_plan_text(
        final_check_cmd="false", stage2_verify_cmd="python -c 'import mod'"))

    d = cli.cmd_approve(ns(session=sid, by="user"), store=store)
    assert d.ok is True

    state = store.load(sid)
    assert state.final_check[0].command == "false"
    assert state.stage(2).criterion.verify_command == "python -c 'import mod'"
    # Unchanged stage 1 carries its PASSED outcome forward.
    assert state.stage(1).outcome.status == StageStatus.PASSED.value
    # Changed stage 2's stale PASSED outcome no longer attests to the new
    # verify_command — it must be invalidated back to PENDING.
    assert state.stage(2).outcome.status == StageStatus.PENDING.value


def test_replan_no_change_refreshes_final_check(store, tmp_path):
    """A legacy (pre-snapshot) session self-diffs plan_path against itself in
    cmd_replan, so ANY in-place edit — including a final_check-only edit — lands in
    the no_change branch. That branch already refreshes per-stage prose/criterion
    fields via _apply_refined_stage_fields; final_check is meta-level and needs its
    own refresh next to it, or a stale final_check command silently survives a
    replan whose plan_snapshot_hash matches the edited file."""
    sid = "refresh-nochange"
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text(_two_stage_plan_text())
    _to_plan_ready(store, sid, str(plan_path))
    cli.cmd_approve(ns(session=sid, by="user"), store=store)

    # Force the legacy no-snapshot code path.
    state = store.load(sid)
    state.plan_snapshot_path = None
    state.plan_snapshot_hash = None
    store.save(state)
    assert store.load(sid).final_check[0].command == "true"

    plan_path.write_text(_two_stage_plan_text(final_check_cmd="false"))

    d = cli.cmd_replan(ns(session=sid, plan=str(plan_path)), store=store)
    assert d.ok is True

    state = store.load(sid)
    assert state.final_check[0].command == "false"


# --- cmd_replan composes the plan_approval plugin gate (stage 3) -----------------

def _approved_two_stage(store, sid, plan_path):
    """Drive a session to a post-approve state against `plan_path`. AGENTCTL_PREMISE
    is force-off in the suite (conftest), so premise never auto-activates and approve
    stays clean — each test below then arms premise MANUALLY on the live session, so
    the plan_approval plugin gate fires independently of the auto-activation knob."""
    _to_plan_ready(store, sid, str(plan_path))
    cli.cmd_approve(ns(session=sid, by="user"), store=store)


def test_replan_composes_plan_approval_plugin_gate(store, tmp_path):
    """cmd_replan must refuse a replan whose plan carries an undispositioned question,
    by the SAME plugins.plugin_gate_blockers(state, "plan_approval") composition
    cmd_approve uses. Without it a refinement/no_change replan rotates the plan bytes
    back to VERIFYING while the plugin gate never re-fires (the 2026-07-09
    attest-vs-execute hole). MUTATION ANCHOR: reverting the one-line composition flips
    the `blocked.ok is False` assertion — the replan would return ok=True."""
    sid = "replan-premise-gate"
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text(_two_stage_plan_text())
    _approved_two_stage(store, sid, plan_path)

    # Arm premise on the live session with a clean enumeration (matching the current
    # plan content) and ONE open question — isolating the open-question blocker from
    # the enumeration-staleness one.
    state = store.load(sid)
    plugins.activate(state, "premise")
    bag = state.plugins["premise"]
    bag["enumerated"] = True
    bag["enumerated_at"] = pp._plan_content_digest(plan.load_plan(str(plan_path)))
    bag["questions"] = [
        {"id": "q1", "target": "plan.goal", "question": "is the goal reachable?"},
    ]
    store.save(state)

    # A replan against the same approved plan is REFUSED: the open question is
    # undispositioned and the composed plugin gate catches it.
    blocked = cli.cmd_replan(ns(session=sid, plan=str(plan_path)), store=store)
    assert blocked.ok is False
    assert any("[premise]" in b and "open" in b for b in blocked.data["blockers"])

    # Disposing the question (assumed, with its required fields) clears the gate; the
    # same replan then proceeds.
    state = store.load(sid)
    state.plugins["premise"]["questions"] = [
        {"id": "q1", "target": "plan.goal", "question": "is the goal reachable?",
         "disposition": "assumed", "own_research": "read the tracker thread",
         "basis": "confirmed reachable by the reporter", "risk": "reporter may be wrong"},
    ]
    store.save(state)
    ok = cli.cmd_replan(ns(session=sid, plan=str(plan_path)), store=store)
    assert ok.ok is True


def test_replan_is_noop_without_active_plugin(store, tmp_path, monkeypatch):
    """A session with NO plugin extending plan_approval sees byte-identical replan
    behaviour: plugins.plugin_gate_blockers returns [] so the composition adds
    nothing. The gate is still EVALUATED (one telemetry row) and passes with an empty
    blocker set — the byte-identical no-op path the composition promises."""
    log_path = tmp_path / "gate-log.jsonl"
    monkeypatch.setattr(cli, "GATE_LOG", log_path)
    sid = "replan-no-plugin"
    plan_path = tmp_path / "plan.toml"
    plan_path.write_text(_two_stage_plan_text())
    _approved_two_stage(store, sid, plan_path)

    state = store.load(sid)
    assert "premise" not in state.plugins  # nothing extends plan_approval

    d = cli.cmd_replan(ns(session=sid, plan=str(plan_path)), store=store)
    assert d.ok is True

    rows = [_json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    plugin_rows = [r for r in rows if r["gate"] == "plan_approval_plugin"]
    assert len(plugin_rows) == 1
    assert plugin_rows[0]["passed"] is True
    assert plugin_rows[0]["blockers"] == []


def test_refinement_replan_rebinds_only_changed_stage_questions(store, tmp_path):
    """F6 pricing: a refinement replan that rewrites ONE stage must demand a rebind
    only for questions bound to THAT stage — a question bound to an untouched stage
    stays disposed. This is what keeps the composition from becoming a bypass trainer:
    it re-blocks a disposed question exactly when its bound stage's bytes changed,
    never indiscriminately."""
    sid = "replan-rebind-scope"
    old_path = tmp_path / "plan.toml"
    old_path.write_text(_two_stage_plan_text(stage2_verify_cmd="true"))
    _approved_two_stage(store, sid, old_path)

    # Stamp each stage's ORIGINAL question key so a disposed question binds to it.
    old_doc = plan.load_plan(str(old_path))
    key_by_index = {s.index: plan.stage_question_key(s) for s in old_doc.stages}

    def _assumed(qid, stage_index):
        return {
            "id": qid, "target": f"stage:{stage_index}.means",
            "question": f"is stage {stage_index}'s means sound?",
            "disposition": "assumed", "own_research": "read the surrounding code",
            "basis": "matches the existing working caller", "risk": "the caller may change",
            "disposed_at_key": key_by_index[stage_index],
        }

    # The refinement rewrites ONLY stage 2 (verify_command true -> false), so only
    # stage 2's stage_question_key moves; stage 1 stays byte-identical.
    new_path = tmp_path / "plan_refined.toml"
    new_path.write_text(_two_stage_plan_text(stage2_verify_cmd="false"))

    state = store.load(sid)
    plugins.activate(state, "premise")
    bag = state.plugins["premise"]
    bag["enumerated"] = True
    # enumeration re-run against the NEW content, so the ONLY live blocker is the
    # stale per-stage binding — not enumeration staleness.
    bag["enumerated_at"] = pp._plan_content_digest(plan.load_plan(str(new_path)))
    bag["questions"] = [_assumed("q1", 1), _assumed("q2", 2)]
    store.save(state)

    d = cli.cmd_replan(ns(session=sid, plan=str(new_path)), store=store)
    assert d.ok is False
    blockers = d.data["blockers"]
    rebind = [b for b in blockers if "changed since this question was disposed" in b]
    assert len(rebind) == 1
    assert "stage 2" in rebind[0]
    # stage 1's question — bound to the untouched stage — is NOT re-blocked.
    assert not any("stage 1" in b for b in blockers)
