"""The per-stage effort judgment: which act judges which arming, and what a fire or a settlement does."""
import contextlib
import io
import json
from pathlib import Path

import pytest

from agentctl import cli, cli_common, cli_replan, cost, effort, gates
from agentctl import effort_segments as seg
from agentctl.config import Thresholds
from agentctl.store import FileStateStore
from conftest import STAGE_OBSERVATIONS, SUBSTANTIVE_ORDER, declare_venue

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TWO_STAGE = (FIXTURES / "plan_two_stage.toml").read_text(encoding="utf-8")
FLOOR = {seg.SCALE_USD: 8.0, seg.SCALE_MINUTES: 60.0}
MEDIUM = {seg.SCALE_USD: 3.0, seg.SCALE_MINUTES: 25.0}
SMALL = {seg.SCALE_USD: 1.0, seg.SCALE_MINUTES: 10.0}
LARGE = {seg.SCALE_USD: 8.0, seg.SCALE_MINUTES: 60.0}


def with_order(text=TWO_STAGE):
    head, sep, tail = text.partition("\n[[stage]]")
    return head + "\n" + SUBSTANTIVE_ORDER + sep + tail


class Eng:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.root = tmp_path / "state"
        self.store = FileStateStore(self.root)
        self._plans = 0

    def run(self, cmd, **flags):
        argv = ["--state-root", str(self.root), cmd.replace("_", "-")]
        for name, value in flags.items():
            flag = "--" + name.replace("_", "-")
            if value is True:
                argv.append(flag)
            elif value is not None and value is not False:
                argv.extend([flag, str(value)])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(argv)
        result = json.loads(out.getvalue())
        result["rc"] = rc
        return result

    def write(self, text=None):
        self._plans += 1
        path = self.tmp / f"plan{self._plans}.toml"
        path.write_text(declare_venue(text or with_order(), self.tmp), encoding="utf-8")
        return str(path)

    def state(self, sid):
        return self.store.load(sid)

    def executing(self, sid):
        self.run("start", session=sid, task=f"task-{sid}", goal="g", done_criterion="dc")
        self.run("classify", session=sid, architectural=True, changed_lines=200, wall_clock_min=60)
        self.run("plan", session=sid)
        self.run("submit_plan", session=sid, plan=self.write())
        assert self.run("approve", session=sid, by="user")["ok"]
        self.run("partition", session=sid)
        return self.run("next_stage", session=sid)

    def passed(self, sid, index=1):
        return self.run("record_result", session=sid, status="passed", actual="ok",
                        control="reviewed: ok", observation=STAGE_OBSERVATIONS[index - 1])

    def failed(self, sid):
        return self.run("record_result", session=sid, status="failed", actual="bad")

    def overspend(self, sid, *, usd=0.0, minutes=0.0, index=1):
        """Book onto the stage's arming directly, as the ledger would."""
        state = self.state(sid)
        arming = stage_arming(state, index)
        arming["booked"][seg.SCALE_USD] = arming["booked"].get(seg.SCALE_USD, 0.0) + usd
        arming["booked"][seg.SCALE_MINUTES] = arming["booked"].get(seg.SCALE_MINUTES, 0.0) + minutes
        self.store.save(state)
        return arming


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")
    monkeypatch.setattr(cost, "COST_LOG", tmp_path / "costs.jsonl")


@pytest.fixture
def eng(tmp_path):
    return Eng(tmp_path)


def stage_arming(state, index):
    return seg.open_arming_of(state.effort_segments, seg.stage_segment(state.stage_uid(index)))


def arming(*, usd=0.0, minutes=0.0, budget=MEDIUM, segment="stage:u1"):
    ledger = {}
    built = seg.open_arming(ledger, segment, dict(budget), seg.SOURCE_DECLARED, 0.0, "t")
    built["booked"] = {seg.SCALE_USD: usd, seg.SCALE_MINUTES: minutes}
    return built


def judged(built, multiple=5.0, floor=None):
    return seg.judge(built, multiple, FLOOR if floor is None else floor)


def executing_overspent(eng, sid="s", **spend):
    eng.executing(sid)
    eng.overspend(sid, **spend)


def test_failing_record_result_judges_the_live_stage(eng):
    executing_overspent(eng, usd=30.0)
    directive = eng.failed("s")
    assert directive["data"]["effort_divergence"]["arming_id"]
    assert eng.state("s").node == "DIAGNOSING"


def test_failing_record_result_under_open_fire_goes_to_diagnosing_with_fire_open(eng):
    executing_overspent(eng, usd=30.0)
    eng.run("dispatch", session="s")
    assert gates.open_fire_record(eng.state("s")) is not None
    eng.failed("s")
    state = eng.state("s")
    assert state.node == "DIAGNOSING"
    assert gates.open_fire_record(state) is not None


def test_dispatch_judges_the_live_stage(eng):
    executing_overspent(eng, usd=30.0)
    directive = eng.run("dispatch", session="s")
    assert directive["ok"] is False
    assert directive["data"]["effort_fire"]["arming_id"]


