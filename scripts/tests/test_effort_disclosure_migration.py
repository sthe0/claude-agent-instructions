"""Per-segment effort disclosure (R6) and the migration switch of a session that was already armed
under the whole-window rule (R7): what the verify-final and resolve directives and the quality row
show, and how a migrated session goes from one window judgment to per-stage judgment."""
import json
import time
from argparse import Namespace

import pytest

from agentctl import cli_common, cli_final, effort, gates
from agentctl import effort_segments as seg
from agentctl.store import FileStateStore
from test_effort_armings_approvals import (
    FALLBACK, LARGE, MEDIUM, Eng, _env, armings_of, eng, open_of, plan_text, renew, retiers,
    with_added_stage, without_stage_one, without_stage_two)

USD, MIN = seg.SCALE_USD, seg.SCALE_MINUTES
PLANNING_LINE, CLOSING_LINE = "planning", "closing"


def settle_both(eng, usd=1.0, sid="s"):
    eng.executing(sid=sid)
    eng.book(usd=usd, sid=sid)
    assert eng.passed(sid, 1)["ok"]
    eng.run("next_stage", session=sid)
    assert eng.passed(sid, 2)["ok"]


def verify_final(eng, sid="s"):
    directive = eng.run("verify_final", session=sid)
    assert directive["ok"], directive
    return directive["data"]["effort"]


def resolve(eng, sid="s"):
    eng.run("plugin_record", session=sid, plugin="experience", phase="searched", note="")
    eng.run("plugin_record", session=sid, plugin="experience", phase="skipped", note="fixture")
    directive = eng.run("resolve", session=sid, by="user", quality=5, quality_by="user-confirmed")
    assert directive["ok"], directive
    return directive["data"]["effort"]


def quality_rows():
    path = cli_final.TASK_QUALITY_LOG
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def titles(disclosure):
    return [line["title"] for line in disclosure["lines"]]


def line_of(disclosure, title):
    return next(line for line in disclosure["lines"] if line["title"] == title)


def disclosure_of(eng, sid="s"):
    return cli_common._effort_disclosure(eng.state(sid))


def planning_armings(state):
    return [a for a in state.effort_segments["armings"] if a["segment"] == seg.PLANNING]


def to_v61(eng, sid="s"):
    """The session as the previous schema stored it, loaded once so its stage identities are stable."""
    path = eng.root / f"{sid}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for holder in (data, *data.get("plan_stack", [])):
        for key in ("stage_uids", "effort_segments", "effort_migrated"):
            holder.pop(key, None)
    data["schema_version"] = 61
    path.write_text(json.dumps(data), encoding="utf-8")
    eng.store.save(eng.state(sid))


def migrated(eng, text=None, usd=0.0, minutes=0.0, upto=0, sid="s"):
    """An armed session executing under an approved plan whose `upto` first stages have passed and
    which carries `usd`/`minutes` of work booked before the change."""
    eng.executing(text, sid)
    for index in range(1, upto + 1):
        assert eng.passed(sid, index)["ok"]
        eng.run("next_stage", session=sid)
    state = eng.state(sid)
    state.effort_actuals = {"spend_usd": usd, "active_minutes": minutes}
    state.effort_spend_seen = {state.plan_path: usd}
    eng.store.save(state)
    to_v61(eng, sid)
    assert eng.state(sid).effort_migrated["armed"] is True
    return sid


def resume(eng, sid="s"):
    """Execution continuing after an approval: partition assessed, the ready stage taken."""
    eng.run("partition", session=sid)
    assert eng.run("next_stage", session=sid)["ok"]


def window_of(eng, sid="s"):
    return seg.window_arming(eng.state(sid).effort_segments)


def user_replan(eng, text=None, sid="s"):
    """The user's own decision on a replan: the failed live stage worked through to a candidate
    plan that changes the stage set (a method-only edit is applied without approval) and that the
    user approves."""
    eng.critiqued(sid)
    eng.replanned(text, sid)
    approved = eng.run("approve", session=sid, by="user")
    assert approved["ok"] and approved["action"] != "no_action", approved
    return approved


# --- R6: what the directives and the quality row show -----------------------------------------


