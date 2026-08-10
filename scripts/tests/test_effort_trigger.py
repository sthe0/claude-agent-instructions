"""Effort-divergence trigger wired into the core spine (stage 4): the two fire sites,
arming at approve, re-derivation on every replan branch, and the quality-ledger
surface — the ACTING half effort.py's own module docstring hands to cli.py.
effort.py's pure estimate/actual/divergence layer (the ranking fix, both re-arm
belts, the CUSTODY sentinel) and sub-plan custody (the five field-by-field seams)
are pinned in test_effort.py and test_subplan.py respectively; this file drives the
fire-site mechanism through the real commands so a fire actually routes the session
into DIAGNOSING with no user question asked.
"""
from __future__ import annotations

import json
from argparse import Namespace

import pytest

from agentctl import cli, effort
from agentctl.state import Node, StageStatus


def ns(**kw):
    return Namespace(**kw)


def _to_executing_stage1(store, sid, plan):
    cli.cmd_start(ns(session=sid, task="demo-two-stage", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)


def _write_cost_log(path, rows):
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )


# The two-stage fixture's re-derived spend estimate at arming: two spawn:developer
# stages at the medium tier (3.00 each) + 3 mandated reviews (1 initial + 2 developers
# + 0 replans since baseline) at the medium review tier (3.00 each) = 15.00.
_ESTIMATE_SPEND = 15.00


def test_approve_arms_and_derives_the_estimate(store, fixtures_dir):
    sid = "arm"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_executing_stage1(store, sid, plan)

    state = store.load(sid)
    assert effort.armed(state)
    assert state.effort_estimate[effort.SCALE_SPEND] == pytest.approx(_ESTIMATE_SPEND)
    assert state.effort_baseline is not None
    assert state.effort_fires == []


def test_record_result_fires_on_overrun_with_no_stage_marked_failed(store, fixtures_dir, tmp_path):
    """Fire site 1: a PASSING record-result whose accumulated spend has run past the
    multiple diverts the ordinary next_stage Directive into DIAGNOSING, with the
    pre-framed divergence carried on the Directive and no user question asked."""
    sid = "fs1"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_executing_stage1(store, sid, plan)

    cost_log = tmp_path / "costs.jsonl"
    _write_cost_log(cost_log, [{"plan_path": plan, "stage_index": 1, "cost_usd": 100.0}])

    d = cli.cmd_record_result(
        ns(session=sid, status="passed", actual="ok", control="reviewed: ok",
           observation="", cost_log=str(cost_log)),
        store=store,
    )

    assert d.ok is False
    assert d.node == Node.DIAGNOSING.value
    assert d.action == "declare"
    assert d.marker == "OVERCOME-DIFFICULTY"
    assert "missing something essential" in d.detail  # effort.py's _framing text
    fire = d.data["effort_divergence"]
    assert fire["scale"] == effort.SCALE_SPEND

    state = store.load(sid)
    assert state.node == Node.DIAGNOSING.value
    assert state.stage(1).outcome.status == StageStatus.PASSED.value  # the work genuinely passed
    assert state.difficulty is not None
    assert len(state.effort_fires) == 1