def test_open_fire_refuses_a_dispatch(eng):
    executing_overspent(eng, usd=30.0)
    eng.run("dispatch", session="s")
    second = eng.run("dispatch", session="s")
    assert second["ok"] is False
    assert second["marker"] == "ESCALATE_TO_USER"
    assert len([f for f in eng.state("s").effort_fires if f.get("arming_id")]) == 1


def test_agent_skip_of_an_overspending_stage_fires_and_is_refused(eng):
    executing_overspent(eng, usd=30.0)
    state = eng.state("s")
    refusal = cli_common._judge_and_refuse(state, eng.store, None, seg.ACT_AGENT_SKIP, "agent skip")
    assert refusal is not None and refusal.ok is False
    assert stage_arming(state, 1)["closed_at"] is None


def test_open_fire_refuses_a_skip(eng):
    executing_overspent(eng, usd=30.0)
    eng.run("dispatch", session="s")
    state = eng.state("s")
    refusal = cli_common._judge_and_refuse(state, eng.store, None, seg.ACT_AGENT_SKIP, "agent skip")
    assert refusal is not None and refusal.ok is False
    assert stage_arming(state, 1)["closed_at"] is None


def test_agent_skip_that_raises_no_fire_settles_with_retier_record(eng):
    eng.executing("s")
    state = eng.state("s")
    stage = state.stages[0]
    assert cli_common._judge_and_refuse(state, eng.store, None, seg.ACT_AGENT_SKIP, "agent skip") is None
    stage_arming(state, 1)["booked"][seg.SCALE_MINUTES] = 130.0
    cli_common._settle_stage_arming(state, stage, seg.ACT_AGENT_SKIP, cli_common._utcnow())
    assert state.effort_segments["retier"][-1]["act"] == seg.ACT_AGENT_SKIP


def test_agent_settling_acts_judge_first_and_settle_only_without_a_fire():
    assert seg.ACT_AGENT_SKIP in seg.LIVE_STAGE_JUDGING_ACTS
    assert seg.ACT_APPROVE_BY_AGENT in seg.LIVE_STAGE_JUDGING_ACTS
    assert seg.ACT_PASSING_RECORD_RESULT not in seg.LIVE_STAGE_JUDGING_ACTS


def test_a_settling_act_settles_instead_of_judging():
    assert seg.scales_to_fire(arming(usd=30.0), seg.ACT_PASSING_RECORD_RESULT, 5.0, FLOOR) == []
    assert seg.scales_to_fire(arming(usd=30.0), seg.ACT_DISPATCH, 5.0, FLOOR) == [seg.SCALE_USD]


def test_replan_and_its_review_cycle_acts_judge_the_live_stage():
    for act in (seg.ACT_REPLAN, seg.ACT_SUBMIT_PLAN, seg.ACT_PLAN_REVIEW, seg.ACT_PRESENT_PLAN,
                seg.ACT_APPROVE_BY_AGENT):
        assert seg.scales_to_fire(arming(usd=30.0), act, 5.0, FLOOR) == [seg.SCALE_USD]


def test_judging_act_sets_are_closed_declared_tuples():
    assert isinstance(seg.LIVE_STAGE_JUDGING_ACTS, tuple)
    assert isinstance(seg.PLANNING_JUDGING_ACTS, tuple)
    assert seg.judging_acts_of(seg.stage_segment("u")) is seg.LIVE_STAGE_JUDGING_ACTS
    assert seg.judging_acts_of(seg.PLANNING) is seg.PLANNING_JUDGING_ACTS
    assert seg.judging_acts_of(seg.CLOSING) == ()


def test_prompt_boundary_stamp_judges_an_in_thread_stage(eng):
    executing_overspent(eng, usd=30.0)
    first = cli_common._utcnow()
    assert cli_common.stamp_and_judge("s", first, store=eng.store) is True
    assert cli_common.stamp_and_judge("s", first + 1.0, store=eng.store) is True
    assert gates.open_fire_record(eng.state("s"))["scales"] == [seg.SCALE_USD]


def test_prompt_boundary_stamp_books_and_judges_under_the_state_lock(eng, monkeypatch):
    executing_overspent(eng, usd=30.0)
    real_lock = cli_common._state_lock
    real_save = eng.store.save
    inside = {"lock": False}
    saves_inside = []

    @contextlib.contextmanager
    def spying_lock(store, session_id):
        with real_lock(store, session_id) as locked:
            inside["lock"] = True
            try:
                yield locked
            finally:
                inside["lock"] = False

    def spying_save(state):
        saves_inside.append(inside["lock"])
        return real_save(state)

    monkeypatch.setattr(cli_common, "_state_lock", spying_lock)
    monkeypatch.setattr(eng.store, "save", spying_save)
    first = cli_common._utcnow()
    assert cli_common.stamp_and_judge("s", first, store=eng.store) is True
    assert cli_common.stamp_and_judge("s", first + 60.0, store=eng.store) is True
    assert saves_inside and all(saves_inside)
    assert stage_arming(eng.state("s"), 1)["booked"][seg.SCALE_MINUTES] == pytest.approx(1.0)
    assert gates.open_fire_record(eng.state("s"))["scales"] == [seg.SCALE_USD]