def test_verify_final_directive_carries_total_and_each_segments_actual_and_budget_line(eng):
    settle_both(eng)
    uids = [eng.state().stage_uid(1), eng.state().stage_uid(2)]
    shown = verify_final(eng)
    assert titles(shown) == [PLANNING_LINE, "Scaffold module", "Add tests", CLOSING_LINE]
    assert [line["identity"] for line in shown["lines"][1:3]] == uids
    assert [line["actual"][USD] for line in shown["lines"]] == [0.0, 1.0, 0.0, 0.0]
    assert [line["budget"] for line in shown["lines"]] == pytest.approx([FALLBACK, MEDIUM, MEDIUM, None])
    assert shown["lines"][0]["budget_source"] == seg.SOURCE_FALLBACK
    assert shown["total"]["actual"] == seg.conserve(eng.state().effort_segments)["total"]
    assert shown["total"]["actual"][USD] == 1.0
    assert shown["total"]["budget"] == pytest.approx({USD: 12.0, MIN: 65.4})
    assert shown["text"][:4] == [line["text"] for line in shown["lines"]]
    assert "Scaffold module [user-approved]: spend 1.00 of 3.00 (0.33x) USD" in shown["text"][1]
    assert shown["migrated"] is False


def test_resolve_directive_carries_total_and_each_segments_actual_and_budget_line(eng):
    settle_both(eng)
    verify_final(eng)
    shown = resolve(eng)
    assert titles(shown) == [PLANNING_LINE, "Scaffold module", "Add tests", CLOSING_LINE]
    assert [line["actual"][USD] for line in shown["lines"]] == [0.0, 1.0, 0.0, 0.0]
    assert shown["total"]["actual"][USD] == 1.0
    assert shown["total"]["budget"] == pytest.approx({USD: 12.0, MIN: 65.4})
    assert shown["total"]["multiple"][USD] == pytest.approx(1.0 / 12.0)
    assert shown["migrated"] is False


def test_quality_row_carries_total_and_each_segments_actual_and_budget_line(eng):
    settle_both(eng)
    verify_final(eng)
    shown = resolve(eng)
    rows = quality_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["effort_estimate"][effort.SCALE_SPEND] == pytest.approx(15.0)
    assert row["effort_actual"] == effort.deltas(eng.state())
    assert row["effort_fires"] == []
    assert row["effort_interactions"] == 0
    assert "effort_ratio_max" in row
    assert [line["title"] for line in row["effort_lines"]] == titles(shown)
    assert [line["actual"][USD] for line in row["effort_lines"]] == [0.0, 1.0, 0.0, 0.0]
    assert [line["armings"] for line in row["effort_lines"]] == [1, 1, 1, 1]
    assert row["effort_total"] == shown["total"]
    assert row["effort_migrated"] is False


def test_a_line_sums_actuals_and_budgets_over_its_armings_with_their_count(eng):
    eng.executing()
    uid = eng.state().stage_uid(1)
    eng.book(usd=1.0)
    renewed = renew(eng, seg.ACT_FIRE_ACK_BY_USER)
    assert renewed["arming_id"] != armings_of(eng.state(), uid)[0]["arming_id"]
    eng.book(usd=0.5, minutes=2.0)
    assert eng.passed(index=1)["ok"]
    eng.run("next_stage", session="s")
    assert eng.passed(index=2)["ok"]
    stage = line_of(verify_final(eng), "Scaffold module")
    assert stage["armings"] == 2
    assert stage["actual"] == pytest.approx({USD: 1.5, MIN: 2.0})
    assert stage["budget"] == pytest.approx({USD: 2 * MEDIUM[USD], MIN: 2 * MEDIUM[MIN]})
    assert stage["multiple"][USD] == pytest.approx(1.5 / 6.0)
    assert stage["text"].endswith("2 armings")
    resolve(eng)
    kept = line_of({"lines": quality_rows()[0]["effort_lines"]}, "Scaffold module")
    assert kept["armings"] == 2 and kept["actual"][USD] == pytest.approx(1.5)


def test_dropped_stage_keeps_its_line_and_a_never_current_stage_has_none(eng):
    eng.executing(sid="a")
    first = eng.state("a").stage_uid(1)
    eng.book(usd=2.0, sid="a")
    user_replan(eng, without_stage_one(), "a")
    shown = disclosure_of(eng, "a")
    dropped = next(line for line in shown["lines"] if line["identity"] == first)
    assert dropped["actual"][USD] == 2.0 and dropped["armings"] == 1
    assert shown["total"]["actual"][USD] == 2.0

    eng.executing(sid="b")
    second = eng.state("b").stage_uid(2)
    user_replan(eng, without_stage_two(), "b")
    shown = disclosure_of(eng, "b")
    assert second not in [line["identity"] for line in shown["lines"]]
    counted = [line["budget"] for line in shown["lines"] if line["kind"] != "closing"]
    assert shown["total"]["budget"] == {scale: sum(b[scale] for b in counted) for scale in seg.SCALES}


