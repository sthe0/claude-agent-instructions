"""`agentctl replan` out of DIAGNOSING, once this task's cross-session replan_count
reaches the Rule-of-Three ceiling (`effort-replan-absolute`): the diagnosing_replan
round-release axis (gates.py, stage 1 of this plan) refuses further replanning until
the order's customer makes an explicit renegotiation decision, recorded on
state.renegotiations (stage 2). This module wires that gate into `cmd_replan` itself
via three new flags and pins the five behaviors stage 3's brief names: below-threshold
is unaffected; at-threshold a bare replan is refused with the ceiling message and the
ESCALATE_TO_USER marker; `continue`/`rescope` clear the block, zero the cross-session
accumulator (also the concrete fix for GitHub #201's unbounded re-fire), and let the
replan proceed; a `--renegotiated-by` mismatching the order's customer_id is refused;
and `abandon` parks the session at BLOCKED without touching `--plan`."""
from argparse import Namespace

from agentctl import cli, task_accumulator
from agentctl.config import Thresholds
from agentctl.directive import DIRECTIVE_ESCALATE_TO_USER
from agentctl.state import Node, StageStatus


def ns(**kw):
    base = dict(renegotiation_decision=None, renegotiated_by=None, renegotiation_note=None)
    base.update(kw)
    return Namespace(**base)


TASK = "demo-renego"


def _replan_count():
    return task_accumulator.get(TASK)["per_axis_totals"]["replan_count"]