def open_run(eng, sid, index=1):
    state = eng.state(sid)
    state.effort_child_runs.append({"started": cli_common._utcnow() - 60.0, "stage_index": index, "child_session": "c"})
    eng.store.save(state)


def test_root_stamp_while_stage_is_spawned_books_without_judging(eng, monkeypatch):
    executing_overspent(eng, usd=30.0)
    open_run(eng, "s")
    monkeypatch.setattr(cli_common.child_runs, "close_dead_runs", lambda *a, **k: None)
    first = cli_common._utcnow()
    assert cli_common.stamp_and_judge("s", first, store=eng.store) is True
    assert cli_common.stamp_and_judge("s", first + 1.0, store=eng.store) is True
    assert gates.open_fire_record(eng.state("s")) is None


def test_spawned_run_end_books_and_defers_judgment_to_record_result():
    assert seg.scales_to_fire(arming(usd=30.0), seg.ACT_SPAWNED_PLANNER_RUN_END, 5.0, FLOOR) == []
    built = arming(usd=30.0, segment=seg.PLANNING)
    assert seg.scales_to_fire(built, seg.ACT_SPAWNED_PLANNER_RUN_END, 5.0, FLOOR) == [seg.SCALE_USD]


def link_child(eng, parent, child):
    state = eng.state(child)
    state.parent_link = {"parent_session": parent}
    eng.store.save(state)


def test_spawned_stage_crossing_mid_run_fires_at_its_descendants_next_engine_act(eng):
    executing_overspent(eng, "p", usd=30.0)
    eng.run("start", session="c", task="task-c", goal="g", done_criterion="dc")
    link_child(eng, "p", "c")
    cli_common.judge_on_parent("c", seg.ACT_DESCENDANT_RECORD_RESULT, store=eng.store, rows=[])
    fire = gates.open_fire_record(eng.state("p"))
    assert fire["arming_id"] == stage_arming(eng.state("p"), 1)["arming_id"]


def test_running_descendant_keeps_booking_into_the_open_fire_record(eng):
    executing_overspent(eng, "p", usd=30.0)
    eng.run("start", session="c", task="task-c", goal="g", done_criterion="dc")
    link_child(eng, "p", "c")
    cli_common.judge_on_parent("c", seg.ACT_DESCENDANT_RECORD_RESULT, store=eng.store, rows=[])
    eng.overspend("p", minutes=200.0)
    cli_common.judge_on_parent("c", seg.ACT_DESCENDANT_RECORD_RESULT, store=eng.store, rows=[])
    fires = [f for f in eng.state("p").effort_fires if f.get("arming_id")]
    assert len(fires) == 1 and set(fires[0]["scales"]) == {seg.SCALE_USD, seg.SCALE_MINUTES}


def test_a_scale_crossing_while_the_other_fire_is_open_joins_that_record(eng):
    executing_overspent(eng, usd=30.0)
    eng.run("dispatch", session="s")
    eng.overspend("s", minutes=200.0)
    eng.run("dispatch", session="s")
    fires = [f for f in eng.state("s").effort_fires if f.get("arming_id")]
    assert len(fires) == 1 and set(fires[0]["scales"]) == {seg.SCALE_USD, seg.SCALE_MINUTES}


def test_child_contour_fire_reaches_parent_as_norm_divergence_not_user_question(eng):
    executing_overspent(eng, "c", usd=30.0)
    link_child(eng, "absent", "c")
    directive = eng.run("dispatch", session="c")
    assert directive["ok"] is False and directive["marker"] != "ESCALATE_TO_USER"
    assert directive["data"]["norm_mismatch"] is True


def test_open_fire_gate_holds_within_the_session_that_holds_the_fire(eng):
    executing_overspent(eng, "p", usd=30.0)
    eng.run("dispatch", session="p")
    eng.executing("other")
    assert gates.effort_fire_blockers(eng.state("p"))
    assert not gates.effort_fire_blockers(eng.state("other"))


def test_open_fire_refuses_the_passing_record_result(eng):
    executing_overspent(eng, usd=30.0)
    eng.run("dispatch", session="s")
    directive = eng.passed("s")
    assert directive["ok"] is False
    assert stage_arming(eng.state("s"), 1)["closed_at"] is None


def plan_ready_with_open_fire(eng, sid="s", fire=None):
    eng.run("start", session=sid, task=f"task-{sid}", goal="g", done_criterion="dc")
    eng.run("classify", session=sid, architectural=True, changed_lines=200, wall_clock_min=60)
    eng.run("plan", session=sid)
    eng.run("submit_plan", session=sid, plan=eng.write())
    state = eng.state(sid)
    state.effort_fires.append(fire or {"scale": effort.SCALE_SPEND, "ts": 1.0})
    eng.store.save(state)