def test_planning_armings_sum_into_one_line(eng):
    eng.executing()
    state = eng.state()
    ledger = state.effort_segments
    first = planning_armings(state)[0]
    first["booked"][USD] = 0.4
    agreed = {USD: 2.0, MIN: 4.0}
    second = seg.open_arming(ledger, seg.PLANNING, agreed, seg.SOURCE_AGREED, time.time(), "test", spanned=False)
    second["booked"][USD] = 0.7
    eng.store.save(state)
    shown = disclosure_of(eng)
    planning = [line for line in shown["lines"] if line["kind"] == "planning"]
    assert len(planning) == 1
    assert planning[0]["armings"] == 2
    assert planning[0]["actual"][USD] == pytest.approx(1.1)
    assert planning[0]["budget"] == pytest.approx({USD: FALLBACK[USD] + 2.0, MIN: FALLBACK[MIN] + 4.0})
    assert planning[0]["budget_sources"] == [seg.SOURCE_FALLBACK, seg.SOURCE_AGREED]


def test_budget_lines_follow_r6_on_a_replanned_lazily_unfolded_fixture(eng):
    eng.executing(plan_text(stage1=("lazy = true",)))
    uid = eng.state().stage_uid(1)
    eng.book(usd=2.0)
    eng.critiqued()
    eng.replanned(with_added_stage(stage1=("lazy = true",)))
    assert eng.run("approve", session="s", by="agent")["ok"]
    shown = disclosure_of(eng)
    stage = line_of(shown, "Scaffold module")
    assert stage["identity"] == uid
    assert stage["budget"] == {scale: 2 * MEDIUM[scale] for scale in seg.SCALES}
    assert line_of(shown, PLANNING_LINE)["budget"] == pytest.approx(FALLBACK)


def test_unfolded_lazy_parent_line_carries_fallback(eng):
    eng.executing(plan_text(stage1=("lazy = true",)))
    parent = eng.unfolded(approve="user")
    shown = disclosure_of(eng)
    line = next(line for line in shown["lines"] if line["identity"] == parent)
    assert line["budget"] == pytest.approx(FALLBACK)
    assert line["budget_source"] == seg.SOURCE_FALLBACK
    assert f"[{seg.SOURCE_FALLBACK}]" in line["text"]
    assert titles(shown)[0] == PLANNING_LINE


def test_unfolded_unmarked_parent_line_carries_capped_budget(eng):
    eng.executing()
    parent = eng.unfolded(approve="user")
    line = next(line for line in disclosure_of(eng)["lines"] if line["identity"] == parent)
    assert line["budget"] == pytest.approx({scale: min(MEDIUM[scale], FALLBACK[scale]) for scale in seg.SCALES})
    assert line["budget_source"] == seg.SOURCE_FALLBACK_CAPPED


def test_resolve_books_before_quality_row(eng):
    settle_both(eng)
    verify_final(eng)
    eng.row(2.5)
    shown = resolve(eng)
    assert shown["total"]["actual"][USD] == pytest.approx(1.0 + 2.5)
    rows = quality_rows()
    assert len(rows) == 1
    assert rows[0]["effort_total"]["actual"][USD] == pytest.approx(3.5)


def test_carried_arming_line_shows_carried_total_counts_booked(eng):
    eng.executing()
    state = eng.state()
    arming = open_of(state, state.stage_uid(1))
    arming["carried"] = {USD: 2.0, MIN: 3.0}
    arming["booked"][USD] = 1.0
    eng.store.save(state)
    assert eng.passed(index=1)["ok"]
    eng.run("next_stage", session="s")
    assert eng.passed(index=2)["ok"]
    shown = verify_final(eng)
    stage = line_of(shown, "Scaffold module")
    assert stage["actual"] == pytest.approx({USD: 3.0, MIN: 3.0})
    assert stage["carried"] == {USD: 2.0, MIN: 3.0}
    assert ", carried 2.00/3.0" in stage["text"]
    assert shown["total"]["actual"][USD] == 1.0


# --- R7: the window judgment until the switch --------------------------------------------------