def _to_diagnosing(store, fixtures_dir, sid, *, normalize=True):
    """Drive a fresh session on TASK to DIAGNOSING with a complete difficulty record
    (declare/investigate/critique — what `difficulty_blockers` itself requires) and,
    unless `normalize=False`, a normalization too, so a replan that clears the
    renegotiation gate reaches the ordinary refinement/no_change/substantive branches
    exactly as it would with the gate absent."""
    plan = str(fixtures_dir / "plan_two_stage.toml")
    cli.cmd_start(ns(session=sid, task=TASK, goal="g", done_criterion="dc",
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
    cli.cmd_record_result(ns(session=sid, status="failed", actual="boom"), store=store)
    assert store.load(sid).node == Node.DIAGNOSING.value
    cli.cmd_declare(ns(session=sid, expected="e", actual="a", mismatch="m"), store=store)
    cli.cmd_investigate(ns(session=sid, localized_expectation="le", localized_actual="la",
                           hypotheses=["h1", "h2"]), store=store)
    cli.cmd_critique(ns(session=sid, functional_ground="fg", replanning_task="rt",
                        failure_address="нормативное"), store=store)
    if normalize:
        cli.cmd_normalize(ns(session=sid, factor="reproducible cause", level="note"), store=store)


def _seed_to_threshold(sid):
    thr = Thresholds().effort_replan_absolute()
    task_accumulator.add(TASK, "replan_count", thr, session_id=sid, now=None)
    return thr


def test_below_threshold_bare_replan_is_unaffected(store, fixtures_dir):
    sid = "a"
    _to_diagnosing(store, fixtures_dir, sid)
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")

    d = cli.cmd_replan(ns(session=sid, plan=refined), store=store)
    assert d.ok is True
    state = store.load(sid)
    assert state.node == Node.VERIFYING.value
    assert state.renegotiations == []


def test_at_threshold_bare_replan_is_refused(store, fixtures_dir):
    sid = "b"
    _to_diagnosing(store, fixtures_dir, sid)
    thr = _seed_to_threshold(sid)
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")

    d = cli.cmd_replan(ns(session=sid, plan=refined), store=store)
    assert d.ok is False
    assert d.marker == DIRECTIVE_ESCALATE_TO_USER
    assert "--renegotiation-decision" in d.detail
    assert str(thr) in d.detail
    state = store.load(sid)
    assert state.node == Node.DIAGNOSING.value          # untouched
    assert state.renegotiations == []
    assert _replan_count() == thr                       # not consumed by the refusal


def test_continue_clears_the_block_and_zeroes_the_accumulator(store, fixtures_dir):
    sid = "c"
    _to_diagnosing(store, fixtures_dir, sid)
    thr = _seed_to_threshold(sid)
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")

    d = cli.cmd_replan(ns(
        session=sid, plan=refined, renegotiation_decision="continue",
        renegotiated_by="user", renegotiation_note="customer wants to keep going",
    ), store=store)
    assert d.ok is True
    state = store.load(sid)
    assert state.node == Node.VERIFYING.value            # ordinary refinement completed
    assert len(state.renegotiations) == 1
    rec = state.renegotiations[0]
    assert rec["decision"] == "continue"
    assert rec["by"] == "user"
    assert rec["note"] == "customer wants to keep going"
    assert rec["task_replan_count_at_decision"] == thr
    # reset() zeroed the accumulator; the replan this call completes then logs its OWN
    # replan_count increment (the ordinary bookkeeping every replan does), so the count
    # after a successful renegotiated replan is 1, not 0 — the next Rule-of-Three
    # budget starts here, per GitHub #201's fix.
    assert _replan_count() == 1


def test_rescope_also_clears_the_block(store, fixtures_dir):
    sid = "c2"
    _to_diagnosing(store, fixtures_dir, sid)
    _seed_to_threshold(sid)
    bigger = str(fixtures_dir / "plan_two_stage_substantive.toml")

    d = cli.cmd_replan(ns(
        session=sid, plan=bigger, renegotiation_decision="rescope",
        renegotiated_by="user", renegotiation_note="customer wants to widen scope",
    ), store=store)
    assert d.ok is True
    assert d.marker == "PLAN-READY"
    state = store.load(sid)
    assert state.node == Node.PLAN_READY.value
    assert state.renegotiations[0]["decision"] == "rescope"
    assert _replan_count() == 1


def test_mismatched_renegotiated_by_is_refused(store, fixtures_dir, tmp_path):
    sid = "d"
    _to_diagnosing(store, fixtures_dir, sid)
    thr = _seed_to_threshold(sid)
    ordered = tmp_path / "plan_two_stage_refined_order.toml"
    ordered.write_text(
        (fixtures_dir / "plan_two_stage_refined.toml").read_text(encoding="utf-8")
        + '\n[meta.order]\ncustomer_id = "user"\ncustomer = "the product owner"\n'
          'functional_place = "the norm this order answers"\n',
        encoding="utf-8",
    )

    d = cli.cmd_replan(ns(
        session=sid, plan=str(ordered), renegotiation_decision="continue",
        renegotiated_by="someone-else", renegotiation_note="not the customer",
    ), store=store)
    assert d.ok is False
    assert "customer_id" in d.detail
    state = store.load(sid)
    assert state.node == Node.DIAGNOSING.value
    assert state.renegotiations == []
    assert _replan_count() == thr


def test_matching_renegotiated_by_against_a_declared_order_succeeds(store, fixtures_dir, tmp_path):
    sid = "d2"
    _to_diagnosing(store, fixtures_dir, sid)
    _seed_to_threshold(sid)
    ordered = tmp_path / "plan_two_stage_refined_order2.toml"
    ordered.write_text(
        (fixtures_dir / "plan_two_stage_refined.toml").read_text(encoding="utf-8")
        + '\n[meta.order]\ncustomer_id = "user"\ncustomer = "the product owner"\n'
          'functional_place = "the norm this order answers"\n',
        encoding="utf-8",
    )

    d = cli.cmd_replan(ns(
        session=sid, plan=str(ordered), renegotiation_decision="continue",
        renegotiated_by="user", renegotiation_note="matches the order",
    ), store=store)
    assert d.ok is True
    state = store.load(sid)
    # adding [meta.order] (absent from the original plan) makes this diff SUBSTANTIVE,
    # not refinement — the customer-id match is proven by the call succeeding at all
    # (a mismatch refuses before diff_plans is ever reached, per the mismatch test above).
    assert state.node == Node.PLAN_READY.value
    assert state.renegotiations[0]["by"] == "user"


def test_abandon_parks_at_blocked_without_touching_the_plan(store, fixtures_dir):
    sid = "e"
    _to_diagnosing(store, fixtures_dir, sid)
    thr = _seed_to_threshold(sid)
    before = store.load(sid)
    before_plan_path = before.plan_path
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")

    d = cli.cmd_replan(ns(
        session=sid, plan=refined, renegotiation_decision="abandon",
        renegotiated_by="user", renegotiation_note="customer wants to stop",
    ), store=store)
    assert d.ok is True
    assert d.marker == "ESCALATE"
    assert d.action == "unblock"
    state = store.load(sid)
    assert state.node == Node.BLOCKED.value
    assert state.blocked_from == Node.DIAGNOSING.value
    assert state.plan_path == before_plan_path            # args.plan never applied
    assert state.stage(1).outcome.status == StageStatus.FAILED.value  # untouched
    assert len(state.renegotiations) == 1
    assert state.renegotiations[0]["decision"] == "abandon"
    # the accumulator is NOT reset on abandon — nothing was renegotiated to continue
    assert _replan_count() == thr

    unblocked = cli.cmd_unblock(ns(session=sid), store=store)
    assert unblocked.ok is True
    assert store.load(sid).node == Node.DIAGNOSING.value


def test_empty_renegotiation_note_is_refused(store, fixtures_dir):
    sid = "f"
    _to_diagnosing(store, fixtures_dir, sid)
    _seed_to_threshold(sid)
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")

    d = cli.cmd_replan(ns(
        session=sid, plan=refined, renegotiation_decision="continue",
        renegotiated_by="user", renegotiation_note="   ",
    ), store=store)
    assert d.ok is False
    assert "--renegotiation-note" in d.detail
    state = store.load(sid)
    assert state.node == Node.DIAGNOSING.value
    assert state.renegotiations == []


def test_empty_renegotiated_by_is_refused(store, fixtures_dir):
    sid = "g"
    _to_diagnosing(store, fixtures_dir, sid)
    _seed_to_threshold(sid)
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")

    d = cli.cmd_replan(ns(
        session=sid, plan=refined, renegotiation_decision="continue",
        renegotiated_by="  ", renegotiation_note="fine",
    ), store=store)
    assert d.ok is False
    assert "--renegotiated-by" in d.detail
    state = store.load(sid)
    assert state.node == Node.DIAGNOSING.value
    assert state.renegotiations == []