def test_open_fire_refuses_every_self_approval(eng):
    plan_ready_with_open_fire(eng)
    directive = eng.run("approve", session="s", by="agent")
    assert directive["ok"] is False
    assert "effort" in json.dumps(directive)


def test_v61_open_fire_carried_refuses_self_approval(eng):
    plan_ready_with_open_fire(eng)
    state = eng.state("s")
    held = seg.current_arming(state.effort_segments)
    effort.carry_open_fire(state, held)
    eng.store.save(state)
    assert gates.open_fire_record(eng.state("s"))["arming_id"] == held["arming_id"]
    assert eng.run("approve", session="s", by="agent")["ok"] is False


def test_open_fire_refuses_every_self_grant(eng):
    executing_overspent(eng, usd=30.0)
    eng.run("dispatch", session="s")
    assert gates.effort_fire_blockers(eng.state("s"))


def test_a_fire_stays_open_until_the_customer_acknowledges_it(eng):
    executing_overspent(eng, usd=30.0)
    eng.run("dispatch", session="s")
    fired = gates.open_fire_record(eng.state("s"))
    assert fired is not None
    eng.run("fire_acknowledge", session="s", by="user", decision="continue")
    state = eng.state("s")
    assert gates.open_fire_record(state) is None
    assert seg.find_arming(state.effort_segments, fired["arming_id"])["fire"]["ack"]


def test_open_fire_carries_into_a_later_session_of_the_same_order():
    state = type("S", (), {})()
    state.effort_segments = {"armings": []}
    state.effort_fires = [{"arming_id": "gone", "scale": "spend"}]
    assert gates.open_fire_record(state)["arming_id"] == "gone"


def test_v61_open_fire_is_carried_by_the_planning_segment(eng):
    eng.executing("s")
    state = eng.state("s")
    held = seg.open_arming(state.effort_segments, seg.PLANNING, None, seg.SOURCE_WINDOW, 0.0, seg.OPENED_BY_MIGRATION)
    state.effort_fires.append({"scale": "spend", "ts": 1.0})
    effort.carry_open_fire(state, held)
    assert state.effort_fires[-1]["arming_id"] == held["arming_id"]
    assert gates.open_fire_record(state)["arming_id"] == held["arming_id"]


def test_whole_window_divergence_is_retained_for_migrated_sessions_only(eng):
    executing_overspent(eng, usd=30.0)
    state = eng.state("s")
    assert effort.whole_window_divergence(state) is None
    assert eng.failed("s")["data"]["effort_divergence"]["arming_id"]


def test_fire_record_names_identity_title_arming_budget_actual_and_multiple(eng):
    executing_overspent(eng, usd=30.0)
    fire = eng.failed("s")["data"]["effort_divergence"]
    state = eng.state("s")
    assert fire["identity"] == state.stage_uid(1)
    assert fire["title"] == stage_arming(state, 1)["title"]
    assert fire["budget"] and fire["arming_actual"]
    assert fire["multiple"] == pytest.approx(30.0 / fire["budget"][seg.SCALE_USD])


def test_an_arming_fires_at_most_once_per_scale():
    built = arming(usd=30.0)
    seg.note_fire(built, [seg.SCALE_USD], 1.0)
    assert seg.scales_to_fire(built, seg.ACT_DISPATCH, 5.0, FLOOR) == []


def test_medium_stage_at_5x_fires_both_scales_in_one_record(eng):
    executing_overspent(eng, usd=15.0, minutes=125.0)
    fire = eng.failed("s")["data"]["effort_divergence"]
    assert set(fire["scales"]) == {seg.SCALE_USD, seg.SCALE_MINUTES}
    assert len([f for f in eng.state("s").effort_fires if f.get("arming_id")]) == 1


def test_judged_quantity_is_the_current_armings_actual_against_its_budget():
    built = arming(usd=30.0, minutes=0.0)
    built["carried"] = {seg.SCALE_USD: 0.0, seg.SCALE_MINUTES: 200.0}
    assert set(judged(built)) == {seg.SCALE_USD, seg.SCALE_MINUTES}


def test_a_settled_segment_raises_no_spend_or_wall_clock_question(eng):
    executing_overspent(eng, usd=30.0)
    assert eng.passed("s")["ok"]
    state = eng.state("s")
    assert gates.open_fire_record(state) is None
    assert state.effort_segments["retier"]


def assert_settled_as_retier_only(eng, sid):
    state = eng.state(sid)
    assert state.node != "DIAGNOSING"
    assert gates.open_fire_record(state) is None
    assert [f for f in state.effort_fires if f.get("arming_id")] == []
    assert state.effort_segments["retier"]
    assert stage_arming(state, 1) is None