def test_migrated_session_keeps_whole_window_judgment_until_its_switch(eng):
    migrated(eng)
    window = window_of(eng)
    assert window["budget_source"] == seg.SOURCE_WINDOW and window["segment"] == seg.PLANNING
    assert eng.passed(index=1)["ok"]
    eng.run("next_stage", session="s")
    assert eng.passed(index=2)["ok"]
    verify_final(eng)
    assert window_of(eng)["arming_id"] == window["arming_id"]
    assert len(eng.state().effort_segments["armings"]) == 1
    resolve(eng)
    closed = next(a for a in eng.state().effort_segments["armings"] if a["arming_id"] == window["arming_id"])
    assert closed["closed_at"] is not None and closed["closed_by"] == "resolve"


def test_agent_self_approval_does_not_switch_a_migrated_session(eng):
    migrated(eng)
    window = window_of(eng)
    eng.critiqued()
    eng.replanned()
    assert eng.run("approve", session="s", by="agent")["ok"]
    state = eng.state()
    assert window_of(eng)["arming_id"] == window["arming_id"]
    assert len(state.effort_segments["armings"]) == 1
    assert state.effort_fires == []


def test_migrated_session_fires_at_a_failing_record_result_before_the_switch(eng):
    migrated(eng, usd=500.0)
    window = window_of(eng)
    eng.run("record_result", session="s", status="failed", actual="bad")
    fire = gates.open_fire_record(eng.state())
    assert fire is not None and fire["arming_id"] == window["arming_id"]
    assert window_of(eng)["arming_id"] == window["arming_id"]


def test_migrated_session_never_fires_at_a_passing_record_result(eng):
    migrated(eng, usd=500.0)
    assert eng.passed(index=1)["ok"]
    state = eng.state()
    assert state.effort_fires == [] and gates.open_fire_record(state) is None
    assert window_of(eng)["booked"][USD] == 500.0


def test_migrated_session_never_fires_at_verify_final(eng):
    migrated(eng, usd=500.0)
    assert eng.passed(index=1)["ok"]
    eng.run("next_stage", session="s")
    assert eng.passed(index=2)["ok"]
    shown = verify_final(eng)
    state = eng.state()
    assert state.effort_fires == [] and gates.open_fire_record(state) is None
    assert window_of(eng) is not None
    assert shown["lines"][0]["actual"][USD] == 500.0


def test_switch_books_the_window_total_to_the_planning_line(eng):
    migrated(eng, usd=2.0)
    eng.row(7.0)
    user_replan(eng)
    state = eng.state()
    window = planning_armings(state)[0]
    assert window["closed_by"] == seg.ACT_APPROVE_BY_USER
    assert window["booked"][USD] == pytest.approx(7.0)
    assert line_of(disclosure_of(eng), PLANNING_LINE)["actual"][USD] == pytest.approx(7.0)
    stage = open_of(state, state.stage_uid(1))
    assert seg.arming_actual(stage) == {USD: 0.0, MIN: 0.0}


def test_switch_closes_window_arming_with_a9_budget(eng):
    migrated(eng)
    eng.row(1.0)
    user_replan(eng)
    state = eng.state()
    window = planning_armings(state)[0]
    assert window["budget_source"] == seg.SOURCE_WINDOW
    assert window["budget"] == pytest.approx({USD: 15.0 - 2 * MEDIUM[USD], MIN: 50.0 - 2 * MEDIUM[MIN]})
    assert window["closed_by"] == seg.ACT_APPROVE_BY_USER and retiers(state) == []
    assert window["booked"][USD] == pytest.approx(1.0)
    stage = open_of(state, state.stage_uid(1))
    assert stage["budget_source"] == seg.SOURCE_USER_APPROVED and stage["opened_by"] == seg.ACT_APPROVE_BY_USER
    assert seg.arming_actual(stage)[USD] == 0.0


def test_switch_opens_live_stage_arming_from_zero(eng):
    migrated(eng, usd=2.0)
    eng.row(2.0 + 4.0)
    user_replan(eng)
    state = eng.state()
    window = planning_armings(state)[0]
    assert window["booked"][USD] == pytest.approx(6.0)
    assert window["budget"] == pytest.approx({USD: 9.0, MIN: 0.0})
    assert effort.deltas(state)[effort.SCALE_SPEND] == pytest.approx(6.0)
    stage = open_of(state, state.stage_uid(1))
    assert seg.arming_actual(stage) == {USD: 0.0, MIN: 0.0}
    assert stage["budget"] == MEDIUM and stage["opened_by"] == seg.ACT_APPROVE_BY_USER