def test_verify_final_fires_when_late_cost_tips_the_ratio(store, fixtures_dir, tmp_path):
    """Fire site 2: a plan that never overruns during either record-result still fires
    at verify-final once the last mandated-review row lands — the plan spec's own
    rationale for making this fire site non-optional (a contracted plan may reach
    resolution without another record-result)."""
    sid = "fs2"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_executing_stage1(store, sid, plan)

    cost_log = tmp_path / "costs.jsonl"
    _write_cost_log(cost_log, [{"plan_path": plan, "stage_index": 1, "cost_usd": 10.0}])
    d1 = cli.cmd_record_result(
        ns(session=sid, status="passed", actual="ok", control="reviewed: ok",
           observation="", cost_log=str(cost_log)),
        store=store,
    )
    assert d1.ok is True and d1.action == "next_stage"

    cli.cmd_next_stage(ns(session=sid), store=store)
    _write_cost_log(cost_log, [
        {"plan_path": plan, "stage_index": 1, "cost_usd": 10.0},
        {"plan_path": plan, "stage_index": 2, "cost_usd": 10.0},
    ])
    d2 = cli.cmd_record_result(
        ns(session=sid, status="passed", actual="ok", control="reviewed: ok",
           observation="", cost_log=str(cost_log)),
        store=store,
    )
    assert d2.ok is True and d2.action == "verify_final"  # under threshold so far

    # A late, unattributed row (no stage_index) lands before verify-final runs —
    # e.g. a mandated review spawn. refresh_spend sums by plan_path alone.
    _write_cost_log(cost_log, [
        {"plan_path": plan, "stage_index": 1, "cost_usd": 10.0},
        {"plan_path": plan, "stage_index": 2, "cost_usd": 10.0},
        {"plan_path": plan, "cost_usd": 100.0},
    ])

    d3 = cli.cmd_verify_final(ns(session=sid, cost_log=str(cost_log)), store=store)

    assert d3.ok is False
    assert d3.node == Node.DIAGNOSING.value
    assert d3.marker == "OVERCOME-DIFFICULTY"
    fire = d3.data["effort_divergence"]
    assert fire["scale"] == effort.SCALE_SPEND

    state = store.load(sid)
    assert state.node == Node.DIAGNOSING.value
    assert state.stage(2).outcome.status == StageStatus.PASSED.value