def test_passing_record_result_settles_the_stage_without_judging(eng):
    executing_overspent(eng, usd=30.0, minutes=500.0)
    assert eng.passed("s")["ok"]
    assert_settled_as_retier_only(eng, "s")


def test_in_thread_stage_passing_at_5x_yields_retier_record_and_no_diagnosing(eng):
    eng.executing("s")
    assert eng.state("s").effort_child_runs == []
    eng.overspend("s", usd=15.0, minutes=125.0)
    assert eng.passed("s")["ok"]
    assert_settled_as_retier_only(eng, "s")


def test_stage_passing_at_5x_through_descendant_spend_yields_retier_record_and_no_diagnosing(eng, tmp_path):
    eng.executing("p")
    eng.run("start", session="c", task="task-c", goal="g", done_criterion="dc")
    link_child(eng, "p", "c")
    cost_log = tmp_path / "descendant-costs.jsonl"
    row = {"plan_path": eng.state("p").plan_path, "stage_index": 1, "cost_usd": 15.0, "session_id": "c"}
    cost_log.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert eng.run("record_result", session="p", status="passed", actual="ok", control="reviewed: ok",
                   observation=STAGE_OBSERVATIONS[0], cost_log=str(cost_log))["ok"]
    assert_settled_as_retier_only(eng, "p")


def test_spawned_stage_passing_at_5x_yields_retier_record_and_no_diagnosing(eng, monkeypatch):
    monkeypatch.setattr(cli_common.child_runs, "close_dead_runs", lambda *a, **k: None)
    eng.executing("s")
    open_run(eng, "s")
    eng.overspend("s", usd=15.0, minutes=125.0)
    assert eng.passed("s")["ok"]
    assert_settled_as_retier_only(eng, "s")


def test_linking_off_judges_a_one_dispatch_stage_only_at_its_record_result(eng, monkeypatch):
    monkeypatch.setenv("AGENTCTL_CHILD_LINKING", "off")
    monkeypatch.setattr(cli_common.child_runs, "close_dead_runs", lambda *a, **k: None)
    executing_overspent(eng, usd=30.0)
    open_run(eng, "s")
    first = cli_common._utcnow()
    cli_common.stamp_and_judge("s", first, store=eng.store)
    cli_common.stamp_and_judge("s", first + 1.0, store=eng.store)
    assert gates.open_fire_record(eng.state("s")) is None
    assert eng.failed("s")["data"]["effort_divergence"]["arming_id"]


def test_stage_worked_inside_one_turn_ends_only_as_a_retier_record(eng):
    eng.executing("s")
    eng.overspend("s", minutes=125.0)
    assert gates.open_fire_record(eng.state("s")) is None
    assert eng.passed("s")["ok"]
    assert_settled_as_retier_only(eng, "s")


def test_settled_overspend_never_enters_diagnosing(eng):
    executing_overspent(eng, usd=30.0)
    eng.passed("s")
    assert eng.state("s").node != "DIAGNOSING"


def test_retier_record_at_the_multiple_on_either_scale_without_the_floor():
    ledger = {}
    built = seg.open_arming(ledger, seg.stage_segment("u"), dict(SMALL), seg.SOURCE_DECLARED, 0.0, "t")
    built["booked"] = {seg.SCALE_USD: 5.0, seg.SCALE_MINUTES: 0.0}
    retier = seg.close_arming(ledger, built["arming_id"], seg.ACT_PASSING_RECORD_RESULT, 1.0, 5.0)
    assert retier["scales"] == [seg.SCALE_USD]
    assert judged(built) == []


def test_retier_record_carries_tiers_budget_actual_and_arming_id():
    ledger = {}
    built = seg.open_arming(ledger, seg.stage_segment("u"), dict(MEDIUM), seg.SOURCE_DECLARED, 0.0, "t")
    built["booked"] = {seg.SCALE_USD: 15.0, seg.SCALE_MINUTES: 0.0}
    retier = seg.close_arming(ledger, built["arming_id"], seg.ACT_PASSING_RECORD_RESULT, 1.0, 5.0)
    for key in ("arming_id", "declared_tier", "observed_tier", "budget", "actual", "multiple"):
        assert key in retier


def test_next_stage_is_judged_from_its_own_zero_after_a_settlement(eng):
    executing_overspent(eng, usd=30.0)
    eng.passed("s")
    eng.run("next_stage", session="s")
    assert seg.arming_actual(stage_arming(eng.state("s"), 2))[seg.SCALE_USD] == 0.0


def test_a_fire_on_one_stage_neither_rebases_nor_consumes_another_stages_budget(eng):
    executing_overspent(eng, usd=30.0)
    fire = eng.failed("s")["data"]["effort_divergence"]
    assert fire["replaced_baseline"] == {}
    assert eng.state("s").effort_segments["armings"]