def test_migrated_carried_fire_refuses_self_approval(eng):
    migrated(eng, usd=500.0)
    eng.run("record_result", session="s", status="failed", actual="bad")
    state = eng.state()
    assert gates.open_fire_record(state)["arming_id"] == window_of(eng)["arming_id"]
    assert any("fire-acknowledge" in blocker for blocker in gates.effort_fire_blockers(state))
    assert eng.run("dispatch", session="s", dry_run=True)["ok"] is False
    eng.run("fire_acknowledge", session="s", by="user", decision="continue")
    assert gates.effort_fire_blockers(eng.state()) == []


def test_switch_budget_is_the_estimate_minus_pending_and_live_tier_budgets_never_negative(eng):
    migrated(eng)
    user_replan(eng)
    planning = planning_armings(eng.state())[0]
    assert planning["budget"] == pytest.approx({USD: 9.0, MIN: 0.0})

    migrated(eng, sid="small")
    state = eng.state("small")
    state.effort_migrated["priced"]["estimate"] = {"spend": 1.0, "wall_clock": 10.0}
    eng.store.save(state)
    user_replan(eng, sid="small")
    assert planning_armings(eng.state("small"))[0]["budget"] == {USD: 0.0, MIN: 0.0}


def test_switch_budget_dropped_stage_gets_no_line_and_no_subtraction(eng):
    migrated(eng)
    dropped = eng.state().stage_uid(2)
    user_replan(eng, without_stage_two())
    shown = disclosure_of(eng)
    assert dropped not in [line["identity"] for line in shown["lines"]]
    assert line_of(shown, PLANNING_LINE)["budget"] == pytest.approx({USD: 15.0 - MEDIUM[USD], MIN: 50.0 - MEDIUM[MIN]})


def test_switch_budget_added_stage_subtracts_nothing_and_gets_its_own_line_later(eng):
    migrated(eng)
    user_replan(eng, with_added_stage())
    shown = disclosure_of(eng)
    assert "Add docs" not in titles(shown)
    assert line_of(shown, PLANNING_LINE)["budget"] == pytest.approx({USD: 9.0, MIN: 0.0})
    resume(eng)
    assert eng.passed(index=1)["ok"]
    eng.run("next_stage", session="s")
    assert eng.passed(index=2)["ok"]
    eng.run("next_stage", session="s")
    added = line_of(disclosure_of(eng), "Add docs")
    assert added["budget"] == pytest.approx(MEDIUM) and added["armings"] == 1


def test_switch_budget_retiered_stage_counts_once_at_each_value(eng):
    migrated(eng)
    first = eng.state().stage_uid(1)
    user_replan(eng, with_added_stage(stage1=('cost_tier = "large"',)))
    state = eng.state()
    assert planning_armings(state)[0]["budget"] == pytest.approx({USD: 9.0, MIN: 0.0})
    own = [line for line in disclosure_of(eng)["lines"] if line["identity"] == first]
    assert len(own) == 1 and own[0]["budget"] == LARGE
    assert own[0]["budget_source"] == seg.SOURCE_USER_APPROVED
    assert retiers(state) == []


def test_stage_settled_before_switch_has_no_line(eng):
    migrated(eng, upto=1)
    settled = eng.state().stage_uid(1)
    live = eng.state().stage_uid(2)
    assert titles(disclosure_of(eng)) == [PLANNING_LINE]
    user_replan(eng)
    shown = disclosure_of(eng)
    assert settled not in [line["identity"] for line in shown["lines"]]
    assert [line["armings"] for line in shown["lines"] if line["identity"] == live] == [1]
    assert line_of(shown, PLANNING_LINE)["budget"] == pytest.approx({USD: 15.0 - MEDIUM[USD], MIN: 50.0 - MEDIUM[MIN]})
    assert shown["total"]["budget"] == pytest.approx({USD: 15.0, MIN: 50.0})