def test_verify_final_failures_attach_a_live_divergence(store, tmp_path):
    """cmd_verify_final's `failures` branch (a failing final_check) must attach a
    live effort divergence exactly like a failed stage's record-result does — not
    silently drop it just because this early-return sits ahead of fire site 2's own
    end-of-function check (the ordering gap this fix closes)."""
    sid = "vf-fail-div"
    plan = tmp_path / "plan_failing_finalcheck.toml"
    plan.write_text(
        '[meta]\n'
        'weight_class = "small_change"\n'
        'task_id = "demo-failing-finalcheck"\n'
        'goal = "Pin verify-final attaching a live divergence on a failing final_check"\n'
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

    cli.cmd_start(ns(session=sid, task="demo-failing-finalcheck", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=str(plan)), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)

    cost_log = tmp_path / "costs.jsonl"
    _write_cost_log(cost_log, [])
    for _ in range(2):
        cli.cmd_next_stage(ns(session=sid), store=store)
        cli.cmd_record_result(
            ns(session=sid, status="passed", actual="ok", control="reviewed: ok",
               observation="", cost_log=str(cost_log)),
            store=store,
        )

    # A late, large cost row lands before verify-final — well past budget by the
    # time the failing final_check is (re-)evaluated.
    _write_cost_log(cost_log, [{"plan_path": str(plan), "cost_usd": 100.0}])

    d = cli.cmd_verify_final(ns(session=sid, cost_log=str(cost_log)), store=store)
    assert d.ok is False
    assert d.node == Node.DIAGNOSING.value
    assert d.data["failures"]  # the ordinary failing-final_check message, not div.framing
    assert d.data["effort_divergence"]["scale"] == effort.SCALE_SPEND

    state = store.load(sid)
    assert state.node == Node.DIAGNOSING.value
    assert len(state.effort_fires) == 1  # divergence()'s CALLER OBLIGATION honored


def test_verify_final_venue_refusal_attaches_a_live_divergence(store, tmp_path):
    """_diagnose_venue_refusal's `div is not None` branch — dead in the suite
    otherwise, since every other verify-final call reaches it with div=None (no
    delivery_worktree declared, so no venue ever refuses). A refusing
    [[final_check]] must attach a live effort divergence exactly like a failing
    final_check does (test_verify_final_failures_attach_a_live_divergence
    above) — not silently drop it just because the venue-refusal early return
    sits ahead of fire site 2's own end-of-function check."""
    sid = "vf-refuse-div"
    worktree = tmp_path / "never-created"  # declared but never created on disk
    plan = tmp_path / "plan_refusing_venue.toml"
    plan.write_text(
        '[meta]\n'
        'weight_class = "small_change"\n'
        'task_id = "demo-refusing-venue"\n'
        'goal = "Pin verify-final attaching a live divergence on a refusing venue"\n'
        'done_criterion = "both stages PASSED and final_check green"\n'
        'criterion_type = "measurable"\n'
        f'delivery_worktree = "{worktree}"\n'
        '\n'
        '[[final_check]]\n'
        'label = "all green"\n'
        'command = "true"\n'
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

    cli.cmd_start(ns(session=sid, task="demo-refusing-venue", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=str(plan)), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)

    cost_log = tmp_path / "costs.jsonl"
    _write_cost_log(cost_log, [])
    for _ in range(2):
        cli.cmd_next_stage(ns(session=sid), store=store)
        cli.cmd_record_result(
            ns(session=sid, status="passed", actual="ok", control="reviewed: ok",
               observation="", cost_log=str(cost_log)),
            store=store,
        )

    # A late, large cost row lands before verify-final — well past budget by the
    # time the refusing final_check venue is (re-)evaluated. The worktree is
    # never created, so the venue refuses before the harmless "true" ever runs.
    _write_cost_log(cost_log, [{"plan_path": str(plan), "cost_usd": 100.0}])
    assert not worktree.exists()

    d = cli.cmd_verify_final(ns(session=sid, cost_log=str(cost_log)), store=store)
    assert d.ok is False
    assert d.node == Node.DIAGNOSING.value
    assert d.marker == "OVERCOME-DIFFICULTY"
    assert d.data["effort_divergence"]["scale"] == effort.SCALE_SPEND

    state = store.load(sid)
    assert state.node == Node.DIAGNOSING.value
    assert len(state.effort_fires) == 1  # divergence()'s CALLER OBLIGATION honored


def test_failed_record_result_attaches_divergence_without_a_second_difficulty(store, fixtures_dir, tmp_path):
    """A stage that FAILS while also past its effort budget enters DIAGNOSING exactly
    as an ordinary failure would (one Difficulty, the failure message) — the
    divergence data rides along on the same Directive rather than opening a second
    diagnose cycle or re-transitioning."""
    sid = "failattach"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_executing_stage1(store, sid, plan)

    cost_log = tmp_path / "costs.jsonl"
    _write_cost_log(cost_log, [{"plan_path": plan, "stage_index": 1, "cost_usd": 100.0}])

    d = cli.cmd_record_result(
        ns(session=sid, status="failed", actual="boom", control="", observation="",
           cost_log=str(cost_log)),
        store=store,
    )

    assert d.ok is False
    assert d.node == Node.DIAGNOSING.value
    assert "stage 1 failed" in d.detail  # the ordinary failure message, not div.framing
    assert d.data["effort_divergence"]["scale"] == effort.SCALE_SPEND

    state = store.load(sid)
    assert state.stage(1).outcome.status == StageStatus.FAILED.value
    assert len(state.effort_fires) == 1  # divergence()'s CALLER OBLIGATION honored


def test_repeat_digest_escalate_does_not_spend_the_fire(store, fixtures_dir, tmp_path):
    """The repeat-digest loop guard returns ESCALATE BEFORE the diagnose transition
    (cli.py's failed branch, ahead of the divergence-attach block) — so a stage
    failing twice on the identical actual must never spend the effort-divergence
    fire, even when a live divergence exists by the second attempt. Accounting (the
    actual vector) is kept regardless — refresh_spend runs unconditionally ahead of
    the repeat check — only the ACT (recording/spending a fire) is skipped."""
    sid = "failattach"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_executing_stage1(store, sid, plan)

    cost_log = tmp_path / "costs.jsonl"
    _write_cost_log(cost_log, [])  # no overrun yet — the first failure must not fire

    d1 = cli.cmd_record_result(
        ns(session=sid, status="failed", actual="boom", control="", observation="",
           cost_log=str(cost_log)),
        store=store,
    )
    assert d1.node == Node.DIAGNOSING.value
    assert store.load(sid).effort_fires == []  # no divergence yet — nothing to spend

    cli.cmd_declare(ns(session=sid, expected="e", actual="a", mismatch="m"), store=store)
    cli.cmd_investigate(ns(session=sid, localized_expectation="le", localized_actual="la",
                           hypotheses=["h1", "h2"]), store=store)
    cli.cmd_critique(ns(session=sid, functional_ground="fg", replanning_task="rt",
                        failure_address="нормативное"), store=store)
    cli.cmd_normalize(ns(session=sid, factor="reproducible cause", level="note"), store=store)
    cli.cmd_replan(ns(session=sid, plan=plan), store=store)  # no_change; re-arms the stage
    cli.cmd_next_stage(ns(session=sid), store=store)

    # Now write the overrun — the SECOND attempt is well past budget, so a fire
    # would be live if the repeat-digest guard didn't short-circuit first.
    _write_cost_log(cost_log, [{"plan_path": plan, "stage_index": 1, "cost_usd": 100.0}])
    d2 = cli.cmd_record_result(
        ns(session=sid, status="failed", actual="boom", control="", observation="",
           cost_log=str(cost_log)),
        store=store,
    )
    assert d2.marker == "ESCALATE"

    state = store.load(sid)
    assert state.effort_fires == []  # never spent — ESCALATE precedes the diagnose transition
    assert state.effort_actuals[effort.ACTUAL_SPEND_KEY] == pytest.approx(100.0)  # accounting kept
    assert effort.divergence(state) is not None  # live and unspent — the guard didn't erase it


def test_kill_switch_suppresses_the_transition_but_not_the_accounting(store, fixtures_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCTL_EFFORT", "0")
    sid = "kill"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_executing_stage1(store, sid, plan)

    cost_log = tmp_path / "costs.jsonl"
    _write_cost_log(cost_log, [{"plan_path": plan, "stage_index": 1, "cost_usd": 100.0}])

    d = cli.cmd_record_result(
        ns(session=sid, status="passed", actual="ok", control="reviewed: ok",
           observation="", cost_log=str(cost_log)),
        store=store,
    )

    assert d.ok is True
    assert d.action == "next_stage"

    state = store.load(sid)
    assert state.node == Node.VERIFYING.value
    assert state.effort_fires == []  # never acted on
    # accounting still ran: the actual vector reflects the true overrun and a
    # divergence is still independently computable from stored state.
    assert state.effort_actuals[effort.ACTUAL_SPEND_KEY] == pytest.approx(100.0)
    assert effort.divergence(state) is not None


def test_kill_switch_suppresses_verify_final_clean_pass_fire_too(store, fixtures_dir, tmp_path, monkeypatch):
    """The kill switch must suppress fire site 2 (verify-final's clean-pass check)
    exactly as it suppresses fire site 1 — the prior kill-switch test pinned only
    record-result, leaving verify-final's own gates.effort_active() check unpinned."""
    monkeypatch.setenv("AGENTCTL_EFFORT", "0")
    sid = "kill-vf"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_executing_stage1(store, sid, plan)

    cost_log = tmp_path / "costs.jsonl"
    _write_cost_log(cost_log, [{"plan_path": plan, "stage_index": 1, "cost_usd": 10.0}])
    d1 = cli.cmd_record_result(
        ns(session=sid, status="passed", actual="ok", control="reviewed: ok",
           observation="", cost_log=str(cost_log)),
        store=store,
    )
    assert d1.ok is True and d1.action == "next_stage"

    cli.cmd_next_stage(ns(session=sid), store=store)
    _write_cost_log(cost_log, [
        {"plan_path": plan, "stage_index": 1, "cost_usd": 10.0},
        {"plan_path": plan, "stage_index": 2, "cost_usd": 10.0},
        {"plan_path": plan, "cost_usd": 100.0},  # same late overrun as the un-killed test
    ])
    d2 = cli.cmd_record_result(
        ns(session=sid, status="passed", actual="ok", control="reviewed: ok",
           observation="", cost_log=str(cost_log)),
        store=store,
    )
    assert d2.ok is True and d2.action == "verify_final"

    d3 = cli.cmd_verify_final(ns(session=sid, cost_log=str(cost_log)), store=store)

    assert d3.ok is True
    assert d3.action == "await_user_confirmation"  # never diverted into DIAGNOSING

    state = store.load(sid)
    assert state.node == Node.RESOLUTION.value
    assert state.effort_fires == []  # never acted on
    # accounting still ran: the actual vector reflects the true overrun and a
    # divergence is still independently computable from stored state.
    assert state.effort_actuals[effort.ACTUAL_SPEND_KEY] == pytest.approx(120.0)
    assert effort.divergence(state) is not None


def test_no_change_replan_rederives_against_current_replan_count(store, fixtures_dir):
    """cmd_replan's no_change branch (call site 3) re-derives the estimate against
    CURRENT history, not a stale cached value — proven by manually logging an extra
    replan event beforehand and observing the mandated-review count (and therefore
    the spend estimate) move, even though the stage list itself is unchanged."""
    sid = "nochg"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_executing_stage1(store, sid, plan)

    before = dict(store.load(sid).effort_estimate)

    state = store.load(sid)
    state.log("replan", kind="substantive")  # +1 replan since baseline
    store.save(state)

    cli.cmd_replan(ns(session=sid, plan=plan), store=store)

    after = store.load(sid).effort_estimate
    assert after[effort.SCALE_SPEND] == pytest.approx(before[effort.SCALE_SPEND] + 3.00)


def test_refinement_replan_rederives_the_estimate(store, fixtures_dir):
    """Proof of a fresh rederive() call, not a stale cached value: a replan event
    manually logged BEFORE this call (so it is already in history when the
    refinement branch's own rederive() runs) moves the mandated-review count, and
    therefore the stored spend estimate, even though the refined plan's stage list
    (title/prose only) is otherwise identical to the original."""
    sid = "refine"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_executing_stage1(store, sid, plan)

    state = store.load(sid)
    before = dict(state.effort_estimate)
    state.log("replan", kind="substantive")  # +1 replan since baseline
    store.save(state)

    cli.cmd_replan(ns(session=sid, plan=refined), store=store)

    after = store.load(sid).effort_estimate
    # +2 replans since baseline: the manually-logged one above, plus this
    # cmd_replan call's own "replan" event — counted because rederive() runs
    # AFTER state.log(), not before.
    assert after[effort.SCALE_SPEND] == pytest.approx(before[effort.SCALE_SPEND] + 2 * 3.00)


def test_replan_rederive_counts_its_own_in_flight_replan_event(store, fixtures_dir):
    """No pre-seeded replan event this time: a single real cmd_replan call must
    itself be counted in the re-derived estimate it produces. This is the ordering
    bug directly: rederive() reading a replan count that doesn't yet include the
    very replan being processed would under-count by exactly one review."""
    sid = "inflight"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_executing_stage1(store, sid, plan)

    before = dict(store.load(sid).effort_estimate)

    cli.cmd_replan(ns(session=sid, plan=refined), store=store)

    after = store.load(sid).effort_estimate
    # +1 replan since baseline: this cmd_replan call's own "replan" event.
    assert after[effort.SCALE_SPEND] == pytest.approx(before[effort.SCALE_SPEND] + 3.00)


def test_substantive_replan_rederives_the_estimate_over_the_new_stage_list(store, fixtures_dir):
    sid = "subst"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    bigger = str(fixtures_dir / "plan_two_stage_substantive.toml")
    _to_executing_stage1(store, sid, plan)
    before = store.load(sid).effort_estimate

    cli.cmd_replan(ns(session=sid, plan=bigger), store=store)

    state = store.load(sid)
    assert [s.index for s in state.stages] == [1, 2, 3]
    # 3 spawn:developer stages (medium, 3.00 each) + mandated reviews
    # (1 initial + 3 developers + 1 replan since baseline — this substantive
    # replan's own log() call now counts itself) at medium (3.00 each).
    assert state.effort_estimate[effort.SCALE_SPEND] == pytest.approx(3 * 3.00 + 5 * 3.00)
    assert state.effort_estimate[effort.SCALE_SPEND] != before[effort.SCALE_SPEND]


def test_fire_diagnose_replan_cycle_then_refires_on_renewed_overrun(store, fixtures_dir, tmp_path):
    """Re-arm end to end: belt 1 (baseline rebased at the fire) neutralizes the
    residual immediately after firing; a legitimate replan (belt 2's required event)
    then lets a FRESH overrun fire again, proving re-arm is not a permanent latch."""
    sid = "refire"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    _to_executing_stage1(store, sid, plan)

    cost_log = tmp_path / "costs.jsonl"
    _write_cost_log(cost_log, [{"plan_path": plan, "stage_index": 1, "cost_usd": 100.0}])
    d1 = cli.cmd_record_result(
        ns(session=sid, status="passed", actual="ok", control="reviewed: ok",
           observation="", cost_log=str(cost_log)),
        store=store,
    )
    assert d1.node == Node.DIAGNOSING.value
    assert len(store.load(sid).effort_fires) == 1

    cli.cmd_declare(ns(session=sid, expected="e", actual="a", mismatch="m"), store=store)
    cli.cmd_investigate(ns(session=sid, localized_expectation="le", localized_actual="la",
                           hypotheses=["h1", "h2"]), store=store)
    cli.cmd_critique(ns(session=sid, functional_ground="fg", replanning_task="rt",
                        failure_address="нормативное"), store=store)
    cli.cmd_normalize(ns(session=sid, factor="reproducible cause", level="note"), store=store)

    d_replan = cli.cmd_replan(ns(session=sid, plan=refined), store=store)
    assert d_replan.action == "next_stage"
    state = store.load(sid)
    assert state.node == Node.VERIFYING.value
    assert state.difficulty is None

    cli.cmd_next_stage(ns(session=sid), store=store)
    _write_cost_log(cost_log, [
        {"plan_path": refined, "stage_index": 1, "cost_usd": 100.0},
        {"plan_path": refined, "stage_index": 2, "cost_usd": 200.0},
    ])
    d2 = cli.cmd_record_result(
        ns(session=sid, status="passed", actual="ok", control="reviewed: ok",
           observation="", cost_log=str(cost_log)),
        store=store,
    )

    assert d2.node == Node.DIAGNOSING.value
    assert d2.marker == "OVERCOME-DIFFICULTY"
    assert len(store.load(sid).effort_fires) == 2


def test_quality_row_carries_both_effort_vectors(store, fixtures_dir):
    sid = "qr"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    cli.cmd_start(ns(session=sid, task="demo-two-stage", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    for _ in range(2):
        cli.cmd_next_stage(ns(session=sid), store=store)
        cli.cmd_record_result(ns(session=sid, status="passed", actual="ok",
                                 control="reviewed: ok", observation="",
                                 cost_log=None), store=store)
    cli.cmd_verify_final(ns(session=sid, cost_log=None), store=store)
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="searched",
                             note=""), store=store)
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="skipped",
                             note="test fixture, nothing to record"), store=store)
    cli.cmd_resolve(ns(session=sid, by="user", quality=5, quality_by="user-confirmed",
                       quality_note=None, cost_log=None), store=store)

    rows = [json.loads(line) for line in cli.TASK_QUALITY_LOG.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    row = rows[-1]
    assert row["effort_estimate"][effort.SCALE_SPEND] == pytest.approx(_ESTIMATE_SPEND)
    assert row["effort_actual"] == effort.deltas(store.load(sid))
    assert row["effort_fires"] == []
    assert row["effort_interactions"] == 0
    assert "effort_ratio_max" in row