def test_a_replan_keeps_settled_actuals_and_does_not_reopen_their_judgment():
    ledger = {}
    built = seg.open_arming(ledger, seg.stage_segment("u"), dict(MEDIUM), seg.SOURCE_DECLARED, 0.0, "t")
    built["booked"] = {seg.SCALE_USD: 30.0, seg.SCALE_MINUTES: 0.0}
    seg.close_arming(ledger, built["arming_id"], seg.ACT_PASSING_RECORD_RESULT, 1.0, 5.0)
    assert seg.current_arming(ledger) is None
    assert seg.arming_actual(seg.find_arming(ledger, built["arming_id"]))[seg.SCALE_USD] == 30.0


def test_ten_stage_fixture_one_stage_at_5x_fires_on_that_stage_alone():
    armings = [arming(usd=3.0, segment=seg.stage_segment(f"u{i}")) for i in range(10)]
    armings[4]["booked"][seg.SCALE_USD] = 15.0
    fired = [a["segment"] for a in armings if seg.scales_to_fire(a, seg.ACT_DISPATCH, 5.0, FLOOR)]
    assert fired == [seg.stage_segment("u4")]


def test_closing_segment_never_fires():
    built = arming(usd=500.0, segment=seg.CLOSING)
    for act in (*seg.LIVE_STAGE_JUDGING_ACTS, *seg.PLANNING_JUDGING_ACTS):
        assert seg.scales_to_fire(built, act, 5.0, FLOOR) == []


def test_replans_scale_and_review_cycle_acts_still_bound_a_replan_in_closing():
    for act in seg.CLOSING_REPLANS_JUDGING_ACTS:
        assert seg.replans_judged_at(seg.CLOSING, act)
    assert not seg.replans_judged_at(seg.CLOSING, seg.ACT_STAMP)


def test_late_row_joins_settled_stage_actual_and_raises_no_question():
    ledger = {}
    built = seg.open_arming(ledger, seg.stage_segment("u"), dict(MEDIUM), seg.SOURCE_DECLARED, 0.0, "t")
    seg.close_arming(ledger, built["arming_id"], seg.ACT_PASSING_RECORD_RESULT, 1.0, 5.0)
    built["booked"][seg.SCALE_USD] = 30.0
    retier = seg.retier_after_late_booking(ledger, built, 5.0)
    assert retier["actual"][seg.SCALE_USD] == pytest.approx(30.0)
    assert built["fire"] is None


def test_late_row_updates_retier_record_per_closed_budgeted_arming():
    ledger = {}
    built = seg.open_arming(ledger, seg.stage_segment("u"), dict(MEDIUM), seg.SOURCE_DECLARED, 0.0, "t")
    built["booked"] = {seg.SCALE_USD: 15.0, seg.SCALE_MINUTES: 0.0}
    seg.close_arming(ledger, built["arming_id"], seg.ACT_PASSING_RECORD_RESULT, 1.0, 5.0)
    built["booked"][seg.SCALE_USD] = 30.0
    seg.retier_after_late_booking(ledger, built, 5.0)
    assert len(ledger["retier"]) == 1 and ledger["retier"][0]["actual"][seg.SCALE_USD] == 30.0
    window = seg.open_arming(ledger, seg.PLANNING, dict(MEDIUM), seg.SOURCE_WINDOW, 0.0, seg.OPENED_BY_MIGRATION)
    window["booked"] = {seg.SCALE_USD: 500.0, seg.SCALE_MINUTES: 0.0}
    assert seg.close_arming(ledger, window["arming_id"], seg.ACT_PASSING_RECORD_RESULT, 3.0, 5.0) is None
    assert seg.retier_after_late_booking(ledger, window, 5.0) is None
    assert len(ledger["retier"]) == 1


def test_verify_final_never_fires_for_any_session(eng):
    executing_overspent(eng, usd=30.0)
    eng.passed("s")
    eng.run("next_stage", session="s")
    eng.passed("s", 2)
    eng.run("verify_final", session="s")
    assert not eng.state("s").effort_fires


def test_status_and_effort_check_never_fire_transition_or_save(eng):
    executing_overspent(eng, usd=30.0)
    before = eng.state("s")
    eng.run("status", session="s")
    eng.run("effort_check", session="s")
    after = eng.state("s")
    assert after.node == before.node and after.effort_fires == before.effort_fires


def test_judge_needs_the_multiple_and_the_floor_on_each_scale():
    assert judged(arming(usd=5.0, budget=SMALL)) == []
    assert judged(arming(usd=9.0, budget=SMALL)) == [seg.SCALE_USD]


def test_fire_point_is_the_later_of_the_multiple_and_the_floor():
    assert judged(arming(minutes=70.0, budget=SMALL)) == [seg.SCALE_MINUTES]
    assert judged(arming(minutes=69.0, budget=SMALL)) == []
    assert judged(arming(minutes=125.0, budget=MEDIUM)) == [seg.SCALE_MINUTES]
    assert judged(arming(minutes=124.0, budget=MEDIUM)) == []