def test_no_stage_inherits_the_window_and_settled_stages_carry_no_line(eng):
    migrated(eng, usd=3.0, upto=1)
    live = eng.state().stage_uid(2)
    user_replan(eng)
    state = eng.state()
    assert planning_armings(state)[0]["booked"][USD] == 3.0
    assert seg.arming_actual(open_of(state, live)) == {USD: 0.0, MIN: 0.0}
    resume(eng)
    assert eng.passed(index=2)["ok"]
    eng.run("next_stage", session="s")
    assert eng.passed(index=3)["ok"]
    verify_final(eng)
    shown = resolve(eng)
    assert titles(shown) == [PLANNING_LINE, "Add tests", "Add docs", CLOSING_LINE]
    assert shown["total"]["actual"][USD] == 3.0


def test_directives_and_quality_row_carry_the_planning_line_total_and_migrated_flag(eng):
    migrated(eng, usd=3.0, upto=1)
    assert eng.passed(index=2)["ok"]
    at_final = verify_final(eng)
    assert at_final["migrated"] is True and at_final["lines"][0]["kind"] == "planning"
    at_resolve = resolve(eng)
    assert at_resolve["migrated"] is True
    assert at_resolve["lines"][0]["actual"][USD] == 3.0
    row = quality_rows()[0]
    assert row["effort_migrated"] is True
    assert row["effort_lines"][0]["kind"] == "planning" and row["effort_lines"][0]["actual"][USD] == 3.0
    assert row["effort_total"]["actual"][USD] == 3.0


def test_per_stage_rule_applies_from_the_switch_on(eng):
    migrated(eng)
    state = eng.state()
    window_of(eng)["booked"][USD] = 31.0
    eng.store.save(state)
    assert cli_common._judge_act(eng.state(), seg.ACT_DISPATCH, store=eng.store) is None
    user_replan(eng)
    state = eng.state()
    live = open_of(state, state.stage_uid(1))
    live["booked"][USD] = 31.0
    eng.store.save(state)
    eng.run("dispatch", session="s", dry_run=True)
    fire = gates.open_fire_record(eng.state())
    assert fire is not None and fire["arming_id"] == live["arming_id"]


def test_the_switch_is_a_user_approval_concerning_a_segment_and_not_a_replans_scale_acknowledgment(eng):
    migrated(eng, usd=500.0, sid="agent")
    eng.run("record_result", session="agent", status="failed", actual="bad")
    assert gates.open_fire_record(eng.state("agent")) is not None
    eng.run("fire_acknowledge", session="agent", by="agent", decision="continue")
    assert window_of(eng, "agent") is not None

    migrated(eng, usd=500.0, sid="user")
    eng.run("record_result", session="user", status="failed", actual="bad")
    eng.run("fire_acknowledge", session="user", by="user", decision="continue")
    closed = planning_armings(eng.state("user"))[0]
    assert window_of(eng, "user") is None and closed["closed_by"] == seg.ACT_FIRE_ACK_BY_USER
    assert closed["fire"]["ack"]["by"] == "user"

    migrated(eng, sid="replans")
    state = eng.state("replans")
    state.history.extend({"event": "replan"} for _ in range(10))
    eng.store.save(state)
    eng.run("record_result", session="replans", status="failed", actual="bad")
    assert any(f.get("scale") == "replans" for f in eng.state("replans").effort_fires)
    eng.run("fire_acknowledge", session="replans", by="user", decision="continue")
    assert window_of(eng, "replans") is not None

    migrated(eng, sid="agree")
    state = eng.state("agree")
    cli_common._renew_order_agreement(state, eng.store, Namespace())
    assert planning_armings(state)[0]["closed_by"] == seg.ACT_ORDER_AGREE


def test_order_agreement_after_the_last_settlement_does_not_switch_a_migrated_session(eng):
    migrated(eng, upto=2)
    assert eng.state().all_stages_settled()
    state = eng.state()
    cli_common._renew_order_agreement(state, eng.store, Namespace())
    assert seg.window_arming(state.effort_segments) is not None
    verify_final(eng)
    resolve(eng)
    assert planning_armings(eng.state())[0]["closed_by"] == "resolve"


def test_verify_final_projects_the_planning_line_and_resolve_books_it(eng):
    migrated(eng, usd=3.0, upto=2)
    shown = verify_final(eng)
    planning = shown["lines"][0]
    assert planning["projected"] is True and planning["actual"][USD] == 3.0
    assert planning["budget"] == pytest.approx({USD: 15.0, MIN: 50.0}) and "(projected)" in planning["text"]
    assert shown["migrated"] is True
    booked = resolve(eng)
    assert "projected" not in booked["lines"][0] and booked["lines"][0]["actual"][USD] == 3.0
    assert titles(booked) == [PLANNING_LINE, CLOSING_LINE]
    assert planning_armings(eng.state())[0]["closed_by"] == "resolve"


def test_carried_fire_closes_at_the_users_acknowledgment_or_the_switch(eng):
    migrated(eng, usd=500.0, sid="ack")
    eng.run("record_result", session="ack", status="failed", actual="bad")
    fired = window_of(eng, "ack")
    assert fired["fire"] is not None and gates.open_fire_record(eng.state("ack")) is not None
    eng.run("fire_acknowledge", session="ack", by="user", decision="continue")
    closed = planning_armings(eng.state("ack"))[0]
    assert closed["fire"]["ack"]["by"] == "user" and gates.open_fire_record(eng.state("ack")) is None

    migrated(eng, usd=500.0, sid="switch")
    eng.run("record_result", session="switch", status="failed", actual="bad")
    state = eng.state("switch")
    cli_common._switch_window(state, eng.store, Namespace(), "user", seg.ACT_APPROVE_BY_USER)
    ack = planning_armings(state)[0]["fire"]["ack"]
    assert ack["by"] == "user" and ack["note"] == f"closed by {seg.ACT_APPROVE_BY_USER}"
    assert gates.open_fire_record(state) is None


# --- R7: a session still planning, and the first act after migration ---------------------------


def planning_migrated(eng, usd=0.0, sid="s"):
    eng.plan_ready(sid=sid)
    state = eng.state(sid)
    state.effort_actuals = {"spend_usd": usd, "active_minutes": 0.0}
    state.effort_spend_seen = {state.plan_path: usd}
    eng.store.save(state)
    to_v61(eng, sid)
    assert eng.state(sid).effort_migrated["armed"] is False


def test_still_planning_session_is_judged_against_the_fallback_from_its_migration_instant(eng):
    planning_migrated(eng, usd=500.0)
    state = eng.state()
    instant = state.effort_migrated["instant"]
    planning = open_of(state, "") if False else seg.open_arming_of(state.effort_segments, seg.PLANNING)
    assert planning["opened_at"] == instant and planning["budget"] is None
    stretch = [a for a in planning_armings(state) if a["closed_at"] is not None][0]
    assert stretch["booked"][USD] == 500.0
    assert cli_common._judge_act(state, seg.ACT_STAMP, store=eng.store) is None
    assert planning["budget"] == pytest.approx(FALLBACK) and planning["budget_source"] == seg.SOURCE_FALLBACK
    planning["booked"][USD] = 30.0
    fire = cli_common._judge_act(state, seg.ACT_STAMP, store=eng.store)
    assert fire is not None and fire["arming_id"] == planning["arming_id"]


def test_migrated_planning_session_judged_against_fallback_from_instant(eng):
    planning_migrated(eng, usd=500.0)
    assert cli_common.stamp_and_judge("s", time.time() + 1, store=eng.store)
    state = eng.state()
    planning = seg.open_arming_of(state.effort_segments, seg.PLANNING)
    assert planning["opened_at"] == state.effort_migrated["instant"]
    assert planning["budget"] == pytest.approx(FALLBACK) and planning["budget_source"] == seg.SOURCE_FALLBACK
    assert seg.arming_actual(planning) == {USD: 0.0, MIN: 0.0}
    assert state.effort_fires == []


def test_no_migrated_session_fires_at_its_first_act_for_work_no_limit_counted(eng):
    planning_migrated(eng, usd=500.0, sid="planning")
    assert cli_common._judge_act(eng.state("planning"), seg.ACT_STAMP, store=eng.store) is None
    assert eng.state("planning").effort_fires == []

    migrated(eng, usd=500.0, sid="armed")
    assert eng.passed("armed", 1)["ok"]
    eng.run("dispatch", session="armed", dry_run=True)
    state = eng.state("armed")
    assert state.effort_fires == [] and gates.open_fire_record(state) is None


def test_still_planning_migrated_session_quality_row_carries_the_migrated_flag(eng):
    planning_migrated(eng, usd=1.0)
    assert eng.run("approve", session="s", by="user")["ok"]
    eng.run("partition", session="s")
    eng.run("next_stage", session="s")
    assert eng.passed(index=1)["ok"]
    eng.run("next_stage", session="s")
    assert eng.passed(index=2)["ok"]
    verify_final(eng)
    shown = resolve(eng)
    assert shown["migrated"] is True
    row = quality_rows()[0]
    assert row["effort_migrated"] is True
    assert row["effort_lines"][0]["kind"] == "planning" and row["effort_total"]["actual"][USD] == pytest.approx(1.0)