def test_floor_keys_are_read_from_config_md():
    thr = Thresholds({"effort-fire-floor-usd": "2.5", "effort-fire-floor-minutes": "nope"})
    assert thr.effort_fire_floor_usd() == 2.5
    assert thr.effort_fire_floor_minutes() is None
    assert Thresholds({}).effort_fire_floor_usd() is None
    disarmed = {seg.SCALE_USD: None, seg.SCALE_MINUTES: None}
    assert seg.judge(arming(usd=5.0, budget=SMALL), 5.0, disarmed) == [seg.SCALE_USD]


def test_floor_initial_values_equal_the_large_tier_budget_and_do_not_follow_it():
    thr = Thresholds()
    assert thr.effort_fire_floor_usd() == 8.0
    assert thr.effort_fire_floor_minutes() == 60.0


def test_small_tier_stage_fires_on_neither_scale_at_5x():
    assert judged(arming(usd=5.0, minutes=50.0, budget=SMALL)) == []


def test_small_tier_stage_fires_on_wall_clock_alone_at_7x():
    assert judged(arming(usd=1.0, minutes=70.0, budget=SMALL)) == [seg.SCALE_MINUTES]


def test_small_tier_stage_fires_on_spend_alone_at_9x_with_wall_clock_at_5x():
    assert judged(arming(usd=9.0, minutes=50.0, budget=SMALL)) == [seg.SCALE_USD]


def test_medium_and_large_tier_stages_fire_at_the_multiple_unaffected_by_the_floor():
    assert judged(arming(usd=15.0, budget=MEDIUM)) == [seg.SCALE_USD]
    assert judged(arming(minutes=300.0, budget=LARGE)) == [seg.SCALE_MINUTES]


def seed_replans(eng, sid, count):
    state = eng.state(sid)
    state.history.extend({"event": "replan"} for _ in range(count))
    eng.store.save(state)


def test_replans_scale_fires_at_its_count_whatever_the_stage_actuals(eng):
    eng.executing("s")
    seed_replans(eng, "s", Thresholds().effort_replan_absolute())
    div = effort.replans_divergence(eng.state("s"), Thresholds())
    assert div.scale == effort.SCALE_REPLANS


def test_replans_scale_is_not_divided_per_stage_and_ignores_the_floor(eng):
    eng.executing("s")
    absolute = Thresholds().effort_replan_absolute()
    seed_replans(eng, "s", absolute - 1)
    assert effort.replans_divergence(eng.state("s"), Thresholds()) is None
    assert eng.passed("s")["ok"]
    seed_replans(eng, "s", 1)
    div = effort.replans_divergence(eng.state("s"), Thresholds())
    assert div.scale == effort.SCALE_REPLANS
    assert stage_arming(eng.state("s"), 1) is None


def test_replans_fire_is_acknowledged_by_the_agent_until_the_backstop_then_the_users(eng, monkeypatch):
    eng.executing("s")
    seed_replans(eng, "s", Thresholds().effort_replan_absolute())
    eng.failed("s")
    fire = [f for f in eng.state("s").effort_fires if f.get("scale") == effort.SCALE_REPLANS][-1]
    assert fire.get("ack") is None
    axes = []

    def at_the_backstop(state, load_doc, *, decision, axis, **kwargs):
        axes.append(axis)
        return True, None

    monkeypatch.setattr(cli_replan, "_since_user_backstop_gate", at_the_backstop)
    refused = eng.run("fire_acknowledge", session="s", by="agent", decision="revise")
    assert refused["ok"] is False
    assert axes == ["replans_since_user_decision"]
    assert eng.run("fire_acknowledge", session="s", by="user", decision="continue")["ok"]


def test_subplan_replan_counts_on_the_replans_scale(eng):
    eng.executing("s")
    absolute = Thresholds().effort_replan_absolute()
    seed_replans(eng, "s", absolute - 1)
    state = eng.state("s")
    state.log("replan", kind="subplan", parent_stage=1)
    eng.store.save(state)
    assert effort.replans_divergence(eng.state("s"), Thresholds()).scale == effort.SCALE_REPLANS


def test_passing_record_result_at_the_replans_count_does_not_enter_diagnosing(eng):
    eng.executing("s")
    seed_replans(eng, "s", Thresholds().effort_replan_absolute())
    assert eng.passed("s")["ok"]
    assert eng.state("s").node != "DIAGNOSING"


def test_replans_count_accumulates_across_sessions_and_fires_on_the_combined_total(eng):
    eng.executing("s")
    absolute = Thresholds().effort_replan_absolute()
    state = eng.state("s")
    below = effort.replans_divergence(state, Thresholds(), cross_session_totals={"replan_count": 0})
    above = effort.replans_divergence(state, Thresholds(), cross_session_totals={"replan_count": absolute})
    assert below is None
    assert above is not None and above.scale == effort.SCALE_REPLANS