# --- a pushed sub-plan under a stored schema-61 state ------------------------------------------


def test_schema_61_state_with_a_pushed_subplan_migrates_and_switches(eng):
    stored = json.loads("""{
 "schema_version": 61, "session_id": "s", "task_id": "child", "goal": "g",
 "node": "APPROVED", "route": "SPAWN", "weight_class": "SUBSTANTIVE",
 "plan_path": "/work/child.toml", "repo_root": "/work",
 "approval": {"armed": true, "by": "user", "name": "plan_approval", "passed": true},
 "resolution": {"armed": false, "by": null, "name": "resolution", "note": null, "passed": false},
 "overall_criterion_type": "measurable", "overall_done_criterion": "both stages PASSED",
 "effort_actuals": {"spend_usd": 7.5},
 "effort_baseline": {"interactions": 0.0, "replan_events": 0.0, "replans": 0.0, "spend": 0.0, "wall_clock": 0.0},
 "effort_estimate": {"spend": 15.0, "wall_clock": 50.0},
 "effort_fires": [],
 "stages": [
  {"index": 1, "title": "Scaffold module", "subject": {"material": "m", "result": "r"},
   "means": {"means": "m", "method": "x"}, "actor": {"executor": "spawn:developer"},
   "criterion": {"criterion_type": "measurable", "done_criterion": "import works"},
   "outcome": {"status": "PENDING"}, "output_artifacts": ["mod.py"]},
  {"index": 2, "title": "Add tests", "subject": {"material": "m", "result": "r"},
   "means": {"means": "m", "method": "x"}, "actor": {"executor": "spawn:developer"},
   "criterion": {"criterion_type": "measurable", "done_criterion": "tests green"},
   "outcome": {"status": "PENDING"}, "output_artifacts": ["tests/test_mod.py"],
   "supplies": [{"on": 1}]}
 ],
 "plan_stack": [
  {"node": "EXECUTING", "current_stage": 1, "originating_stage": 1, "goal": "g", "task_id": "parent",
   "route": "SPAWN", "plan_path": "/work/parent.toml", "repo_root": "/work",
   "approval": {"armed": true, "by": "user", "name": "plan_approval", "passed": true},
   "resolution": {"armed": false, "by": null, "name": "resolution", "note": null, "passed": false},
   "overall_criterion_type": "measurable", "overall_done_criterion": "dc",
   "effort_actuals": {"spend_usd": 1.0},
   "effort_baseline": {"interactions": 0.0, "replan_events": 0.0, "replans": 0.0, "spend": 0.0, "wall_clock": 0.0},
   "effort_estimate": {"spend": 15.0, "wall_clock": 50.0},
   "stages": [
    {"index": 1, "title": "Scaffold the parent", "lazy": true, "subject": {"material": "m", "result": "r"},
     "means": {"means": "m", "method": "x"}, "actor": {"executor": "spawn:developer"},
     "criterion": {"criterion_type": "measurable", "done_criterion": "module exists"},
     "outcome": {"status": "ACTIVE"}, "output_artifacts": ["parent.py"]}
   ]}
 ]
}""")
    eng.root.mkdir(parents=True, exist_ok=True)
    (eng.root / "s.json").write_text(json.dumps(stored), encoding="utf-8")
    state = eng.state()
    assert state.effort_migrated["armed"] is True
    window = seg.window_arming(state.effort_segments)
    assert window["segment"] == seg.PLANNING and window["budget_source"] == seg.SOURCE_WINDOW
    assert window["booked"][USD] == pytest.approx(7.5 + 1.0)
    live = {state.stage_uid(1), state.stage_uid(2)}
    assert set(state.effort_migrated["priced"]["stages"]) == live
    eng.store.save(state)
    assert eng.state().stage_uid(1) in live

    state = eng.state()
    cli_common._switch_window(state, eng.store, Namespace(), "user", seg.ACT_APPROVE_BY_USER)
    closed = planning_armings(state)[0]
    assert closed["budget"] == pytest.approx({USD: 15.0 - 2 * MEDIUM[USD], MIN: 0.0})
    assert closed["closed_by"] == seg.ACT_APPROVE_BY_USER and retiers(state) == []