def foreign_row(spawner, usd, ts):
    return {"plan_path": "elsewhere", "stage_index": 1, "cost_usd": usd, "ts": ts, "spawner_session": spawner}


def write_rows(*rows):
    Path(cost.COST_LOG).write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_stamp_refresh_loads_only_spawners_since_lower_bound(eng, monkeypatch):
    eng.executing("s")
    state = eng.state("s")
    write_rows(foreign_row("old", 1.0, "2020-01-01T00:00:00+00:00"))
    loaded = []
    real = eng.store.load
    monkeypatch.setattr(eng.store, "load", lambda sid: (loaded.append(sid), real(sid))[1])
    cli_common._stamp_refresh(state, eng.store, cli_common._utcnow())
    assert "old" not in loaded


def cost_row(plan, usd, ts, spawner=None):
    return {"plan_path": plan, "stage_index": 1, "cost_usd": usd, "ts": ts, "spawner_session": spawner}


def test_bounded_then_unbounded_refresh_books_older_descendant_row_once(eng):
    eng.executing("s")
    state = eng.state("s")
    plan = state.plan_path
    floor = state.effort_segments["session_floor"]
    older = cost_row(plan, 2.0, floor + 10, spawner="descendant")
    later = cost_row(plan, 1.0, floor + 20)
    rows = [older, later]
    cli_common._refresh_spend(state, eng.store, rows, plan, since=floor + 15)
    assert sum(state.effort_spend_seen.values()) == pytest.approx(1.0)
    assert stage_arming(state, 1)["booked"][seg.SCALE_USD] == pytest.approx(1.0)
    cli_common._refresh_spend(state, eng.store, rows, plan)
    assert sum(state.effort_spend_seen.values()) == pytest.approx(3.0)
    cli_common._refresh_spend(state, eng.store, rows, plan)
    assert sum(state.effort_spend_seen.values()) == pytest.approx(3.0)
    assert seg.conserve(state.effort_segments)["total"][seg.SCALE_USD] == pytest.approx(3.0)
    pre_floor_row_books_nowhere_after_a_reset_with_the_plan_path_kept(eng)


def pre_floor_row_books_nowhere_after_a_reset_with_the_plan_path_kept(eng):
    eng.executing("s1")
    first = eng.state("s1")
    plan = first.plan_path
    before_reset = cost_row(plan, 5.0, first.effort_segments["session_floor"])
    cli_common._refresh_spend(first, eng.store, [before_reset], plan)
    eng.store.save(first)
    eng.run("start", session="s2", task="task-s2", goal="g", done_criterion="dc")
    eng.run("classify", session="s2", architectural=True, changed_lines=200, wall_clock_min=60)
    eng.run("plan", session="s2")
    eng.run("submit_plan", session="s2", plan=plan)
    assert eng.run("approve", session="s2", by="user")["ok"]
    second = eng.state("s2")
    floor = second.effort_segments["session_floor"]
    assert before_reset["ts"] < floor
    cli_common._refresh_spend(second, eng.store, [before_reset], plan)
    assert seg.conserve(second.effort_segments)["total"][seg.SCALE_USD] == 0.0
    assert sum(second.effort_spend_seen.values()) == 0.0
    assert second.effort_fires == []
    after_reset = cost_row(plan, 0.5, floor + 1)
    cli_common._refresh_spend(second, eng.store, [before_reset, after_reset], plan)
    assert seg.conserve(second.effort_segments)["total"][seg.SCALE_USD] == pytest.approx(0.5)
    total = (seg.conserve(first.effort_segments)["total"][seg.SCALE_USD]
             + seg.conserve(second.effort_segments)["total"][seg.SCALE_USD])
    assert total == pytest.approx(5.5)


def test_migrated_stamp_leaves_rows_to_next_act(eng):
    eng.executing("s")
    state = eng.state("s")
    seg.open_arming(state.effort_segments, seg.PLANNING, None, seg.SOURCE_WINDOW, 0.0, seg.OPENED_BY_MIGRATION)
    write_rows({"plan_path": state.plan_path, "stage_index": 1, "cost_usd": 1.0,
                "ts": "2099-01-01T00:00:00+00:00", "spawner_session": "s"})
    before = dict(state.effort_spend_seen)
    cli_common._stamp_refresh(state, eng.store, cli_common._utcnow())
    assert state.effort_spend_seen == before


def test_failed_stamp_hook_books_nothing_and_the_next_act_books_the_gap(eng, monkeypatch):
    eng.executing("s")
    before = eng.state("s").last_prompt_at if hasattr(eng.state("s"), "last_prompt_at") else None
    monkeypatch.setattr(cli_common, "_stamp_loaded", lambda *a, **k: 1 / 0)
    assert cli_common.stamp_and_judge("s", cli_common._utcnow(), store=eng.store) is True
    after = eng.state("s").last_prompt_at if hasattr(eng.state("s"), "last_prompt_at") else None
    assert after == before
